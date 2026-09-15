#!/usr/bin/env python3
"""Convert data exported by the in-browser collector (tools/browser_collect.js) into ROAR item files.

Used when the machine running ROAR cannot reach PubMed / the feeds directly (e.g. a sandbox) but a
browser can. Produces `items_pubmed.json` and `items_rss.json` for `python -m roar fetch --from-items`.

    python tools/items_from_browser_export.py --stage1 roar_pubmed_stage1.json \
        --abstracts roar_pubmed_abstracts.json --rss roar_rss*.json --out digests/_offline/
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from roar.models import Item  # noqa: E402
from roar.util import (find_doi, load_config, norm_doi, norm_text, parse_date, strip_html,  # noqa: E402
                       truncate, utcnow, within_days)

CDATA = re.compile(r"<!\[CDATA\[|\]\]>")


def pubmed_items(stage1: Path, abstracts: Path | None, cfg: dict) -> list[dict]:
    data = json.loads(stage1.read_text(encoding="utf-8"))
    recs = data["recs"] if isinstance(data, dict) else data
    names = {q["id"]: q["name"] for q in cfg["pubmed"]["queries"]}
    abs_by_pmid: dict[str, dict] = {}
    if abstracts and abstracts.exists():
        for a in json.loads(abstracts.read_text(encoding="utf-8")):
            abs_by_pmid[a["p"]] = a
    chars = int(cfg["settings"]["candidates"]["abstract_chars"])
    out = []
    for r in recs:
        a = abs_by_pmid.get(r["p"], {})
        d = parse_date(a.get("d") or r.get("d"))
        doi = norm_doi(a.get("o") or r.get("o") or "")
        it = Item(
            id=f"pmid:{r['p']}",
            title=(r.get("t") or "").strip().rstrip("."),
            url=f"https://pubmed.ncbi.nlm.nih.gov/{r['p']}/",
            doi=doi, pmid=r["p"], journal=r.get("j", ""),
            published=d.isoformat() if d else None,
            abstract=truncate(a.get("a", ""), chars),
            pub_types=a.get("y") or r.get("y") or [],
            source_type="pubmed", sources=list(r.get("q", [])),
            source_names=[names.get(q, q) for q in r.get("q", [])],
            tier=int(r.get("r", 3)), category="pubmed", topics=list(r.get("k", [])),
            fetched_at=utcnow().isoformat(timespec="seconds"),
        )
        out.append(it.to_dict())
    return out


def rss_items(files: list[Path], cfg: dict) -> list[dict]:
    feeds = {f["id"]: f for f in cfg["sources"]["feeds"]}
    lookback = int(cfg["settings"]["digest"]["lookback_days"])
    chars = int(cfg["settings"]["candidates"]["abstract_chars"])
    topic_terms = [norm_text(t) for t in cfg["interests"].get("topic_filter_terms", [])]
    out = []
    for f in files:
        for r in json.loads(f.read_text(encoding="utf-8")):
            fid = r.get("src", f.stem.replace("roar_rss_", ""))
            fcfg = feeds.get(fid, {"id": fid, "name": fid, "tier": 3, "category": "news"})
            title = CDATA.sub("", strip_html(r.get("t", ""))).strip()
            if not title:
                continue
            d = parse_date(r.get("d"))
            if d is not None and not within_days(d, lookback):
                continue
            summary = truncate(CDATA.sub("", strip_html(r.get("s", ""))), chars)
            if fcfg.get("topic_filter"):
                hay = norm_text(f"{title} {summary}")
                if not any(t in hay for t in topic_terms):
                    continue
            ident = r.get("i") or ""
            doi = norm_doi(ident) if ident.lower().startswith(("doi:", "10.")) else find_doi(ident, r.get("l"), summary)
            link = (r.get("l") or "").strip()
            item_id = f"doi:{doi}" if doi else "url:" + hashlib.sha1((link or fid + "|" + title).encode()).hexdigest()[:16]
            it = Item(
                id=item_id, title=title, url=link or (f"https://doi.org/{doi}" if doi else ""), doi=doi,
                journal=fcfg["name"].split(" — ")[0], published=d.isoformat() if d else None,
                abstract=summary, source_type="rss", sources=[fid], source_names=[fcfg["name"]],
                tier=int(fcfg.get("tier", 3)), category=fcfg.get("category", ""),
                sites=[fcfg["site"]] if fcfg.get("site") else [],
                fetched_at=utcnow().isoformat(timespec="seconds"),
            )
            out.append(it.to_dict())
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage1", required=True)
    ap.add_argument("--abstracts", default=None)
    ap.add_argument("--rss", nargs="*", default=[])
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    cfg = load_config()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    pm = pubmed_items(Path(args.stage1), Path(args.abstracts) if args.abstracts else None, cfg)
    (out / "items_pubmed.json").write_text(json.dumps({"items": pm}, indent=1), encoding="utf-8")
    rss_files = [Path(p) for pat in args.rss for p in glob.glob(pat)]
    rs = rss_items(rss_files, cfg)
    (out / "items_rss.json").write_text(json.dumps({"items": rs}, indent=1), encoding="utf-8")
    print(f"pubmed items: {len(pm)} (with abstract: {sum(1 for i in pm if i['abstract'])}); rss items: {len(rs)} "
          f"from {len(rss_files)} files -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
