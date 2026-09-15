"""Persistent memory of what has already been shown, so an item never appears in two digests."""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from .models import Item
from .util import read_json, title_fingerprint, write_json


class SeenState:
    def __init__(self, path: Path, ttl_days: int = 120):
        self.path = Path(path)
        self.ttl_days = ttl_days
        data = read_json(self.path, default={}) or {}
        self.seen: dict[str, str] = dict(data.get("seen", {}))  # key -> ISO date first seen

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

    def filter_unseen(self, items: list[Item]) -> tuple[list[Item], int]:
        keep = [it for it in items if not self.is_seen(it)]
        return keep, len(items) - len(keep)

    # persistence -------------------------------------------------------------
    def prune(self, today: date | None = None) -> None:
        today = today or date.today()
        cutoff = (today - timedelta(days=self.ttl_days)).isoformat()
        self.seen = {k: v for k, v in self.seen.items() if v >= cutoff}

    def save(self) -> None:
        self.prune()
        write_json(self.path, {"seen": dict(sorted(self.seen.items()))})
