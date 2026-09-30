"""Pruebas del adaptador de Cuballama con respuestas SINTÉTICAS (forma real de api.cuballama.com, 2026-09-30)."""

import json
from datetime import datetime, timezone

import pytest

from controlador_mercado import MarketAnalyzer
from controlador_mercado.adapters.cuballama import CuballamaSource, parse_envios, parse_mercado
from controlador_mercado.adapters.http import HttpResponse, PoliteFetcher
from controlador_mercado.models import TargetProduct

NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)


def mercado_payload(businesses, page_total=1):
    return {"total": len(businesses), "pageTotal": page_total, "page": 1, "list": businesses}


def business(bid, name, offers, slug=None):
    return {"id": bid, "name": name, "slug": slug or f"negocio-{bid}", "averageScore": 4.8, "sponsored": False,
            "currency": "USD", "categories": [{"name": "Supermercado"}],
            "deliveryTimeRestaurantDto": {"deliveryTime": 4, "deliveryTimeUnitEs": "horas"},
            "offers": [{"offerId": oid, "offerName": n, "price": p, "currency": "USD", "originalPrice": None,
                        "priceByMeasure": "x"} for oid, n, p in offers]}


def envios_payload(products):
    return {"code": 0, "description": "OK", "data": {"elements": products}}


def envios_product(pid, title, price, shipping=(54.9, 27.55), qty=None):
    return {"id": pid, "title": title, "price": price, "currency": "USD", "customs_duty": 70, "weight": 19,
            "compare_at_price": price + 10, "category": {"name": "Cocinas de Gas"},
            "variants": [{"id": pid + 7, "title": title, "slug": f"producto-{pid + 7}", "price": price,
                          "qty_available": qty,
                          "shipping": [{"carrier_name": f"c{i}", "carrier_price": c, "estimated_time": "2-3 días",
                                        "delivery_type": "home_delivery"} for i, c in enumerate(shipping)]}]}


STATES = {"code": 0, "data": {"elements": [{"id": 1766, "name": "La Habana"}, {"id": 1779, "name": "Santiago De Cuba"}]}}


class Api:
    def __init__(self, mercado_pages, envios):
        self.mercado_pages, self.envios, self.calls = mercado_pages, envios, []

    def __call__(self, url, headers, timeout, data=None):
        self.calls.append((url, json.loads(data) if data else None, headers))
        if url.endswith("/robots.txt"):
            return HttpResponse(url, 404, "Default backend - 404")
        if "searchList" in url:
            return HttpResponse(url, 200, json.dumps(self.mercado_pages[json.loads(data)["pointer"]]))
        if "countries/51/states" in url:
            return HttpResponse(url, 200, json.dumps(STATES))
        if "envio_personalizado/search" in url:
            return HttpResponse(url, 200, json.dumps(self.envios))
        return HttpResponse(url, 404, "")


def source(api, **kw):
    return CuballamaSource(fetcher=PoliteFetcher(transport=api, min_delay_seconds=0, sleep=lambda s: None),
                           now_fn=lambda: NOW, **kw)


def test_parse_mercado():
    obs = parse_mercado(mercado_payload([business(1, "La Catedral", [(10, "Aceite de girasol NAZ 1 litro", 5.79)],
                                                  slug="la-catedral-1")]), province="La Habana", captured_at=NOW)
    o = obs[0]
    assert (o.title, o.price, o.currency, o.seller, o.province) == ("Aceite de girasol NAZ 1 litro", 5.79, "USD",
                                                                    "La Catedral", "La Habana")
    assert o.url == "https://www.cuballama.com/mercado/negocio/la-catedral-1"
    assert o.source_id == "cuballama_mercado" and o.raw["delivery_time"] == "4 horas"


def test_parse_envios_uses_price_shown_on_site():
    obs = parse_envios(envios_payload([envios_product(733, "Cocina de gas Koblenz PFK400", 105.09)]),
                       province="La Habana", captured_at=NOW)
    o = obs[0]
    assert o.price == 132.64  # 105.09 + envío más barato (27.55), como muestra la web
    assert o.raw["base_price"] == 105.09 and o.raw["cheapest_shipping"]["carrier_price"] == 27.55
    assert o.raw["customs_duty"] == 70 and o.url == "https://www.cuballama.com/envios/producto/producto-740"
    no_ship = parse_envios(envios_payload([envios_product(1, "X", 10, shipping=())]), province="La Habana",
                           captured_at=NOW)
    assert no_ship[0].price is None  # sin envío no hay precio entregado: no se inventa
    sold_out = parse_envios(envios_payload([envios_product(2, "Y", 10, qty=0)]), province="La Habana", captured_at=NOW)
    assert sold_out[0].availability == "agotado"


def test_source_queries_both_channels_with_right_province_ids():
    api = Api([mercado_payload([business(1, "A", [(1, "Aceite de girasol 1 litro", 6)])], page_total=2),
               mercado_payload([business(2, "B", [(2, "Aceite de girasol 1 litro", 7)])], page_total=2)],
              envios_payload([envios_product(5, "Aceite de girasol 1 L importado", 4.0, shipping=(2.0,))]))
    obs = source(api).fetch(TargetProduct(name="aceite de girasol", provinces=["La Habana"]), None, None)
    assert sorted(o.source_id for o in obs) == ["cuballama_envios", "cuballama_mercado", "cuballama_mercado"]
    bodies = [b for u, b, _ in api.calls if b]
    assert [b["pointer"] for b in bodies] == [0, 1] and all(b["provinceid"] == 13 for b in bodies)
    assert all(b["search"] == "aceite de girasol" for b in bodies)
    assert any("provinceId=1766" in u for u, _, _ in api.calls)
    assert all(h["Content-Type"] == "application/json" for u, b, h in api.calls if b)


def test_never_touches_disallowed_www_api():
    api = Api([mercado_payload([])], envios_payload([]))
    source(api).fetch(TargetProduct(name="arroz", provinces=["Santiago de Cuba"]), None, None)
    assert all(not u.startswith("https://www.cuballama.com/api/") for u, _, _ in api.calls)
    bodies = [b for _, b, _ in api.calls if b]
    assert bodies[0]["provinceid"] == 2


def test_unknown_province_raises():
    api = Api([mercado_payload([])], envios_payload([]))
    with pytest.raises(ValueError, match="provincia desconocida"):
        source(api, channels=("mercado",)).fetch(TargetProduct(name="arroz", provinces=["Narnia"]), None, None)


def test_channels_are_separate_sources_in_analysis():
    api = Api([mercado_payload([business(i, f"N{i}", [(i, "Aceite de girasol 1 litro", 6 + i / 10)])
                                for i in range(6)])], envios_payload([]))
    obs = source(api, channels=("mercado",)).fetch(TargetProduct(name="aceite de girasol"), None, None)
    res = MarketAnalyzer().analyze(TargetProduct(name="aceite de girasol", quantity=1, unit="L"), obs, now=NOW)
    assert res["supply_statistics"]["seller_count"] == 6
    assert "cuballama_mercado" in res["price_statistics"]["by_currency"]["USD"]["by_source"]


def test_robots_on_www_would_block():
    from controlador_mercado.adapters.http import RobotsRules
    rules = RobotsRules("User-agent: *\nDisallow: /api/\nDisallow: /mi-cuenta\n")
    assert not rules.can_fetch("x", "https://www.cuballama.com/api/auth/session")
    assert rules.can_fetch("x", "https://www.cuballama.com/mercado/negocio/la-catedral-1")
