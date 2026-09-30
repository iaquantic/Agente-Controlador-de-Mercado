"""Adaptador para Revolico (clasificados, www.revolico.com).

Acceso (verificado el 2026-09-30): el sitio responde a clientes que se
identifican honestamente como bot (User-Agent propio) y bloquea con Cloudflare
las peticiones que se hacen pasar por un navegador. El adaptador respeta
robots.txt, espacia las peticiones y NO evade ningún desafío anti-bot: si
aparece uno, lanza ``SourceBlockedError`` y la fuente se registra como fallida.

Extracción: la página de búsqueda (``/search?q=…&page=N&province=<slug>``)
incluye ``__NEXT_DATA__`` con el estado de Apollo:
- ``ROOT_QUERY.adsPerPage(...)``: resultados (100 por página, ``pageInfo.pageCount``).
- ``ROOT_QUERY.promotedAds(...)``: anuncios promocionados (se marcan como tales).
- ``AdType:<id>``: título, precio, moneda, permalink, fecha, provincia/municipio (por id),
  vistas e imagen.
- ``ProvinceType``/``MunicipalityType``: nombres de provincia y municipio.

Privacidad: los teléfonos NO se guardan. Solo se deriva un identificador de
vendedor seudónimo (HMAC-SHA256 con la clave ``CONTROLADOR_SELLER_HASH_KEY``)
para contar vendedores distintos. Sin esa clave, el identificador solo es
estable dentro de una misma ejecución.

Si la estructura cambia, se recurre a un parser genérico (JSON-LD, RSC de
Next.js u objetos con título y precio).
"""

from __future__ import annotations

import hashlib
import hmac
import html as html_lib
import json
import os
import re
import secrets
import unicodedata
from datetime import datetime, timezone
from typing import Any, Iterator
from urllib.parse import urlencode, urljoin

from ..models import Observation, TargetProduct, parse_datetime
from ..sources import SourceAdapter
from .http import PoliteFetcher

DEFAULT_BASE_URL = "https://www.revolico.com"

_TITLE_KEYS = ("title", "adTitle", "name")
_PRICE_KEYS = ("price", "priceValue", "amount")
_CURRENCY_KEYS = ("currency", "currencyCode", "priceCurrency")
_ID_KEYS = ("id", "adId", "_id", "sku")
_URL_KEYS = ("permalink", "url", "absoluteUrl", "link", "slug")
_DESC_KEYS = ("shortDescription", "description", "body")
_DATE_KEYS = ("updatedOnToOrder", "updatedOn", "updatedAt", "publishedAt", "createdAt", "created", "datePosted", "date")
_SELLER_KEYS = ("user", "seller", "owner", "contact")
_SELLER_NAME_KEYS = ("name", "displayName", "username", "contactName", "sellerName")


def _first(d: dict, keys: tuple[str, ...]) -> Any:
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


def _as_price(value: Any) -> float | str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str) and re.fullmatch(r"\s*\d[\d.,\s]*\s*", value):
        return value
    if isinstance(value, dict):  # {"amount": 10, "currency": "USD"}
        return _as_price(_first(value, ("amount", "value", "price")))
    return None


def _name_of(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        v = _first(value, ("name", "displayName", "title", "label"))
        return v if isinstance(v, str) else None
    return None


def _walk(node: Any, depth: int = 0) -> Iterator[dict]:
    if depth > 40:
        return
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v, depth + 1)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v, depth + 1)


def _is_ad(d: dict) -> bool:
    title = _first(d, _TITLE_KEYS)
    price = _first(d, _PRICE_KEYS)
    if not isinstance(title, str) or len(title) < 3 or _as_price(price) is None:
        return False
    # Descarta traducciones/etiquetas de interfaz ("price": "Precio") y objetos sin identidad.
    return _first(d, _ID_KEYS) is not None or _first(d, _URL_KEYS) is not None


def _json_blobs(page: str) -> Iterator[Any]:
    """Estructuras JSON embebidas en la página."""
    for m in re.finditer(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S):
        try:
            yield json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    for m in re.finditer(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', page, re.S):
        try:
            yield json.loads(html_lib.unescape(m.group(1)))
        except json.JSONDecodeError:
            pass
    for m in re.finditer(r'__APOLLO_STATE__\s*=\s*(\{.*?\})\s*;?\s*</script>', page, re.S):
        try:
            yield json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Next.js App Router: fragmentos RSC con objetos JSON incrustados.
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>', page, re.S)
    if chunks:
        try:
            rsc = "".join(json.loads('"' + c + '"') for c in chunks)
        except json.JSONDecodeError:
            rsc = ""
        decoder = json.JSONDecoder()
        for m in re.finditer(r'\{"', rsc):
            window = rsc[m.start(): m.start() + 400]
            if '"price' not in window:
                continue
            try:
                obj, _ = decoder.raw_decode(rsc, m.start())
            except json.JSONDecodeError:
                continue
            yield obj


_PER_RUN_KEY = secrets.token_bytes(32)


def _seller_key() -> bytes:
    key = os.environ.get("CONTROLADOR_SELLER_HASH_KEY")
    return key.encode() if key else _PER_RUN_KEY


def pseudonymous_seller(ad: dict) -> str | None:
    """Identificador de vendedor seudónimo a partir de los teléfonos (no se guardan)."""
    info = ad.get("phoneInfo") or {}
    phones = sorted(
        f"{p.get('prefix', '')}{p.get('number', '')}"
        for p in (info.get("firstPhone"), info.get("secondPhone"))
        if isinstance(p, dict) and p.get("number")
    )
    if phones:
        digest = hmac.new(_seller_key(), "|".join(phones).encode(), hashlib.sha256).hexdigest()
        return f"vendedor-{digest[:12]}"
    for key in ("email", "contactEmail"):
        if ad.get(key):
            digest = hmac.new(_seller_key(), str(ad[key]).lower().encode(), hashlib.sha256).hexdigest()
            return f"vendedor-{digest[:12]}"
    return None


_PRIVATE_KEYS = {"phoneInfo", "email", "contactEmail", "phone", "phones", "whatsapp"}


def _next_data(page: str) -> dict | None:
    m = re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def _deref(state: dict, node: Any) -> Any:
    if isinstance(node, dict) and "__ref" in node:
        return state.get(node["__ref"])
    return node


def parse_revolico_apollo(page: str, *, base_url: str, captured_at: datetime,
                          source_id: str = "revolico") -> tuple[list[Observation], dict] | None:
    """Parser específico del estado Apollo. Devuelve (observaciones, info de paginación) o None."""
    data = _next_data(page)
    state = ((data or {}).get("props", {}).get("pageProps", {}) or {}).get("__APOLLO_STATE__")
    if not isinstance(state, dict) or "ROOT_QUERY" not in state:
        return None
    root = state["ROOT_QUERY"]
    provinces = {v["id"]: v.get("name") for k, v in state.items() if k.startswith("ProvinceType:")}
    municipalities = {v["id"]: v.get("name") for k, v in state.items() if k.startswith("MunicipalityType:")}

    ordered: list[tuple[dict, bool]] = []
    page_info: dict = {}
    for key, value in root.items():
        if not isinstance(value, dict):
            continue
        promoted = key.startswith("promotedAds")
        if not (key.startswith("adsPerPage") or promoted):
            continue
        if not promoted:
            page_info = value.get("pageInfo") or {}
        for edge in value.get("edges") or []:
            ad = _deref(state, (edge or {}).get("node"))
            if isinstance(ad, dict):
                ordered.append((ad, promoted))

    out: list[Observation] = []
    seen: set[str] = set()
    for ad, promoted in ordered:
        ad_id = str(ad.get("id") or "")
        if not ad_id or ad_id in seen:
            continue
        seen.add(ad_id)
        permalink = ad.get("permalink")
        image = (ad.get("mainImage") or {}).get("gcsKey") if isinstance(ad.get("mainImage"), dict) else None
        record = {
            "observation_id": f"{source_id}:{ad_id}",
            "source_id": source_id,
            "source_name": "Revolico",
            "listing_id": ad_id,
            "url": urljoin(base_url + "/", permalink) if permalink else None,
            "title": ad.get("title"),
            "description": ad.get("shortDescription") or ad.get("description"),
            "seller": pseudonymous_seller(ad),
            "province": provinces.get(str(ad.get("provinceId"))) if ad.get("provinceId") else None,
            "municipality": municipalities.get(str(ad.get("municipalityId"))) if ad.get("municipalityId") else None,
            "price": ad.get("price"),
            "currency": ad.get("currency"),
            "image_id": image,
            "captured_at": captured_at.isoformat(),
            "published_at": ad.get("updatedOnToOrder"),
        }
        obs = Observation.from_dict(record)
        # Traza sin datos personales; se conservan métricas públicas del anuncio.
        obs.raw = {**record, "ad": {k: v for k, v in ad.items() if k not in _PRIVATE_KEYS},
                   "is_promoted": promoted or bool(ad.get("isPromoted")), "view_count": ad.get("viewCount")}
        out.append(obs)
    return out, page_info


def _slug(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def parse_revolico_page(page: str, *, base_url: str, captured_at: datetime, source_id: str = "revolico") -> list[Observation]:
    seen: set[str] = set()
    out: list[Observation] = []
    for blob in _json_blobs(page):
        for d in _walk(blob):
            if not _is_ad(d):
                continue
            ad_id = _first(d, _ID_KEYS)
            url = _first(d, _URL_KEYS)
            if isinstance(url, str):
                url = urljoin(base_url + "/", url)
            key = str(ad_id or url)
            if key in seen:
                continue
            seen.add(key)

            price_raw = _first(d, _PRICE_KEYS)
            currency = _first(d, _CURRENCY_KEYS)
            if currency is None and isinstance(price_raw, dict):
                currency = _first(price_raw, _CURRENCY_KEYS)
            seller = None
            for k in _SELLER_KEYS:
                seller = _name_of(d.get(k))
                if seller:
                    break
            seller = seller or _first(d, ("contactName", "sellerName"))
            date = _first(d, _DATE_KEYS)
            if isinstance(date, (int, float)):  # epoch en s o ms
                date = datetime.fromtimestamp(date / 1000 if date > 1e11 else date, tz=timezone.utc)

            out.append(Observation.from_dict({
                "observation_id": f"{source_id}:{key}",
                "source_id": source_id,
                "source_name": "Revolico",
                "listing_id": str(ad_id) if ad_id is not None else None,
                "url": url if isinstance(url, str) else None,
                "title": _first(d, _TITLE_KEYS),
                "description": _first(d, _DESC_KEYS) if isinstance(_first(d, _DESC_KEYS), str) else None,
                "seller": seller if isinstance(seller, str) else None,
                "province": _name_of(_first(d, ("province", "provinceName", "location"))),
                "municipality": _name_of(_first(d, ("municipality", "municipalityName"))),
                "price": _as_price(price_raw),
                # Sin moneda explícita se deja vacía: el analizador la trata como desconocida.
                "currency": currency if isinstance(currency, str) else None,
                "captured_at": captured_at.isoformat(),
                "published_at": date.isoformat() if isinstance(date, datetime) else date,
            }))
    return out


class RevolicoSource(SourceAdapter):
    source_id = "revolico"
    source_name = "Revolico"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        search_path: str = "/search",
        max_pages: int = 3,
        fetcher: PoliteFetcher | None = None,
        auth_headers: dict[str, str] | None = None,
        now_fn=lambda: datetime.now(timezone.utc),
    ) -> None:
        self.base_url = (base_url or os.environ.get("REVOLICO_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.search_path = search_path
        self.max_pages = max_pages
        if auth_headers is None and os.environ.get("REVOLICO_AUTH_HEADERS"):
            auth_headers = json.loads(os.environ["REVOLICO_AUTH_HEADERS"])
        self.fetcher = fetcher or PoliteFetcher(extra_headers=auth_headers)
        self.now_fn = now_fn

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "base_url": self.base_url, "max_pages": self.max_pages,
                "note": "Clasificados; precios en CUP/USD/MLC según el anuncio. Vendedores seudonimizados."}

    @staticmethod
    def build_query(target: TargetProduct) -> str:
        parts = [target.name]
        if target.brand and target.brand.lower() not in target.name.lower():
            parts.append(target.brand)
        return " ".join(parts)

    def search_url(self, query: str, page: int, province_slug: str | None = None) -> str:
        params = {"q": query}
        if page > 1:
            params["page"] = str(page)
        if province_slug:
            params["province"] = province_slug
        return f"{self.base_url}{self.search_path}?{urlencode(params)}"

    def fetch(self, target: TargetProduct, since: datetime | None, until: datetime | None) -> list[Observation]:
        query = self.build_query(target)
        slugs = [_slug(p) for p in target.provinces] or [None]
        results: dict[str, Observation] = {}
        for slug in slugs:
            for page in range(1, self.max_pages + 1):
                resp = self.fetcher.get(self.search_url(query, page, slug))
                captured = self.now_fn()
                parsed = parse_revolico_apollo(resp.text, base_url=self.base_url, captured_at=captured)
                if parsed is None:
                    found, page_count = parse_revolico_page(resp.text, base_url=self.base_url,
                                                            captured_at=captured), None
                else:
                    found, info = parsed
                    page_count = info.get("pageCount")
                new = [o for o in found if o.observation_id not in results]
                for o in new:
                    results[o.observation_id] = o
                if not new or (page_count is not None and page >= page_count):
                    break
        return list(results.values())
