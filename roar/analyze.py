"""The analyst step: turn candidates into a ranked, summarised `analysis.json`.

Backends
  file       Write `analyst_input.md` + `analyst_prompt.md`; an external analyst (Claude in a Cowork
             scheduled task, or a human) writes `analysis.json` next to them. Used by the default flow.
  anthropic  Call the Claude API directly (needs ANTHROPIC_API_KEY). Used on the work computer or in
             GitHub Actions when an API key secret is configured.
  none       Keyword-only fallback: no summaries beyond the abstract, ranking by pre-score.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from .models import Item
from .util import ROOT, truncate

log = logging.getLogger("roar.analyze")

SECTIONS = ["radonc", "gu", "gi", "hn", "cutaneous", "heme", "gyn", "breast", "thoracic", "cns",
            "other_sites", "systemic", "policy", "screening", "ai"]


# ----------------------------------------------------------------------------- prompt assembly
def build_system_prompt(cfg: dict) -> str:
    tmpl = (ROOT / "roar" / "prompts" / "analyst.md").read_text(encoding="utf-8")
    profile = (cfg["interests"].get("profile") or "").strip()
    return tmpl.replace("{{PROFILE}}", profile)


def build_analyst_input(candidates: list[Item], cfg: dict, week_of: str) -> str:
    """Compact, token-frugal listing of candidates for the analyst."""
    lines = [f"# ROAR 2.0 candidates — week of {week_of}", "",
             f"{len(candidates)} candidates. Fields: id | journal | date | sites | prescore (reasons) | pub types",
             ""]
    for i, it in enumerate(candidates, 1):
        meta = " | ".join([
            it.id,
            it.journal or "-",
            it.published or "n.d.",
            ",".join(it.sites[:3]) or "-",
            f"{it.prescore:+d} ({', '.join(it.score_reasons[:6])})",
            ", ".join(it.pub_types[:3]) or "-",
        ])
        lines.append(f"## {i}. {it.title}")
        lines.append(meta)
        if it.sources:
            lines.append("via: " + ", ".join(it.source_names[:3]))
        if it.abstract:
            lines.append(it.abstract)
        lines.append("")
    return "\n".join(lines)


# ----------------------------------------------------------------------------- validation
def validate_analysis(analysis: dict, candidates: list[Item], cfg: dict) -> tuple[dict, list[str]]:
    """Coerce an analyst's JSON into something the renderer can trust. Returns (clean, warnings)."""
    shape = cfg["settings"]["digest_shape"]
    warnings: list[str] = []
    ids = {c.id: c for c in candidates}
    # tolerate common id slips (missing prefix, DOI url)
    alt: dict[str, str] = {}
    for c in candidates:
        if c.pmid:
            alt[c.pmid] = c.id
        if c.doi:
            alt[c.doi] = c.id
            alt["doi:" + c.doi] = c.id
    seen: set[str] = set()
    clean_picks: list[dict] = []
    for p in analysis.get("picks", []) or []:
        pid = str(p.get("id", "")).strip()
        if pid not in ids:
            pid = alt.get(pid, alt.get(pid.lower(), ""))
        if not pid or pid in seen:
            warnings.append(f"dropped pick with unknown/duplicate id: {p.get('id')}")
            continue
        seen.add(pid)
        section = str(p.get("section", "")).strip().lower()
        if section == "top":
            section = _guess_section(ids[pid])
            p["top"] = True
        if section not in SECTIONS:
            warnings.append(f"{pid}: unknown section '{section}', guessed")
            section = _guess_section(ids[pid])
        try:
            score = int(round(float(p.get("score", 50))))
        except Exception:
            score = 50
        score = max(0, min(100, score))
        headline = truncate(str(p.get("headline") or ids[pid].title).strip(), 140)
        clean_picks.append({
            "id": pid,
            "top": bool(p.get("top", False)),
            "section": section,
            "score": score,
            "headline": headline,
            "summary": str(p.get("summary", "")).strip(),
            "why_it_matters": str(p.get("why_it_matters", "")).strip(),
            "tags": [str(t)[:40] for t in (p.get("tags") or [])][:6],
        })
    clean_picks.sort(key=lambda x: -x["score"])

    # enforce ceilings
    max_top = int(shape.get("top_picks", 5))
    min_top = int(shape.get("top_pick_min_score", 80))
    tops = [p for p in clean_picks if p["top"] and p["score"] >= min_top][:max_top]
    top_ids = {p["id"] for p in tops}
    for p in clean_picks:
        p["top"] = p["id"] in top_ids
    tidbit_cap = int(shape.get("tidbits_per_section", 4))
    counts: dict[str, int] = {}
    limited: list[dict] = []
    for p in clean_picks:
        if p["section"] in ("screening", "ai") and not p["top"]:
            counts[p["section"]] = counts.get(p["section"], 0) + 1
            if counts[p["section"]] > tidbit_cap:
                warnings.append(f"{p['id']}: over tidbit cap for {p['section']}")
                continue
        limited.append(p)
    max_items = int(shape.get("max_items", 40)) + max_top
    if len(limited) > max_items:
        warnings.append(f"trimmed picks from {len(limited)} to {max_items}")
        limited = limited[:max_items]

    clean = {
        "week_of": str(analysis.get("week_of") or ""),
        "editor_note": str(analysis.get("editor_note", "")).strip(),
        "skipped_summary": str(analysis.get("skipped_summary", "")).strip(),
        "picks": limited,
    }
    return clean, warnings


def _guess_section(item: Item) -> str:
    if item.primary_site and item.primary_site in SECTIONS:
        return item.primary_site
    if "ai" in item.topics:
        return "ai"
    if "screening" in item.topics:
        return "screening"
    if "policy" in item.topics:
        return "policy"
    return "radonc" if "radonc" in item.topics else "systemic"


# ----------------------------------------------------------------------------- backends
def analyze_none(candidates: list[Item], cfg: dict, week_of: str) -> dict:
    n = int(cfg["settings"]["analysis"].get("keyword_only_max_items", 40))
    ranked = sorted(candidates, key=lambda c: -c.prescore)[:n]
    picks = []
    for i, c in enumerate(ranked):
        score = max(40, min(95, 50 + c.prescore * 4))
        picks.append({
            "id": c.id, "top": i < 5 and score >= 80, "section": _guess_section(c), "score": score,
            "headline": c.title, "summary": truncate(c.abstract, 420) or "(no abstract available)",
            "why_it_matters": "", "tags": [t for t in c.topics][:3],
        })
    return {
        "week_of": week_of,
        "editor_note": ("Keyword-only digest: no analyst was available this week, so items are ranked by "
                        "journal tier and study-design keywords and summaries are the abstracts."),
        "skipped_summary": f"{len(candidates) - len(ranked)} lower-scoring candidates not shown.",
        "picks": picks,
    }


def _extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


def analyze_anthropic(candidates: list[Item], cfg: dict, week_of: str) -> dict:
    try:
        import anthropic  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("pip install anthropic  (the 'anthropic' backend needs the SDK)") from exc
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY is not set (put it in .env or a GitHub secret)")
    a_cfg = cfg["settings"]["analysis"]
    client = anthropic.Anthropic()
    system = build_system_prompt(cfg)
    user = build_analyst_input(candidates, cfg, week_of)
    user += f"\n\nToday's week_of value is {week_of}. Return the JSON now."
    last_err = None
    for attempt in range(2):
        msg = client.messages.create(
            model=a_cfg.get("anthropic_model", "claude-sonnet-5"),
            max_tokens=int(a_cfg.get("anthropic_max_tokens", 16000)),
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(getattr(b, "text", "") for b in msg.content)
        try:
            return _extract_json(text)
        except Exception as exc:  # retry once on malformed JSON
            last_err = exc
            log.warning("analyst returned non-JSON (attempt %d): %s", attempt + 1, exc)
            user += "\n\nYour previous reply was not valid JSON. Return only the JSON object."
    raise RuntimeError(f"analyst output could not be parsed: {last_err}")


def run_analysis(candidates: list[Item], cfg: dict, week_of: str, out_dir: Path,
                 backend: str | None = None) -> dict | None:
    """Dispatch to a backend. Writes analyst files in `out_dir`. Returns validated analysis or None
    when the `file` backend is waiting for an external analyst."""
    backend = backend or cfg["settings"]["analysis"].get("backend", "file")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # always leave the prompt + input on disk: useful for audit and for the Cowork flow
    (out_dir / "analyst_prompt.md").write_text(build_system_prompt(cfg), encoding="utf-8")
    (out_dir / "analyst_input.md").write_text(build_analyst_input(candidates, cfg, week_of), encoding="utf-8")

    if backend == "file":
        p = out_dir / "analysis.json"
        if not p.exists():
            log.info("file backend: waiting for %s (see analyst_prompt.md + analyst_input.md)", p)
            return None
        raw = json.loads(p.read_text(encoding="utf-8"))
    elif backend == "anthropic":
        raw = analyze_anthropic(candidates, cfg, week_of)
    elif backend == "none":
        raw = analyze_none(candidates, cfg, week_of)
    else:
        raise SystemExit(f"unknown analysis backend: {backend}")

    clean, warnings = validate_analysis(raw, candidates, cfg)
    if not clean.get("week_of"):
        clean["week_of"] = week_of
    for w in warnings:
        log.warning("analysis: %s", w)
    (out_dir / "analysis.validated.json").write_text(json.dumps(clean, indent=2, ensure_ascii=False),
                                                     encoding="utf-8")
    return clean
