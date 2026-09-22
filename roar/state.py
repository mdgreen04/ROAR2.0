"""Persistent memory of what has already been shown, so an item never appears in two digests.

Two registers, both keyed by item id / PMID / DOI / title fingerprint and stamped with an ISO date:

* ``seen``    — shown to the analyst; filtered out of every later fetch (``seen_ttl_days``).
* ``pending`` — surfaced by a feed without an abstract and held back (still *unseen*) so that a later
                PubMed/Crossref record can claim it; after ``enrich.defer_days`` it is shown as it is.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from .models import Item
from .util import parse_date, read_json, title_fingerprint, write_json


class SeenState:
    def __init__(self, path: Path, ttl_days: int = 120):
        self.path = Path(path)
        self.ttl_days = ttl_days
        data = read_json(self.path, default={}) or {}
        self.seen: dict[str, str] = dict(data.get("seen", {}))        # key -> ISO date first seen
        self.pending: dict[str, str] = dict(data.get("pending", {}))  # key -> ISO date first deferred

    # keys ------------------------------------------------------------------
    @staticmethod
    def keys_for(item: Item) -> list[str]:
        ks = [item.id]
        if item.pmid:
            ks.append(f"pmid:{item.pmid}")
        if item.doi:
            ks.append(f"doi:{item.doi}")
        if item.title:
            ks.append("title:" + title_fingerprint(item.title))
        return ks

    def is_seen(self, item: Item) -> bool:
        return any(k in self.seen for k in self.keys_for(item))

    def mark(self, items: list[Item], when: date | None = None) -> None:
        d = (when or date.today()).isoformat()
        for it in items:
            for k in self.keys_for(it):
                self.seen.setdefault(k, d)
                self.pending.pop(k, None)

    def filter_unseen(self, items: list[Item]) -> tuple[list[Item], int]:
        keep = [it for it in items if not self.is_seen(it)]
        return keep, len(items) - len(keep)

    # deferral ----------------------------------------------------------------
    def defer(self, items: list[Item], when: date | None = None) -> None:
        """Remember that these items were held back; the first date is kept on repeat deferrals."""
        d = (when or date.today()).isoformat()
        for it in items:
            for k in self.keys_for(it):
                self.pending.setdefault(k, d)

    def pending_age(self, item: Item, today: date | None = None) -> int | None:
        """Days since the item was first deferred, or None if it has never been deferred."""
        today = today or date.today()
        dates = [parse_date(self.pending[k]) for k in self.keys_for(item) if k in self.pending]
        dates = [d for d in dates if d]
        return (today - min(dates)).days if dates else None

    # persistence -------------------------------------------------------------
    def prune(self, today: date | None = None) -> None:
        today = today or date.today()
        cutoff = (today - timedelta(days=self.ttl_days)).isoformat()
        self.seen = {k: v for k, v in self.seen.items() if v >= cutoff}
        self.pending = {k: v for k, v in self.pending.items() if v >= cutoff}

    def save(self) -> None:
        self.prune()
        data = {"seen": dict(sorted(self.seen.items()))}
        if self.pending:
            data["pending"] = dict(sorted(self.pending.items()))
        write_json(self.path, data)
