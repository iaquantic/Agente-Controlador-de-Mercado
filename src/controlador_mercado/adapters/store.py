"""Almacén local de capturas para adaptadores web.

- ``catalog.json``: última captura de cada URL (evita volver a descargar lo reciente).
- ``history.jsonl``: todas las capturas, sin sobrescribir, para analizar tendencias.
"""

from __future__ import annotations

import json
import os
from dataclasses import fields
from datetime import datetime
from pathlib import Path
from typing import Any

from ..models import Observation, TargetProduct, parse_datetime
from ..sources import SourceAdapter


def default_cache_dir(source_id: str) -> Path:
    base = os.environ.get("CONTROLADOR_CACHE_DIR") or Path.home() / ".cache" / "controlador_mercado"
    return Path(base) / source_id


class SnapshotStore:
    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.catalog_path = self.dir / "catalog.json"
        self.history_path = self.dir / "history.jsonl"
        self.catalog: dict[str, dict[str, Any]] = (
            json.loads(self.catalog_path.read_text("utf-8")) if self.catalog_path.exists() else {}
        )

    def fetched_at(self, key: str) -> datetime | None:
        entry = self.catalog.get(key)
        return parse_datetime(entry.get("captured_at")) if entry else None

    def put(self, key: str, record: dict[str, Any]) -> None:
        self.catalog[key] = record
        self.append_history([record])

    def append_history(self, records: list[dict[str, Any]]) -> None:
        with self.history_path.open("a", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def remove(self, key: str) -> None:
        self.catalog.pop(key, None)

    def save(self) -> None:
        tmp = self.catalog_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.catalog, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.catalog_path)

    def history(self, since: datetime | None = None, until: datetime | None = None) -> list[dict[str, Any]]:
        if not self.history_path.exists():
            return []
        out = []
        for line in self.history_path.read_text("utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            ts = parse_datetime(rec.get("captured_at"))
            if ts is None or (since and ts < since) or (until and ts > until):
                continue
            out.append(rec)
        return out


def observation_record(obs: Observation) -> dict[str, Any]:
    record = {f.name: getattr(obs, f.name) for f in fields(obs)}
    for key in ("captured_at", "published_at"):
        if record[key] is not None:
            record[key] = record[key].isoformat()
    return record


class RecordingSource(SourceAdapter):
    """Guarda cada captura de otra fuente y devuelve también las anteriores.

    Las fuentes web solo ven el estado actual del sitio; sin guardar las capturas
    no hay series temporales. Cada ejecución añade sus observaciones a
    ``history.jsonl`` (sin sobrescribir) y ``fetch`` devuelve todas las capturas
    del intervalo pedido. Si la fuente falla, el error se propaga: no se presenta
    el histórico como si fuera una captura nueva.
    """

    def __init__(self, inner: SourceAdapter, store: SnapshotStore | None = None) -> None:
        self.inner = inner
        self.source_id = inner.source_id
        self.source_name = inner.source_name
        self._store = store

    @property
    def store(self) -> SnapshotStore:
        if self._store is None:
            self._store = SnapshotStore(default_cache_dir(self.source_id))
        return self._store

    def describe(self) -> dict[str, Any]:
        return {**self.inner.describe(), "history": str(self.store.history_path)}

    def fetch(self, target: TargetProduct, since: datetime | None, until: datetime | None) -> list[Observation]:
        fresh = self.inner.fetch(target, since, until)
        self.store.append_history([observation_record(o) for o in fresh])
        out = []
        for i, rec in enumerate(self.store.history(since, until)):
            obs = Observation.from_dict(rec, i)
            obs.raw = rec.get("raw") or {}
            out.append(obs)
        return out
