"""Tipos de cambio del mercado informal cubano publicados por elTOQUE.

API: ``GET https://tasas.eltoque.com/v1/trmi?date_from=..&date_to=..`` con
``Authorization: Bearer <ELTOQUE_API_KEY>``. Devuelve la tasa representativa
del mercado informal (TRMI) del intervalo pedido: CUP por unidad de cada moneda,
por ejemplo ``{"date": "2026-10-01", "hour": 10, "minutes": 22, "seconds": 5,
"tasas": {"USD": 760.0, "ECU": 860.0, "MLC": 495.01, ...}}``.

Son tasas informales, no oficiales: la fuente lo indica en cada tipo de cambio.
Solo se incorporan las monedas fiduciarias que el sistema normaliza (USD, EUR,
MLC); las criptomonedas se ignoran. "ECU" es el euro en la nomenclatura de
elTOQUE.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from ..models import ExchangeRate
from ..sources import ExchangeRateProvider
from .http import PoliteFetcher

API_URL = "https://tasas.eltoque.com/v1/trmi"
SOURCE = "elTOQUE TRMI (tasa representativa del mercado informal, no oficial)"
_CURRENCIES = {"USD": "USD", "ECU": "EUR", "EUR": "EUR", "MLC": "MLC"}
_HAVANA = ZoneInfo("America/Havana")


def parse_eltoque(payload: dict, *, window_hours: float) -> list[ExchangeRate]:
    """Convierte la respuesta en tipos de cambio ``<moneda> -> CUP`` fechados."""
    date = payload.get("date")
    tasas = payload.get("tasas") or {}
    if not date or not isinstance(tasas, dict):
        return []
    day = datetime.fromisoformat(date)
    # elTOQUE publica la hora en horario de Cuba.
    as_of = day.replace(hour=int(payload.get("hour") or 0), minute=int(payload.get("minutes") or 0),
                        second=int(payload.get("seconds") or 0), tzinfo=_HAVANA)
    out = []
    for code, value in tasas.items():
        cur = _CURRENCIES.get(str(code).upper())
        if cur is None or not isinstance(value, (int, float)) or value <= 0:
            continue
        out.append(ExchangeRate(from_currency=cur, to_currency="CUP", rate=float(value),
                                as_of=as_of.astimezone(timezone.utc),
                                source=f"{SOURCE}; ventana {window_hours:g} h"))
    return out


def eltoque_rates(
    *,
    api_key: str | None = None,
    until: datetime | None = None,
    window_hours: float = 24.0,
    fetcher: PoliteFetcher | None = None,
) -> ExchangeRateProvider:
    """Descarga la TRMI de las ``window_hours`` previas a ``until`` (por defecto, ahora)."""
    api_key = api_key or os.environ.get("ELTOQUE_API_KEY")
    if not api_key:
        raise RuntimeError("Falta ELTOQUE_API_KEY para consultar las tasas de elTOQUE.")
    fetcher = fetcher or PoliteFetcher(min_delay_seconds=1.0)
    fetcher.extra_headers["Authorization"] = f"Bearer {api_key}"
    until = (until or datetime.now(timezone.utc)).astimezone(_HAVANA)
    since = until - timedelta(hours=window_hours)
    fmt = "%Y-%m-%d %H:%M:%S"
    params = urlencode({"date_from": since.strftime(fmt), "date_to": until.strftime(fmt)})
    payload = json.loads(fetcher.get(f"{API_URL}?{params}").text)
    return ExchangeRateProvider(parse_eltoque(payload, window_hours=window_hours))
