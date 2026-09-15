"""PubMed E-utilities fetcher (two-stage: esearch+esummary for everything, efetch abstracts for the top N).

NCBI usage policy: <=3 requests/s without an API key (10/s with NCBI_API_KEY), identify tool+email.
"""
from __future__ import annotations

import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import date

from .models import Item
from .util import norm_doi, parse_date, truncate, utcnow

log = logging.getLogger("roar.pubmed")

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"


def expand_query(term: str, fragments: dict[str, str]) -> str:
    """Replace {fragment} placeholders; collapse whitespace so the URL stays sane."""
    out = term
    for _ in range(3):  # fragments may reference fragments
        for k, v in fragments.items():
            out = out.replace("{" + k + "}", v.strip())
    if "{" in out:
        raise ValueError(f"unexpanded fragment in query: {out[:80]}")
    return re.sub(r"\s+", " ", out).strip()


class PubMedClient:
    def __init__(self, session, tool: str, email: str, api_key: str | None = None):
        self.s = session
        self.tool = tool
        self.email = email
        self.api_key = api_key or os.environ.get("NCBI_API_KEY")
        self._min_interval = 0.11 if self.api_key else 0.34
        self._last = 0.0

    def _params(self, **kw) -> dict:
        p = {"tool": self.tool, "email": self.email}
        if self.api_key:
            p["api_key"] = self.api_key
        p.update(kw)
        return p

    def _throttle(self) -> None:
        wait = self._min_interval - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()

    def _get(self, endpoint: str, **params):
        self._throttle()
        r = self.s.get(EUTILS + endpoint, params=self._params(**params), timeout=60)
        r.raise_for_status()
        return r

    def _post(self, endpoint: str, **params):
        self._throttle()
        r = self.s.post(EUTILS + endpoint, data=self._params(**params), timeout=120)
        r.raise_for_status()
        return r

    # -- stage 1 -------------------------------------------------------------
    def esearch(self, term: str, reldate: int, retmax: int) -> list[str]:
        # POST: the expanded queries are ~3 KB, beyond what E-utilities accept reliably on a GET URL
        r = self._post("esearch.fcgi", db="pubmed", term=term, reldate=reldate, datetype="edat",
                       retmode="json", retmax=retmax, sort="date")
        data = r.json()
        return list(data.get("esearchresult", {}).get("idlist", []))

    def esummary(self, pmids: list[str]) -> dict:
        out: dict = {}
        for i in range(0, len(pmids), 200):
            chunk = pmids[i:i + 200]
            r = self._post("esummary.fcgi", db="pubmed", id=",".join(chunk), retmode="json")
            res = r.json().get("result", {})
            for pmid in chunk:
                if pmid in res:
                    out[pmid] = res[pmid]
        return out

    # -- stage 2 -------------------------------------------------------------
    def efetch_xml(self, pmids: list[str]) -> str:
        parts = []
        for i in range(0, len(pmids), 100):
            chunk = pmids[i:i + 100]
            r = self._post("efetch.fcgi", db="pubmed", id=",".join(chunk), retmode="xml", rettype="abstract")
            parts.append(r.text)
        return parts if len(parts) != 1 else parts[0]


# ----------------------------------------------------------------------------- parsers (pure functions)
def item_from_esummary(pmid: str, s: dict, *, query_tags: list[str], query_names: list[str],
                       tier: int, topics: list[str]) -> Item:
    doi = None
    for aid in s.get("articleids", []) or []:
        if aid.get("idtype") == "doi":
            doi = norm_doi(aid.get("value"))
    if not doi:
        eloc = s.get("elocationid", "") or ""
        if "doi:" in eloc.lower():
            doi = norm_doi(eloc.split("doi:")[-1])
    pubdate = parse_date(s.get("epubdate") or s.get("sortpubdate") or s.get("pubdate"))
    authors = [a.get("name", "") for a in (s.get("authors") or []) if a.get("name")]
    if len(authors) > 3:
        auth = ", ".join(authors[:3]) + ", et al."
    else:
        auth = ", ".join(authors)
    return Item(
        id=f"pmid:{pmid}",
        title=(s.get("title") or "").strip().rstrip("."),
        url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        doi=doi,
        pmid=pmid,
        journal=s.get("source") or s.get("fulljournalname") or "",
        published=pubdate.isoformat() if pubdate else None,
        authors=auth,
        abstract="",
        pub_types=list(s.get("pubtype") or []),
        source_type="pubmed",
        sources=list(query_tags),
        source_names=list(query_names),
        tier=tier,
        category="pubmed",
        topics=list(topics),
        fetched_at=utcnow().isoformat(timespec="seconds"),
    )


def parse_efetch_abstracts(xml_text: str) -> dict[str, dict]:
    """Return {pmid: {abstract, pub_types, doi, published}} from an efetch XML document."""
    out: dict[str, dict] = {}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        log.warning("efetch XML parse error: %s", exc)
        return out
    for art in root.iter("PubmedArticle"):
        pmid_el = art.find("./MedlineCitation/PMID")
        if pmid_el is None or not pmid_el.text:
            continue
        pmid = pmid_el.text.strip()
        article = art.find("./MedlineCitation/Article")
        if article is None:
            continue
        chunks = []
        for at in article.findall("./Abstract/AbstractText"):
            label = at.get("Label")
            text = "".join(at.itertext()).strip()
            if not text:
                continue
            chunks.append(f"{label.title()}: {text}" if label and label.upper() != "UNLABELLED" else text)
        abstract = " ".join(chunks)
        pub_types = [pt.text.strip() for pt in article.findall("./PublicationTypeList/PublicationType")
                     if pt.text]
        doi = None
        for el in article.findall("./ELocationID"):
            if el.get("EIdType") == "doi" and el.text:
                doi = norm_doi(el.text)
        if not doi:
            for aid in art.findall("./PubmedData/ArticleIdList/ArticleId"):
                if aid.get("IdType") == "doi" and aid.text:
                    doi = norm_doi(aid.text)
        published = None
        ad = article.find("./ArticleDate")
        if ad is not None:
            y, m, d = (ad.findtext("Year"), ad.findtext("Month"), ad.findtext("Day"))
            if y:
                published = parse_date(f"{y}-{m or 1}-{d or 1}")
        if not published:
            pd = article.find("./Journal/JournalIssue/PubDate")
            if pd is not None:
                y, m, d = (pd.findtext("Year"), pd.findtext("Month"), pd.findtext("Day"))
                published = parse_date(f"{y} {m or 'Jan'} {d or 1}") if y else parse_date(pd.findtext("MedlineDate"))
        out[pmid] = {"abstract": abstract, "pub_types": pub_types, "doi": doi,
                     "published": published.isoformat() if published else None}
    return out


# ----------------------------------------------------------------------------- orchestration
def search_all(cfg: dict, client: PubMedClient, *, only: set[str] | None = None) -> tuple[dict[str, Item], list[dict]]:
    """Stage 1: run every query, esummary all unique PMIDs, return {pmid: Item} and a report."""
    pm_cfg = cfg["pubmed"]
    settings = cfg["settings"]
    lookback = int(settings["digest"]["lookback_days"])
    retmax = int(settings["pubmed"]["retmax_per_query"])
    fragments = pm_cfg.get("fragments", {})

    hits: dict[str, dict] = {}       # pmid -> {tags, names, tier, topics}
    report: list[dict] = []
    for q in pm_cfg.get("queries", []):
        if q.get("enabled", True) is False:
            continue
        if only and q["id"] not in only:
            continue
        term = expand_query(q["term"], fragments)
        t0 = time.time()
        try:
            ids = client.esearch(term, reldate=lookback, retmax=retmax)
        except Exception as exc:
            log.warning("query %-18s FAILED %s", q["id"], exc)
            report.append({"id": q["id"], "ok": False, "error": str(exc), "count": 0})
            continue
        log.info("query %-18s %4d ids (%.1fs)", q["id"], len(ids), time.time() - t0)
        report.append({"id": q["id"], "ok": True, "count": len(ids)})
        for pmid in ids:
            h = hits.setdefault(pmid, {"tags": [], "names": [], "tier": 9, "topics": []})
            h["tags"].append(q["id"])
            h["names"].append(q["name"])
            h["tier"] = min(h["tier"], int(q.get("tier", 3)))
            if q.get("topic") and q["topic"] not in h["topics"]:
                h["topics"].append(q["topic"])

    pmids = list(hits)
    summaries = client.esummary(pmids) if pmids else {}
    items: dict[str, Item] = {}
    for pmid in pmids:
        s = summaries.get(pmid)
        if not s or "error" in s:
            continue
        h = hits[pmid]
        items[pmid] = item_from_esummary(pmid, s, query_tags=h["tags"], query_names=h["names"],
                                         tier=h["tier"], topics=h["topics"])
    log.info("pubmed stage 1: %d unique records", len(items))
    return items, report


def add_abstracts(items: dict[str, Item], pmids: list[str], client: PubMedClient, abstract_chars: int) -> None:
    """Stage 2: efetch abstracts for the selected PMIDs and update the Items in place."""
    if not pmids:
        return
    xml = client.efetch_xml(pmids)
    docs = [xml] if isinstance(xml, str) else xml
    parsed: dict[str, dict] = {}
    for doc in docs:
        parsed.update(parse_efetch_abstracts(doc))
    for pmid in pmids:
        it = items.get(pmid)
        p = parsed.get(pmid)
        if not it or not p:
            continue
        it.abstract = truncate(p["abstract"], abstract_chars)
        if p["pub_types"]:
            it.pub_types = p["pub_types"]
        if p["doi"] and not it.doi:
            it.doi = p["doi"]
        if p["published"] and not it.published:
            it.published = p["published"]
    log.info("pubmed stage 2: abstracts added for %d/%d", sum(1 for p in pmids if p in parsed), len(pmids))
