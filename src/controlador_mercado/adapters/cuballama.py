"""Adaptador para Cuballama (www.cuballama.com): canales Mercado y Envíos.

Acceso (verificado el 2026-09-30):
- robots.txt de www.cuballama.com prohíbe ``/api/``; este adaptador no la usa.
- El catálogo se sirve desde ``api.cuballama.com``, que no publica robots.txt
  (404: sin restricciones declaradas) y responde sin autenticación ni firma a
  un cliente que se identifica honestamente. Se usan los mismos endpoints de
  búsqueda que la web, con pausas entre peticiones.
- Términos (abril 2023): no prohíben expresamente la recopilación automatizada;
  afirman la propiedad intelectual de Cuballama sobre el contenido.

Canales:
- **Mercado** (``POST /restaurantes/business/searchList``): negocios que venden
  y entregan dentro de Cuba (supermercados, agro, combos…). Cada oferta es una
  observación; el vendedor es el negocio. Precio del producto en USD, sin la
  entrega (se cobra aparte).
- **Envíos** (``GET /odoo/api/v1/envios/products/envio_personalizado/search``):
  productos enviados desde el extranjero. El precio observado es el que muestra
  la web: precio + envío más barato a la provincia. Se conservan en la traza el
  precio base, las opciones de envío, ``customs_duty`` y ``compare_at_price``.

Los identificadores de provincia difieren entre canales: Mercado usa los de
``MERCADO_PROVINCE_IDS`` (obtenidos eligiendo cada provincia en la web) y
Envíos los del catálogo ``/odoo/api/v1/countries/51/states``.
"""

from __future__ import annotations

import json
import unicodedata
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import urlencode

from ..models import Observation, TargetProduct
from ..sources import SourceAdapter
from .http import PoliteFetcher

API = "https://api.cuballama.com"
WEB = "https://www.cuballama.com"

# Verificado el 2026-09-30 seleccionando cada provincia en /mercado.
MERCADO_PROVINCE_IDS = {
    "guantanamo": 1, "santiago de cuba": 2, "granma": 3, "holguin": 4, "las tunas": 5,
    "camaguey": 6, "ciego de avila": 7, "sancti spiritus": 8, "villa clara": 9, "cienfuegos": 10,
    "matanzas": 11, "mayabeque": 12, "la habana": 13, "artemisa": 14, "pinar del rio": 15,
    "isla de la juventud": 16,
}


def _key(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    return " ".join("".join(ch for ch in text if not unicodedata.combining(ch)).lower().split())


def _display(province_key: str) -> str:
    return " ".join(w if w in ("de", "del", "la") else w.capitalize() for w in province_key.split())


def parse_mercado(payload: dict, *, province: str, captured_at: datetime) -> list[Observation]:
    out = []
    for biz in payload.get("list") or []:
        slug = biz.get("slug")
        for offer in biz.get("offers") or []:
            oid = offer.get("offerId")
            if oid is None or not offer.get("offerName"):
                continue
            record = {
                "observation_id": f"cuballama-mercado:{oid}:{captured_at.isoformat()}",
                "source_id": "cuballama_mercado",
                "source_name": "Cuballama Mercado",
                "listing_id": str(oid),
                "url": f"{WEB}/mercado/negocio/{slug}" if slug else None,
                "title": offer.get("offerName"),
                "seller": biz.get("name"),
                "province": province,
                "price": offer.get("price"),
                "currency": offer.get("currency") or biz.get("currency"),
                "captured_at": captured_at.isoformat(),
            }
            obs = Observation.from_dict(record)
            delivery = biz.get("deliveryTimeRestaurantDto") or {}
            obs.raw = {**record, "business_id": biz.get("id"), "business_score": biz.get("averageScore"),
                       "sponsored": bool(biz.get("sponsored")),
                       "business_categories": [c.get("name") for c in biz.get("categories") or []],
                       "original_price": offer.get("originalPrice"),
                       "site_price_by_measure": offer.get("priceByMeasure"),
                       "delivery_time": f"{delivery.get('deliveryTime')} {delivery.get('deliveryTimeUnitEs')}"
                       if delivery.get("deliveryTime") else None}
            out.append(obs)
    return out


def parse_envios(payload: dict, *, province: str, captured_at: datetime) -> list[Observation]:
    out = []
    for product in ((payload.get("data") or {}).get("elements")) or []:
        for variant in product.get("variants") or [{}]:
            vid = variant.get("id") or product.get("id")
            base = variant.get("price", product.get("price"))
            if vid is None or base is None:
                continue
            shipping = [s for s in variant.get("shipping") or [] if s.get("carrier_price") is not None]
            cheapest = min(shipping, key=lambda s: s["carrier_price"]) if shipping else None
            delivered = round(base + cheapest["carrier_price"], 2) if cheapest else None
            qty = variant.get("qty_available")
            record = {
                "observation_id": f"cuballama-envios:{vid}:{_key(province)}:{captured_at.isoformat()}",
                "source_id": "cuballama_envios",
                "source_name": "Cuballama Envíos",
                "listing_id": f"{vid}@{_key(province)}",
                "url": f"{WEB}/envios/producto/{variant['slug']}" if variant.get("slug") else None,
                "title": variant.get("title") or product.get("title"),
                "seller": "Cuballama Envíos",
                "province": province,
                # Precio mostrado en la web: producto + envío más barato. Sin opciones de envío no hay precio entregado.
                "price": delivered,
                "currency": product.get("currency"),
                "availability": "agotado" if qty == 0 else None,
                "captured_at": captured_at.isoformat(),
            }
            obs = Observation.from_dict(record)
            obs.raw = {**record, "product_id": product.get("id"), "base_price": base,
                       "cheapest_shipping": cheapest and {k: cheapest.get(k) for k in
                                                          ("carrier_name", "carrier_price", "estimated_time", "delivery_type")},
                       "shipping_options": len(shipping), "customs_duty": product.get("customs_duty"),
                       "compare_at_price": product.get("compare_at_price"), "weight": product.get("weight"),
                       "category": (product.get("category") or {}).get("name"), "qty_available": qty}
            out.append(obs)
    return out


class CuballamaSource(SourceAdapter):
    """Una sola fuente con dos canales; cada observación lleva su ``source_id`` de canal."""

    source_id = "cuballama"
    source_name = "Cuballama"

    def __init__(
        self,
        *,
        channels: Iterable[str] = ("mercado", "envios"),
        default_province: str = "La Habana",
        max_pages: int = 2,
        page_size: int = 100,
        offers_per_business: int = 20,
        fetcher: PoliteFetcher | None = None,
        now_fn=lambda: datetime.now(timezone.utc),
    ) -> None:
        self.channels = tuple(channels)
        unknown = set(self.channels) - {"mercado", "envios"}
        if unknown:
            raise ValueError(f"canales desconocidos: {unknown}")
        self.default_province = default_province
        self.max_pages = max_pages
        self.page_size = page_size
        self.offers_per_business = offers_per_business
        self.fetcher = fetcher or PoliteFetcher()
        self.now_fn = now_fn
        self._envios_states: dict[str, int] | None = None

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "channels": list(self.channels), "default_province": self.default_province,
                "note": "Mercado: negocios en Cuba (precio sin entrega). Envíos: precio + envío más barato. USD."}

    def _envios_state_id(self, province_key: str) -> int:
        if self._envios_states is None:
            data = json.loads(self.fetcher.get(f"{API}/odoo/api/v1/countries/51/states").text)
            self._envios_states = {_key(e["name"]): e["id"] for e in (data.get("data") or {}).get("elements", [])}
        if province_key not in self._envios_states:
            raise ValueError(f"provincia desconocida para Envíos: {province_key}")
        return self._envios_states[province_key]

    def _mercado(self, query: str, province_key: str) -> list[Observation]:
        if province_key not in MERCADO_PROVINCE_IDS:
            raise ValueError(f"provincia desconocida para Mercado: {province_key}")
        out: list[Observation] = []
        for page in range(self.max_pages):
            body = {"currency": "USD", "pointer": page, "counter": self.page_size,
                    "offersPreview": self.offers_per_business, "accountId": None,
                    "provinceid": MERCADO_PROVINCE_IDS[province_key], "municipalityid": None, "localityid": None,
                    "deliveryOrderType": "DELIVERY", "search": query, "productTypes": []}
            payload = json.loads(self.fetcher.post_json(f"{API}/restaurantes/business/searchList", body).text)
            out.extend(parse_mercado(payload, province=_display(province_key), captured_at=self.now_fn()))
            if page + 1 >= (payload.get("pageTotal") or 1):
                break
        return out

    def _envios(self, query: str, province_key: str) -> list[Observation]:
        params = urlencode({"search_term": query, "provinceId": self._envios_state_id(province_key), "currency": "USD"})
        payload = json.loads(self.fetcher.get(f"{API}/odoo/api/v1/envios/products/envio_personalizado/search?{params}").text)
        return parse_envios(payload, province=_display(province_key), captured_at=self.now_fn())

    def fetch(self, target: TargetProduct, since: datetime | None, until: datetime | None) -> list[Observation]:
        query = target.name if not target.brand else f"{target.name} {target.brand}"
        out: list[Observation] = []
        for province in target.provinces or [self.default_province]:
            key = _key(province)
            if "mercado" in self.channels:
                out.extend(self._mercado(query, key))
            if "envios" in self.channels:
                out.extend(self._envios(query, key))
        return out
