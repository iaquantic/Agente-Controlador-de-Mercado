"""Motor de análisis determinista del Agente Controlador de Mercado.

Implementa los pasos 2–13 del proceso de análisis y devuelve el contrato
estructurado para el Agente Central. No inventa datos: toda cifra procede de
las observaciones recibidas y cualquier métrica no calculable es ``null``.
"""

from __future__ import annotations

import re
import statistics
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from . import __version__
from .dedup import deduplicate
from .matching import MatchResult, PresentationStatus, match_observation, target_presentation
from .models import (
    Confidence,
    EvidenceType,
    ExchangeRate,
    MatchLevel,
    Observation,
    ReferenceCost,
    TargetProduct,
)
from .normalization import (
    Presentation,
    normalize_availability,
    normalize_currency,
    normalize_location,
    normalize_seller,
    observation_presentation,
)
from .signals import detect_signals
from .stats import concentration, detect_outliers, price_summary
from .temporal import classify_series, period_end, period_start

_INJECTION_RE = re.compile(
    r"ignor[ae]\w*\s+(?:\w+\s+){0,3}(?:instrucciones|instructions)|system\s*prompt|"
    r"(?:ejecuta|execute|run)\s+(?:esta|this|la|the)\s+(?:herramienta|tool)|"
    r"(?:olvida|forget)\s+(?:\w+\s+){0,3}(?:instrucciones|instructions|reglas|rules)|"
    r"eres\s+ahora|you\s+are\s+now|modifica\s+el\s+sistema",
    re.IGNORECASE,
)


@dataclass
class AnalyzerConfig:
    granularity: str = "week"
    include_medium_matches: bool = True
    min_obs_per_period: int = 3
    min_trend_periods: int = 3
    min_obs_for_dispersion: int = 4
    min_periods_unusual_event: int = 5
    price_change_threshold_pct: float = 10.0
    supply_change_threshold_pct: float = 20.0
    shortage_supply_drop_pct: float = 30.0
    saturation_supply_rise_pct: float = 30.0
    emerging_growth_pct: float = 100.0
    high_dispersion_spread_pct: float = 50.0
    high_dispersion_cv: float = 0.25
    low_competition_max_sellers: int = 2
    high_competition_min_sellers: int = 10
    availability_low_max: int = 3
    availability_medium_max: int = 10
    geo_difference_pct: float = 25.0
    source_difference_pct: float = 30.0
    fresh_days: int = 7
    stale_days: int = 30
    listing_max_age_days: int = 60


@dataclass
class _Record:
    obs: Observation
    match: MatchResult
    presentation: Presentation
    currency: str | None
    seller: str | None
    province: str | None
    availability: str | None
    status: str = "pendiente"
    exclusion_reasons: list[str] = field(default_factory=list)
    duplicate_of: str | None = None
    duplicate_reasons: list[str] = field(default_factory=list)
    outlier: dict | None = None
    unit_outlier: dict | None = None
    in_window: bool = False
    in_price_stats: bool = False
    in_unit_price_stats: bool = False
    in_supply: bool = False

    @property
    def price_valid(self) -> bool:
        return self.obs.price is not None and self.obs.price > 0

    @property
    def unit_price(self) -> float | None:
        if not self.price_valid or not self.presentation.known:
            return None
        return self.obs.price / self.presentation.standard_quantity


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _r(v: float | None, d: int = 2) -> float | None:
    return None if v is None else round(v, d)


class MarketAnalyzer:
    def __init__(self, config: AnalyzerConfig | None = None) -> None:
        self.cfg = config or AnalyzerConfig()

    # ------------------------------------------------------------------ API
    def analyze(
        self,
        target: TargetProduct,
        observations: list[Observation],
        *,
        analysis_start: datetime | None = None,
        analysis_end: datetime | None = None,
        now: datetime | None = None,
        exchange_rates: list[ExchangeRate] | None = None,
        convert_to: str | None = None,
        reference_cost: ReferenceCost | None = None,
        source_status: list[dict] | None = None,
    ) -> dict[str, Any]:
        cfg = self.cfg
        now = now or datetime.now(timezone.utc)
        end = analysis_end or now
        start = analysis_start or (end - (period_end(period_start(end, cfg.granularity), cfg.granularity)
                                          - period_start(end, cfg.granularity)))
        issues: list[dict] = []
        limitations: list[str] = []

        def issue(code: str, severity: str, detail: str, count: int | None = None, ids: list[str] | None = None):
            item = {"code": code, "severity": severity, "detail": detail}
            if count is not None:
                item["count"] = count
            if ids:
                item["observation_ids"] = ids[:25]
            issues.append(item)

        t_pres = target_presentation(target)
        provinces_filter = {normalize_location(p) for p in target.provinces if p}

        # ---- 1-2) Registro y evaluación individual de cada observación.
        records = [self._record(target, o) for o in observations]
        no_ts = [r for r in records if r.obs.captured_at is None]
        if no_ts:
            issue("SIN_TIMESTAMP_CAPTURA", "ALTA",
                  "Observaciones sin captured_at: no pueden situarse en el tiempo y se excluyen.",
                  len(no_ts), [r.obs.observation_id for r in no_ts])
            for r in no_ts:
                r.status = "excluida"
                r.exclusion_reasons.append("sin timestamp de captura")
        future = [r for r in records if r.obs.captured_at and r.obs.captured_at > now + timedelta(minutes=5)]
        if future:
            issue("TIMESTAMP_FUTURO", "ALTA", "captured_at posterior al momento del análisis (posible error de scraping).",
                  len(future), [r.obs.observation_id for r in future])
            for r in future:
                r.status = "excluida"
                r.exclusion_reasons.append("timestamp de captura en el futuro")

        injected = [r for r in records if _INJECTION_RE.search(
            " ".join(filter(None, [r.obs.title, r.obs.description, r.obs.seller])))]
        if injected:
            issue("POSIBLE_INYECCION_DE_INSTRUCCIONES", "MEDIA",
                  "Texto scrapeado con apariencia de instrucciones. Se trata exclusivamente como dato y no se ejecuta.",
                  len(injected), [r.obs.observation_id for r in injected])

        usable = [r for r in records if r.status != "excluida"]
        window = [r for r in usable if start <= r.obs.captured_at <= end]
        for r in window:
            r.in_window = True
        for r in usable:
            if not r.in_window:
                r.status = "historico" if r.obs.captured_at < start else "fuera_de_periodo"

        # ---- 3) Duplicados (dentro de la ventana de análisis).
        by_id = {r.obs.observation_id: r for r in window}
        dd = deduplicate([r.obs for r in window])
        for dup_id, info in dd.duplicates.items():
            rec = by_id[dup_id]
            rec.status = "duplicado"
            rec.duplicate_of = info.canonical_id
            rec.duplicate_reasons = info.reasons
        unique = [by_id[o.observation_id] for o in dd.unique]
        if dd.duplicates:
            issue("DUPLICADOS", "INFO", "Duplicados detectados con señales fuertes; se cuentan una sola vez.",
                  len(dd.duplicates), list(dd.duplicates))
        if dd.possible_duplicates:
            issue("POSIBLES_DUPLICADOS", "BAJA",
                  "Coincidencias débiles (mismo texto y precio, vendedor distinto o desconocido). No se eliminan.",
                  len(dd.possible_duplicates),
                  [i for g in dd.possible_duplicates for i in g["observation_ids"]])

        # ---- 4-6) Normalización, monedas, equivalencia, ámbito geográfico.
        min_level = MatchLevel.MEDIUM if cfg.include_medium_matches else MatchLevel.HIGH
        counters: Counter = Counter()
        for r in unique:
            reasons = r.exclusion_reasons
            if r.match.level.rank < MatchLevel.MEDIUM.rank:
                reasons.append(f"coincidencia {r.match.level.value}")
                counters["match_" + r.match.level.value] += 1
            if provinces_filter and r.province not in provinces_filter:
                reasons.append("fuera del ámbito geográfico solicitado" if r.province else "provincia desconocida")
                counters["fuera_geo"] += 1
            if reasons:
                r.status = "excluida"
                continue
            r.in_supply = True
            price_ok = True
            if not r.price_valid:
                reasons.append("precio ausente o inválido")
                counters["precio_invalido"] += 1
                price_ok = False
            if r.currency is None:
                reasons.append("moneda desconocida o ambigua")
                counters["moneda_desconocida"] += 1
                price_ok = False
            if price_ok and r.match.level.rank >= min_level.rank:
                if r.match.presentation_status in (PresentationStatus.MISMA, PresentationStatus.NO_ESPECIFICADA):
                    r.in_price_stats = True
                if r.presentation.known and (not t_pres.known or r.presentation.dimension == t_pres.dimension):
                    r.in_unit_price_stats = True
            r.status = "valida" if (r.in_price_stats or r.in_unit_price_stats) else "solo_oferta"

        if counters["precio_invalido"]:
            issue("PRECIO_INVALIDO", "MEDIA", "Anuncios coincidentes sin precio válido (cuentan como oferta, no como precio).",
                  counters["precio_invalido"])
        if counters["moneda_desconocida"]:
            issue("MONEDA_DESCONOCIDA", "MEDIA",
                  "Moneda ausente o ambigua (p. ej. '$'); no se usan en estadísticas de precio.",
                  counters["moneda_desconocida"])
        ambiguous = [r for r in unique if r.match.level == MatchLevel.MEDIUM]
        if ambiguous:
            issue("PRODUCTO_AMBIGUO", "MEDIA", "Coincidencias MEDIUM: posible equivalencia con ambigüedad; tratadas con precaución.",
                  len(ambiguous), [r.obs.observation_id for r in ambiguous])
        unknown_pres = [r for r in unique if r.in_supply and not r.presentation.known]
        if unknown_pres:
            issue("UNIDAD_DESCONOCIDA", "MEDIA", "Presentación/unidad no identificable; excluidas del precio por unidad estándar.",
                  len(unknown_pres), [r.obs.observation_id for r in unknown_pres])
        if not t_pres.known:
            limitations.append("El producto objetivo no especifica presentación: los precios por anuncio pueden "
                               "mezclar formatos; se prioriza el precio por unidad estándar.")

        # ---- 7) Outliers por moneda (marcados, no eliminados).
        price_groups: dict[str, list[_Record]] = defaultdict(list)
        unit_groups: dict[str, list[_Record]] = defaultdict(list)
        for r in unique:
            if r.in_price_stats:
                price_groups[r.currency].append(r)
            if r.in_unit_price_stats:
                unit_groups[(r.currency, r.presentation.dimension)].append(r)
        outlier_ids: set[str] = set()
        outlier_methods: dict[str, str] = {}
        for cur, group in price_groups.items():
            flags, method = detect_outliers([g.obs.price for g in group])
            outlier_methods[f"{cur}:presentacion"] = method
            for f in flags:
                group[f.index].outlier = f.as_dict()
                outlier_ids.add(group[f.index].obs.observation_id)
        for (cur, dim), group in unit_groups.items():
            flags, method = detect_outliers([g.unit_price for g in group])
            outlier_methods[f"{cur}:unidad_estandar:{dim}"] = method
            for f in flags:
                group[f.index].unit_outlier = f.as_dict()
                outlier_ids.add(group[f.index].obs.observation_id)
        if outlier_ids:
            issue("POSIBLES_OUTLIERS", "MEDIA",
                  "Precios anómalos marcados y excluidos de las estadísticas principales (se conservan en la traza).",
                  len(outlier_ids), sorted(outlier_ids))

        # ---- 8) Estadísticas por moneda (sin mezclar monedas).
        price_statistics: dict[str, Any] = {"by_currency": {}, "outlier_methods": outlier_methods}
        currencies = sorted(set(price_groups) | {c for c, _ in unit_groups})
        for cur in currencies:
            group = price_groups.get(cur, [])
            clean = [g for g in group if g.outlier is None]
            entry: dict[str, Any] = {
                "evidence_type": EvidenceType.ESTADISTICA_CALCULADA.value,
                "presentation_price": {
                    "basis": ("precio por anuncio, misma presentación que el objetivo" if t_pres.known
                              else "precio por anuncio (presentación del objetivo no especificada)"),
                    "presentation": t_pres.as_dict() if t_pres.known else None,
                    **price_summary([g.obs.price for g in clean]),
                    "outliers_excluded": len(group) - len(clean),
                    "match_levels": dict(Counter(g.match.level.value for g in clean)),
                },
                "unit_price": {},
                "by_province": self._breakdown(clean, key=lambda g: g.province or "desconocida"),
                "by_source": self._breakdown(clean, key=lambda g: g.obs.source_id),
            }
            for (c, dim), ugroup in unit_groups.items():
                if c != cur:
                    continue
                uclean = [g for g in ugroup if g.unit_outlier is None]
                std_unit = ugroup[0].presentation.standard_unit
                entry["unit_price"][dim] = {
                    "basis": f"{cur} por {std_unit} (comparable entre presentaciones)",
                    "standard_unit": std_unit,
                    **price_summary([g.unit_price for g in uclean], digits=4),
                    "outliers_excluded": len(ugroup) - len(uclean),
                    "presentations_included": sorted({
                        f"{g.presentation.standard_quantity:g} {std_unit}" for g in uclean}),
                }
            price_statistics["by_currency"][cur] = entry
        if len(currencies) > 1:
            limitations.append(f"Precios observados en varias monedas ({', '.join(currencies)}); se presentan como "
                               "mercados monetarios separados.")

        primary_currency = max(currencies, key=lambda c: len(price_groups.get(c, [])), default=None)
        primary_stats = (price_statistics["by_currency"][primary_currency]["presentation_price"]
                         if primary_currency else None)
        if primary_stats is not None and primary_stats["n"] == 0 and t_pres.known and t_pres.dimension:
            unit_entry = price_statistics["by_currency"][primary_currency]["unit_price"].get(t_pres.dimension)
            if unit_entry and unit_entry["n"]:
                primary_stats = unit_entry
        price_statistics["primary_currency"] = primary_currency

        # Conversión solo con tipos de cambio autorizados y fechados.
        price_statistics["converted"] = self._convert(price_statistics, currencies, convert_to, exchange_rates, limitations)

        # Diferencias geográficas y entre plataformas.
        geo_gap = self._max_gap(price_statistics, primary_currency, "by_province", exclude={"desconocida"})
        if geo_gap and geo_gap["gap_pct"] >= cfg.geo_difference_pct:
            issue("DIFERENCIAS_GEOGRAFICAS", "MEDIA",
                  f"Las medianas por provincia difieren un {geo_gap['gap_pct']}% en {primary_currency}; "
                  "no se deben tratar como un único mercado.", None)
        source_gap = self._max_gap(price_statistics, primary_currency, "by_source")
        if source_gap and source_gap["gap_pct"] >= cfg.source_difference_pct:
            issue("INCONSISTENCIA_ENTRE_FUENTES", "MEDIA",
                  f"Las medianas por fuente difieren un {source_gap['gap_pct']}% en {primary_currency}.", None)

        # ---- Oferta.
        supply_recs = [r for r in unique if r.in_supply]
        supply = self._supply(supply_recs, t_pres)

        # ---- 9) Histórico / tendencias.
        trends = self._trends(target, usable, end, issues, limitations)

        # ---- 10) Fuentes.
        sources = self._sources(records, unique, source_status)
        failed = [s for s in sources if s["status"] == "error"]
        if failed:
            issue("FUENTE_FALLIDA", "ALTA", "Una o más fuentes fallaron durante la captura; la cobertura es menor.",
                  len(failed))
        if len({r.obs.source_id for r in supply_recs}) == 1:
            issue("FUENTES_INSUFICIENTES", "MEDIA", "Todas las observaciones válidas proceden de una única fuente.", None)

        # ---- Actualidad de los datos.
        freshness = self._freshness(window, now, issues)

        # ---- 11) Señales.
        signals = detect_signals(cfg, primary_currency, primary_stats, supply, trends)

        # ---- 12-13) Calidad y confianza.
        confidence, factors = self._confidence(
            primary_stats, unique, supply_recs, outlier_ids, freshness, failed, issues)
        if primary_stats is None or primary_stats.get("n", 0) == 0:
            limitations.append("No hay observaciones de precio válidas para el producto en el periodo analizado.")
        limitations.append("La muestra representa únicamente el mercado observable en las fuentes consultadas, "
                           "no la totalidad del mercado cubano.")
        limitations.append("No se dispone de datos de ventas reales: oferta y número de anuncios son proxies, no ventas.")

        external = self._external_indicators(primary_currency, primary_stats, supply, trends, reference_cost, limitations)

        summary = self._summary(start, end, supply, primary_currency, primary_stats, trends, signals, currencies)

        return {
            "analysis_id": f"an_{uuid.uuid4().hex[:12]}",
            "engine_version": __version__,
            "product": {
                "name": target.name,
                "brand": target.brand,
                "model": target.model,
                "variant": target.variant,
                "condition": target.condition,
                "presentation": t_pres.as_dict(),
                "keywords": target.keywords,
                "exclude_keywords": target.exclude_keywords,
                "geographic_scope": sorted(provinces_filter) or "sin filtro (todas las provincias observadas)",
            },
            "analysis_period": {
                "analysis_start": _iso(start),
                "analysis_end": _iso(end),
                "analysis_generated_at": _iso(now),
                "granularity": cfg.granularity,
                "observation_count": len(window),
                "observation_count_total_received": len(observations),
                "unique_observation_count": len(unique),
                "valid_price_observation_count": sum(1 for r in unique if r.in_price_stats or r.in_unit_price_stats),
                "captured_at_min": _iso(min((r.obs.captured_at for r in window), default=None)),
                "captured_at_max": _iso(max((r.obs.captured_at for r in window), default=None)),
            },
            "market_summary": summary,
            "price_statistics": price_statistics,
            "supply_statistics": supply,
            "trends": trends,
            "external_indicators": external,
            "market_signals": signals,
            "sources": sources,
            "data_quality": {
                "issues": issues,
                "freshness": freshness,
                "duplicates_removed": len(dd.duplicates),
                "outliers_flagged": len(outlier_ids),
                "excluded_by_match": {k[6:]: v for k, v in counters.items() if k.startswith("match_")},
                "excluded_out_of_scope": counters["fuera_geo"],
            },
            "confidence": confidence.value,
            "confidence_factors": factors,
            "limitations": limitations,
            "observations": [self._trace(r) for r in records],
        }

    # -------------------------------------------------------------- helpers
    def _record(self, target: TargetProduct, obs: Observation) -> _Record:
        return _Record(
            obs=obs,
            match=match_observation(target, obs),
            presentation=observation_presentation(obs),
            currency=normalize_currency(obs.currency),
            seller=normalize_seller(obs.seller),
            province=normalize_location(obs.province),
            availability=normalize_availability(obs.availability),
        )

    @staticmethod
    def _breakdown(group: list[_Record], key) -> dict:
        buckets: dict[str, list[float]] = defaultdict(list)
        for g in group:
            buckets[key(g)].append(g.obs.price)
        return {
            k: {"n": len(v), "price_median": _r(statistics.median(v)), "price_min": _r(min(v)), "price_max": _r(max(v))}
            for k, v in sorted(buckets.items())
        }

    @staticmethod
    def _max_gap(price_statistics: dict, currency: str | None, field_name: str, exclude: set | None = None,
                 min_n: int = 3) -> dict | None:
        if not currency:
            return None
        data = price_statistics["by_currency"][currency][field_name]
        meds = {k: v["price_median"] for k, v in data.items() if v["n"] >= min_n and k not in (exclude or set())}
        if len(meds) < 2:
            return None
        lo_k, hi_k = min(meds, key=meds.get), max(meds, key=meds.get)
        if meds[lo_k] <= 0:
            return None
        return {"low": lo_k, "high": hi_k, "gap_pct": round((meds[hi_k] - meds[lo_k]) / meds[lo_k] * 100, 1)}

    def _convert(self, price_statistics, currencies, convert_to, rates, limitations) -> dict | None:
        if not convert_to:
            return None
        convert_to = convert_to.upper()
        out: dict[str, Any] = {
            "evidence_type": EvidenceType.ESTIMACION.value,
            "target_currency": convert_to,
            "note": "Conversión con tipo de cambio autorizado; los precios originales se conservan en by_currency.",
            "by_currency": {},
        }
        for cur in currencies:
            if cur == convert_to:
                continue
            rate = self._pick_rate(rates or [], cur, convert_to)
            if rate is None:
                out["by_currency"][cur] = {"status": "DATO_NO_DISPONIBLE",
                                           "reason": f"sin tipo de cambio autorizado {cur}->{convert_to}"}
                limitations.append(f"No hay tipo de cambio autorizado {cur}->{convert_to}; no se convierte.")
                continue
            factor, rate_obj = rate
            stats = price_statistics["by_currency"][cur]
            out["by_currency"][cur] = {
                "rate": factor,
                "rate_as_of": _iso(rate_obj.as_of),
                "rate_source": rate_obj.source,
                **self._scaled(stats["presentation_price"], factor),
                "unit_price": {dim: {"standard_unit": u.get("standard_unit"), **self._scaled(u, factor)}
                               for dim, u in (stats.get("unit_price") or {}).items()},
            }
        return out

    @staticmethod
    def _scaled(block: dict, factor: float) -> dict:
        out = {"n": block.get("n")}
        for key in ("price_median", "price_min", "price_max"):
            out[key] = _r(block[key] * factor) if block.get(key) is not None else None
        return out

    @staticmethod
    def _pick_rate(rates: list[ExchangeRate], frm: str, to: str) -> tuple[float, ExchangeRate] | None:
        direct = [r for r in rates if r.from_currency == frm and r.to_currency == to]
        if direct:
            best = max(direct, key=lambda r: r.as_of)
            return best.rate, best
        inverse = [r for r in rates if r.from_currency == to and r.to_currency == frm]
        if inverse:
            best = max(inverse, key=lambda r: r.as_of)
            return 1 / best.rate, best
        return None

    def _supply(self, recs: list[_Record], t_pres: Presentation) -> dict:
        cfg = self.cfg
        sellers = [r.seller for r in recs]
        known = {s for s in sellers if s}
        available = [r for r in recs if r.availability != "AGOTADO"]
        sold_out = len(recs) - len(available)
        n_avail = len(available)
        if n_avail == 0:
            level = "SIN_OFERTA_OBSERVADA"
        elif n_avail <= cfg.availability_low_max:
            level = "BAJA"
        elif n_avail <= cfg.availability_medium_max:
            level = "MEDIA"
        else:
            level = "ALTA"
        by_province = Counter(r.province or "desconocida" for r in recs)
        by_source = Counter(r.obs.source_id for r in recs)
        return {
            "evidence_type": EvidenceType.HECHO_OBSERVADO.value,
            "listing_count": len(recs),
            "listing_count_same_presentation": sum(
                1 for r in recs if r.match.presentation_status in (PresentationStatus.MISMA, PresentationStatus.NO_ESPECIFICADA)),
            "available_listing_count": n_avail,
            "sold_out_listing_count": sold_out,
            "seller_count": len(known),
            "seller_count_note": ("aproximado: vendedores distintos identificados por nombre normalizado"
                                  + (f"; {sellers.count(None)} anuncio(s) sin vendedor identificado" if None in sellers else "")),
            "listings_without_seller": sellers.count(None),
            "source_count": len(by_source),
            "availability_level": level,
            "availability_basis": (f"proxy por número de anuncios disponibles en la muestra "
                                   f"(BAJA ≤{cfg.availability_low_max}, MEDIA ≤{cfg.availability_medium_max}, ALTA >"
                                   f"{cfg.availability_medium_max}); no mide existencias reales"),
            "market_concentration": concentration(sellers),
            "by_province": dict(sorted(by_province.items())),
            "by_source": dict(sorted(by_source.items())),
        }

    def _trends(self, target: TargetProduct, usable: list[_Record], end: datetime, issues: list, limitations: list) -> dict:
        cfg = self.cfg
        provinces_filter = {normalize_location(p) for p in target.provinces if p}
        t_pres = target_presentation(target)
        buckets: dict[datetime, list[_Record]] = defaultdict(list)
        for r in usable:
            if r.obs.captured_at <= end:
                buckets[period_start(r.obs.captured_at, cfg.granularity)].append(r)
        keys = sorted(buckets)
        periods = []
        price_series: dict[str, list[float | None]] = defaultdict(list)
        per_period_sources: list[set[str]] = []
        per_period_matches: list[list[_Record]] = []
        all_currencies: set[str] = set()
        for k in keys:
            recs = buckets[k]
            by_id = {r.obs.observation_id: r for r in recs}
            uniq = [by_id[o.observation_id] for o in deduplicate([r.obs for r in recs]).unique]
            matched = [
                r for r in uniq
                if r.match.level.rank >= MatchLevel.MEDIUM.rank
                and (not provinces_filter or r.province in provinces_filter)
            ]
            per_period_matches.append(matched)
            per_period_sources.append({r.obs.source_id for r in recs})
            medians: dict[str, float | None] = {}
            cur_groups: dict[str, list[float]] = defaultdict(list)
            for r in matched:
                if (r.price_valid and r.currency
                        and r.match.presentation_status in (PresentationStatus.MISMA, PresentationStatus.NO_ESPECIFICADA)
                        and r.match.level.rank >= (MatchLevel.MEDIUM if cfg.include_medium_matches else MatchLevel.HIGH).rank):
                    cur_groups[r.currency].append(r.obs.price)
            all_currencies |= set(cur_groups)
            for cur, vals in cur_groups.items():
                flags, _ = detect_outliers(vals)
                bad = {f.index for f in flags}
                clean = [v for i, v in enumerate(vals) if i not in bad]
                medians[cur] = _r(statistics.median(clean)) if len(clean) >= cfg.min_obs_per_period else None
            periods.append({
                "period_start": _iso(k),
                "period_end": _iso(period_end(k, cfg.granularity)),
                "listing_count": len(matched),
                "seller_count": len({r.seller for r in matched if r.seller}),
                "sources_captured": sorted(per_period_sources[-1]),
                "price_median": medians,
                "price_n": {c: len(v) for c, v in cur_groups.items()},
                "complete": period_end(k, cfg.granularity) <= end,
            })
        for p in periods:
            for cur in all_currencies:
                price_series[cur].append(p["price_median"].get(cur))

        price_trend = {
            cur: classify_series(vals, cfg.price_change_threshold_pct, cfg.min_trend_periods)
            for cur, vals in price_series.items()
        }

        # Conteos: solo periodos completos y solo fuentes presentes en todos ellos,
        # para no confundir cambios de cobertura de scraping con cambios de oferta.
        complete_idx = [i for i, p in enumerate(periods) if p["complete"]]
        if periods and not periods[-1]["complete"]:
            limitations.append("El último periodo está incompleto; se excluye de las tendencias de anuncios y vendedores.")
        common = set.intersection(*(per_period_sources[i] for i in complete_idx)) if complete_idx else set()
        all_sources = set().union(*per_period_sources) if per_period_sources else set()
        if complete_idx and common != all_sources:
            issues.append({"code": "COBERTURA_VARIABLE_ENTRE_PERIODOS", "severity": "MEDIA",
                           "detail": "Las fuentes capturadas cambian entre periodos; las tendencias de oferta usan "
                                     f"solo fuentes comunes: {sorted(common) or 'ninguna'}."})
        listing_counts = [sum(1 for r in per_period_matches[i] if r.obs.source_id in common) for i in complete_idx]
        seller_counts = [len({r.seller for r in per_period_matches[i] if r.obs.source_id in common and r.seller})
                         for i in complete_idx]
        listing_trend = classify_series(listing_counts, cfg.supply_change_threshold_pct, cfg.min_trend_periods) \
            if common else classify_series([], cfg.supply_change_threshold_pct)
        seller_trend = classify_series(seller_counts, cfg.supply_change_threshold_pct, cfg.min_trend_periods) \
            if common else classify_series([], cfg.supply_change_threshold_pct)
        listing_trend["sources_used"] = sorted(common)
        seller_trend["sources_used"] = sorted(common)

        if len(keys) < cfg.min_trend_periods:
            limitations.append(f"Histórico insuficiente para tendencias ({len(keys)} periodo(s); se requieren "
                               f"{cfg.min_trend_periods}).")
        return {
            "evidence_type": EvidenceType.ESTADISTICA_CALCULADA.value,
            "granularity": cfg.granularity,
            "periods": periods,
            "price_basis": ("mediana de precio por anuncio, misma presentación" if t_pres.known
                            else "mediana de precio por anuncio (presentación no especificada)"),
            "price_series": dict(price_series),
            "price_trend": price_trend,
            "listing_trend": listing_trend,
            "seller_trend": seller_trend,
            "definitions": {
                "CAMBIO_PUNTUAL": "solo dos periodos comparables",
                "MOVIMIENTO_RECIENTE": "el último cambio supera el umbral sin tendencia consistente",
                "TENDENCIA": f">= {cfg.min_trend_periods} periodos, variación total >= umbral, >= 2/3 de cambios en la "
                             "misma dirección y movimiento sostenido antes del último periodo",
            },
        }

    @staticmethod
    def _sources(records: list[_Record], unique: list[_Record], source_status: list[dict] | None) -> list[dict]:
        info: dict[str, dict] = {}
        for s in source_status or []:
            # Un adaptador (p. ej. un directorio) puede devolver registros de varias fuentes:
            # su estado se atribuye a esas fuentes en lugar de crear una fuente ficticia.
            returned = [sid for sid in s.get("source_ids_returned") or [] if sid != s["source_id"]]
            for sid in returned or [s["source_id"]]:
                info[sid] = {
                    "source_id": sid,
                    "source_name": s.get("source_name") if sid == s["source_id"] else None,
                    "status": s.get("status", "ok"),
                    "error": s.get("error"),
                    "fetched_at": s.get("fetched_at"),
                    "adapter": s["source_id"],
                }
        for r in records:
            entry = info.setdefault(r.obs.source_id, {
                "source_id": r.obs.source_id, "source_name": r.obs.source_name,
                "status": "ok", "error": None, "fetched_at": None,
            })
            entry["source_name"] = entry.get("source_name") or r.obs.source_name
            entry["observations_received"] = entry.get("observations_received", 0) + 1
            if r.obs.captured_at:
                lo, hi = entry.get("captured_at_min"), entry.get("captured_at_max")
                ts = r.obs.captured_at.isoformat()
                entry["captured_at_min"] = min(lo, ts) if lo else ts
                entry["captured_at_max"] = max(hi, ts) if hi else ts
        used = Counter(r.obs.source_id for r in unique if r.in_price_stats or r.in_unit_price_stats)
        for sid, entry in info.items():
            entry.setdefault("observations_received", 0)
            entry["observations_used_in_price_stats"] = used.get(sid, 0)
        return sorted(info.values(), key=lambda e: e["source_id"])

    def _freshness(self, window: list[_Record], now: datetime, issues: list) -> dict:
        cfg = self.cfg
        if not window:
            return {"status": "SIN_DATOS", "latest_capture_age_days": None}
        latest = max(r.obs.captured_at for r in window)
        age = (now - latest).total_seconds() / 86400
        status = "ACTUAL" if age <= cfg.fresh_days else "ANTIGUA" if age > cfg.stale_days else "ENVEJECIDA"
        if status != "ACTUAL":
            issues.append({"code": "DATOS_ANTIGUOS", "severity": "ALTA" if status == "ANTIGUA" else "MEDIA",
                           "detail": f"La captura más reciente tiene {age:.1f} días; puede no representar el mercado actual."})
        old = [r for r in window if r.obs.published_at and (now - r.obs.published_at).days > cfg.listing_max_age_days]
        if old:
            issues.append({"code": "ANUNCIOS_ANTIGUOS", "severity": "BAJA",
                           "detail": f"Anuncios publicados hace más de {cfg.listing_max_age_days} días; el precio puede no estar vigente.",
                           "count": len(old), "observation_ids": [r.obs.observation_id for r in old][:25]})
        return {"status": status, "latest_capture_age_days": round(age, 2),
                "fresh_days": cfg.fresh_days, "stale_days": cfg.stale_days}

    def _confidence(self, primary_stats, unique, supply_recs, outlier_ids, freshness, failed, issues):
        factors: list[dict] = []
        score = 0
        force_low = False
        n = primary_stats.get("n", 0) if primary_stats else 0

        def add(delta: int, text: str):
            nonlocal score
            score += delta
            factors.append({"delta": delta, "factor": text})

        if n >= 10:
            add(2, f"muestra de precios suficiente (n={n})")
        elif n >= 5:
            add(1, f"muestra de precios moderada (n={n})")
        else:
            force_low = True
            factors.append({"delta": 0, "factor": f"muestra de precios pequeña (n={n} < 5): confianza forzada a LOW"})
        n_sources = len({r.obs.source_id for r in supply_recs})
        if n_sources >= 2:
            add(1, f"{n_sources} fuentes distintas")
        else:
            add(0, f"{n_sources} fuente(s)")
        valid = [r for r in unique if r.in_price_stats or r.in_unit_price_stats]
        if valid:
            strong = sum(1 for r in valid if r.match.level in (MatchLevel.EXACT, MatchLevel.HIGH)) / len(valid)
            if strong >= 0.8:
                add(1, f"coincidencia de producto fuerte ({strong:.0%} EXACT/HIGH)")
            elif strong < 0.5:
                add(-1, f"coincidencia de producto mayoritariamente ambigua ({strong:.0%} EXACT/HIGH)")
            else:
                add(0, f"coincidencia de producto mixta ({strong:.0%} EXACT/HIGH)")
            share = len(outlier_ids & {r.obs.observation_id for r in valid}) / len(valid)
            if share > 0.2:
                add(-1, f"proporción alta de outliers ({share:.0%})")
        if freshness["status"] == "ACTUAL":
            add(1, "datos actuales")
        elif freshness["status"] == "ANTIGUA":
            force_low = True
            factors.append({"delta": 0, "factor": "datos antiguos: confianza forzada a LOW"})
        else:
            add(0, f"actualidad de datos: {freshness['status']}")
        if failed:
            add(-1, f"{len(failed)} fuente(s) fallida(s)")
        codes = {i["code"] for i in issues}
        if "DIFERENCIAS_GEOGRAFICAS" in codes:
            add(-1, "diferencias geográficas importantes")
        if "INCONSISTENCIA_ENTRE_FUENTES" in codes:
            add(-1, "inconsistencia de precios entre fuentes")

        if force_low:
            level = Confidence.LOW
        elif score >= 5 and not failed:
            level = Confidence.HIGH
        elif score >= 3:
            level = Confidence.MEDIUM
        else:
            level = Confidence.LOW
        factors.append({"delta": None, "factor": f"puntuación {score}; HIGH >= 5 sin fuentes fallidas, MEDIUM >= 3"})
        return level, factors

    def _external_indicators(self, currency, stats, supply, trends, reference_cost, limitations) -> dict:
        sellers = supply["seller_count"]
        pressure = None
        if supply["listing_count"]:
            pressure = ("ALTA" if sellers >= self.cfg.high_competition_min_sellers
                        else "BAJA" if sellers <= self.cfg.low_competition_max_sellers else "MEDIA")
        cv = stats.get("coefficient_of_variation") if stats else None
        stability = None if cv is None else ("ESTABLE" if cv < 0.10 else "MODERADA" if cv < 0.25 else "INESTABLE")
        pt = (trends.get("price_trend") or {}).get(currency or "", {})
        out: dict[str, Any] = {
            "evidence_type": EvidenceType.INFERENCIA.value,
            "competitive_pressure": {"value": pressure, "basis": "vendedores distintos observados"},
            "supply_level": {"value": supply["availability_level"], "basis": supply["availability_basis"]},
            "price_stability": {"value": stability, "basis": "coeficiente de variación entre anuncios comparables",
                                "coefficient_of_variation": cv},
            "market_evolution": {"price": pt.get("classification"), "price_direction": pt.get("direction"),
                                 "listings": trends["listing_trend"].get("classification"),
                                 "listings_direction": trends["listing_trend"].get("direction")},
            "estimated_potential_margin": None,
        }
        if reference_cost is not None:
            out["estimated_potential_margin"] = self._margin(currency, stats, reference_cost, limitations)
        return out

    @staticmethod
    def _margin(currency, stats, cost: ReferenceCost, limitations) -> dict:
        base = {"label": "MARGEN POTENCIAL ESTIMADO (simulación; no es el margen real del negocio)",
                "evidence_type": EvidenceType.ESTIMACION.value,
                "reference_cost": {"amount": cost.amount, "currency": cost.currency, "source": cost.source}}
        if not stats or stats.get("price_median") is None:
            return {**base, "status": "DATO_NO_DISPONIBLE", "reason": "sin mediana de precio de mercado"}
        if cost.currency.upper() != (currency or ""):
            limitations.append("El coste de referencia está en otra moneda que la mediana de mercado; no se simula margen.")
            return {**base, "status": "DATO_NO_DISPONIBLE", "reason": "moneda del coste distinta a la del mercado principal"}
        med = stats["price_median"]
        return {**base, "status": "CALCULADO", "market_price_median": med,
                "potential_margin_abs": _r(med - cost.amount),
                "potential_margin_pct_over_price": _r((med - cost.amount) / med * 100, 1) if med else None}

    @staticmethod
    def _summary(start, end, supply, currency, stats, trends, signals, currencies) -> dict:
        s = []
        s.append({"type": EvidenceType.HECHO_OBSERVADO.value,
                  "text": f"Entre {start.date()} y {end.date()} se observaron {supply['listing_count']} anuncios "
                          f"coincidentes únicos de {supply['seller_count']} vendedor(es) identificado(s) en "
                          f"{supply['source_count']} fuente(s)."})
        if stats and stats.get("n"):
            s.append({"type": EvidenceType.ESTADISTICA_CALCULADA.value,
                      "text": f"Precio típico (mediana) {stats['price_median']} {currency}; rango "
                              f"{stats['price_min']}–{stats['price_max']} {currency} (n={stats['n']}, sin outliers)."})
        else:
            s.append({"type": EvidenceType.DATO_NO_DISPONIBLE.value,
                      "text": "No hay precios válidos suficientes para estimar un precio típico."})
        if len(currencies) > 1:
            s.append({"type": EvidenceType.HECHO_OBSERVADO.value,
                      "text": f"Se observan precios en {len(currencies)} monedas ({', '.join(currencies)}), analizadas por separado."})
        pt = (trends.get("price_trend") or {}).get(currency or "", {})
        if pt.get("classification") not in (None, "INSUFICIENTE"):
            s.append({"type": EvidenceType.ESTADISTICA_CALCULADA.value,
                      "text": f"Evolución de la mediana en {currency}: {pt['classification']} "
                              f"({pt.get('direction')}, {pt.get('change_pct_total')}% en {pt['periods_with_data']} periodos)."})
        else:
            s.append({"type": EvidenceType.DATO_NO_DISPONIBLE.value,
                      "text": "Histórico insuficiente para evaluar la evolución del precio."})
        if signals:
            s.append({"type": EvidenceType.INFERENCIA.value,
                      "text": "Señales detectadas: " + ", ".join(sig["type"] for sig in signals) + "."})
        s.append({"type": EvidenceType.DATO_NO_DISPONIBLE.value,
                  "text": "Ventas reales: no disponibles en las fuentes consultadas."})
        return {"statements": s, "conclusions": []}

    @staticmethod
    def _trace(r: _Record) -> dict:
        o = r.obs
        return {
            "observation_id": o.observation_id,
            "source_id": o.source_id,
            "url": o.url,
            "listing_id": o.listing_id,
            "title": o.title,
            "seller": o.seller,
            "province": o.province,
            "municipality": o.municipality,
            "captured_at": _iso(o.captured_at),
            "price": {"raw_value": {"price": o.raw.get("price"), "currency": o.raw.get("currency")},
                      "normalized_value": {"price": o.price, "currency": r.currency}},
            "presentation": r.presentation.as_dict(),
            "unit_price": _r(r.unit_price, 4),
            "availability": {"raw_value": o.availability, "normalized_value": r.availability},
            "match": r.match.as_dict(),
            "status": r.status,
            "exclusion_reasons": r.exclusion_reasons,
            "duplicate_of": r.duplicate_of,
            "duplicate_reasons": r.duplicate_reasons,
            "outlier": r.outlier,
            "unit_price_outlier": r.unit_outlier,
            "used_in_price_stats": r.in_price_stats and r.outlier is None,
            "used_in_unit_price_stats": r.in_unit_price_stats and r.unit_outlier is None,
        }
