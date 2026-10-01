"""Adaptador para Cubatel Market (www.cubatel.com/market).

CONSENTIMIENTO OBLIGATORIO. Los términos de Cubatel (revisados el 2026-09-30)
dicen: "El usuario/cliente acepta no realizar ninguna actividad de recopilación
de datos sistemática o automatizada (incluidos [...] la extracción de datos y la
recolección de datos) [...] sin el consentimiento previo por escrito de Cubatel
-el correo electrónico no constituye un consentimiento por escrito-".
Por eso el adaptador NO se ejecuta sin ``CUBATEL_CONSENT_REF`` (referencia del
consentimiento escrito obtenido) y la incluye en su descripción y trazas.

Extracción:
- ``/market-products-sitemap.xml`` lista las URLs de producto (≈288 en 2026-09).
- Cada página de producto incluye JSON-LD ``Product`` con nombre, SKU, marca
  (en la práctica "Vendido por <tienda>"), y ``Offer`` con precio, moneda (USD),
  disponibilidad y estado.
- Para no sobrecargar el sitio, se guarda un catálogo local y solo se vuelve a
  descargar un producto cuando su captura supera ``cache_ttl_hours``. Cada
  captura se añade a un histórico local que alimenta las tendencias.

Limitación conocida: Cubatel pide elegir provincia/municipio antes de comprar;
el precio publicado en la página de producto no indica provincia.
"""

from __future__ import annotations

import html as html_lib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from ..models import Observation, TargetProduct
from ..sources import SourceAdapter
from .http import PoliteFetcher
from .store import SnapshotStore, default_cache_dir

SITEMAP_URL = "https://www.cubatel.com/market-products-sitemap.xml"
_SCHEMA_AVAILABILITY = {
    "instock": "disponible", "limitedavailability": "disponible", "onlineonly": "disponible",
    "outofstock": "agotado", "soldout": "agotado", "discontinued": "agotado",
    "preorder": "preventa", "backorder": "preventa",
}


class ConsentRequiredError(PermissionError):
    """La fuente exige consentimiento previo por escrito para la recopilación automatizada."""


def parse_sitemap(xml: str) -> list[str]:
    return [html_lib.unescape(u).strip() for u in re.findall(r"<loc>\s*([^<]+?)\s*</loc>", xml)]


def _ld_objects(page: str) -> list[dict]:
    out: list[dict] = []
    for raw in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', page, re.S):
        try:
            data = json.loads(html_lib.unescape(raw))
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        out.extend(i for i in items if isinstance(i, dict))
    return out


def _type_is(obj: dict, name: str) -> bool:
    t = obj.get("@type")
    return t == name or (isinstance(t, list) and name in t)


def _seller_from_brand(brand: Any) -> tuple[str | None, str | None]:
    """Cubatel usa ``brand`` como "Vendido por <tienda>": se separa marca y vendedor."""
    name = brand.get("name") if isinstance(brand, dict) else brand if isinstance(brand, str) else None
    if not name:
        return None, None
    m = re.match(r"\s*vendido\s+por\s+(.+)", name, re.I)
    return (None, m.group(1).strip()) if m else (name.strip(), None)


def parse_cubatel_product(page: str, url: str, captured_at: datetime) -> dict[str, Any] | None:
    """Extrae el registro de observación de una página de producto (o None si no hay JSON-LD Product)."""
    product = next((o for o in _ld_objects(page) if _type_is(o, "Product")), None)
    if product is None:
        return None
    offers = product.get("offers")
    if isinstance(offers, list):
        offers = offers[0] if offers else None
    offers = offers if isinstance(offers, dict) else {}
    price = offers.get("price")
    if price is None and _type_is(offers, "AggregateOffer"):
        price = offers.get("lowPrice")
    availability = str(offers.get("availability") or "").rsplit("/", 1)[-1].lower()
    condition = str(offers.get("itemCondition") or "").rsplit("/", 1)[-1].lower()
    brand, seller = _seller_from_brand(product.get("brand"))
    sku = product.get("sku") or url.rstrip("/").rsplit("/", 1)[-1]
    return {
        "observation_id": f"cubatel:{sku}:{captured_at.isoformat()}",
        "source_id": "cubatel",
        "source_name": "Cubatel Market",
        "listing_id": str(sku),
        "url": offers.get("url") or url,
        "title": product.get("name"),
        "description": product.get("description") if isinstance(product.get("description"), str) else None,
        "brand": brand,
        "seller": seller,
        "price": price,
        "currency": offers.get("priceCurrency"),
        "availability": _SCHEMA_AVAILABILITY.get(availability, availability or None),
        "condition": {"newcondition": "nuevo", "usedcondition": "usado",
                      "refurbishedcondition": "reacondicionado"}.get(condition),
        "captured_at": captured_at.isoformat(),
    }


class CubatelSource(SourceAdapter):
    source_id = "cubatel"
    source_name = "Cubatel Market"

    def __init__(
        self,
        *,
        consent_reference: str | None = None,
        fetcher: PoliteFetcher | None = None,
        store: SnapshotStore | None = None,
        cache_ttl_hours: float = 24.0,
        max_fetches_per_run: int = 300,
        sitemap_url: str = SITEMAP_URL,
        now_fn=lambda: datetime.now(timezone.utc),
    ) -> None:
        self.consent_reference = consent_reference or os.environ.get("CUBATEL_CONSENT_REF")
        self._fetcher = fetcher
        self._store = store
        self.cache_ttl = timedelta(hours=cache_ttl_hours)
        self.max_fetches_per_run = max_fetches_per_run
        self.sitemap_url = sitemap_url
        self.now_fn = now_fn
        self.last_run: dict[str, Any] = {}

    @property
    def fetcher(self) -> PoliteFetcher:
        if self._fetcher is None:
            self._fetcher = PoliteFetcher()
        return self._fetcher

    @property
    def store(self) -> SnapshotStore:
        if self._store is None:
            self._store = SnapshotStore(default_cache_dir(self.source_id))
        return self._store

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "consent_reference": self.consent_reference,
                "enabled": bool(self.consent_reference),
                "note": "Tienda online (precios en USD). Requiere consentimiento escrito de Cubatel.",
                "last_run": self.last_run or None}

    def _require_consent(self) -> None:
        if not self.consent_reference:
            raise ConsentRequiredError(
                "Los términos de Cubatel prohíben la recopilación automatizada sin su consentimiento previo "
                "por escrito. Configura CUBATEL_CONSENT_REF con la referencia de ese consentimiento."
            )

    def refresh(self) -> dict[str, int]:
        """Actualiza el catálogo local respetando el TTL y el límite de descargas."""
        self._require_consent()
        urls = parse_sitemap(self.fetcher.get(self.sitemap_url).text)
        now = self.now_fn()
        stats = {"sitemap_urls": len(urls), "fetched": 0, "cached": 0, "without_product_data": 0,
                 "errors": 0, "pending": 0, "removed": 0}
        for key in [k for k in self.store.catalog if k not in set(urls)]:
            self.store.remove(key)  # ya no está en el catálogo público (el histórico se conserva)
            stats["removed"] += 1
        stale = []
        for url in urls:
            ts = self.store.fetched_at(url)
            if ts is not None and now - ts < self.cache_ttl:
                stats["cached"] += 1
            else:
                stale.append((ts or datetime.min.replace(tzinfo=timezone.utc), url))
        stale.sort()  # primero lo nunca visto y lo más antiguo
        for i, (_, url) in enumerate(stale):
            if i >= self.max_fetches_per_run:
                stats["pending"] = len(stale) - i
                break
            try:
                resp = self.fetcher.get(url)
            except Exception:  # noqa: BLE001 - un producto fallido no detiene el resto
                stats["errors"] += 1
                continue
            record = parse_cubatel_product(resp.text, url, self.now_fn())
            if record is None:
                stats["without_product_data"] += 1
                continue
            record["consent_reference"] = self.consent_reference
            self.store.put(url, record)
            stats["fetched"] += 1
        self.store.save()
        self.last_run = stats
        return stats

    def fetch(self, target: TargetProduct, since: datetime | None, until: datetime | None) -> list[Observation]:
        self.refresh()
        records = self.store.history(since, until)
        return [Observation.from_dict(r, i) for i, r in enumerate(records)]
