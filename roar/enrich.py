"""Fill in abstracts (and dates, DOIs, PMIDs) for items that arrived as a citation line.

Publisher RSS feeds are uneven: Atypon "etoc" feeds (ascopubs.org) describe every paper as
"Journal of Clinical Oncology, Ahead of Print.", Elsevier's "in press" feeds give "Publication date: …
Source: … Author(s): …", and PubMed records below the stage-2 cut have no abstract yet. The analyst cannot
summarise what it cannot read, so after merging, items from journal-type sources that still lack an
abstract are looked up, cheapest source first:

    1. PubMed efetch by PMID (records we already know but skipped in stage 2)
    2. Crossref by DOI  — ASCO, JAMA, NEJM, Wiley, Springer, Oxford and most societies deposit abstracts;
                          Elsevier does not. Also supplies the online-publication date.
    3. PubMed by DOI    — same-day for most journals once the publisher deposits the record
    4. Semantic Scholar by DOI (optional, last resort)

Items without a DOI (ScienceDirect feeds) are first matched by exact normalised title in Crossref.
Everything is best-effort: any network failure is logged and the item is left as it was.
"""
from __future__ import annotations

import logging
import os
import re
import time
from collections import Counter
from datetime import date
from urllib.parse import quote

from .models import Item
from .util import USER_AGENT, norm_doi, strip_html, title_fingerprint, truncate

log = logging.getLogger("roar.enrich")

CROSSREF_WORKS = "https://api.crossref.org/works"
SEMANTIC_SCHOLAR = "https://api.semanticscholar.org/graph/v1/paper/"

# Feed descriptions that are a citation, not an abstract.
_CITATION_PATTERNS = (
    re.compile(r"^[^.]{3,90}, (Ahead of Print|Volume \d+|Vol\.? \d+|Issue \d+)", re.I),   # Atypon etoc feeds
    re.compile(r"^Publication date:", re.I),                                              # ScienceDirect feeds
    re.compile(r"^(Source|Author\(s\)):", re.I),
)


# ----------------------------------------------------------------------------- pure helpers
def is_citation_only(abstract: str | None, min_chars: int = 160) -> bool:
    """True when the text is too short to be an abstract or matches a known citation-line shape."""
    a = (abstract or "").strip()
    if len(a) < min_chars:
        return True
    return any(p.match(a) for p in _CITATION_PATTERNS)


def is_eligible(item: Item, categories: set[str] | None) -> bool:
    """Only journal-type sources are worth network look-ups; news items are meant to be short."""
    if item.source_type == "pubmed":
        return True
    return bool(categories) and item.category in categories


_JATS_TITLE = re.compile(r"<jats:title[^>]*>(.*?)</jats:title>", re.I | re.S)
_JATS_BREAK = re.compile(r"</jats:(p|sec|list-item)>", re.I)


def _label(text: str) -> str:
    t = strip_html(text).strip().rstrip(":")
    return t.capitalize() if t.isupper() else t


def jats_to_text(xml: str | None) -> str:
    """Crossref abstracts are JATS fragments: keep section labels ("Purpose: …"), drop the mark-up."""
    if not xml:
        return ""
    def repl(m):
        lab = _label(m.group(1))
        return " " if lab.lower() in ("", "abstract", "summary") else f" {lab}: "
    s = _JATS_TITLE.sub(repl, xml)
    s = _JATS_BREAK.sub(" ", s)
    return strip_html(s)


def crossref_date(msg: dict) -> str | None:
    """Earliest useful date in a Crossref work: online first, then print, then issued/created."""
    for key in ("published-online", "published", "published-print", "issued", "created"):
        parts = ((msg.get(key) or {}).get("date-parts") or [[]])[0]
        if parts and parts[0]:
            try:
                y = int(parts[0])
                m = int(parts[1]) if len(parts) > 1 and parts[1] else 1
                d = int(parts[2]) if len(parts) > 2 and parts[2] else 1
                return date(y, m, d).isoformat()
            except (TypeError, ValueError):
                continue
    return None


# ----------------------------------------------------------------------------- network
class Enricher:
    """Small, throttled client for Crossref / PubMed / Semantic Scholar with a per-run budget."""

    def __init__(self, session, cfg: dict, pubmed_client=None, *, abstract_chars: int | None = None):
        settings = cfg["settings"]
        e = settings.get("enrich") or {}
        self.session = session
        self.pubmed = pubmed_client
        self.min_chars = int(e.get("min_abstract_chars", 160))
        self.categories = set(e.get("categories") or [])
        self.max_lookups = int(e.get("max_lookups", 150))
        self.time_budget = float(e.get("time_budget_s", 300))
        self.use_s2 = bool(e.get("semantic_scholar", True))
        self.abstract_chars = int(abstract_chars or settings["candidates"]["abstract_chars"])
        self.mailto = (os.environ.get("ROAR_CROSSREF_MAILTO") or e.get("crossref_mailto")
                       or os.environ.get("ROAR_NCBI_EMAIL") or settings.get("pubmed", {}).get("email") or "")
        self.s2_key = os.environ.get("S2_API_KEY")
        self.lookups = 0
        self.t0 = time.time()
        self.stats: Counter = Counter()

    # -- plumbing ---------------------------------------------------------------
    def budget_ok(self) -> bool:
        return self.lookups < self.max_lookups and (time.time() - self.t0) < self.time_budget

    def _get_json(self, url: str, *, params: dict | None = None, headers: dict | None = None,
                  pause: float = 0.15, timeout: int = 30):
        self.lookups += 1
        r = self.session.get(url, params=params, headers=headers, timeout=timeout)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        time.sleep(pause)
        return r.json()

    def _try(self, fn, *args):
        try:
            return fn(*args)
        except Exception as exc:  # network / JSON / HTTP errors are never fatal here
            log.debug("enrich %s(%s) failed: %s", getattr(fn, "__name__", fn), args[:1], exc)
            self.stats["errors"] += 1
            return None

    # -- sources ----------------------------------------------------------------
    def crossref_work(self, doi: str) -> dict | None:
        hdrs = {"User-Agent": f"{USER_AGENT} (mailto:{self.mailto})"} if self.mailto else None
        params = {"mailto": self.mailto} if self.mailto else None
        data = self._get_json(f"{CROSSREF_WORKS}/{quote(doi, safe='')}", params=params, headers=hdrs)
        return (data or {}).get("message") or None

    def crossref_find_doi(self, title: str) -> str | None:
        params = {"query.bibliographic": title[:300], "rows": 3, "select": "DOI,title"}
        if self.mailto:
            params["mailto"] = self.mailto
        hdrs = {"User-Agent": f"{USER_AGENT} (mailto:{self.mailto})"} if self.mailto else None
        data = self._get_json(CROSSREF_WORKS, params=params, headers=hdrs)
        want = title_fingerprint(title)
        for work in ((data or {}).get("message") or {}).get("items") or []:
            for t in work.get("title") or []:
                if title_fingerprint(t) == want:
                    return norm_doi(work.get("DOI"))
        return None

    def semantic_scholar(self, doi: str) -> dict | None:
        hdrs = {"x-api-key": self.s2_key} if self.s2_key else None
        return self._get_json(f"{SEMANTIC_SCHOLAR}DOI:{quote(doi, safe='')}",
                              params={"fields": "abstract,publicationDate,externalIds"}, headers=hdrs, pause=1.0)

    def pubmed_by_doi(self, doi: str) -> dict | None:
        if self.pubmed is None:
            return None
        from .fetch_pubmed import parse_efetch_abstracts
        self.lookups += 1
        pmids = self.pubmed.pmids_for_doi(doi)
        for pmid in pmids[:2]:
            self.lookups += 1
            xml = self.pubmed.efetch_xml([pmid])
            parsed = parse_efetch_abstracts(xml if isinstance(xml, str) else xml[0])
            p = parsed.get(pmid)
            if p and (not p.get("doi") or p["doi"] == doi):
                p["pmid"] = pmid
                return p
        return None

    # -- one item ---------------------------------------------------------------
    def enrich_one(self, it: Item) -> bool:
        """Try each source in turn; return True when an abstract was found."""
        if not it.doi and it.title:
            doi = self._try(self.crossref_find_doi, it.title)
            if doi:
                it.doi = doi
                self.stats["doi_resolved"] += 1
                if not it.url:
                    it.url = f"https://doi.org/{doi}"
        if not it.doi:
            self.stats["no_doi"] += 1
            return False

        msg = self._try(self.crossref_work, it.doi)
        if msg:
            d = crossref_date(msg)
            if d and not it.published:
                it.published = d
            if not it.journal and msg.get("container-title"):
                it.journal = msg["container-title"][0]
            text = jats_to_text(msg.get("abstract"))
            if text and not is_citation_only(text, self.min_chars):
                it.abstract = truncate(text, self.abstract_chars)
                self.stats["crossref"] += 1
                return True

        p = self._try(self.pubmed_by_doi, it.doi) if self.budget_ok() else None
        if p:
            it.pmid = it.pmid or p["pmid"]
            if p.get("pub_types") and not it.pub_types:
                it.pub_types = list(p["pub_types"])
            if p.get("published") and not it.published:
                it.published = p["published"]
            if p.get("abstract") and not is_citation_only(p["abstract"], self.min_chars):
                it.abstract = truncate(p["abstract"], self.abstract_chars)
                self.stats["pubmed_doi"] += 1
                return True

        if self.use_s2 and self.budget_ok():
            data = self._try(self.semantic_scholar, it.doi)
            if data and data.get("abstract") and not is_citation_only(data["abstract"], self.min_chars):
                it.abstract = truncate(strip_html(data["abstract"]), self.abstract_chars)
                ext = data.get("externalIds") or {}
                if not it.pmid and ext.get("PubMed"):
                    it.pmid = str(ext["PubMed"])
                if not it.published and data.get("publicationDate"):
                    it.published = data["publicationDate"]
                self.stats["semantic_scholar"] += 1
                return True

        self.stats["unresolved"] += 1
        return False


# ----------------------------------------------------------------------------- orchestration
def enrich_items(items: list[Item], cfg: dict, session, pubmed_client=None, *,
                 abstract_chars: int | None = None) -> dict:
    """Enrich in place every eligible item that is still citation-only. Returns a stats dict for the report."""
    settings = cfg["settings"]
    e = settings.get("enrich") or {}
    if not e.get("enabled", True):
        return {"enabled": False}
    enricher = Enricher(session, cfg, pubmed_client, abstract_chars=abstract_chars)
    min_chars, cats = enricher.min_chars, enricher.categories
    todo = [it for it in items if is_eligible(it, cats) and is_citation_only(it.abstract, min_chars)]
    # Spend the budget where it pays: tier-1 sources first, and items that already have a DOI (one look-up)
    # before items that need a title search first.
    todo.sort(key=lambda it: (int(it.tier), 0 if it.doi or it.pmid else 1))
    stats: Counter = Counter(candidates=len(todo))

    # 1. PubMed records we already know: one batched efetch for the PMIDs that missed the stage-2 cut.
    with_pmid = [it for it in todo if it.pmid]
    if with_pmid and pubmed_client is not None:
        from .fetch_pubmed import add_abstracts
        before = {id(it): len(it.abstract or "") for it in with_pmid}
        try:
            add_abstracts({it.pmid: it for it in with_pmid}, [it.pmid for it in with_pmid], pubmed_client,
                          enricher.abstract_chars)
            enricher.lookups += (len(with_pmid) + 99) // 100
        except Exception as exc:
            log.warning("enrich: batched efetch failed: %s", exc)
        stats["pubmed_pmid"] = sum(1 for it in with_pmid if len(it.abstract or "") > before[id(it)])

    # 2. Everything else, one item at a time, until the budget runs out.
    for it in todo:
        if not is_citation_only(it.abstract, min_chars):
            continue
        if not enricher.budget_ok():
            stats["budget_exhausted"] += 1
            continue
        enricher.enrich_one(it)

    stats.update(enricher.stats)
    stats["filled"] = sum(1 for it in todo if not is_citation_only(it.abstract, min_chars))
    stats["still_missing"] = len(todo) - stats["filled"]
    stats["lookups"] = enricher.lookups
    stats["seconds"] = round(time.time() - enricher.t0, 1)
    log.info("enrich: %d citation-only items -> %d filled (crossref %d, pubmed %d+%d, s2 %d), %d still missing, "
             "%d lookups in %.0fs", len(todo), stats["filled"], stats.get("crossref", 0), stats.get("pubmed_pmid", 0),
             stats.get("pubmed_doi", 0), stats.get("semantic_scholar", 0), stats["still_missing"],
             enricher.lookups, stats["seconds"])
    return dict(stats)
