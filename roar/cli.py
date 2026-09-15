"""Command-line entry point: python -m roar <command>."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from . import __version__
from .models import Item
from .util import ROOT, load_config, load_dotenv, read_json, setup_logging, write_json

log = logging.getLogger("roar.cli")


# ----------------------------------------------------------------------------- helpers
def default_week_of(today: date | None = None) -> str:
    """The Monday this digest is (or will be) delivered: today if Monday, else the next Monday."""
    today = today or date.today()
    days_ahead = (0 - today.weekday()) % 7
    return (today + timedelta(days=days_ahead)).isoformat()


def week_dir(cfg: dict, week_of: str) -> Path:
    return ROOT / cfg["settings"]["digest"]["output_dir"] / week_of


def latest_week_dir(cfg: dict) -> Path | None:
    base = ROOT / cfg["settings"]["digest"]["output_dir"]
    if not base.exists():
        return None
    dirs = sorted(p for p in base.iterdir() if p.is_dir() and (p / "candidates.json").exists())
    return dirs[-1] if dirs else None


def load_candidates(wdir: Path) -> tuple[list[Item], dict]:
    data = read_json(wdir / "candidates.json")
    if not data:
        raise SystemExit(f"no candidates.json in {wdir} — run `python -m roar fetch` first")
    return [Item.from_dict(d) for d in data["items"]], data.get("meta", {})


def candidates_markdown(items: list[Item], meta: dict) -> str:
    lines = [f"# Candidates — {meta.get('week_of', '')}", "",
             f"{len(items)} candidates from {meta.get('items_fetched', '?')} fetched items.", ""]
    for it in items:
        lines.append(f"- **{it.title}** — {it.journal} ({it.published or 'n.d.'}) · score {it.prescore:+d} · "
                     f"{', '.join(it.sites[:2]) or '-'} · {it.url}")
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------------- commands
def cmd_fetch(args, cfg: dict) -> int:
    from .fetch_pubmed import PubMedClient, add_abstracts, search_all
    from .fetch_rss import fetch_all
    from .normalize import merge_items
    from .prescore import Scorer, prescore_all
    from .state import SeenState
    from .util import http_session

    settings = cfg["settings"]
    week_of = args.week_of or default_week_of()
    wdir = week_dir(cfg, week_of)
    wdir.mkdir(parents=True, exist_ok=True)
    report: dict = {"week_of": week_of, "feeds": [], "queries": []}
    items: list[Item] = []

    if args.from_items:
        for f in args.from_items:
            data = read_json(Path(f))
            rows = data["items"] if isinstance(data, dict) else data
            items.extend(Item.from_dict(d) for d in rows)
        log.info("loaded %d items from %d file(s)", len(items), len(args.from_items))
    else:
        session = http_session()
        only_feeds = set(args.only_feeds.split(",")) if getattr(args, "only_feeds", None) else None
        only_queries = set(args.only_queries.split(",")) if getattr(args, "only_queries", None) else None
        if not args.only_pubmed and not only_queries:
            rss_items, report["feeds"] = fetch_all(cfg, session, only=only_feeds)
            items.extend(rss_items)
        if not args.only_rss and not only_feeds:
            pm = settings["pubmed"]
            client = PubMedClient(session, pm["tool"], os.environ.get("ROAR_NCBI_EMAIL") or pm["email"])
            pm_items, report["queries"] = search_all(cfg, client, only=only_queries)
            # stage 2: abstracts for the most promising records (scored on title + journal + pubtypes)
            scorer = Scorer(cfg["interests"])
            ranked = sorted(pm_items.values(), key=lambda it: -scorer.score(it)[0])
            top_pmids = [it.pmid for it in ranked[: int(pm["abstracts_for_top"])] if it.pmid]
            add_abstracts(pm_items, top_pmids, client, int(settings["candidates"]["abstract_chars"]))
            items.extend(pm_items.values())

    report["items_fetched"] = len(items)
    merged = merge_items(items)
    scored = prescore_all(merged, cfg["interests"])

    # state: never show something twice
    state = SeenState(ROOT / settings["digest"]["state_file"], int(settings["digest"]["seen_ttl_days"]))
    unseen, n_seen = state.filter_unseen(scored) if not args.no_state else (scored, 0)
    report["already_seen"] = n_seen

    # carry over last week's candidates if that digest was never rendered (analyst did not run)
    carried = 0
    prev = latest_week_dir(cfg)
    carry_ok = not args.no_state and not getattr(args, "no_carry_over", False)
    if carry_ok and prev and prev != wdir and not (prev / "digest.html").exists():
        prev_items, _ = load_candidates(prev)
        have = {it.id for it in unseen}
        for it in prev_items[:40]:
            if it.id not in have:
                it.score_reasons.append("carried-over")
                unseen.append(it)
                carried += 1
        log.info("carried over %d unrendered candidates from %s", carried, prev.name)
    report["carried_over"] = carried

    # selection
    c_cfg = settings["candidates"]
    min_score = int(c_cfg["min_prescore"])
    kept = [it for it in unseen if it.prescore >= min_score]
    kept.sort(key=lambda it: (-it.prescore, it.published or ""), reverse=False)
    per_source: dict[str, int] = {}
    selected: list[Item] = []
    cap_src = int(c_cfg["max_per_source"])
    for it in kept:
        src = it.sources[0] if it.sources else "?"
        if per_source.get(src, 0) >= cap_src and it.tier > 1:
            continue
        per_source[src] = per_source.get(src, 0) + 1
        selected.append(it)
        if len(selected) >= int(c_cfg["max_total"]):
            break
    report["candidates"] = len(selected)
    if args.from_items:
        report["sources_ok"] = len({s for it in items for s in it.sources})
    else:
        report["sources_ok"] = (sum(1 for f in report["feeds"] if f.get("ok"))
                                + sum(1 for q in report["queries"] if q.get("ok")))

    write_json(wdir / "candidates.json", {"meta": report, "items": [it.to_dict() for it in selected]})
    (wdir / "candidates.md").write_text(candidates_markdown(selected, report), encoding="utf-8")
    write_json(wdir / "fetch_report.json", report)
    if not args.no_state:
        state.mark(selected, when=date.today())
        state.save()
    log.info("fetched %d -> merged %d -> unseen %d -> candidates %d (written to %s)",
             len(items), len(merged), len(unseen), len(selected), wdir)
    # leave the analyst prompt/input ready for whoever analyses next
    from .analyze import build_analyst_input, build_system_prompt
    (wdir / "analyst_prompt.md").write_text(build_system_prompt(cfg), encoding="utf-8")
    (wdir / "analyst_input.md").write_text(build_analyst_input(selected, cfg, week_of), encoding="utf-8")
    print(wdir)
    return 0


def cmd_analyze(args, cfg: dict) -> int:
    from .analyze import run_analysis
    wdir = week_dir(cfg, args.week_of) if args.week_of else latest_week_dir(cfg)
    if not wdir:
        raise SystemExit("no candidates yet — run fetch first")
    candidates, meta = load_candidates(wdir)
    result = run_analysis(candidates, cfg, meta.get("week_of", wdir.name), wdir, backend=args.backend)
    if result is None:
        print(f"Waiting for {wdir / 'analysis.json'} — give analyst_prompt.md + analyst_input.md to the analyst.")
        return 2
    print(f"analysis ok: {len(result['picks'])} picks -> {wdir / 'analysis.validated.json'}")
    return 0


def cmd_render(args, cfg: dict) -> int:
    from .analyze import validate_analysis
    from .render import build_context, render_all, subject_line, write_outputs
    wdir = week_dir(cfg, args.week_of) if args.week_of else latest_week_dir(cfg)
    if not wdir:
        raise SystemExit("no candidates yet — run fetch first")
    candidates, meta = load_candidates(wdir)
    analysis = read_json(wdir / "analysis.validated.json")
    if analysis is None:
        raw = read_json(wdir / "analysis.json")
        if raw is None:
            raise SystemExit(f"no analysis in {wdir} — run analyze first")
        analysis, warnings = validate_analysis(raw, candidates, cfg)
        for w in warnings:
            log.warning("analysis: %s", w)
        if not analysis.get("week_of"):
            analysis["week_of"] = meta.get("week_of", wdir.name)
        write_json(wdir / "analysis.validated.json", analysis)
    ctx = build_context(analysis, candidates, cfg, fetch_meta=meta)
    rendered = render_all(ctx)
    paths = write_outputs(wdir, rendered)
    subject = subject_line(ctx)
    (wdir / "subject.txt").write_text(subject + "\n", encoding="utf-8")
    print(f"rendered {ctx['n_selected']} items -> {paths['html']}")
    print(f"subject: {subject}")
    return 0


def cmd_send(args, cfg: dict) -> int:
    from .send import deliver
    wdir = week_dir(cfg, args.week_of) if args.week_of else latest_week_dir(cfg)
    if not wdir or not (wdir / "digest.html").exists():
        raise SystemExit("nothing rendered yet — run render first")
    html_body = (wdir / "digest.html").read_text(encoding="utf-8")
    text_body = (wdir / "digest.txt").read_text(encoding="utf-8")
    subject = (wdir / "subject.txt").read_text(encoding="utf-8").strip()
    p = deliver(cfg, subject=subject, html_body=html_body, text_body=text_body, out_dir=wdir,
                transport=args.transport, recipient=args.to)
    print(p or "sent")
    return 0


def cmd_run(args, cfg: dict) -> int:
    rc = cmd_fetch(args, cfg)
    if rc:
        return rc
    rc = cmd_analyze(args, cfg)
    if rc:
        return rc
    rc = cmd_render(args, cfg)
    if rc:
        return rc
    if args.transport == "none":
        return 0
    return cmd_send(args, cfg)


def cmd_verify_sources(args, cfg: dict) -> int:
    """Hit every feed and query once and print a table. Needs network access."""
    from .fetch_pubmed import PubMedClient, expand_query
    from .fetch_rss import fetch_feed
    from .util import http_session
    session = http_session()
    print(f"{'feed':30} {'status':8} {'entries':>7}  latest")
    for f in cfg["sources"].get("feeds", []):
        if f.get("enabled", True) is False and not args.all:
            continue
        parsed, err = fetch_feed(session, f)
        if err:
            print(f"{f['id']:30} {'FAIL':8} {0:7}  {err[:60]}")
            continue
        from .fetch_rss import _entry_date
        dates = sorted((d for d in (_entry_date(e) for e in parsed.entries) if d), reverse=True)
        print(f"{f['id']:30} {'ok':8} {len(parsed.entries):7}  {dates[0] if dates else 'undated'}")
    pm = cfg["settings"]["pubmed"]
    client = PubMedClient(session, pm["tool"], os.environ.get("ROAR_NCBI_EMAIL") or pm["email"])
    frags = cfg["pubmed"].get("fragments", {})
    print()
    print(f"{'query':30} {'ids/8d':>7}")
    for q in cfg["pubmed"].get("queries", []):
        try:
            ids = client.esearch(expand_query(q["term"], frags), reldate=int(cfg["settings"]["digest"]["lookback_days"]),
                                 retmax=int(pm["retmax_per_query"]))
            print(f"{q['id']:30} {len(ids):7}")
        except Exception as exc:
            print(f"{q['id']:30} {'FAIL':>7}  {exc}")
    return 0


def cmd_show_prompt(args, cfg: dict) -> int:
    from .analyze import build_system_prompt
    print(build_system_prompt(cfg))
    return 0


def cmd_latest(args, cfg: dict) -> int:
    wdir = latest_week_dir(cfg)
    print(wdir or "")
    return 0


# ----------------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="roar", description=f"ROAR {__version__} — weekly radiation oncology digest")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--config-dir", default=None, help="alternate config directory")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--week-of", default=None, help="digest date YYYY-MM-DD (default: this/next Monday)")

    sp = sub.add_parser("fetch", help="collect candidates from RSS + PubMed")
    common(sp)
    sp.add_argument("--only-rss", action="store_true")
    sp.add_argument("--only-pubmed", action="store_true")
    sp.add_argument("--from-items", nargs="*", default=None, help="skip network; load item JSON file(s)")
    sp.add_argument("--no-state", action="store_true", help="ignore/skip the seen-state file")
    sp.add_argument("--only-feeds", default=None, help="comma-separated feed ids (implies no PubMed)")
    sp.add_argument("--only-queries", default=None, help="comma-separated PubMed query ids (implies no RSS)")
    sp.add_argument("--no-carry-over", action="store_true",
                    help="do not carry over last week's candidates when its folder has no digest.html "
                         "(use when the digest is rendered elsewhere, e.g. the Cowork task, and never committed)")
    sp.set_defaults(func=cmd_fetch)

    sp = sub.add_parser("analyze", help="rank + summarise candidates")
    common(sp)
    sp.add_argument("--backend", choices=["file", "anthropic", "none"], default=None)
    sp.set_defaults(func=cmd_analyze)

    sp = sub.add_parser("render", help="build digest.html / .md / .txt")
    common(sp)
    sp.set_defaults(func=cmd_render)

    sp = sub.add_parser("send", help="email the rendered digest")
    common(sp)
    sp.add_argument("--transport", choices=["smtp", "file"], default=None)
    sp.add_argument("--to", default=None)
    sp.set_defaults(func=cmd_send)

    sp = sub.add_parser("run", help="fetch -> analyze -> render -> send")
    common(sp)
    sp.add_argument("--only-rss", action="store_true")
    sp.add_argument("--only-pubmed", action="store_true")
    sp.add_argument("--from-items", nargs="*", default=None)
    sp.add_argument("--no-state", action="store_true")
    sp.add_argument("--only-feeds", default=None)
    sp.add_argument("--only-queries", default=None)
    sp.add_argument("--no-carry-over", action="store_true")
    sp.add_argument("--backend", choices=["file", "anthropic", "none"], default=None)
    sp.add_argument("--transport", choices=["smtp", "file", "none"], default=None)
    sp.add_argument("--to", default=None)
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("verify-sources", help="check every feed and query (network)")
    sp.add_argument("--all", action="store_true", help="include disabled feeds")
    sp.set_defaults(func=cmd_verify_sources)

    sp = sub.add_parser("show-prompt", help="print the analyst system prompt")
    sp.set_defaults(func=cmd_show_prompt)

    sp = sub.add_parser("latest", help="print the most recent digest folder")
    sp.set_defaults(func=cmd_latest)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    load_dotenv()
    cfg = load_config(Path(args.config_dir) if args.config_dir else None)
    return args.func(args, cfg)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
