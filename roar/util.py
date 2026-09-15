"""Shared helpers: config loading, paths, dates, text normalisation, HTTP."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("roar")

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"

USER_AGENT = "ROAR2.0/2.0 (+https://github.com/ROAR2.0; weekly oncology digest; contact via repo)"


# ----------------------------------------------------------------------------- config
def load_yaml(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_config(config_dir: Path | None = None) -> dict:
    """Load every YAML file in config/ into one dict keyed by file stem."""
    cdir = Path(config_dir) if config_dir else CONFIG_DIR
    cfg: dict[str, Any] = {}
    for name in ("settings", "sources", "pubmed", "interests", "conferences"):
        p = cdir / f"{name}.yaml"
        cfg[name] = load_yaml(p) if p.exists() else {}
    cfg["_config_dir"] = str(cdir)
    return cfg


def load_dotenv(path: Path | None = None) -> None:
    """Minimal .env loader (KEY=VALUE lines). Real secrets stay out of the repo."""
    p = Path(path) if path else ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


# ----------------------------------------------------------------------------- dates
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def today_iso() -> str:
    return date.today().isoformat()


def parse_date(value: Any) -> date | None:
    """Best-effort parse of the many date shapes found in feeds and PubMed."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if hasattr(value, "tm_year"):  # time.struct_time from feedparser
        try:
            return date(value.tm_year, value.tm_mon, value.tm_mday)
        except Exception:
            return None
    s = str(value).strip()
    # PubMed style: "2026 Sep 12", "2026 Sep", "2026", "2026/09/12"
    m = re.match(r"^(\d{4})[/-](\d{1,2})[/-](\d{1,2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    months = {m: i for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
    m = re.match(r"^(\d{4})\s+([A-Za-z]{3})\w*\.?\s*(\d{1,2})?", s)
    if m and m.group(2).lower() in months:
        try:
            return date(int(m.group(1)), months[m.group(2).lower()], int(m.group(3) or 1))
        except ValueError:
            return None
    try:
        import warnings
        from dateutil import parser as dp  # type: ignore
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return dp.parse(s, fuzzy=True, tzinfos=_TZINFOS).date()
    except Exception:
        return None


# Named US zones that feeds use in RFC-822 dates; dateutil only knows a few by default.
_TZINFOS = {"EST": -5 * 3600, "EDT": -4 * 3600, "CST": -6 * 3600, "CDT": -5 * 3600, "MST": -7 * 3600,
            "MDT": -6 * 3600, "PST": -8 * 3600, "PDT": -7 * 3600, "AKST": -9 * 3600, "AKDT": -8 * 3600,
            "GMT": 0, "UTC": 0, "BST": 3600, "CET": 3600, "CEST": 2 * 3600}


def within_days(d: date | None, days: int, ref: date | None = None) -> bool:
    if d is None:
        return False
    ref = ref or date.today()
    return (ref - timedelta(days=days)) <= d <= (ref + timedelta(days=2))


# ----------------------------------------------------------------------------- text
_WS = re.compile(r"\s+")
_TAGS = re.compile(r"<[^>]+>")


def strip_html(s: str | None) -> str:
    if not s:
        return ""
    import html as _html
    s = _TAGS.sub(" ", s)
    s = _html.unescape(s)
    return _WS.sub(" ", s).strip()


def norm_text(s: str | None) -> str:
    """Lower-case, ASCII-fold, collapse whitespace — for matching, never for display."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return _WS.sub(" ", s.lower()).strip()


def title_fingerprint(title: str) -> str:
    """Stable hash of a title with punctuation/case removed, for de-duplication across sources."""
    t = norm_text(title)
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    t = _WS.sub(" ", t).strip()
    return hashlib.sha1(t.encode("utf-8")).hexdigest()[:16]


def norm_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    d = doi.strip()
    d = re.sub(r"^(https?://)?(dx\.)?doi\.org/", "", d, flags=re.I)
    d = re.sub(r"^doi:\s*", "", d, flags=re.I)
    d = d.rstrip(".,;)")
    return d.lower() if d.startswith("10.") else None


DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+)", re.I)


def find_doi(*texts: str | None) -> str | None:
    for t in texts:
        if not t:
            continue
        m = DOI_RE.search(t)
        if m:
            return norm_doi(m.group(1))
    return None


def truncate(s: str, n: int) -> str:
    s = s or ""
    if len(s) <= n:
        return s
    cut = s[: n - 1]
    # cut at a sentence or word boundary when possible
    for sep in (". ", "; ", " "):
        i = cut.rfind(sep)
        if i > n * 0.6:
            return cut[: i + (1 if sep == ". " else 0)].rstrip() + "…"
    return cut.rstrip() + "…"


# ----------------------------------------------------------------------------- json
def read_json(path: Path, default: Any = None) -> Any:
    p = Path(path)
    if not p.exists():
        return default
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: Path, data: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False, default=str)
        fh.write("\n")


# ----------------------------------------------------------------------------- http
def http_session():
    """requests.Session with a polite UA and retries. Imported lazily so tests don't need network."""
    import requests
    from requests.adapters import HTTPAdapter

    try:
        from urllib3.util.retry import Retry
        retry = Retry(total=3, backoff_factor=1.0, status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=frozenset(["GET", "POST"]))
    except Exception:  # pragma: no cover
        retry = 3
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
