"""Normalización de texto, monedas, unidades y presentaciones.

Solo se aplican conversiones físicas exactas (ml→L, g→kg, lb→kg). Las
conversiones monetarias NO se hacen aquí: requieren un tipo de cambio
autorizado (ver ``analyzer``).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .models import Observation


def normalize_text(text: str | None) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r"[^a-z0-9.,x×\s-]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------- monedas

_CURRENCY_ALIASES = {
    "CUP": "CUP",
    "MN": "CUP",
    "M.N.": "CUP",
    "PESO CUBANO": "CUP",
    "PESOS CUBANOS": "CUP",
    "PESOS": "CUP",
    "USD": "USD",
    "US$": "USD",
    "U$S": "USD",
    "DOLAR": "USD",
    "DOLARES": "USD",
    "USDT": "USDT",
    "MLC": "MLC",
    "EUR": "EUR",
    "€": "EUR",
    "EURO": "EUR",
    "EUROS": "EUR",
    "CUC": "CUC",
}


def normalize_currency(raw: str | None) -> str | None:
    """Devuelve el código de moneda o ``None`` si es desconocida o ambigua.

    El símbolo "$" se considera ambiguo en Cuba (CUP o USD) y no se resuelve.
    """
    if not raw:
        return None
    key = unicodedata.normalize("NFKD", raw)
    key = "".join(ch for ch in key if not unicodedata.combining(ch)).strip().upper()
    return _CURRENCY_ALIASES.get(key)


# ---------------------------------------------------------------- unidades

# unidad -> (dimensión, factor a unidad estándar)
_UNITS: dict[str, tuple[str, float]] = {
    "ml": ("volumen", 0.001),
    "cl": ("volumen", 0.01),
    "l": ("volumen", 1.0),
    "lt": ("volumen", 1.0),
    "lts": ("volumen", 1.0),
    "litro": ("volumen", 1.0),
    "litros": ("volumen", 1.0),
    "g": ("masa", 0.001),
    "gr": ("masa", 0.001),
    "grs": ("masa", 0.001),
    "gramo": ("masa", 0.001),
    "gramos": ("masa", 0.001),
    "kg": ("masa", 1.0),
    "kgs": ("masa", 1.0),
    "kilo": ("masa", 1.0),
    "kilos": ("masa", 1.0),
    "kilogramo": ("masa", 1.0),
    "kilogramos": ("masa", 1.0),
    "lb": ("masa", 0.45359237),
    "lbs": ("masa", 0.45359237),
    "libra": ("masa", 0.45359237),
    "libras": ("masa", 0.45359237),
    "u": ("unidades", 1.0),
    "ud": ("unidades", 1.0),
    "uds": ("unidades", 1.0),
    "unidad": ("unidades", 1.0),
    "unidades": ("unidades", 1.0),
}

STANDARD_UNIT = {"volumen": "L", "masa": "kg", "unidades": "unidad"}

_SIZE_RE = re.compile(
    r"(?<![a-z0-9])(\d+(?:[.,]\d+)?)\s*"
    r"(ml|cl|lts|lt|litros|litro|l|kilogramos|kilogramo|kilos|kilo|kgs|kg|"
    r"gramos|gramo|grs|gr|g|libras|libra|lbs|lb)(?![a-z])"
)
_PACK_RE = [
    re.compile(r"(?<![a-z0-9.,])(\d{1,3})\s*[x×]\s*(?=\d)"),
    re.compile(r"(?:pack|paquete|caja|jaba|blister|bulto)\s+(?:de\s+)?(\d{1,4})(?!\s*(?:ml|cl|l|g|kg|lb)\b)"),
    re.compile(r"(?<![a-z0-9.,])(\d{1,4})\s*(?:unidades|uds|u)(?![a-z])"),
]


def unit_info(unit: str | None) -> tuple[str, float] | None:
    if not unit:
        return None
    return _UNITS.get(normalize_text(unit).rstrip("."))


_SPACED_DECIMAL_RE = re.compile(
    r"(?<![\d.,])(\d{1,3})[.,]\s+(\d{1,2})(?=\s*(?:ml|cl|lts|lt|litros|litro|l|kg|kgs|kilos?|gr|g|lbs?|libras?)(?![a-z]))"
)


def parse_size(text: str) -> tuple[float, str] | None:
    """Extrae la primera cantidad+unidad física del texto (p. ej. '1,5 L').

    Admite decimales escritos con espacio ("1. 89 l", "1, 42 litros"), frecuentes
    en catálogos; no une listas como "3, 900 ml" (más de dos decimales).
    """
    norm = _SPACED_DECIMAL_RE.sub(r"\1.\2", normalize_text(text))
    match = _SIZE_RE.search(norm)
    if not match:
        return None
    return float(match.group(1).replace(",", ".")), match.group(2)


def parse_pack_count(text: str) -> int | None:
    norm = normalize_text(text)
    for pattern in _PACK_RE:
        match = pattern.search(norm)
        if match:
            value = int(match.group(1))
            if value > 1:
                return value
    return None


@dataclass(frozen=True)
class Presentation:
    """Presentación normalizada de una observación o del producto objetivo."""

    raw_quantity: float | None
    raw_unit: str | None
    pack_count: int
    dimension: str | None  # volumen | masa | unidades | None
    standard_quantity: float | None  # por paquete completo, en unidad estándar
    standard_unit: str | None
    origin: str  # "campos", "texto" o "desconocida"

    @property
    def known(self) -> bool:
        return self.standard_quantity is not None and self.standard_quantity > 0

    def as_dict(self) -> dict:
        return {
            "raw_value": {
                "quantity": self.raw_quantity,
                "unit": self.raw_unit,
                "pack_count": self.pack_count,
            },
            "normalized_value": {
                "dimension": self.dimension,
                "standard_quantity": _round(self.standard_quantity, 6),
                "standard_unit": self.standard_unit,
            },
            "origin": self.origin,
        }


def build_presentation(
    quantity: float | None,
    unit: str | None,
    pack_count: int | None,
    text: str = "",
) -> Presentation:
    origin = "campos"
    if quantity is None or unit is None:
        parsed = parse_size(text) if text else None
        if parsed:
            quantity, unit = parsed
            origin = "texto"
        elif unit and quantity is None and unit_info(unit) and unit_info(unit)[0] == "unidades":
            quantity = 1.0
        else:
            origin = "desconocida"
    if pack_count is None and text:
        pack_count = parse_pack_count(text)
    pack = pack_count if pack_count and pack_count > 0 else 1

    info = unit_info(unit)
    if quantity is None or info is None:
        return Presentation(quantity, unit, pack, None, None, None, "desconocida" if info is None else origin)
    dimension, factor = info
    return Presentation(
        raw_quantity=quantity,
        raw_unit=unit,
        pack_count=pack,
        dimension=dimension,
        standard_quantity=quantity * factor * pack,
        standard_unit=STANDARD_UNIT[dimension],
        origin=origin,
    )


_PER_UNIT_RE = re.compile(
    r"\b(?:el|la|por|cada)\s+(litros?|lts?|kilos?|kilogramos?|kgs?|libras?|lbs?|unidad(?:es)?)(?![a-z])"
)


def parse_price_basis(text: str) -> str | None:
    """Detecta precios expresados por unidad en el texto ("4.40 USD el litro", "300 CUP/lb")."""
    match = _PER_UNIT_RE.search(normalize_text((text or "").replace("/", " por ")))
    return match.group(1) if match else None


def observation_presentation(obs: Observation) -> Presentation:
    if obs.quantity is None and obs.unit is None:
        basis = parse_price_basis(obs.title)
        if basis and unit_info(basis):
            # El precio corresponde a 1 unidad de medida, no al envase mencionado en el texto.
            pres = build_presentation(1.0, basis, 1)
            return Presentation(pres.raw_quantity, pres.raw_unit, 1, pres.dimension,
                                pres.standard_quantity, pres.standard_unit, "texto_precio_por_unidad")
    return build_presentation(obs.quantity, obs.unit, obs.pack_count, obs.title)


def same_presentation(a: Presentation, b: Presentation, tolerance: float = 0.02) -> bool | None:
    """True/False si ambas presentaciones son conocidas; None si no se puede saber."""
    if not a.known or not b.known:
        return None
    if a.dimension != b.dimension:
        return False
    return abs(a.standard_quantity - b.standard_quantity) <= tolerance * max(
        a.standard_quantity, b.standard_quantity
    )


# ---------------------------------------------------------------- disponibilidad

_OUT_OF_STOCK = {"agotado", "sin stock", "no disponible", "vendido", "out of stock", "sold out", "sold"}
_IN_STOCK = {"disponible", "en stock", "in stock", "hay", "si"}


def normalize_availability(raw: str | None) -> str | None:
    """'DISPONIBLE', 'AGOTADO' o None si no se informa o no se reconoce."""
    norm = normalize_text(raw)
    if not norm:
        return None
    if norm in _OUT_OF_STOCK or any(term in norm for term in ("agotado", "sin stock", "vendido")):
        return "AGOTADO"
    if norm in _IN_STOCK or "disponible" in norm or "stock" in norm:
        return "DISPONIBLE"
    return None


def normalize_location(raw: str | None) -> str | None:
    norm = normalize_text(raw)
    return norm.title() if norm else None


def normalize_seller(raw: str | None) -> str | None:
    norm = normalize_text(raw)
    return norm or None


def normalize_url(raw: str | None) -> str | None:
    if not raw:
        return None
    url = raw.strip().lower()
    url = re.sub(r"^https?://(www\.)?", "", url)
    url = url.split("#", 1)[0]
    # Descarta parámetros de seguimiento habituales, conserva el resto.
    if "?" in url:
        base, query = url.split("?", 1)
        params = [p for p in query.split("&") if p and not p.startswith(("utm_", "fbclid", "ref="))]
        url = base + ("?" + "&".join(sorted(params)) if params else "")
    return url.rstrip("/")


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)
