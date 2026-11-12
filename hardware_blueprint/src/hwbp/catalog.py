"""Component catalog: loading, indexing and search.

The catalog is the engine's ground truth for electrical ratings, interfaces and pin capabilities —
the LLM (if one is plugged in) may *propose* parts, but every part that lands in a project is resolved
against a catalog entry (``resolve``) or canonicalised into one (see ``enrichment.canonicalize``).
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from hwbp.catalog_data import ALL_PARTS
from hwbp.models import CatalogPart, canonical_type

_TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_TOKEN.findall(text.lower()))


class Catalog:
    def __init__(self, parts: Iterable[CatalogPart]):
        self._parts: dict[str, CatalogPart] = {}
        for p in parts:
            self.add(p)

    # ------------------------------------------------------------------ construction
    @classmethod
    def from_dicts(cls, items: Iterable[dict]) -> "Catalog":
        return cls(CatalogPart.model_validate(d) for d in items)

    @classmethod
    def from_json(cls, path: str | Path) -> "Catalog":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dicts(data["parts"] if isinstance(data, dict) else data)

    def add(self, part: CatalogPart) -> None:
        if part.id in self._parts:
            raise ValueError(f"duplicate catalog id {part.id!r}")
        self._parts[part.id] = part

    def extend(self, other: "Catalog") -> "Catalog":
        for p in other.all():
            if p.id not in self._parts:
                self.add(p)
        return self

    # ------------------------------------------------------------------ queries
    def __len__(self) -> int:
        return len(self._parts)

    def __contains__(self, part_id: str) -> bool:
        return part_id in self._parts

    def get(self, part_id: str) -> CatalogPart:
        try:
            return self._parts[part_id]
        except KeyError:
            raise KeyError(f"unknown catalog part {part_id!r}") from None

    def all(self) -> list[CatalogPart]:
        return list(self._parts.values())

    def by_type(self, *types: str) -> list[CatalogPart]:
        return [p for p in self._parts.values() if p.type in types]

    def with_capability(self, capability: str) -> list[CatalogPart]:
        """Parts advertising ``capability``; a prefix like ``sense.`` matches any ``sense.*``."""
        if capability.endswith("."):
            return [p for p in self._parts.values() if any(c.startswith(capability) for c in p.capabilities)]
        return [p for p in self._parts.values() if capability in p.capabilities]

    def search(self, query: str, type_filter: str | None = None, limit: int = 10) -> list[tuple[CatalogPart, float]]:
        """Token-overlap search over id/name/mpn/capabilities/tags (Jaccard-like with name boost)."""
        q = _tokens(query)
        if not q:
            return []
        scored = []
        for p in self._parts.values():
            if type_filter and type_filter not in (p.type, canonical_type(p.type)):
                continue
            name_t = _tokens(f"{p.name} {p.mpn or ''} {p.id}")
            meta_t = _tokens(" ".join(p.capabilities + p.tags) + " " + p.description)
            hit_name = len(q & name_t)
            hit_meta = len(q & meta_t)
            if not hit_name and not hit_meta:
                continue
            score = (2.0 * hit_name + hit_meta) / (len(q) * 2.0) + 0.05 * p.popularity
            scored.append((p, round(score, 4)))
        scored.sort(key=lambda t: (-t[1], t[0].id))
        return scored[:limit]

    def resolve(self, name_or_mpn: str) -> CatalogPart | None:
        """Exact id/mpn match, else best name search hit above a confidence threshold."""
        key = name_or_mpn.strip().lower()
        for p in self._parts.values():
            if key in (p.id.lower(), (p.mpn or "").lower(), p.name.lower()):
                return p
        hits = self.search(name_or_mpn, limit=1)
        return hits[0][0] if hits and hits[0][1] >= 0.6 else None


@lru_cache(maxsize=1)
def default_catalog() -> Catalog:
    return Catalog.from_dicts(ALL_PARTS)
