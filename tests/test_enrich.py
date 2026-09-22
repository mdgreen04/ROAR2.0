"""Offline tests for the abstract-enrichment step and the deferral register (no network)."""
from __future__ import annotations

import copy
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from roar.cli import build_parser, cmd_fetch
from roar.enrich import (Enricher, crossref_date, enrich_items, is_citation_only, is_eligible,
                         jats_to_text)
from roar.models import Item
from roar.state import SeenState
from roar.util import load_config

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def cfg():
    return load_config()


def _mk(**kw) -> Item:
    base = dict(id="url:x", title="t", sources=["s"], source_names=["S"], tier=1, category="oncology_journal")
    base.update(kw)
    return Item(**base)


# ----------------------------------------------------------------------------- pure helpers
def test_is_citation_only_recognises_feed_citation_lines():
    assert is_citation_only("Journal of Clinical Oncology, Ahead of Print.")
    assert is_citation_only("Journal of Clinical Oncology, Volume 44, Issue 27 , Page 2574-2581, September 2026.")
    assert is_citation_only("Publication date: November 2026 Source: Clinical and Translational Radiation Oncology, "
                            "Volume 61 Author(s): " + ", ".join(f"Author {i}" for i in range(40)))
    assert is_citation_only("")
    assert is_citation_only(None)
    real = ("Purpose: To compare hypofractionated radiotherapy with conventional radiotherapy. Methods: 316 patients "
            "were randomly assigned. Results: Biochemical control did not differ. Conclusion: Hypofractionation is "
            "non-inferior and shortens treatment by seven fractions for every patient treated in the trial.")
    assert not is_citation_only(real)


def test_is_eligible_by_category_and_pubmed():
    cats = {"oncology_journal", "radonc_journal"}
    assert is_eligible(_mk(category="radonc_journal"), cats)
    assert is_eligible(_mk(category="news", source_type="pubmed"), cats)
    assert not is_eligible(_mk(category="news"), cats)
    assert not is_eligible(_mk(category="oncology_journal"), set())


def test_jats_to_text_keeps_section_labels_and_drops_markup():
    xml = ("<jats:sec><jats:title>Abstract</jats:title><jats:sec><jats:title>PURPOSE</jats:title>"
           "<jats:p>To compare <jats:italic>A</jats:italic> with B.</jats:p></jats:sec>"
           "<jats:sec><jats:title>Patients and Methods</jats:title><jats:p>316 patients &amp; 2 arms.</jats:p>"
           "</jats:sec></jats:sec>")
    out = jats_to_text(xml)
    assert out == "Purpose: To compare A with B. Patients and Methods: 316 patients & 2 arms."
    assert jats_to_text(None) == ""


def test_crossref_date_prefers_online_first_then_print():
    msg = {"published-print": {"date-parts": [[2027, 1]]}, "published-online": {"date-parts": [[2026, 8, 25]]},
           "created": {"date-parts": [[2026, 8, 25]]}}
    assert crossref_date(msg) == "2026-08-25"
    assert crossref_date({"issued": {"date-parts": [[2026]]}}) == "2026-01-01"
    assert crossref_date({"issued": {"date-parts": [[None]]}}) is None


# ----------------------------------------------------------------------------- fakes
class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class FakeSession:
    """Routes Crossref / Semantic Scholar URLs to canned answers and records every call."""

    def __init__(self, crossref: dict[str, dict], s2: dict[str, dict] | None = None, search: list | None = None):
        self.crossref = crossref      # doi -> message
        self.s2 = s2 or {}            # doi -> payload
        self.search = search or []    # items for query.bibliographic
        self.calls: list[tuple[str, dict | None]] = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params))
        if url.startswith("https://api.crossref.org/works/"):
            doi = url.rsplit("/works/", 1)[1].replace("%2F", "/").lower()
            msg = self.crossref.get(doi)
            return _Resp(200, {"message": msg}) if msg else _Resp(404)
        if url == "https://api.crossref.org/works":
            return _Resp(200, {"message": {"items": self.search}})
        if url.startswith("https://api.semanticscholar.org/"):
            doi = url.split("DOI:", 1)[1].replace("%2F", "/").lower()
            p = self.s2.get(doi)
            return _Resp(200, p) if p else _Resp(404)
        raise AssertionError(f"unexpected URL {url}")


class FakePubMed:
    def __init__(self, doi_to_pmid: dict[str, str], xml: str | None = None):
        self.doi_to_pmid = doi_to_pmid
        self.xml = xml or (FIX / "sample_efetch.xml").read_text(encoding="utf-8")
        self.efetch_calls = 0

    def pmids_for_doi(self, doi):
        return [self.doi_to_pmid[doi]] if doi in self.doi_to_pmid else []

    def efetch_xml(self, pmids):
        self.efetch_calls += 1
        return self.xml


JATS = ("<jats:sec><jats:title>PURPOSE</jats:title><jats:p>To compare hypofractionated radiotherapy (RT) with "
        "conventional RT for biochemical recurrence after prostatectomy.</jats:p></jats:sec><jats:sec>"
        "<jats:title>METHODS</jats:title><jats:p>Between 2019 and 2021, 316 patients were randomly assigned to "
        "65 Gy in 26 fractions or 66 Gy in 33 fractions to the prostate bed.</jats:p></jats:sec>")


# ----------------------------------------------------------------------------- enrichment
def test_enrich_fills_from_crossref_and_sets_date(cfg):
    it = _mk(id="doi:10.1200/jco-25-02234", doi="10.1200/jco-25-02234",
             title="Salvage Hypofractionated Accelerated Versus Standard Radiotherapy",
             abstract="Journal of Clinical Oncology, Ahead of Print.")
    news = _mk(id="url:n", title="News story", abstract="", category="news")   # not eligible, untouched
    sess = FakeSession({"10.1200/jco-25-02234": {"abstract": JATS, "container-title": ["Journal of Clinical Oncology"],
                                                 "published-online": {"date-parts": [[2026, 8, 25]]}}})
    stats = enrich_items([it, news], cfg, sess, pubmed_client=None)
    assert it.abstract.startswith("Purpose: To compare hypofractionated radiotherapy")
    assert it.published == "2026-08-25"
    assert news.abstract == ""
    assert stats["candidates"] == 1 and stats["crossref"] == 1 and stats["filled"] == 1
    assert stats["still_missing"] == 0


def test_enrich_falls_back_to_pubmed_by_doi_then_semantic_scholar(cfg):
    # Elsevier-style: Crossref knows the DOI but deposits no abstract -> PubMed by DOI supplies it (+PMID, pubtypes)
    a = _mk(id="doi:10.1016/j.clon.2026.104329", doi="10.1016/j.clon.2026.104329", category="radonc_journal",
            title="Whole-pelvic versus prostate-only radiotherapy", abstract="Publication date: October 2026 Source: X")
    # Nothing anywhere but Semantic Scholar
    b = _mk(id="doi:10.9999/zzz", doi="10.9999/zzz", title="Other paper", abstract="")
    # Unresolvable
    c = _mk(id="doi:10.9999/none", doi="10.9999/none", title="Nothing known", abstract="")
    sess = FakeSession(
        crossref={"10.1016/j.clon.2026.104329": {"published-online": {"date-parts": [[2026, 9, 1]]}}},
        s2={"10.9999/zzz": {"abstract": "A" * 250, "publicationDate": "2026-09-02", "externalIds": {"PubMed": "777"}}},
    )
    pm = FakePubMed({"10.1016/j.clon.2026.104329": "42727425"})
    stats = enrich_items([a, b, c], cfg, sess, pubmed_client=pm)
    assert a.abstract.startswith("Aims: Whether elective whole-pelvic radiotherapy")
    assert a.pmid == "42727425" and a.published == "2026-09-01"
    assert b.abstract == "A" * 250 and b.pmid == "777" and b.published == "2026-09-02"
    assert c.abstract == ""
    assert stats["pubmed_doi"] == 1 and stats["semantic_scholar"] == 1 and stats["unresolved"] == 1
    assert stats["filled"] == 2 and stats["still_missing"] == 1


def test_enrich_resolves_doi_by_exact_title_then_looks_up(cfg):
    title = "Artificial Intelligence impact on decision-making regarding short-term ADT with prostate radiotherapy"
    it = _mk(id="url:abc", title=title, category="radonc_journal",
             abstract="Publication date: November 2026 Source: Clinical and Translational Radiation Oncology Author(s): A")
    sess = FakeSession(
        crossref={"10.1016/j.ctro.2026.101300": {"abstract": JATS}},
        search=[{"DOI": "10.1016/J.CTRO.2026.101299", "title": ["A different paper about prostate radiotherapy"]},
                {"DOI": "10.1016/J.CTRO.2026.101300", "title": [title + "."]}],
    )
    stats = enrich_items([it], cfg, sess)
    assert it.doi == "10.1016/j.ctro.2026.101300"
    assert it.url == "https://doi.org/10.1016/j.ctro.2026.101300"
    assert it.abstract.startswith("Purpose:")
    assert stats["doi_resolved"] == 1 and stats["crossref"] == 1


def test_enrich_batches_known_pmids_and_respects_budget(cfg):
    cfg2 = copy.deepcopy(cfg)
    cfg2["settings"]["enrich"]["max_lookups"] = 1
    known = _mk(id="pmid:42715505", pmid="42715505", source_type="pubmed", category="pubmed",
                title="Personalized ctDNA Analysis in Localized Sarcomas", abstract="")
    others = [_mk(id=f"doi:10.1/{i}", doi=f"10.1/{i}", title=f"Paper {i}", abstract="") for i in range(3)]
    long_xml = (FIX / "sample_efetch.xml").read_text(encoding="utf-8").replace(
        "ctDNA tracked response to radiotherapy and pembrolizumab in 106 patients.",
        "ctDNA tracked response to radiotherapy and pembrolizumab in 106 patients. " * 5)
    pm = FakePubMed({}, xml=long_xml)
    sess = FakeSession({})
    stats = enrich_items([known] + others, cfg2, sess, pubmed_client=pm)
    assert known.abstract.startswith("ctDNA tracked response")          # one batched efetch
    assert pm.efetch_calls == 1
    assert stats["pubmed_pmid"] == 1 and stats["filled"] == 1
    assert stats["budget_exhausted"] == 3 and len(sess.calls) == 0       # budget spent on the batch


def test_enrich_disabled_is_a_no_op(cfg):
    cfg2 = copy.deepcopy(cfg)
    cfg2["settings"]["enrich"]["enabled"] = False
    it = _mk(doi="10.1/x", abstract="")
    assert enrich_items([it], cfg2, FakeSession({})) == {"enabled": False}
    assert it.abstract == ""


def test_enricher_never_raises_on_network_errors(cfg):
    class Boom:
        def get(self, *a, **k):
            raise ConnectionError("offline")
    en = Enricher(Boom(), cfg)
    it = _mk(doi="10.1/x", abstract="")
    assert en.enrich_one(it) is False
    assert en.stats["errors"] >= 1 and en.stats["unresolved"] == 1


# ----------------------------------------------------------------------------- deferral register
def test_seen_state_pending_register(tmp_path):
    p = tmp_path / "seen.json"
    st = SeenState(p, ttl_days=60)
    it = _mk(id="doi:10.1/abc", doi="10.1/abc", title="Held back paper")
    assert st.pending_age(it) is None
    st.defer([it], when=date(2026, 9, 21))
    st.defer([it], when=date(2026, 9, 28))                       # repeat deferral keeps the first date
    assert st.pending_age(it, today=date(2026, 10, 5)) == 14
    assert not st.is_seen(it)                                     # pending is not seen
    st.save()
    st2 = SeenState(p, ttl_days=60)
    twin = _mk(id="pmid:9", pmid="9", title="Held Back Paper")   # same title fingerprint
    assert st2.pending_age(twin, today=date(2026, 10, 5)) == 14
    st2.mark([twin], when=date(2026, 10, 5))                      # shown -> its keys leave the pending register
    assert st2.is_seen(it)                                        # ...and the original is now seen via the title
    assert all(k not in st2.pending for k in st2.keys_for(twin))
    st2.mark([it], when=date(2026, 10, 5))
    assert not st2.pending
    st2.save()
    assert "pending" not in json.loads(p.read_text())


def test_fetch_defers_citation_only_journal_items_until_window_expires(tmp_path, cfg):
    cfg2 = copy.deepcopy(cfg)
    cfg2["settings"]["digest"]["output_dir"] = str(tmp_path / "digests")
    cfg2["settings"]["digest"]["state_file"] = str(tmp_path / "seen.json")
    items = [
        _mk(id="doi:10.1200/jco-99-00001", doi="10.1200/jco-99-00001", category="oncology_journal",
            title="Randomized phase 3 trial of hypofractionated radiotherapy for prostate cancer",
            journal="Journal of Clinical Oncology", abstract="Journal of Clinical Oncology, Ahead of Print.").to_dict(),
        _mk(id="pmid:1", pmid="1", source_type="pubmed", category="pubmed", journal="Int J Radiat Oncol Biol Phys",
            title="Randomized phase 3 trial of SBRT for prostate cancer", published="2026-09-20",
            abstract="Randomized phase 3 trial. " * 20).to_dict(),
        _mk(id="url:news", category="news", tier=2, journal="The ASCO Post", title="FDA approves radiotherapy device",
            abstract="Short news blurb about a radiotherapy approval in prostate cancer.").to_dict(),
    ]
    f = tmp_path / "items.json"
    f.write_text(json.dumps({"items": items}), encoding="utf-8")

    def run(week):
        args = build_parser().parse_args(["fetch", "--from-items", str(f), "--week-of", week, "--no-carry-over"])
        assert cmd_fetch(args, cfg2) == 0
        w = tmp_path / "digests" / week
        return json.loads((w / "candidates.json").read_text()), json.loads((w / "fetch_report.json").read_text())

    cands, rep = run("2026-09-28")
    ids = {c["id"] for c in cands["items"]}
    assert "doi:10.1200/jco-99-00001" not in ids                 # journal item without abstract: held back
    assert "pmid:1" in ids and "url:news" in ids                  # abstract present / news is never deferred
    assert rep["deferred"] == 1
    state = json.loads((tmp_path / "seen.json").read_text())
    assert "doi:10.1200/jco-99-00001" in state["pending"]
    assert "pmid:1" in state["seen"] and "doi:10.1200/jco-99-00001" not in state["seen"]

    # Age the pending entry past defer_days: the item is now shown as-is and marked seen.
    old = (date.today() - timedelta(days=cfg2["settings"]["enrich"]["defer_days"] + 1)).isoformat()
    state["pending"] = {k: old for k in state["pending"]}
    (tmp_path / "seen.json").write_text(json.dumps(state), encoding="utf-8")
    cands, rep = run("2026-10-05")
    ids = {c["id"] for c in cands["items"]}
    assert "doi:10.1200/jco-99-00001" in ids and rep["deferred"] == 0
    shown = next(c for c in cands["items"] if c["id"] == "doi:10.1200/jco-99-00001")
    assert "no-abstract:shown-after-deferral" in shown["score_reasons"]
    state = json.loads((tmp_path / "seen.json").read_text())
    assert "doi:10.1200/jco-99-00001" in state["seen"] and "pending" not in state
