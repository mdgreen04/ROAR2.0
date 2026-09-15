"""Offline tests for ROAR 2.0 (no network)."""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import feedparser
import pytest

from roar.analyze import build_analyst_input, validate_analysis, analyze_none
from roar.fetch_pubmed import expand_query, item_from_esummary, parse_efetch_abstracts
from roar.fetch_rss import entries_to_items
from roar.models import Item
from roar.normalize import merge_items
from roar.prescore import Scorer, prescore_all
from roar.render import build_context, render_all, subject_line, week_label
from roar.state import SeenState
from roar.util import load_config, norm_doi, parse_date, title_fingerprint, truncate

FIX = Path(__file__).parent / "fixtures"
TODAY = date(2026, 9, 15)


@pytest.fixture(scope="module")
def cfg():
    return load_config()


# ----------------------------------------------------------------------------- util
def test_norm_doi_variants():
    assert norm_doi("https://doi.org/10.1016/J.IJROBP.2026.01.001") == "10.1016/j.ijrobp.2026.01.001"
    assert norm_doi("doi:10.1200/JCO-26-00769.") == "10.1200/jco-26-00769"
    assert norm_doi("not a doi") is None


def test_parse_date_shapes():
    assert parse_date("2026 Sep 12") == date(2026, 9, 12)
    assert parse_date("2026/09/12") == date(2026, 9, 12)
    assert parse_date("2026 Sep") == date(2026, 9, 1)
    assert parse_date("Fri, 12 Sep 2026 10:00:00 GMT") == date(2026, 9, 12)
    assert parse_date("") is None


def test_title_fingerprint_ignores_punctuation_and_case():
    a = title_fingerprint("Whole-pelvic versus prostate-only radiotherapy: a meta-analysis.")
    b = title_fingerprint("Whole pelvic versus prostate only radiotherapy — A META-ANALYSIS")
    assert a == b


def test_truncate_prefers_sentence_boundary():
    s = "First sentence is here. Second sentence is much longer and keeps going on and on for a while."
    out = truncate(s, 60)
    assert out.startswith("First sentence is here.") and out.endswith("…") and len(out) <= 60


# ----------------------------------------------------------------------------- rss
def test_rss_entries_to_items_filters_old_and_keeps_doi(cfg):
    parsed = feedparser.parse((FIX / "sample_feed.xml").read_bytes())
    feed_cfg = {"id": "redjournal_inpress", "name": "Red Journal", "tier": 1, "category": "radonc_journal"}
    items = entries_to_items(feed_cfg, parsed, lookback_days=8, abstract_chars=500, topic_terms=[], today=TODAY)
    titles = [i.title for i in items]
    assert any("PACE-B" in t for t in titles)
    assert not any("Old Article" in t for t in titles)          # outside lookback window
    pace = next(i for i in items if "PACE-B" in i.title)
    assert pace.id == "doi:10.1016/j.ijrobp.2026.01.001"
    assert pace.published == "2026-09-12"
    assert pace.journal.startswith("International Journal of Radiation Oncology")


def test_rss_topic_filter_drops_offtopic():
    parsed = feedparser.parse((FIX / "sample_feed.xml").read_bytes())
    feed_cfg = {"id": "x", "name": "X", "tier": 1, "topic_filter": True}
    items = entries_to_items(feed_cfg, parsed, lookback_days=8, abstract_chars=500,
                             topic_terms=["prostate"], today=TODAY)
    assert len(items) == 1 and "PACE-B" in items[0].title


# ----------------------------------------------------------------------------- pubmed
def test_expand_query_and_fragments(cfg):
    frags = cfg["pubmed"]["fragments"]
    for q in cfg["pubmed"]["queries"]:
        term = expand_query(q["term"], frags)
        assert "{" not in term and "}" not in term
        assert term.count("(") == term.count(")"), q["id"]
        assert "case report" in term  # every query excludes obvious non-clinical records


def test_parse_efetch_abstracts():
    parsed = parse_efetch_abstracts((FIX / "sample_efetch.xml").read_text())
    a = parsed["42727425"]
    assert a["doi"] == "10.1016/j.clon.2026.104329"
    assert a["published"] == "2026-08-22"
    assert "Meta-Analysis" in a["pub_types"]
    assert a["abstract"].startswith("Aims: Whether") and "Results:" in a["abstract"]
    b = parsed["42715505"]
    assert b["doi"] == "10.1200/jco-26-00769" and b["published"] == "2026-09-09"


def test_item_from_esummary():
    s = {"title": "A trial.", "source": "J Clin Oncol", "epubdate": "2026 Sep 9",
         "articleids": [{"idtype": "doi", "value": "10.1200/JCO-26-1"}],
         "authors": [{"name": "A B"}, {"name": "C D"}, {"name": "E F"}, {"name": "G H"}],
         "pubtype": ["Journal Article"]}
    it = item_from_esummary("1", s, query_tags=["q"], query_names=["Q"], tier=1, topics=["radonc"])
    assert it.doi == "10.1200/jco-26-1" and it.published == "2026-09-09"
    assert it.authors.endswith("et al.") and it.url.endswith("/1/")


# ----------------------------------------------------------------------------- merge + score
def _mk(**kw) -> Item:
    base = dict(id="url:x", title="t", sources=["s"], source_names=["S"], tier=3)
    base.update(kw)
    return Item(**base)


def test_merge_prefers_pubmed_and_unions_sources():
    rss = _mk(id="doi:10.1/abc", doi="10.1/abc", title="Trial of SBRT in prostate cancer", abstract="short",
              sources=["redjournal_inpress"], source_names=["Red Journal"], tier=1)
    pm = _mk(id="pmid:9", pmid="9", doi="10.1/abc", title="Trial of SBRT in Prostate Cancer.",
             abstract="a much longer abstract " * 20, source_type="pubmed", sources=["rt_all"],
             source_names=["RT all"], tier=2, pub_types=["Randomized Controlled Trial"])
    merged = merge_items([rss, pm])
    assert len(merged) == 1
    m = merged[0]
    assert m.source_type == "pubmed" and m.pmid == "9"
    assert set(m.sources) == {"redjournal_inpress", "rt_all"}
    assert m.tier == 1


def test_merge_by_title_when_no_ids():
    a = _mk(id="url:1", title="Adaptive radiotherapy for bladder cancer: results of a phase II trial")
    b = _mk(id="url:2", title="Adaptive Radiotherapy for Bladder Cancer — Results of a Phase II Trial.")
    assert len(merge_items([a, b])) == 1


def test_prescore_ranks_rct_over_case_report(cfg):
    sc = Scorer(cfg["interests"])
    rct = _mk(title="Randomized phase 3 trial of hypofractionated radiotherapy for prostate cancer",
              abstract="Overall survival improved.", tier=1, journal="Int J Radiat Oncol Biol Phys")
    case = _mk(title="A rare case of radiation recall dermatitis: case report", abstract="", tier=3,
               journal="Cureus")
    breast = _mk(title="Hypofractionated whole breast irradiation after lumpectomy", tier=1,
                 journal="Int J Radiat Oncol Biol Phys")
    s_rct, r_rct = sc.score(rct)
    s_case, _ = sc.score(case)
    s_breast, _ = sc.score(breast)
    assert s_rct > s_case and s_rct > s_breast          # the RCT wins on design words, not a breast penalty
    assert "randomized:+3" in r_rct and "rt_core:+3" in r_rct
    assert sc.classify_sites(rct)[0] == "gu"
    assert sc.classify_sites(breast)[0] == "breast"


def test_prescore_all_sets_sites_topics(cfg):
    it = _mk(title="Deep learning auto-segmentation for head and neck radiotherapy planning",
             abstract="oropharynx cancer", tier=2)
    out = prescore_all([it], cfg["interests"])[0]
    assert out.primary_site == "hn"
    assert "ai" in out.topics and "radonc" in out.topics


# ----------------------------------------------------------------------------- state
def test_seen_state_roundtrip(tmp_path):
    p = tmp_path / "seen.json"
    st = SeenState(p, ttl_days=30)
    it = _mk(id="pmid:1", pmid="1", title="Hello world trial")
    assert not st.is_seen(it)
    st.mark([it])                       # today
    st.save()                           # prunes anything older than ttl_days
    st2 = SeenState(p, ttl_days=30)
    assert st2.is_seen(_mk(id="doi:zzz", title="Hello World Trial"))  # matched by title fingerprint
    st2.prune(today=date.today() + timedelta(days=40))
    assert not st2.is_seen(it)


# ----------------------------------------------------------------------------- analysis + render
def _candidates(cfg):
    items = [
        _mk(id="pmid:1", pmid="1", doi="10.1/a", title="PACE-B 7-year results", journal="Int J Radiat Oncol Biol Phys",
            published="2026-09-12", abstract="SBRT noninferior to conventional RT for prostate cancer.", tier=1,
            source_type="pubmed", pub_types=["Randomized Controlled Trial"]),
        _mk(id="doi:10.1/b", doi="10.1/b", title="Adaptive RT for bladder preservation", journal="Red Journal",
            published="2026-09-11", abstract="Online adaptive radiotherapy is feasible.", tier=1),
        _mk(id="url:c", title="USPSTF updates lung screening", journal="JAMA", published="2026-09-10",
            abstract="Screening recommendation widened.", tier=1, url="https://jamanetwork.com/x"),
        _mk(id="url:d", title="LLM ambient scribe trial", journal="NEJM AI", published="2026-09-09",
            abstract="Ambient documentation reduced burden.", tier=2, url="https://ai.nejm.org/x"),
    ]
    return prescore_all(items, cfg["interests"])


def test_validate_analysis_fixes_ids_sections_and_caps(cfg):
    cands = _candidates(cfg)
    raw = {
        "week_of": "2026-09-14", "editor_note": "Busy week.",
        "picks": [
            {"id": "1", "top": True, "section": "gu", "score": 95, "headline": "SBRT holds up at 7 years", "summary": "..."},
            {"id": "10.1/b", "top": True, "section": "top", "score": 70, "headline": "x", "summary": "y"},
            {"id": "url:c", "section": "screening", "score": 60, "headline": "h", "summary": "s"},
            {"id": "url:d", "section": "nonsense", "score": 555, "headline": "h2", "summary": "s2"},
            {"id": "pmid:999", "section": "gu", "score": 90, "headline": "ghost", "summary": "ghost"},
            {"id": "pmid:1", "section": "gu", "score": 90, "headline": "dup", "summary": "dup"},
        ],
    }
    clean, warnings = validate_analysis(raw, cands, cfg)
    ids = [p["id"] for p in clean["picks"]]
    assert "pmid:1" in ids and "doi:10.1/b" in ids and "pmid:999" not in ids
    assert [p["score"] for p in clean["picks"]] == sorted((p["score"] for p in clean["picks"]), reverse=True)
    assert ids.count("pmid:1") == 1
    tops = [p for p in clean["picks"] if p["top"]]
    assert [p["id"] for p in tops] == ["pmid:1"]           # score 70 < top_pick_min_score
    d = next(p for p in clean["picks"] if p["id"] == "url:d")
    assert d["section"] == "ai" and d["score"] == 100
    assert any("unknown/duplicate" in w for w in warnings)


def test_render_html_and_subject(cfg):
    cands = _candidates(cfg)
    analysis = {
        "week_of": "2026-09-14", "editor_note": "ASTRO starts soon.", "skipped_summary": "3 letters",
        "picks": [
            {"id": "pmid:1", "top": True, "section": "gu", "score": 95, "headline": "SBRT holds up at 7 years",
             "summary": "Noninferior.", "why_it_matters": "Supports 5-fraction prostate SBRT.", "tags": ["phase 3"]},
            {"id": "doi:10.1/b", "top": False, "section": "gu", "score": 70, "headline": "Adaptive bladder RT feasible",
             "summary": "Feasible.", "why_it_matters": "", "tags": []},
            {"id": "url:c", "top": False, "section": "screening", "score": 60, "headline": "Lung screening widened",
             "summary": "Wider criteria.", "why_it_matters": "", "tags": []},
        ],
    }
    ctx = build_context(analysis, cands, cfg, fetch_meta={"items_fetched": 512, "sources_ok": 40}, today=TODAY)
    out = render_all(ctx)
    html = out["html"]
    assert "SBRT holds up at 7 years" in html
    assert "https://doi.org/10.1/a" in html and "proxy.lib.umich.edu/login?url=https://doi.org/10.1/a" in html
    assert "pubmed.ncbi.nlm.nih.gov/1/" in html
    assert "Genitourinary" in html and "Briefly noted" in html
    assert "ASTRO 2026 Annual Meeting" in html          # within horizon of 2026-09-15
    assert "Screened 512 new items from 40 sources" in html
    assert "<script" not in html
    assert week_label("2026-09-14") == "September 7–13, 2026"
    subj = subject_line(ctx)
    assert subj.startswith("ROAR 2.0 · September 7–13, 2026 · SBRT holds up")
    assert "## Genitourinary" in out["md"]


def test_analyze_none_backend_produces_valid_analysis(cfg):
    cands = _candidates(cfg)
    raw = analyze_none(cands, cfg, "2026-09-14")
    clean, _ = validate_analysis(raw, cands, cfg)
    assert len(clean["picks"]) == len(cands)
    assert all(p["section"] in ("gu", "screening", "ai", "radonc", "systemic") for p in clean["picks"])


def test_analyst_input_is_compact(cfg):
    cands = _candidates(cfg)
    text = build_analyst_input(cands, cfg, "2026-09-14")
    assert "pmid:1 |" in text and "## 1. PACE-B 7-year results" in text
    assert len(text) < 3000


def test_fetch_feed_falls_back_to_browser_ua_then_feedly():
    """Publishers that 403 cloud IPs: retry with browser headers, then Feedly's public stream cache."""
    from roar import fetch_rss

    class Resp:
        def __init__(self, code, text=""):
            self.status_code, self.text = code, text
            self.content = text.encode("utf-8")

    class Session:
        def __init__(self, browser_ok):
            self.browser_ok, self.calls = browser_ok, []

        def get(self, url, **kw):
            self.calls.append(url)
            if url.startswith(fetch_rss.FEEDLY_STREAM):
                return Resp(200, '{"title":"Red Journal","items":[{"title":"A trial","published":1789400000000,'
                                 '"alternate":[{"href":"https://doi.org/10.1016/j.ijrobp.2026.1"}],'
                                 '"originId":"10.1016/j.ijrobp.2026.1","author":"Smith J",'
                                 '"summary":{"content":"<p>Abstract text</p>"}}]}')
            if kw.get("headers", {}).get("User-Agent", "").startswith("Mozilla") and self.browser_ok:
                return Resp(200, '<?xml version="1.0"?><rss version="2.0"><channel><title>RJ</title>'
                                 '<item><title>Direct item</title><link>https://x/1</link></item></channel></rss>')
            return Resp(403)

    feed = {"id": "redjournal_inpress", "name": "RJ", "url": "https://www.redjournal.org/inpress.rss"}
    s = Session(browser_ok=True)
    parsed, err = fetch_rss.fetch_feed(s, feed)
    assert err is None and parsed.roar_via == "browser-ua" and parsed.entries[0].title == "Direct item"

    s = Session(browser_ok=False)
    parsed, err = fetch_rss.fetch_feed(s, feed)
    assert err is None and parsed.roar_via == "feedly"
    e = parsed.entries[0]
    assert e.title == "A trial" and e.link == "https://doi.org/10.1016/j.ijrobp.2026.1"
    assert fetch_rss._entry_doi(e) == "10.1016/j.ijrobp.2026.1"
    assert fetch_rss._entry_date(e) is not None and "Abstract text" in fetch_rss._entry_summary(e)
    assert len(s.calls) == 3
