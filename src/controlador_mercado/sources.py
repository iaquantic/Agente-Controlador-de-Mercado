"""Fuentes de datos autorizadas.

El agente no navega ni scrapea por su cuenta: consume observaciones que
entregan adaptadores autorizados por el sistema (exportaciones de scrapers,
APIs de marketplaces, catálogos…). Cada adaptador implementa ``SourceAdapter``.
Los fallos se registran y nunca se sustituyen por datos inventados.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .matching import match_observation
from .models import ExchangeRate, MatchLevel, Observation, TargetProduct, parse_datetime


@dataclass
class FetchResult:
    source_id: str
    source_name: str | None
    observations: list[Observation] = field(default_factory=list)
    status: str = "ok"  # ok | error | vacia
    error: str | None = None
    fetched_at: datetime | None = None
    discarded_by_prefilter: int = 0

    def status_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_name": self.source_name,
            "status": self.status,
            "error": self.error,
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
            "observations_returned": len(self.observations),
            "source_ids_returned": sorted({o.source_id for o in self.observations}),
            "discarded_by_prefilter": self.discarded_by_prefilter,
        }


class SourceAdapter(ABC):
    """Interfaz que debe implementar cada fuente autorizada."""

    source_id: str
    source_name: str | None = None

    @abstractmethod
    def fetch(self, target: TargetProduct, since: datetime | None, until: datetime | None) -> list[Observation]:
        """Devuelve observaciones candidatas. Puede lanzar excepciones: el registro las captura."""

    def describe(self) -> dict[str, Any]:
        return {"source_id": self.source_id, "source_name": self.source_name, "type": type(self).__name__}


def _load_records(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    data = json.loads(text)
    if isinstance(data, dict):
        data = data.get("observations", [])
    if not isinstance(data, list):
        raise ValueError(f"{path}: se esperaba una lista de observaciones")
    return data


class JsonFileSource(SourceAdapter):
    """Lee observaciones de un archivo o directorio JSON/JSONL (exportaciones de scrapers autorizados).

    Formato de cada registro: ver ``Observation.from_dict``. Si un registro no
    trae ``source_id``, se usa el de este adaptador.
    """

    def __init__(self, path: str | Path, source_id: str | None = None, source_name: str | None = None) -> None:
        self.path = Path(path)
        self.source_id = source_id or self.path.stem
        self.source_name = source_name

    def _files(self) -> list[Path]:
        if self.path.is_dir():
            return sorted(p for p in self.path.iterdir() if p.suffix in (".json", ".jsonl"))
        if not self.path.exists():
            raise FileNotFoundError(f"fuente no encontrada: {self.path}")
        return [self.path]

    def fetch(self, target: TargetProduct, since: datetime | None, until: datetime | None) -> list[Observation]:
        out: list[Observation] = []
        for file in self._files():
            for i, rec in enumerate(_load_records(file)):
                rec = dict(rec)
                rec.setdefault("source_id", self.source_id)
                if self.source_name:
                    rec.setdefault("source_name", self.source_name)
                rec.setdefault("observation_id", f"{file.stem}:{i}")
                obs = Observation.from_dict(rec, i)
                if obs.captured_at is not None:
                    if since and obs.captured_at < since:
                        continue
                    if until and obs.captured_at > until:
                        continue
                out.append(obs)
        return out


class SourceRegistry:
    """Consulta todas las fuentes, registra fallos y aplica un prefiltro de relevancia."""

    def __init__(self, adapters: list[SourceAdapter] | None = None) -> None:
        self.adapters: list[SourceAdapter] = list(adapters or [])

    def add(self, adapter: SourceAdapter) -> None:
        self.adapters.append(adapter)

    def describe(self) -> list[dict[str, Any]]:
        return [a.describe() for a in self.adapters]

    def fetch_all(
        self,
        target: TargetProduct,
        since: datetime | None = None,
        until: datetime | None = None,
        source_ids: list[str] | None = None,
    ) -> list[FetchResult]:
        results: list[FetchResult] = []
        for adapter in self.adapters:
            if source_ids and adapter.source_id not in source_ids:
                continue
            res = FetchResult(adapter.source_id, adapter.source_name, fetched_at=datetime.now(timezone.utc))
            try:
                candidates = adapter.fetch(target, since, until)
            except Exception as exc:  # noqa: BLE001 - cualquier fallo de fuente se registra
                res.status = "error"
                res.error = f"{type(exc).__name__}: {exc}"
                results.append(res)
                continue
            for obs in candidates:
                m = match_observation(target, obs)
                if m.level == MatchLevel.NO_MATCH and m.token_coverage == 0:
                    res.discarded_by_prefilter += 1
                else:
                    res.observations.append(obs)
            if not candidates:
                res.status = "vacia"
            results.append(res)
        return results


class ExchangeRateProvider:
    """Tipos de cambio autorizados y fechados, cargados de un JSON.

    Formato: ``[{"from_currency": "USD", "to_currency": "CUP", "rate": 0, "as_of": "...", "source": "..."}]``.
    Sin archivo configurado no hay conversiones: nunca se inventan tasas.
    """

    def __init__(self, rates: list[ExchangeRate] | None = None) -> None:
        self.rates = list(rates or [])

    @classmethod
    def from_file(cls, path: str | Path) -> "ExchangeRateProvider":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls([ExchangeRate.from_dict(d) for d in data])

    def lookup(self, frm: str, to: str, on: datetime | None = None) -> list[ExchangeRate]:
        frm, to = frm.upper(), to.upper()
        matches = [r for r in self.rates if {r.from_currency, r.to_currency} == {frm, to}]
        if on is not None:
            on = parse_datetime(on) or on
            matches = [r for r in matches if r.as_of <= on]
        return sorted(matches, key=lambda r: r.as_of, reverse=True)
