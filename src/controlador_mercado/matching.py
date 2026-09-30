"""Evaluación de equivalencia entre el producto objetivo y cada observación.

Reglas deterministas y explicables: cada resultado incluye las razones que
lo justifican para que el Agente Central pueda auditarlo.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import MatchLevel, Observation, TargetProduct
from .normalization import (
    Presentation,
    build_presentation,
    normalize_text,
    observation_presentation,
    same_presentation,
)

_STOPWORDS = {
    "de", "del", "la", "el", "los", "las", "en", "con", "para", "y", "o", "a",
    "x", "×", "un", "una", "por", "sin", "al", "tipo",
}
_UNIT_TOKENS = {
    "ml", "cl", "l", "lt", "lts", "litro", "litros", "g", "gr", "grs", "gramo", "gramos",
    "kg", "kgs", "kilo", "kilos", "lb", "lbs", "libra", "libras", "u", "ud", "uds",
    "unidad", "unidades",
}


_SIZE_TOKEN_RE = re.compile(r"(?:\d+(?:[.,]\d+)?)([a-z×]{1,9})|[x×](\d+)")


class PresentationStatus:
    MISMA = "MISMA"
    DIFERENTE = "DIFERENTE"
    DESCONOCIDA = "DESCONOCIDA"
    NO_ESPECIFICADA = "NO_ESPECIFICADA"  # el objetivo no fija presentación


@dataclass
class MatchResult:
    level: MatchLevel
    token_coverage: float
    presentation_status: str
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "level": self.level.value,
            "token_coverage": round(self.token_coverage, 3),
            "presentation_status": self.presentation_status,
            "reasons": self.reasons,
        }


def _stem(token: str) -> str:
    if len(token) > 4 and token.endswith("es"):
        return token[:-2]
    if len(token) > 3 and token.endswith("s"):
        return token[:-1]
    return token


def _tokens(text: str | None) -> list[str]:
    out = []
    for tok in normalize_text(text).replace(",", " ").replace("-", " ").split():
        tok = tok.strip(".-")
        if not tok or tok in _STOPWORDS or tok in _UNIT_TOKENS:
            continue
        if any(ch.isdigit() for ch in tok) and not any(ch.isalpha() for ch in tok):
            continue  # cantidades sueltas: se evalúan como presentación
        size = _SIZE_TOKEN_RE.fullmatch(tok)
        if size and (size.group(2) or size.group(1) in _UNIT_TOKENS or size.group(1) in ("x", "×")):
            continue  # "1.5l", "330ml", "6x": presentación, no identidad
        out.append(_stem(tok))
    return out


def _cap(level: MatchLevel, maximum: MatchLevel) -> MatchLevel:
    return level if level.rank <= maximum.rank else maximum


def target_presentation(target: TargetProduct) -> Presentation:
    return build_presentation(target.quantity, target.unit, target.pack_count, "")


def target_tokens(target: TargetProduct) -> list[str]:
    tokens: list[str] = []
    for part in (target.name, target.model, target.variant, *target.keywords):
        for tok in _tokens(part):
            if tok not in tokens:
                tokens.append(tok)
    brand_tokens = set(_tokens(target.brand))
    return [t for t in tokens if t not in brand_tokens]


def match_observation(target: TargetProduct, obs: Observation) -> MatchResult:
    reasons: list[str] = []
    title_tokens = set(_tokens(obs.title)) | set(_tokens(obs.brand))
    title_norm = normalize_text(obs.title)

    # 1) Palabras excluyentes definidas por quien solicita el análisis.
    for kw in target.exclude_keywords:
        kw_norm = normalize_text(kw)
        if kw_norm and kw_norm in title_norm:
            return MatchResult(MatchLevel.NO_MATCH, 0.0, PresentationStatus.DESCONOCIDA,
                               [f"contiene palabra excluyente '{kw}'"])

    # 2) Marca.
    brand_status = "no_requerida"
    if target.brand:
        tb = set(_tokens(target.brand))
        if tb and tb <= title_tokens:
            brand_status = "confirmada"
        elif obs.brand:
            return MatchResult(MatchLevel.NO_MATCH, 0.0, PresentationStatus.DESCONOCIDA,
                               [f"marca distinta: '{obs.brand}' vs '{target.brand}'"])
        else:
            brand_status = "no_verificable"
    reasons.append(f"marca: {brand_status}")

    # 3) Cobertura de términos del producto.
    wanted = target_tokens(target)
    if wanted:
        hits = sum(1 for t in wanted if t in title_tokens)
        coverage = hits / len(wanted)
    else:
        coverage = 1.0 if brand_status == "confirmada" else 0.0
    reasons.append(f"cobertura de términos: {coverage:.0%} de {len(wanted)}")

    if coverage >= 1.0:
        level = MatchLevel.EXACT
    elif coverage >= 0.75:
        level = MatchLevel.HIGH
    elif coverage >= 0.5:
        level = MatchLevel.MEDIUM
    elif coverage >= 0.25:
        level = MatchLevel.LOW
    else:
        level = MatchLevel.NO_MATCH

    if brand_status == "no_verificable":
        level = _cap(level, MatchLevel.HIGH)

    # 4) Estado del producto (nuevo/usado...).
    if target.condition and obs.condition:
        if normalize_text(target.condition) != normalize_text(obs.condition):
            level = _cap(level, MatchLevel.LOW)
            reasons.append(f"estado distinto: '{obs.condition}' vs '{target.condition}'")

    # 5) Presentación.
    t_pres = target_presentation(target)
    o_pres = observation_presentation(obs)
    if not t_pres.known:
        status = PresentationStatus.NO_ESPECIFICADA
        level = _cap(level, MatchLevel.HIGH)
        reasons.append("presentación del objetivo no especificada: EXACT no verificable")
    else:
        same = same_presentation(t_pres, o_pres)
        if same is None:
            status = PresentationStatus.DESCONOCIDA
            level = _cap(level, MatchLevel.MEDIUM)
            reasons.append("presentación de la observación desconocida")
        elif same:
            status = PresentationStatus.MISMA
        elif o_pres.dimension != t_pres.dimension:
            status = PresentationStatus.DIFERENTE
            level = MatchLevel.NO_MATCH
            reasons.append(f"dimensión distinta ({o_pres.dimension} vs {t_pres.dimension})")
        else:
            status = PresentationStatus.DIFERENTE
            level = _cap(level, MatchLevel.MEDIUM)
            reasons.append(
                f"presentación distinta ({o_pres.standard_quantity:g} {o_pres.standard_unit} "
                f"vs {t_pres.standard_quantity:g} {t_pres.standard_unit}); "
                "solo comparable por unidad estándar"
            )

    return MatchResult(level, coverage, status, reasons)
