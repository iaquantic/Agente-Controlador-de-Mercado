"""Almacén local de capturas para adaptadores web.

- ``catalog.json``: última captura de cada URL (evita volver a descargar lo reciente).
- ``history.jsonl``: todas las capturas, sin sobrescribir, para analizar tendencias.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from ..models import parse_datetime


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
        with self.history_path.open("a", encoding="utf-8") as fh:
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
