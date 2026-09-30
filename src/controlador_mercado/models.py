"""Modelos de datos del Agente Controlador de Mercado.

Todas las observaciones conservan el registro original (``raw``) para que
cualquier valor normalizado pueda rastrearse hasta el dato capturado.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class MatchLevel(str, Enum):
    EXACT = "EXACT"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    NO_MATCH = "NO_MATCH"

    @property
    def rank(self) -> int:
        return _MATCH_RANK[self]


_MATCH_RANK = {
    MatchLevel.NO_MATCH: 0,
    MatchLevel.LOW: 1,
    MatchLevel.MEDIUM: 2,
    MatchLevel.HIGH: 3,
    MatchLevel.EXACT: 4,
}


class Confidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class EvidenceType(str, Enum):
    """Naturaleza epistemológica de cada afirmación del informe."""

    HECHO_OBSERVADO = "HECHO_OBSERVADO"
    ESTADISTICA_CALCULADA = "ESTADISTICA_CALCULADA"
    ESTIMACION = "ESTIMACION"
    INFERENCIA = "INFERENCIA"
    DATO_NO_DISPONIBLE = "DATO_NO_DISPONIBLE"


def parse_datetime(value: Any) -> datetime | None:
    """Parsea ISO-8601; las fechas sin zona horaria se asumen UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _opt_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(" ", "")
    # "1.500,50" -> 1500.50 ; "1,500.50" -> 1500.50 ; "1,5" -> 1.5
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def _opt_int(value: Any) -> int | None:
    number = _opt_float(value)
    if number is None:
        return None
    return int(number) if number.is_integer() else None


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


@dataclass
class Observation:
    """Una observación de mercado tal como la entregó una fuente autorizada."""

    observation_id: str
    source_id: str
    title: str
    source_name: str | None = None
    url: str | None = None
    listing_id: str | None = None
    description: str | None = None
    brand: str | None = None
    seller: str | None = None
    province: str | None = None
    municipality: str | None = None
    price: float | None = None
    currency: str | None = None
    quantity: float | None = None
    unit: str | None = None
    pack_count: int | None = None
    availability: str | None = None
    condition: str | None = None
    image_id: str | None = None
    captured_at: datetime | None = None
    published_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any], index: int = 0) -> "Observation":
        source_id = _opt_str(data.get("source_id")) or "desconocida"
        obs_id = _opt_str(data.get("observation_id")) or f"{source_id}:{index}"
        return cls(
            observation_id=obs_id,
            source_id=source_id,
            title=_opt_str(data.get("title") or data.get("product")) or "",
            source_name=_opt_str(data.get("source_name")),
            url=_opt_str(data.get("url")),
            listing_id=_opt_str(data.get("listing_id")),
            description=_opt_str(data.get("description")),
            brand=_opt_str(data.get("brand")),
            seller=_opt_str(data.get("seller")),
            province=_opt_str(data.get("province")),
            municipality=_opt_str(data.get("municipality")),
            price=_opt_float(data.get("price")),
            currency=_opt_str(data.get("currency")),
            quantity=_opt_float(data.get("quantity")),
            unit=_opt_str(data.get("unit")),
            pack_count=_opt_int(data.get("pack_count")),
            availability=_opt_str(data.get("availability")),
            condition=_opt_str(data.get("condition")),
            image_id=_opt_str(data.get("image_id")),
            captured_at=parse_datetime(data.get("captured_at")),
            published_at=parse_datetime(data.get("published_at")),
            raw=dict(data),
        )


@dataclass
class TargetProduct:
    """Producto objetivo que se desea medir en el mercado."""

    name: str
    brand: str | None = None
    model: str | None = None
    variant: str | None = None
    quantity: float | None = None
    unit: str | None = None
    pack_count: int | None = None
    condition: str | None = None
    keywords: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)
    provinces: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TargetProduct":
        return cls(
            name=str(data["name"]),
            brand=_opt_str(data.get("brand")),
            model=_opt_str(data.get("model")),
            variant=_opt_str(data.get("variant")),
            quantity=_opt_float(data.get("quantity")),
            unit=_opt_str(data.get("unit")),
            pack_count=_opt_int(data.get("pack_count")),
            condition=_opt_str(data.get("condition")),
            keywords=list(data.get("keywords") or []),
            exclude_keywords=list(data.get("exclude_keywords") or []),
            provinces=list(data.get("provinces") or []),
        )


@dataclass(frozen=True)
class ExchangeRate:
    """Tipo de cambio autorizado por el sistema. Nunca se inventa."""

    from_currency: str
    to_currency: str
    rate: float
    as_of: datetime
    source: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExchangeRate":
        as_of = parse_datetime(data.get("as_of"))
        rate = _opt_float(data.get("rate"))
        if as_of is None or rate is None or rate <= 0:
            raise ValueError("Un tipo de cambio requiere 'rate' > 0 y fecha 'as_of'.")
        return cls(
            from_currency=str(data["from_currency"]).upper(),
            to_currency=str(data["to_currency"]).upper(),
            rate=rate,
            as_of=as_of,
            source=str(data.get("source") or "no_especificada"),
        )


@dataclass(frozen=True)
class ReferenceCost:
    """Coste de referencia autorizado para simular un margen potencial."""

    amount: float
    currency: str
    source: str
    unit_basis: str = "presentacion"  # "presentacion" o "unidad_estandar"
