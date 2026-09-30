"""Detección de patrones de mercado con evidencia explícita.

Un patrón solo se etiqueta si la evidencia cumple los umbrales configurados;
cada señal incluye la evidencia numérica que la respalda.
"""

from __future__ import annotations

import statistics
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .analyzer import AnalyzerConfig


def _signal(kind: str, description: str, evidence: dict, strength: str, evidence_type: str = "INFERENCIA") -> dict:
    return {
        "type": kind,
        "evidence_type": evidence_type,
        "strength": strength,
        "description": description,
        "evidence": evidence,
    }


def detect_signals(
    cfg: "AnalyzerConfig",
    primary_currency: str | None,
    primary_price_stats: dict | None,
    supply: dict,
    trends: dict,
) -> list[dict]:
    signals: list[dict] = []

    # --- Dispersión de precios
    if primary_price_stats and primary_price_stats.get("n", 0) >= cfg.min_obs_for_dispersion:
        spread = primary_price_stats.get("price_spread_pct")
        cv = primary_price_stats.get("coefficient_of_variation")
        if (spread is not None and spread >= cfg.high_dispersion_spread_pct) or (
            cv is not None and cv >= cfg.high_dispersion_cv
        ):
            signals.append(_signal(
                "HIGH_PRICE_DISPERSION",
                f"Diferencias importantes de precio entre anuncios comparables en {primary_currency}: "
                f"dispersión {spread}% (máx−mín sobre mediana), CV {cv}.",
                {"currency": primary_currency, "price_spread_pct": spread, "coefficient_of_variation": cv,
                 "n": primary_price_stats["n"], "price_min": primary_price_stats["price_min"],
                 "price_max": primary_price_stats["price_max"]},
                "OBSERVADA",
                "ESTADISTICA_CALCULADA",
            ))

    # --- Competencia (vendedores observables)
    sellers = supply.get("seller_count") or 0
    unknown = supply.get("listings_without_seller", 0)
    if 1 <= sellers <= cfg.low_competition_max_sellers and unknown == 0:
        signals.append(_signal(
            "LOW_COMPETITION",
            f"Pocos vendedores observables: {sellers} vendedor(es) con anuncios comparables en la muestra.",
            {"seller_count": sellers, "listing_count": supply.get("listing_count")},
            "OBSERVADA",
            "HECHO_OBSERVADO",
        ))
    if sellers >= cfg.high_competition_min_sellers:
        signals.append(_signal(
            "HIGH_COMPETITION",
            f"Muchos vendedores comparables: {sellers} vendedores distintos observados.",
            {"seller_count": sellers, "listing_count": supply.get("listing_count"),
             "concentration": supply.get("market_concentration", {}).get("level")},
            "OBSERVADA",
            "HECHO_OBSERVADO",
        ))

    # --- Precio
    price_trend = (trends.get("price_trend") or {}).get(primary_currency or "", {})
    if price_trend.get("classification") == "TENDENCIA" and price_trend.get("direction") in ("ALZA", "BAJA"):
        kind = "PRICE_INCREASE" if price_trend["direction"] == "ALZA" else "PRICE_DECREASE"
        signals.append(_signal(
            kind,
            f"Mediana de precio en {primary_currency} con variación consistente de "
            f"{price_trend['change_pct_total']}% en {price_trend['periods_with_data']} periodos.",
            {"currency": primary_currency, **price_trend},
            "TENDENCIA",
        ))

    # --- Oferta
    listing_trend = trends.get("listing_trend") or {}
    cls = listing_trend.get("classification")
    change = listing_trend.get("change_pct_total")
    periods = listing_trend.get("periods_with_data", 0)
    supply_down = supply_up = False
    if cls == "TENDENCIA" and change is not None and abs(change) >= cfg.supply_change_threshold_pct:
        supply_up, supply_down = change > 0, change < 0
    elif cls == "DESAPARICION" and periods >= cfg.min_trend_periods:
        supply_down = True
    if supply_up or supply_down:
        signals.append(_signal(
            "SUPPLY_INCREASE" if supply_up else "SUPPLY_DECREASE",
            f"Número de anuncios comparables {'en aumento' if supply_up else 'en descenso'} "
            f"({change}% en {periods} periodos, fuentes comunes a todos los periodos).",
            listing_trend,
            cls,
        ))

    price_dir = price_trend.get("direction")
    price_cls = price_trend.get("classification")
    if supply_down and (
        cls == "DESAPARICION" or (change is not None and change <= -cfg.shortage_supply_drop_pct)
    ):
        support = price_dir == "ALZA" and price_cls in ("TENDENCIA", "MOVIMIENTO_RECIENTE")
        signals.append(_signal(
            "POSSIBLE_SHORTAGE",
            "Reducción considerable de la oferta observable"
            + (" acompañada de presión alcista de precios." if support else "; sin presión alcista de precios confirmada."),
            {"listing_trend": listing_trend, "price_trend": price_trend or None,
             "price_pressure_confirmed": support},
            "FUERTE" if support else "MODERADA",
        ))
    if supply_up and change is not None and change >= cfg.saturation_supply_rise_pct and price_dir != "ALZA":
        support = price_dir in ("BAJA", "ESTABLE") and price_cls in ("TENDENCIA", "ESTABLE", "MOVIMIENTO_RECIENTE")
        signals.append(_signal(
            "POSSIBLE_SATURATION",
            "Incremento importante de la oferta observable"
            + (" con precios estables o a la baja." if support else "; evolución de precios no confirmada."),
            {"listing_trend": listing_trend, "price_trend": price_trend or None,
             "price_pressure_confirmed": support},
            "FUERTE" if support else "MODERADA",
        ))

    # --- Producto emergente
    if periods >= cfg.min_trend_periods and (
        cls == "APARICION"
        or (cls == "TENDENCIA" and change is not None and change >= cfg.emerging_growth_pct)
    ):
        signals.append(_signal(
            "EMERGING_PRODUCT",
            "La presencia del producto en anuncios crece significativamente en el periodo analizado "
            "(proxy: número de anuncios, no ventas).",
            listing_trend,
            cls,
        ))

    # --- Evento inusual respecto al histórico
    series = [p for p in (trends.get("price_series") or {}).get(primary_currency or "", []) if p is not None]
    if len(series) >= cfg.min_periods_unusual_event + 1:
        history, last = series[:-1], series[-1]
        med = statistics.median(history)
        mad = statistics.median([abs(v - med) for v in history])
        if mad > 0:
            z = 0.6745 * (last - med) / mad
            if abs(z) > 3.5:
                signals.append(_signal(
                    "UNUSUAL_MARKET_EVENT",
                    f"La mediana del último periodo ({last}) se aleja significativamente del histórico "
                    f"(mediana histórica {round(med, 2)}, z robusto {round(z, 2)}).",
                    {"currency": primary_currency, "last_period_median": last,
                     "historical_median": round(med, 2), "robust_z": round(z, 2),
                     "historical_periods": len(history)},
                    "OBSERVADA",
                ))
    return signals
