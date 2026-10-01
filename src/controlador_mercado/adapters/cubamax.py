"""Adaptador para la tienda de Cubamax (cubamax.com/es/shop).

Cómo obtiene los datos (verificado el 2026-09-30):
- La tienda muestra productos y precios por municipio de entrega. Hay que elegir
  provincia y municipio en un formulario antes de ver el catálogo.
- El catálogo lo carga el navegador desde ``api.cubamax.xyz/store/products`` con
  peticiones firmadas (HMAC) por el propio código del sitio.
- Este adaptador NO replica esa firma ni llama a la API por su cuenta: maneja
  un Chromium normal (sin ocultar que es headless ni falsear su identidad) que
  usa la web como un usuario (elige la ubicación, usa el buscador
  ``/es/shop/products?description=…&page=N``) y lee la respuesta JSON que el
  sitio entrega a esa página.
- Respeta robots.txt de cubamax.com y espacia las navegaciones.

Campos por producto: nombre, descripción corta, precio de venta, tienda
proveedora (``commercialName``), categoría, agotado/disponible, stock y
etiquetas de entrega. La moneda no viene en la respuesta: se toma "USD" solo
si la página muestra precios en USD; si no, queda desconocida.

Términos (revisados el 2026-09-30): no prohíben expresamente la recopilación
automatizada, pero sí "cualquier reproducción de los contenidos del website [...]
sin consentimiento previo".

Requiere Playwright (``pip install -e ".[navegador]"``) y Chromium
(``CONTROLADOR_CHROMIUM_PATH`` si no está en la ruta por defecto de Playwright).
"""

from __future__ import annotations

import os
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Protocol
from urllib.parse import urlencode

from ..models import Observation, TargetProduct
from ..sources import SourceAdapter
from .http import PoliteFetcher, RobotsDisallowedError

BASE_URL = "https://cubamax.com"
SHOP_PATH = "/es/shop/products"


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in text if not unicodedata.combining(ch)).strip().lower()


@dataclass
class SearchPage:
    payload: dict[str, Any]  # respuesta JSON de store/products tal como la recibió la página
    page_text: str  # texto visible (para confirmar la moneda mostrada)
    url: str


class ShopSession(Protocol):
    def select_location(self, province: str, municipality: str | None) -> tuple[str, str]: ...
    def search(self, query: str, page: int) -> SearchPage: ...
    def close(self) -> None: ...


def parse_cubamax_products(page: SearchPage, *, captured_at: datetime, province: str,
                           municipality: str) -> list[Observation]:
    data = (page.payload or {}).get("data") or {}
    currency = "USD" if "USD" in page.page_text else None
    out = []
    for p in data.get("products") or []:
        pid = p.get("id")
        if not pid or not p.get("name"):
            continue
        record = {
            "observation_id": f"cubamax:{pid}:{_norm(municipality)}:{captured_at.isoformat()}",
            "source_id": "cubamax",
            "source_name": "Cubamax Shop",
            "listing_id": f"{pid}@{_norm(municipality)}",
            # La ficha usa una ruta de 4 segmentos no documentada: no se reconstruye.
            # La URL de búsqueda es común a muchos productos (no sirve para deduplicar).
            "url": None,
            "title": p.get("name"),
            "description": p.get("shortDescription"),
            "seller": p.get("commercialName"),
            "province": province,
            "municipality": municipality,
            "price": p.get("salePrice"),
            "currency": currency,
            "availability": "agotado" if p.get("isOutOfStock") else "disponible",
            # Las tiendas reutilizan la misma foto en ofertas distintas: no se usa para deduplicar.
            "captured_at": captured_at.isoformat(),
        }
        obs = Observation.from_dict(record)
        obs.raw = {**record, "observed_on_url": page.url, "product_id": pid, "image": p.get("thumbnailImageUrl"), "category": p.get("categoryName"), "stock": p.get("stock"),
                   "tags": [t.get("name") for t in p.get("tags") or [] if isinstance(t, dict)],
                   "shipment_limit_quantity": p.get("shipmentLimitQuantity")}
        out.append(obs)
    return out


class PlaywrightShopSession:
    """Sesión de navegador real sobre la tienda de Cubamax."""

    def __init__(self, *, base_url: str = BASE_URL, timeout_ms: int = 60000) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - depende del entorno
            raise RuntimeError('Cubamax requiere Playwright: pip install -e ".[navegador]"') from exc
        self.base_url = base_url
        self.timeout_ms = timeout_ms
        self._pw = sync_playwright().start()
        launch: dict[str, Any] = {}
        if os.environ.get("CONTROLADOR_CHROMIUM_PATH"):
            launch["executable_path"] = os.environ["CONTROLADOR_CHROMIUM_PATH"]
        if os.environ.get("HTTPS_PROXY"):
            launch["proxy"] = {"server": os.environ["HTTPS_PROXY"]}
        self._browser = self._pw.chromium.launch(**launch)
        self._page = self._browser.new_context(locale="es-ES").new_page()

    def _pick(self, combobox_index: int, wanted: str | None) -> str:
        page = self._page
        page.get_by_role("combobox").nth(combobox_index).click()
        options = page.get_by_role("option")
        options.first.wait_for(timeout=self.timeout_ms)
        names = [options.nth(i).inner_text().strip() for i in range(options.count())]
        if wanted is None:
            choice = 0
        else:
            matches = [i for i, n in enumerate(names) if _norm(n) == _norm(wanted)]
            if not matches:
                raise ValueError(f"'{wanted}' no está entre las opciones de Cubamax: {names}")
            choice = matches[0]
        options.nth(choice).click()
        return names[choice]

    def select_location(self, province: str, municipality: str | None) -> tuple[str, str]:
        self._page.goto(self.base_url + SHOP_PATH, wait_until="networkidle", timeout=self.timeout_ms)
        prov = self._pick(0, province)
        self._page.wait_for_timeout(1000)
        muni = self._pick(1, municipality)
        with self._page.expect_response(lambda r: "store/products" in r.url, timeout=self.timeout_ms):
            self._page.get_by_role("button", name="Continuar").click()
        return prov, muni

    def search(self, query: str, page: int) -> SearchPage:
        url = f"{self.base_url}{SHOP_PATH}?{urlencode({'description': query, 'page': page})}"
        predicate = (lambda r: "store/products" in r.url and "description=" in r.url
                     and f"page={page}" in r.url and r.request.method == "GET")
        with self._page.expect_response(predicate, timeout=self.timeout_ms) as info:
            self._page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        payload = info.value.json()
        self._page.wait_for_load_state("networkidle", timeout=self.timeout_ms)
        return SearchPage(payload=payload, page_text=self._page.inner_text("body"), url=url)

    def close(self) -> None:
        try:
            self._browser.close()
        finally:
            self._pw.stop()


class CubamaxSource(SourceAdapter):
    source_id = "cubamax"
    source_name = "Cubamax Shop"

    def __init__(
        self,
        *,
        default_province: str = "La Habana",
        municipalities: dict[str, str] | None = None,
        max_pages: int = 3,
        min_delay_seconds: float = 5.0,
        session_factory: Callable[[], ShopSession] | None = None,
        robots: PoliteFetcher | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now_fn=lambda: datetime.now(timezone.utc),
    ) -> None:
        self.default_province = default_province
        self.municipalities = dict(municipalities or {})
        self.max_pages = max_pages
        self.min_delay_seconds = min_delay_seconds
        self.session_factory = session_factory or PlaywrightShopSession
        self.robots = robots or PoliteFetcher()
        self.sleep = sleep
        self.now_fn = now_fn

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "max_pages": self.max_pages, "default_province": self.default_province,
                "note": "Tienda online; precios por municipio de entrega. Navegador real, sin replicar la firma de su API."}

    def _check_robots(self, query: str) -> None:
        url = f"{BASE_URL}{SHOP_PATH}?{urlencode({'description': query, 'page': 1})}"
        rules = self.robots._robots_for(url)
        if rules is not None and not rules.can_fetch(self.robots.user_agent, url):
            raise RobotsDisallowedError(f"robots.txt prohíbe {url}")

    def fetch(self, target: TargetProduct, since: datetime | None, until: datetime | None) -> list[Observation]:
        query = target.name if not target.brand else f"{target.name} {target.brand}"
        self._check_robots(query)
        provinces = target.provinces or [self.default_province]
        out: dict[str, Observation] = {}
        session = self.session_factory()
        try:
            for province in provinces:
                prov, muni = session.select_location(province, self.municipalities.get(province))
                for page in range(1, self.max_pages + 1):
                    self.sleep(self.min_delay_seconds)
                    result = session.search(query, page)
                    found = parse_cubamax_products(result, captured_at=self.now_fn(), province=prov,
                                                   municipality=muni)
                    for o in found:
                        out.setdefault(o.listing_id, o)
                    data = (result.payload or {}).get("data") or {}
                    if not found or not data.get("hasNextPage") or page >= (data.get("pageCount") or page):
                        break
        finally:
            session.close()
        return list(out.values())
