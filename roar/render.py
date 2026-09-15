"""Render analysis.json + candidates into HTML (email), Markdown and plain text."""
from __future__ import annotations

import html
from datetime import date, datetime, timedelta
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import __version__
from .models import Item
from .util import ROOT, parse_date

SECTION_LABELS = {
    "radonc": "Radiation oncology — practice, technique & physics",
    "gu": "Genitourinary",
    "gi": "Gastrointestinal",
    "hn": "Head & neck",
    "cutaneous": "Cutaneous",
    "heme": "Hematologic",
    "gyn": "Gynecologic",
    "breast": "Breast",
    "thoracic": "Thoracic",
    "cns": "CNS",
    "other_sites": "Other sites & palliative",
    "systemic": "Systemic therapy worth knowing",
    "policy": "Regulatory & policy",
    "screening": "Briefly noted — cancer screening",
    "ai": "Briefly noted — AI in medicine",
}
COMPACT_SECTIONS = {"screening", "ai"}


def _fmt_date(d: date | None, with_year: bool = True) -> str:
    if not d:
        return "n.d."
    return d.strftime("%b %-d, %Y") if with_year else d.strftime("%b %-d")


def week_label(week_of: str) -> str:
    wk = parse_date(week_of) or date.today()
    start, end = wk - timedelta(days=7), wk - timedelta(days=1)
    if start.month == end.month:
        return f"{start.strftime('%B')} {start.day}–{end.day}, {end.year}"
    if start.year == end.year:
        return f"{start.strftime('%b')} {start.day} – {end.strftime('%b')} {end.day}, {end.year}"
    return f"{_fmt_date(start)} – {_fmt_date(end)}"


def _links(item: Item, cfg: dict) -> tuple[str, str, str | None, str]:
    links_cfg = cfg["settings"]["links"]
    doi_url = f"{links_cfg['doi_base']}{item.doi}" if item.doi else None
    article_url = doi_url or item.url
    pubmed_url = f"{links_cfg['pubmed_base']}{item.pmid}/" if item.pmid else None
    proxy_url = f"{links_cfg['proxy_prefix']}{doi_url or item.url}" if (doi_url or item.url) else ""
    return article_url, proxy_url, pubmed_url, doi_url or ""


def _pick_context(p: dict, item: Item, cfg: dict) -> dict:
    article_url, proxy_url, pubmed_url, _ = _links(item, cfg)
    d = parse_date(item.published)
    section_label = SECTION_LABELS.get(p["section"], p["section"])
    journal = item.journal or (item.source_names[0] if item.source_names else "")
    meta_bits = [b for b in [journal, _fmt_date(d), section_label if p.get("top") else None] if b]
    meta = " · ".join(meta_bits)
    meta_short = f"{journal}, {_fmt_date(d, with_year=False)}" if journal else _fmt_date(d)
    parts_html, parts_md = [], []
    if article_url:
        parts_html.append(f'<a href="{html.escape(article_url)}">Article ↗</a>')
        parts_md.append(f"[Article]({article_url})")
    if pubmed_url:
        parts_html.append(f'<a href="{html.escape(pubmed_url)}">PubMed</a>')
        parts_md.append(f"[PubMed]({pubmed_url})")
    if proxy_url:
        parts_html.append(f'<a href="{html.escape(proxy_url)}">U-M full text</a>')
        parts_md.append(f"[U-M full text]({proxy_url})")
    return {
        **p,
        "title": item.title,
        "article_url": article_url or "#",
        "pubmed_url": pubmed_url,
        "proxy_url": proxy_url,
        "meta": meta,
        "meta_short": meta_short,
        "links_html": " &nbsp;·&nbsp; ".join(parts_html),
        "links_md": " · ".join(parts_md),
    }


def _conferences(cfg: dict, today: date) -> list[dict]:
    conf = cfg.get("conferences") or {}
    horizon = int(conf.get("horizon_days", 75))
    out = []
    for m in conf.get("meetings", []):
        start = parse_date(m.get("start"))
        end = parse_date(m.get("end")) or start
        if not start or end < today or start > today + timedelta(days=horizon):
            continue
        if start.month == end.month:
            when = f"{start.strftime('%b')} {start.day}–{end.day}, {end.year}"
        else:
            when = f"{start.strftime('%b')} {start.day} – {end.strftime('%b')} {end.day}, {end.year}"
        delta = (start - today).days
        if delta < 0:
            days_away = "under way"
        elif delta == 0:
            days_away = "starts today"
        elif delta < 14:
            days_away = f"in {delta} day{'s' if delta != 1 else ''}"
        else:
            days_away = f"in {round(delta / 7)} weeks"
        out.append({"name": m["name"], "when": when, "location": m.get("location", ""),
                    "url": m.get("url", "#"), "days_away": days_away})
    return out


def build_context(analysis: dict, candidates: list[Item], cfg: dict, fetch_meta: dict | None = None,
                  today: date | None = None) -> dict:
    today = today or date.today()
    settings = cfg["settings"]
    by_id = {c.id: c for c in candidates}
    picks = [p for p in analysis.get("picks", []) if p["id"] in by_id]
    ctx_picks = [_pick_context(p, by_id[p["id"]], cfg) for p in picks]
    top = [p for p in ctx_picks if p.get("top")]
    rest = [p for p in ctx_picks if not p.get("top")]

    order = [s for s in settings["digest_shape"]["section_order"] if s != "top"]
    sections = []
    for key in order:
        items = [p for p in rest if p["section"] == key]
        if items:
            sections.append({"key": key, "label": SECTION_LABELS.get(key, key), "entries": items,
                             "compact": key in COMPACT_SECTIONS})

    fetch_meta = fetch_meta or {}
    n_screened = fetch_meta.get("items_fetched") or fetch_meta.get("candidates") or len(candidates)
    n_sources = fetch_meta.get("sources_ok")
    stats = f"Screened {n_screened} new items"
    if n_sources:
        stats += f" from {n_sources} sources"
    stats += f" · {len(ctx_picks)} selected"

    week_of = analysis.get("week_of") or today.isoformat()
    generated = datetime.now().strftime("%b %-d, %Y %H:%M")
    preheader = "; ".join(p["headline"] for p in top[:3]) or analysis.get("editor_note", "")[:140]
    return {
        "title": settings["digest"]["title"],
        "subtitle": settings["digest"]["subtitle"],
        "week_of": week_of,
        "week_label": week_label(week_of),
        "stats_line": stats,
        "editor_note": analysis.get("editor_note", ""),
        "skipped_summary": analysis.get("skipped_summary", ""),
        "top_picks": top,
        "sections": sections,
        "conferences": _conferences(cfg, today),
        "footer_line": f"Generated {generated} by ROAR {__version__} · candidates {week_of}",
        "preheader": preheader,
        "n_selected": len(ctx_picks),
    }


def render_all(ctx: dict) -> dict[str, str]:
    env = Environment(loader=FileSystemLoader(str(ROOT / "roar" / "templates")),
                      autoescape=select_autoescape(["html", "j2"]), trim_blocks=False, lstrip_blocks=False)
    html_out = env.get_template("digest.html.j2").render(**ctx)
    env_md = Environment(loader=FileSystemLoader(str(ROOT / "roar" / "templates")), autoescape=False)
    md_out = env_md.get_template("digest.md.j2").render(**ctx)
    return {"html": html_out, "md": md_out, "txt": md_out}


def write_outputs(out_dir: Path, rendered: dict[str, str]) -> dict[str, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {}
    for ext in ("html", "md", "txt"):
        p = out_dir / f"digest.{ext}"
        p.write_text(rendered[ext], encoding="utf-8")
        paths[ext] = p
    return paths


def subject_line(ctx: dict) -> str:
    """Short enough to survive Gmail's subject truncation; the lead headline carries the hook."""
    lead = ctx["top_picks"][0]["headline"] if ctx.get("top_picks") else ""
    s = f"{ctx['title']} · {ctx['week_label']}"
    if lead:
        s += " · " + (lead if len(lead) <= 90 else lead[:88].rsplit(" ", 1)[0] + "…")
    return s[:160]
