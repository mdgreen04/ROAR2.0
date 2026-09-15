"""De-duplication and merging of items that arrive from several sources."""
from __future__ import annotations

from .models import Item
from .util import title_fingerprint


def merge_items(items: list[Item]) -> list[Item]:
    """Merge items that share a PMID, DOI or (near-)identical title. PubMed records win as the base
    (they carry abstracts and publication types); every source id is retained."""
    by_key: dict[str, Item] = {}
    order: list[str] = []

    def keys(it: Item) -> list[str]:
        ks = []
        if it.pmid:
            ks.append(f"pmid:{it.pmid}")
        if it.doi:
            ks.append(f"doi:{it.doi}")
        if it.title:
            ks.append("title:" + title_fingerprint(it.title))
        return ks or [it.id]

    alias: dict[str, str] = {}  # any key -> canonical key

    def canonical(it: Item) -> str | None:
        for k in keys(it):
            if k in alias:
                return alias[k]
        return None

    for it in items:
        ck = canonical(it)
        if ck is None:
            ck = keys(it)[0]
            by_key[ck] = it
            order.append(ck)
        else:
            base = by_key[ck]
            _merge_into(base, it)
            if _prefers(it, base):
                # promote the richer record but keep merged fields
                promoted = it
                _merge_into(promoted, base)
                by_key[ck] = promoted
        for k in keys(by_key[ck]):
            alias[k] = ck
        for k in keys(it):
            alias[k] = ck
    return [by_key[k] for k in order]


def _prefers(a: Item, b: Item) -> bool:
    """Should `a` replace `b` as the base record?"""
    if a.source_type == "pubmed" and b.source_type != "pubmed":
        return True
    if a.source_type == b.source_type and len(a.abstract) > len(b.abstract) + 200:
        return True
    return False


def _merge_into(base: Item, other: Item) -> None:
    for s, n in zip(other.sources, other.source_names):
        if s not in base.sources:
            base.sources.append(s)
            base.source_names.append(n)
    base.tier = min(base.tier, other.tier)
    if not base.doi and other.doi:
        base.doi = other.doi
    if not base.pmid and other.pmid:
        base.pmid = other.pmid
        if not base.url or "doi.org" in base.url:
            base.url = other.url
    if not base.published and other.published:
        base.published = other.published
    if len(other.abstract) > len(base.abstract):
        base.abstract = other.abstract
    if not base.pub_types and other.pub_types:
        base.pub_types = list(other.pub_types)
    if not base.authors and other.authors:
        base.authors = other.authors
    if (not base.url) and other.url:
        base.url = other.url
    for t in other.topics:
        if t not in base.topics:
            base.topics.append(t)
    for s in other.sites:
        if s not in base.sites:
            base.sites.append(s)
    if not base.category or base.category == "pubmed":
        base.category = other.category or base.category
