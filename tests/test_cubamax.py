"""Pruebas del adaptador de Cubamax con una sesión de navegador SIMULADA.

La forma de la respuesta reproduce ``store/products`` (verificada el 2026-09-30);
los productos son sintéticos.
"""

from datetime import datetime, timezone

import pytest

from controlador_mercado import MarketAnalyzer
from controlador_mercado.adapters.cubamax import CubamaxSource, SearchPage, parse_cubamax_products
from controlador_mercado.adapters.http import HttpResponse, PoliteFetcher, RobotsDisallowedError
from controlador_mercado.models import TargetProduct
from controlador_mercado.sources import SourceRegistry

NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)


def product(pid, name, price, seller="SPM", out=False, img="https://media.example/foto"):
    return {"id": pid, "name": name, "shortDescription": name, "thumbnailImageUrl": img, "categoryName": "Alimentos",
            "commercialName": seller, "salePrice": price, "isOutOfStock": out, "stock": None,
            "tags": [{"name": "ENTREGA EN CASA (HABANA)"}], "shipmentLimitQuantity": None}


def page(products, page_no=1, page_count=1, text="$5.19 USD"):
    payload = {"errors": [], "data": {"total": len(products), "page": page_no, "take": 24, "pageCount": page_count,
                                      "hasNextPage": page_no < page_count, "products": products}}
    return SearchPage(payload=payload, page_text=text, url=f"https://cubamax.com/es/shop/products?page={page_no}")


class FakeSession:
    def __init__(self, pages):
        self.pages = pages
        self.locations, self.searches, self.closed = [], [], False

    def select_location(self, province, municipality):
        self.locations.append((province, municipality))
        return province, municipality or "Arroyo Naranjo"

    def search(self, query, page_no):
        self.searches.append((query, page_no))
        return self.pages[page_no - 1]

    def close(self):
        self.closed = True


def robots(text="User-Agent: *\nAllow: /\nDisallow: /private/\n"):
    def transport(url, headers, timeout):
        return HttpResponse(url, 200, text)
    return PoliteFetcher(transport=transport, min_delay_seconds=0, sleep=lambda s: None)


def test_parse_products():
    obs = parse_cubamax_products(page([product("a", "Aceite vegetal Alberto (887 ml)", 5.19),
                                       product("b", "Aceite vegetal Alberto (887 ml)", 6.07, out=True)]),
                                 captured_at=NOW, province="La Habana", municipality="Plaza")
    assert [(o.price, o.currency, o.seller, o.municipality) for o in obs] == [
        (5.19, "USD", "SPM", "Plaza"), (6.07, "USD", "SPM", "Plaza")]
    assert obs[1].availability == "agotado"
    assert obs[0].url is None and obs[0].image_id is None  # sin URL/imagen compartidas que fusionen ofertas
    assert obs[0].raw["category"] == "Alimentos" and obs[0].raw["tags"] == ["ENTREGA EN CASA (HABANA)"]


def test_currency_unknown_if_not_shown():
    obs = parse_cubamax_products(page([product("a", "Arroz 1 kg", 2)], text="$2.00"), captured_at=NOW,
                                 province="La Habana", municipality="Plaza")
    assert obs[0].currency is None


def test_shared_image_and_search_url_do_not_merge_offers():
    obs = parse_cubamax_products(page([product(str(i), "Aceite vegetal Alberto (887 ml)", 5 + i) for i in range(5)]),
                                 captured_at=NOW, province="La Habana", municipality="Plaza")
    target = TargetProduct(name="aceite vegetal", quantity=887, unit="ml")
    res = MarketAnalyzer().analyze(target, obs, now=NOW)
    assert res["data_quality"]["duplicates_removed"] == 0
    assert res["price_statistics"]["by_currency"]["USD"]["presentation_price"]["n"] == 5


def test_source_paginates_per_province_and_closes_session():
    session = FakeSession([page([product("a", "Aceite 1 L", 5)], 1, 2), page([product("b", "Aceite 1 L", 6)], 2, 2)])
    waits = []
    src = CubamaxSource(session_factory=lambda: session, robots=robots(), sleep=waits.append,
                        municipalities={"La Habana": "Plaza"}, max_pages=5, now_fn=lambda: NOW)
    obs = src.fetch(TargetProduct(name="aceite", provinces=["La Habana"]), None, None)
    assert [o.raw["product_id"] for o in obs] == ["a", "b"]
    assert session.locations == [("La Habana", "Plaza")]
    assert session.searches == [("aceite", 1), ("aceite", 2)]
    assert len(waits) == 2 and session.closed


def test_robots_disallow_stops_before_opening_browser():
    opened = []
    src = CubamaxSource(session_factory=lambda: opened.append(1), robots=robots("User-agent: *\nDisallow: /es/shop/\n"))
    with pytest.raises(RobotsDisallowedError):
        src.fetch(TargetProduct(name="aceite"), None, None)
    assert opened == []


def test_session_errors_are_recorded():
    class Broken(FakeSession):
        def select_location(self, province, municipality):
            raise ValueError("'Habana' no está entre las opciones de Cubamax")

    session = Broken([])
    src = CubamaxSource(session_factory=lambda: session, robots=robots(), sleep=lambda s: None)
    res = SourceRegistry([src]).fetch_all(TargetProduct(name="aceite", provinces=["Habana"]))
    assert res[0].status == "error" and "no está entre las opciones" in res[0].error
    assert session.closed
