"""Detección de duplicados.

Solo se fusionan observaciones con señales fuertes (misma URL, mismo
identificador de anuncio, misma imagen y vendedor) o con una combinación de
varias señales (mismo vendedor + mismo texto + mismo precio). Las
coincidencias débiles se marcan como *posibles* duplicados y NO se eliminan.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .models import Observation
from .normalization import normalize_currency, normalize_seller, normalize_text, normalize_url

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass
class DuplicateInfo:
    canonical_id: str
    reasons: list[str]


@dataclass
class DedupResult:
    unique: list[Observation]
    duplicates: dict[str, DuplicateInfo] = field(default_factory=dict)
    possible_duplicates: list[dict] = field(default_factory=list)


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _strong_keys(obs: Observation) -> list[tuple[str, tuple]]:
    keys: list[tuple[str, tuple]] = []
    url = normalize_url(obs.url)
    if url:
        keys.append(("misma URL", ("url", url)))
    if obs.listing_id:
        keys.append(("mismo identificador de anuncio", ("lid", obs.source_id, obs.listing_id)))
    seller = normalize_seller(obs.seller)
    if obs.image_id and seller:
        keys.append(("misma imagen y vendedor", ("img", obs.image_id, seller)))
    if seller and obs.price is not None:
        keys.append((
            "mismo vendedor, texto, precio y moneda (publicación replicada)",
            ("rep", seller, normalize_text(obs.title), obs.price, normalize_currency(obs.currency)),
        ))
    return keys


def deduplicate(observations: list[Observation]) -> DedupResult:
    n = len(observations)
    uf = _UnionFind(n)
    first_by_key: dict[tuple, int] = {}
    link_reasons: dict[int, set[str]] = defaultdict(set)

    for i, obs in enumerate(observations):
        for reason, key in _strong_keys(obs):
            if key in first_by_key:
                j = first_by_key[key]
                uf.union(j, i)
                link_reasons[i].add(reason)
            else:
                first_by_key[key] = i

    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        groups[uf.find(i)].append(i)

    kept: list[int] = []
    duplicates: dict[str, DuplicateInfo] = {}
    for members in groups.values():
        # Se conserva la captura más reciente del grupo.
        keep = max(members, key=lambda k: (observations[k].captured_at or _EPOCH, -k))
        kept.append(keep)
        for k in members:
            if k == keep:
                continue
            reasons = sorted(link_reasons.get(k, set()) | link_reasons.get(keep, set()))
            duplicates[observations[k].observation_id] = DuplicateInfo(
                canonical_id=observations[keep].observation_id,
                reasons=reasons or ["agrupado por transitividad con otro duplicado"],
            )

    unique = [observations[k] for k in sorted(kept)]

    # Señal débil: mismo texto y precio con vendedor distinto o ausente.
    weak: dict[tuple, list[str]] = defaultdict(list)
    for obs in unique:
        if obs.price is None:
            continue
        weak[(normalize_text(obs.title), obs.price, normalize_currency(obs.currency))].append(
            obs.observation_id
        )
    possible = [
        {
            "observation_ids": ids,
            "reason": "mismo texto y precio con vendedor distinto o desconocido (señal débil; no eliminado)",
        }
        for ids in weak.values()
        if len(ids) > 1
    ]
    return DedupResult(unique=unique, duplicates=duplicates, possible_duplicates=possible)
