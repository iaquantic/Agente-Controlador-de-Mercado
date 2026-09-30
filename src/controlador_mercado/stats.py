"""Estadísticas robustas de precio, detección de outliers y concentración."""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass


def _r(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


def quantile(sorted_values: list[float], q: float) -> float:
    """Cuantil con interpolación lineal (método 'inclusive')."""
    if not sorted_values:
        raise ValueError("lista vacía")
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = (len(sorted_values) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


MAD_FLOOR_FRACTION = 0.05


@dataclass
class OutlierFlag:
    index: int
    value: float
    direction: str  # "BAJO" o "ALTO"
    method: str
    score: float

    def as_dict(self) -> dict:
        return {
            "value": _r(self.value, 4),
            "direction": self.direction,
            "method": self.method,
            "score": _r(self.score, 3),
            "posibles_causas": [
                "error de captura", "producto o formato diferente", "anuncio antiguo",
                "promoción", "posible estafa", "dato legítimo",
            ],
        }


def detect_outliers(values: list[float], min_n: int = 3) -> tuple[list[OutlierFlag], str]:
    """Marca valores anómalos sin eliminarlos.

    - n >= 5: z-score modificado con MAD (|z| > 3.5, MAD con suelo del 5 % de la
      mediana); si MAD = 0, vallas IQR 3×.
    - 3 <= n < 5: razón respecto a la mediana (> 3× o < 1/3).
    - n < 3: no se evalúa (se informa como limitación).
    """
    n = len(values)
    if n < min_n:
        return [], "no_evaluado_muestra_insuficiente"
    med = statistics.median(values)
    flags: list[OutlierFlag] = []
    if n >= 5:
        mad = statistics.median([abs(v - med) for v in values])
        if mad > 0:
            # Suelo del 5 % de la mediana: con precios muy agrupados, un MAD diminuto
            # marcaría como anómalas diferencias comercialmente normales.
            mad = max(mad, MAD_FLOOR_FRACTION * abs(med))
            for i, v in enumerate(values):
                z = 0.6745 * (v - med) / mad
                if abs(z) > 3.5:
                    flags.append(OutlierFlag(i, v, "ALTO" if z > 0 else "BAJO", "mad_zscore", z))
            return flags, "mad_zscore"
        s = sorted(values)
        q1, q3 = quantile(s, 0.25), quantile(s, 0.75)
        iqr = q3 - q1
        lo, hi = q1 - 3 * iqr, q3 + 3 * iqr
        for i, v in enumerate(values):
            if v < lo or v > hi or (iqr == 0 and med > 0 and (v > 3 * med or v < med / 3)):
                flags.append(OutlierFlag(i, v, "ALTO" if v > med else "BAJO", "iqr", v / med if med else 0))
        return flags, "iqr"
    if med <= 0:
        return [], "no_evaluado_mediana_no_positiva"
    for i, v in enumerate(values):
        ratio = v / med
        if ratio > 3 or ratio < 1 / 3:
            flags.append(OutlierFlag(i, v, "ALTO" if ratio > 1 else "BAJO", "ratio_mediana", ratio))
    return flags, "ratio_mediana"


def price_summary(values: list[float], digits: int = 2) -> dict:
    """PRICE_MIN/MAX/MEAN/MEDIAN/RANGE/SPREAD_PCT sobre valores ya validados."""
    n = len(values)
    if n == 0:
        return {
            "n": 0, "price_min": None, "price_max": None, "price_mean": None,
            "price_median": None, "price_range": None, "price_spread_pct": None,
            "p25": None, "p75": None, "iqr": None, "coefficient_of_variation": None,
        }
    s = sorted(values)
    mean = statistics.fmean(s)
    med = statistics.median(s)
    rng = s[-1] - s[0]
    out = {
        "n": n,
        "price_min": _r(s[0], digits),
        "price_max": _r(s[-1], digits),
        "price_mean": _r(mean, digits),
        "price_median": _r(med, digits),
        "price_range": _r(rng, digits),
        # Dispersión relativa: (máx − mín) / mediana.
        "price_spread_pct": _r(rng / med * 100, 1) if med > 0 and n >= 2 else None,
        "p25": _r(quantile(s, 0.25), digits) if n >= 4 else None,
        "p75": _r(quantile(s, 0.75), digits) if n >= 4 else None,
        "iqr": _r(quantile(s, 0.75) - quantile(s, 0.25), digits) if n >= 4 else None,
        "coefficient_of_variation": _r(statistics.stdev(s) / mean, 3) if n >= 3 and mean > 0 else None,
    }
    return out


def concentration(sellers: list[str | None]) -> dict:
    """Concentración de ANUNCIOS por vendedor (no de ventas).

    HHI en escala 0–10 000 sobre la cuota de anuncios con vendedor conocido.
    """
    known = [s for s in sellers if s]
    unknown = len(sellers) - len(known)
    if len(known) < 2:
        return {
            "basis": "cuota de anuncios por vendedor observado (proxy; no representa ventas)",
            "hhi": None,
            "top_seller_share_pct": None,
            "level": None,
            "listings_without_seller": unknown,
            "note": "vendedores identificados insuficientes para estimar concentración",
        }
    counts = Counter(known)
    total = len(known)
    shares = [c / total for c in counts.values()]
    hhi = sum((sh * 100) ** 2 for sh in shares)
    level = "ALTA" if hhi >= 2500 else "MODERADA" if hhi >= 1500 else "BAJA"
    return {
        "basis": "cuota de anuncios por vendedor observado (proxy; no representa ventas)",
        "hhi": round(hhi),
        "top_seller_share_pct": round(max(shares) * 100, 1),
        "level": level,
        "listings_without_seller": unknown,
    }
