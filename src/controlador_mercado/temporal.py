"""Análisis temporal: agregación por periodos comparables y clasificación
CAMBIO_PUNTUAL / MOVIMIENTO_RECIENTE / TENDENCIA."""

from __future__ import annotations

from datetime import datetime, timedelta


def period_start(dt: datetime, granularity: str) -> datetime:
    day = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if granularity == "day":
        return day
    if granularity == "week":
        return day - timedelta(days=day.weekday())
    if granularity == "month":
        return day.replace(day=1)
    raise ValueError(f"granularidad no soportada: {granularity}")


def period_end(start: datetime, granularity: str) -> datetime:
    if granularity == "day":
        return start + timedelta(days=1)
    if granularity == "week":
        return start + timedelta(days=7)
    nxt = start.replace(day=28) + timedelta(days=4)
    return nxt.replace(day=1)


def _pct(new: float, old: float) -> float | None:
    if old == 0:
        return None
    return (new - old) / old * 100


def classify_series(values: list[float | None], threshold_pct: float, min_trend_periods: int = 3) -> dict:
    """Clasifica una serie cronológica de valores por periodo.

    - INSUFICIENTE: menos de 2 periodos con dato.
    - ESTABLE: variaciones por debajo del umbral.
    - CAMBIO_PUNTUAL: solo 2 periodos comparables (no basta para tendencia).
    - TENDENCIA: >= ``min_trend_periods`` periodos, variación total >= umbral,
      al menos 2/3 de los cambios consecutivos en la misma dirección y
      movimiento ya visible antes del último periodo (>= umbral/2).
    - MOVIMIENTO_RECIENTE: el último cambio supera el umbral sin tendencia consistente.
    - SIN_PATRON_CLARO: variaciones relevantes pero inconsistentes.
    """
    points = [v for v in values if v is not None]
    base = {
        "classification": "INSUFICIENTE",
        "direction": None,
        "change_pct_total": None,
        "change_pct_last": None,
        "periods_with_data": len(points),
        "consistency": None,
        "threshold_pct": threshold_pct,
    }
    if len(points) < 2:
        return base

    first, prev, last = points[0], points[-2], points[-1]
    if first == 0 and last > 0:
        return {**base, "classification": "APARICION", "direction": "ALZA"}
    if last == 0 and prev > 0:
        total = _pct(last, first)
        return {
            **base, "classification": "DESAPARICION", "direction": "BAJA",
            "change_pct_total": round(total, 1) if total is not None else None,
            "change_pct_last": -100.0,
        }

    total = _pct(last, first)
    last_change = _pct(last, prev)
    base["change_pct_total"] = round(total, 1) if total is not None else None
    base["change_pct_last"] = round(last_change, 1) if last_change is not None else None
    if total is None or last_change is None:
        return base

    def direction(change: float) -> str:
        if abs(change) < threshold_pct:
            return "ESTABLE"
        return "ALZA" if change > 0 else "BAJA"

    if len(points) == 2:
        d = direction(total)
        base["direction"] = d
        base["classification"] = "ESTABLE" if d == "ESTABLE" else "CAMBIO_PUNTUAL"
        return base

    diffs = [b - a for a, b in zip(points, points[1:])]
    sign_total = 1 if total > 0 else -1 if total < 0 else 0
    consistent = sum(1 for d in diffs if (d > 0 and sign_total > 0) or (d < 0 and sign_total < 0))
    consistency = consistent / len(diffs)
    base["consistency"] = round(consistency, 2)

    step_changes = [_pct(b, a) for a, b in zip(points, points[1:]) if a != 0]
    # Movimiento sostenido: antes del último periodo ya debe verse al menos la
    # mitad del umbral en la misma dirección (un salto final aislado no es tendencia).
    before_last = _pct(prev, first)
    sustained = before_last is not None and before_last * sign_total >= threshold_pct / 2
    if (abs(total) >= threshold_pct and consistency >= 2 / 3 and sustained
            and len(points) >= min_trend_periods):
        base["classification"] = "TENDENCIA"
        base["direction"] = direction(total)
    elif abs(last_change) >= threshold_pct:
        base["classification"] = "MOVIMIENTO_RECIENTE"
        base["direction"] = direction(last_change)
    elif abs(total) < threshold_pct and all(abs(c) < threshold_pct for c in step_changes if c is not None):
        base["classification"] = "ESTABLE"
        base["direction"] = "ESTABLE"
    else:
        base["classification"] = "SIN_PATRON_CLARO"
        base["direction"] = direction(total)
    return base
