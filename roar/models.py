"""Data model for a candidate item. Plain dataclass so it serialises to JSON trivially."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Item:
    id: str                          # "pmid:12345" | "doi:10.1000/x" | "url:<hash>"
    title: str
    url: str = ""                    # best link to the article / story
    doi: str | None = None
    pmid: str | None = None
    journal: str = ""                # journal or outlet name
    published: str | None = None     # ISO date (YYYY-MM-DD) when known
    authors: str = ""                # "Smith J, Lee K, et al."
    abstract: str = ""               # abstract or feed summary (trimmed)
    pub_types: list[str] = field(default_factory=list)   # PubMed publication types when known
    source_type: str = "rss"         # "rss" | "pubmed"
    sources: list[str] = field(default_factory=list)      # feed ids / query ids that surfaced it
    source_names: list[str] = field(default_factory=list)
    tier: int = 3                    # best (lowest) tier among its sources
    category: str = ""               # radonc_journal | oncology_journal | news | ...
    topics: list[str] = field(default_factory=list)       # radonc | oncology | ai | screening | policy
    sites: list[str] = field(default_factory=list)        # gu, gi, hn, ...
    primary_site: str | None = None
    prescore: int = 0
    score_reasons: list[str] = field(default_factory=list)
    fetched_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Item":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})

    # convenience -------------------------------------------------------------
    @property
    def text_for_matching(self) -> str:
        return f"{self.title} {self.abstract}"
