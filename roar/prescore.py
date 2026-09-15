"""Keyword / tier pre-scoring and disease-site classification.

This is deliberately crude: its only job is to make sure the analyst sees the plausible ~150 items
instead of ~800. The analyst (LLM or Claude) makes the real editorial decisions.
"""
from __future__ import annotations

from .models import Item
from .util import norm_text


def _terms(lst) -> list[str]:
    return [norm_text(t) if t.strip() == t else t.lower() for t in (lst or [])]


class Scorer:
    def __init__(self, interests: dict):
        self.interests = interests
        sc = interests.get("scoring", {})
        self.tier_bonus = {int(k): int(v) for k, v in (sc.get("tier_bonus") or {}).items()}
        self.emphasis_points = int(sc.get("emphasis_site_points", 1))
        self.rules = []
        for r in sc.get("rules", []):
            self.rules.append({
                "name": r["name"],
                "points": int(r["points"]),
                "any": _terms(r.get("any")),
                "journals": _terms(r.get("journals")),
            })
        self.sites = {}
        for key, spec in (interests.get("sites") or {}).items():
            self.sites[key] = {"label": spec.get("label", key), "terms": _terms(spec.get("terms"))}
        self.emphasis = set(interests.get("emphasis_sites") or [])
        self.topic_terms = _terms(interests.get("topic_filter_terms"))

    # ------------------------------------------------------------------ sites
    def classify_sites(self, item: Item) -> list[str]:
        title = norm_text(item.title)
        body = norm_text(item.abstract)
        found: list[tuple[int, str]] = []
        for key, spec in self.sites.items():
            best = 0
            for t in spec["terms"]:
                if t in title:
                    best = max(best, 3)
                elif t in body:
                    best = max(best, 1)
            if best:
                found.append((best, key))
        found.sort(key=lambda x: (-x[0], list(self.sites).index(x[1])))
        sites = [k for _, k in found]
        # keep feed-provided site hints
        for s in item.sites:
            if s not in sites:
                sites.append(s)
        return sites

    # ------------------------------------------------------------------ score
    def score(self, item: Item) -> tuple[int, list[str]]:
        hay = " " + norm_text(item.text_for_matching) + " "
        journal = norm_text(item.journal)
        reasons: list[str] = []
        pts = self.tier_bonus.get(int(item.tier), 0)
        if pts:
            reasons.append(f"tier{item.tier}:{pts:+d}")
        for r in self.rules:
            hit = False
            if r["any"] and any(t in hay for t in r["any"]):
                hit = True
            if r["journals"] and any(j in journal for j in r["journals"]):
                hit = True
            if hit:
                pts += r["points"]
                reasons.append(f"{r['name']}:{r['points']:+d}")
        # PubMed publication types are reliable when present
        pt = " ".join(item.pub_types).lower()
        if "randomized controlled trial" in pt and "randomized:+3" not in reasons:
            pts += 3
            reasons.append("pubtype-rct:+3")
        if "practice guideline" in pt or "guideline" in pt:
            pts += 2
            reasons.append("pubtype-guideline:+2")
        if "editorial" in pt or "comment" in pt or "letter" in pt:
            pts -= 2
            reasons.append("pubtype-comment:-2")
        if "review" in pt and "systematic review" not in pt and "meta-analysis" not in pt:
            pts -= 1
            reasons.append("pubtype-review:-1")
        sites = self.classify_sites(item)
        if any(s in self.emphasis for s in sites[:2]):
            pts += self.emphasis_points
            reasons.append(f"emphasis-site:+{self.emphasis_points}")
        return pts, reasons

    def passes_topic_filter(self, item: Item) -> bool:
        hay = norm_text(item.text_for_matching)
        return any(t in hay for t in self.topic_terms)

    # ------------------------------------------------------------------ apply
    def apply(self, item: Item) -> Item:
        item.sites = self.classify_sites(item)
        item.primary_site = item.sites[0] if item.sites else None
        item.prescore, item.score_reasons = self.score(item)
        # topic tags for the analyst (cheap heuristics; queries may already have set them)
        hay = " " + norm_text(item.text_for_matching) + " "
        topics = set(item.topics)
        for r in self.rules:
            if r["name"] in ("rt_core", "ai", "screening", "policy") and r["any"] and any(t in hay for t in r["any"]):
                topics.add({"rt_core": "radonc"}.get(r["name"], r["name"]))
        item.topics = sorted(topics)
        return item


def prescore_all(items: list[Item], interests: dict) -> list[Item]:
    sc = Scorer(interests)
    return [sc.apply(it) for it in items]
