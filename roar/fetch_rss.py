"""Fetch RSS/Atom feeds listed in config/sources.yaml and turn entries into Items."""
from __future__ import annotations

import hashlib
import logging
import time
from datetime import date

import feedparser

from .models import Item
from .util import (find_doi, norm_doi, norm_text, parse_date, strip_html, truncate,
                   utcnow, within_days)

log = logging.getLogger("roar.rss")


def _entry_date(e) -> date | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        v = e.get(key)
        if v:
            d = parse_date(v)
            if d:
                return d
    for key in ("published", "updated", "dc_date", "date", "prism_publicationdate", "prism_coverdate"):
        v = e.get(key)
        if v:
            d = parse_date(v)
            if d:
                return d
    return None


def _entry_doi(e) -> str | None:
    for key in ("dc_identifier", "prism_doi", "id", "link"):
        v = e.get(key)
        if isinstance(v, str):
            d = norm_doi(v) if v.lower().startswith(("doi:", "10.")) else find_doi(v)
            if d:
                return d
    return find_doi(e.get("summary", ""), e.get("title", ""))


def _entry_summary(e) -> str:
    if e.get("content"):
        try:
            return strip_html(e["content"][0].get("value", ""))
        except Exception:  # pragma: no cover
            pass
    return strip_html(e.get("summary") or e.get("description") or "")


def _entry_authors(e) -> str:
    names = []
    for a in e.get("authors", []) or []:
        n = a.get("name") if isinstance(a, dict) else None
        if n:
            names.append(n)
    if not names and e.get("author"):
        names = [x.strip() for x in str(e["author"]).split(",") if x.strip()][:3]
    if len(names) > 3:
        return ", ".join(names[:3]) + ", et al."
    return ", ".join(names)


def entries_to_items(feed_cfg: dict, parsed, *, lookback_days: int, abstract_chars: int,
                     topic_terms: list[str], today: date | None = None) -> list[Item]:
    items: list[Item] = []
    today = today or date.today()
    feed_title = (parsed.feed.get("title") if getattr(parsed, "feed", None) else None) or feed_cfg["name"]
    for e in parsed.entries:
        title = strip_html(e.get("title", "")).strip()
        if not title:
            continue
        link = (e.get("link") or "").strip()
        d = _entry_date(e)
        # Undated items are kept (state/seen.json stops them recurring); dated items must be recent.
        if d is not None and not within_days(d, lookback_days, ref=today):
            continue
        summary = truncate(_entry_summary(e), abstract_chars)
        if feed_cfg.get("topic_filter"):
            hay = norm_text(f"{title} {summary}")
            if not any(t in hay for t in topic_terms):
                continue
        doi = _entry_doi(e)
        if doi:
            item_id = f"doi:{doi}"
        else:
            basis = link or (feed_cfg["id"] + "|" + title)
            item_id = "url:" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]
        items.append(Item(
            id=item_id,
            title=title,
            url=link or (f"https://doi.org/{doi}" if doi else ""),
            doi=doi,
            journal=_clean_feed_title(feed_title, feed_cfg["name"]),
            published=d.isoformat() if d else None,
            authors=_entry_authors(e),
            abstract=summary,
            source_type="rss",
            sources=[feed_cfg["id"]],
            source_names=[feed_cfg["name"]],
            tier=int(feed_cfg.get("tier", 3)),
            category=feed_cfg.get("category", ""),
            sites=[feed_cfg["site"]] if feed_cfg.get("site") else [],
            fetched_at=utcnow().isoformat(timespec="seconds"),
        ))
    return items


def _clean_feed_title(feed_title: str, fallback: str) -> str:
    t = (feed_title or "").strip()
    for junk in ("ScienceDirect Publication: ", "WoltersKluwer: ", "Wiley: ", ": Table of Contents",
                 "Most Recent Articles: ", " RSS Feed", "massmed: "):
        t = t.replace(junk, "")
    return t or fallback


BROWSER_HEADERS = {
    # Many publisher sites (Elsevier "Health Advance" journals, Wiley, Lancet, NEJM, Substack) answer 403 to
    # anything that does not look like a browser, especially from cloud IP ranges such as GitHub Actions.
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/128.0.0.0 Safari/537.36"),
    "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5",
    "Accept-Language": "en-US,en;q=0.9",
}
FEEDLY_STREAM = "https://cloud.feedly.com/v3/streams/contents"


def _parse_bytes(content: bytes):
    parsed = feedparser.parse(content)
    if parsed.bozo and not parsed.entries:
        return None, f"parse error: {getattr(parsed, 'bozo_exception', 'unknown')}"
    return parsed, None


def _feedly_fallback(session, url: str, timeout: int, count: int = 40):
    """Read the feed through Feedly's public stream cache and rebuild it as RSS for feedparser.

    Feedly keeps polling most journal feeds from its own infrastructure, so this works for feeds whose
    publishers block cloud IP ranges. Unauthenticated access is rate-limited, so it is only used as a
    fallback."""
    import json
    from email.utils import formatdate
    from xml.sax.saxutils import escape

    r = session.get(FEEDLY_STREAM, params={"streamId": f"feed/{url}", "count": count},
                    headers={"Accept": "application/json", "User-Agent": BROWSER_HEADERS["User-Agent"]},
                    timeout=timeout)
    if r.status_code >= 400:
        return None, f"feedly HTTP {r.status_code}"
    data = json.loads(r.text)
    items = data.get("items") or []
    if not items:
        return None, "feedly: no items"
    parts = ['<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel>',
             f"<title>{escape(str(data.get('title') or url))}</title>"]
    for it in items:
        link = ""
        for alt in it.get("alternate") or []:
            if alt.get("href"):
                link = alt["href"]
                break
        link = link or it.get("canonicalUrl") or it.get("originId") or ""
        body = ((it.get("content") or {}).get("content")) or ((it.get("summary") or {}).get("content")) or ""
        ts = it.get("published") or it.get("crawled")
        pub = formatdate(ts / 1000.0, usegmt=True) if ts else ""
        parts.append("<item>")
        parts.append(f"<title>{escape(str(it.get('title') or ''))}</title>")
        if link:
            parts.append(f"<link>{escape(str(link))}</link>")
        if it.get("originId"):
            parts.append(f"<guid isPermaLink=\"false\">{escape(str(it['originId']))}</guid>")
        if pub:
            parts.append(f"<pubDate>{pub}</pubDate>")
        if it.get("author"):
            parts.append(f"<author>{escape(str(it['author']))}</author>")
        if body:
            parts.append(f"<description>{escape(str(body))}</description>")
        parts.append("</item>")
    parts.append("</channel></rss>")
    return _parse_bytes("".join(parts).encode("utf-8"))


def fetch_feed(session, feed_cfg: dict, timeout: int = 30):
    """Download and parse one feed. Returns (parsed, error).

    Order of attempts: polite UA -> browser-like headers -> Feedly's public stream cache. The route that
    worked is recorded on the parsed object as ``parsed.roar_via`` ("direct" | "browser-ua" | "feedly")."""
    url = feed_cfg["url"]
    errors: list[str] = []
    try:
        r = session.get(url, timeout=timeout)
        if r.status_code < 400:
            parsed, err = _parse_bytes(r.content)
            if parsed is not None:
                parsed.roar_via = "direct"
                return parsed, None
            errors.append(err)
        else:
            errors.append(f"HTTP {r.status_code}")
    except Exception as exc:  # network errors, timeouts
        errors.append(f"{type(exc).__name__}: {exc}")

    try:
        r = session.get(url, headers=BROWSER_HEADERS, timeout=timeout)
        if r.status_code < 400:
            parsed, err = _parse_bytes(r.content)
            if parsed is not None:
                parsed.roar_via = "browser-ua"
                return parsed, None
            errors.append("browser-ua " + err)
        else:
            errors.append(f"browser-ua HTTP {r.status_code}")
    except Exception as exc:
        errors.append(f"browser-ua {type(exc).__name__}: {exc}")

    try:
        parsed, err = _feedly_fallback(session, url, timeout)
        if parsed is not None:
            parsed.roar_via = "feedly"
            return parsed, None
        errors.append(err)
    except Exception as exc:
        errors.append(f"feedly {type(exc).__name__}: {exc}")
    return None, "; ".join(errors)


def fetch_all(cfg: dict, session=None, *, only: set[str] | None = None) -> tuple[list[Item], list[dict]]:
    """Fetch every enabled feed. Returns (items, per-feed report)."""
    from .util import http_session
    session = session or http_session()
    settings = cfg["settings"]
    lookback = int(settings["digest"]["lookback_days"])
    abstract_chars = int(settings["candidates"]["abstract_chars"])
    topic_terms = [norm_text(t) for t in cfg["interests"].get("topic_filter_terms", [])]

    items: list[Item] = []
    report: list[dict] = []
    for feed_cfg in cfg["sources"].get("feeds", []):
        if feed_cfg.get("enabled", True) is False:
            continue
        if only and feed_cfg["id"] not in only:
            continue
        t0 = time.time()
        parsed, err = fetch_feed(session, feed_cfg)
        if err:
            log.warning("feed %-28s FAILED %s", feed_cfg["id"], err)
            report.append({"id": feed_cfg["id"], "ok": False, "error": err, "entries": 0, "kept": 0})
            continue
        kept = entries_to_items(feed_cfg, parsed, lookback_days=lookback, abstract_chars=abstract_chars,
                                topic_terms=topic_terms)
        via = getattr(parsed, "roar_via", "direct")
        log.info("feed %-28s %3d entries -> %3d kept (%.1fs, %s)", feed_cfg["id"], len(parsed.entries),
                 len(kept), time.time() - t0, via)
        report.append({"id": feed_cfg["id"], "ok": True, "entries": len(parsed.entries), "kept": len(kept),
                       "via": via})
        items.extend(kept)
    return items, report
