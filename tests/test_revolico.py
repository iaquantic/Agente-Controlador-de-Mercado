"""Pruebas del adaptador de Revolico con respuestas HTTP simuladas.

Las páginas de prueba son SINTÉTICAS: reproducen formatos genéricos de Next.js
y JSON-LD, no la estructura real de Revolico (inaccesible por Cloudflare).
"""

import json
from datetime import datetime, timezone

import pytest

from controlador_mercado.adapters.http import (
    HttpResponse,
    PoliteFetcher,
    RobotsDisallowedError,
    SourceBlockedError,
)
from controlador_mercado.adapters.revolico import RevolicoSource, parse_revolico_page
from controlador_mercado.models import TargetProduct
from controlador_mercado.sources import SourceRegistry

NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
BASE = "https://www.revolico.com"
ROBOTS = "User-agent: *\nAllow: /\nDisallow: /checkout/\n"
CHALLENGE = "<html><head><title>Just a moment...</title></head><body>cf-chl-bypass</body></html>"


def next_data_page(ads):
    payload = {"props": {"pageProps": {"i18n": {"price": "Precio", "title": "Título"},
                                       "search": {"edges": [{"node": ad} for ad in ads]}}}}
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script></html>'


AD_1 = {"id": 101, "title": "Aceite de girasol 1 L", "price": 1150, "currency": "CUP",
        "permalink": "/compra-venta/alimentos/aceite-101.html", "province": {"name": "La Habana"},
        "municipality": {"name": "Plaza"}, "user": {"name": "Tienda A"}, "updatedOnToOrder": "2026-09-29T10:00:00Z"}
AD_2 = {"id": 102, "title": "Aceite girasol Ole 1L", "price": "4", "currency": "USD", "permalink": "/a-102.html"}


class FakeTransport:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, url, headers, timeout):
        self.calls.append((url, headers))
        for prefix, resp in self.routes.items():
            if url.startswith(prefix):
                return resp(url) if callable(resp) else resp
        return HttpResponse(url, 404, "")


def fetcher(routes, **kw):
    kw.setdefault("min_delay_seconds", 0)
    t = FakeTransport(routes)
    return PoliteFetcher(transport=t, sleep=lambda s: None, **kw), t


def test_parse_next_data_ads():
    obs = parse_revolico_page(next_data_page([AD_1, AD_2]), base_url=BASE, captured_at=NOW)
    assert len(obs) == 2  # las etiquetas de interfaz ("price": "Precio") no son anuncios
    a = obs[0]
    assert a.title == "Aceite de girasol 1 L" and a.price == 1150 and a.currency == "CUP"
    assert a.url == "https://www.revolico.com/compra-venta/alimentos/aceite-101.html"
    assert (a.province, a.municipality, a.seller) == ("La Habana", "Plaza", "Tienda A")
    assert a.captured_at == NOW and a.published_at.year == 2026
    assert obs[1].price == 4.0 and obs[1].currency == "USD"


def test_parse_without_currency_keeps_it_unknown():
    ad = {"id": 7, "title": "Arroz 1 kg", "price": 300}
    obs = parse_revolico_page(next_data_page([ad]), base_url=BASE, captured_at=NOW)
    assert obs[0].currency is None


def test_parse_rsc_and_jsonld():
    rsc_obj = json.dumps({"id": "r1", "title": "Leche en polvo 1 kg", "price": 2500, "currency": "CUP"})
    rsc = f'<script>self.__next_f.push([1,{json.dumps("5:" + rsc_obj)}])</script>'
    ld = {"@type": "Product", "name": "Café Serrano 250 g", "sku": "c1",
          "offers": {"@type": "Offer", "price": "900", "priceCurrency": "CUP"}}
    page = rsc + f'<script type="application/ld+json">{json.dumps(ld)}</script>'
    obs = parse_revolico_page(page, base_url=BASE, captured_at=NOW)
    titles = {o.title for o in obs}
    assert "Leche en polvo 1 kg" in titles


def test_challenge_raises_blocked():
    f, _ = fetcher({f"{BASE}/robots.txt": HttpResponse("", 200, ROBOTS),
                    f"{BASE}/search": HttpResponse("", 403, CHALLENGE)})
    with pytest.raises(SourceBlockedError, match="no intenta evadirlo"):
        f.get(f"{BASE}/search?q=aceite")


def test_blocked_robots_stops_before_search():
    f, t = fetcher({f"{BASE}/robots.txt": HttpResponse("", 403, CHALLENGE)})
    with pytest.raises(SourceBlockedError):
        f.get(f"{BASE}/search?q=aceite")
    assert len(t.calls) == 1


def test_robots_disallow_respected():
    f, t = fetcher({f"{BASE}/robots.txt": HttpResponse("", 200, ROBOTS)})
    with pytest.raises(RobotsDisallowedError):
        f.get(f"{BASE}/checkout/pago")
    assert all("/checkout/" not in url for url, _ in t.calls)


def test_throttling_between_requests():
    waits = []
    clock = iter([0.0, 0.0, 1.0, 1.0, 2.0, 2.0])
    t = FakeTransport({BASE: HttpResponse("", 200, ROBOTS)})
    f = PoliteFetcher(transport=t, sleep=waits.append, clock=lambda: next(clock), min_delay_seconds=5)
    f.get(f"{BASE}/search?q=a")
    f.get(f"{BASE}/search?q=b")
    assert waits and all(w > 0 for w in waits)


def apollo_page(ads, page_count=1, promoted=()):
    """Página SINTÉTICA con la forma real del estado Apollo de Revolico (teléfonos ficticios)."""
    state = {
        "ProvinceType:1": {"__typename": "ProvinceType", "id": "1", "name": "La Habana", "slug": "la-habana"},
        "MunicipalityType:34": {"__typename": "MunicipalityType", "id": "34", "name": "Arroyo Naranjo"},
    }
    edges = []
    for ad in ads:
        state[f"AdType:{ad['id']}"] = {"__typename": "AdType", **ad}
        edges.append({"node": {"__ref": f"AdType:{ad['id']}"}})
    promo_edges = []
    for ad in promoted:
        state[f"AdType:{ad['id']}"] = {"__typename": "AdType", **ad}
        promo_edges.append({"node": {"__ref": f"AdType:{ad['id']}"}})
    state["ROOT_QUERY"] = {
        "__typename": "Query",
        'adsPerPage({"contains":"aceite","page":1,"pageLength":100})': {
            "pageInfo": {"hasNextPage": page_count > 1, "pageCount": page_count}, "edges": edges},
        'promotedAds({"contains":"aceite","first":20})': {"edges": promo_edges},
    }
    payload = {"props": {"pageProps": {"__APOLLO_STATE__": state}}}
    return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'


def fake_ad(i, phone="50000001", **kw):
    ad = {"id": str(i), "title": f"Aceite Girasol 1lt #{i}", "price": 4.6, "currency": "USD",
          "permalink": f"/item/aceite-{i}", "updatedOnToOrder": "2026-09-13T09:20:53.391Z",
          "provinceId": "1", "municipalityId": "34", "viewCount": 20, "isPromoted": False,
          "mainImage": {"gcsKey": f"pics/{i}"},
          "phoneInfo": {"firstPhone": {"prefix": "+53", "number": phone}, "secondPhone": None}}
    ad.update(kw)
    return ad


def test_apollo_parser_real_shape(monkeypatch):
    from controlador_mercado.adapters.revolico import parse_revolico_apollo

    monkeypatch.setenv("CONTROLADOR_SELLER_HASH_KEY", "clave-de-prueba")
    ads = [fake_ad(1), fake_ad(2, phone="50000002", price=None, currency=None), fake_ad(3)]
    obs, info = parse_revolico_apollo(apollo_page(ads, promoted=[fake_ad(9, phone="59999999")]),
                                      base_url=BASE, captured_at=NOW)
    assert info["pageCount"] == 1
    assert [o.listing_id for o in obs] == ["1", "2", "3", "9"]
    first = obs[0]
    assert (first.price, first.currency, first.province, first.municipality) == (4.6, "USD", "La Habana", "Arroyo Naranjo")
    assert first.url == "https://www.revolico.com/item/aceite-1"
    assert first.image_id == "pics/1" and first.published_at.day == 13
    # Anuncio sin precio: se conserva como oferta, sin inventar precio ni moneda.
    assert obs[1].price is None and obs[1].currency is None
    # Vendedor seudónimo estable; los teléfonos no se guardan.
    assert obs[0].seller == obs[2].seller != obs[1].seller
    assert obs[0].seller.startswith("vendedor-")
    dumped = json.dumps([o.raw for o in obs])
    assert "50000001" not in dumped and "phoneInfo" not in dumped
    assert obs[3].raw["is_promoted"] is True and obs[0].raw["view_count"] == 20


def test_source_paginates_with_page_count():
    def search(url):
        page = int(url.split("page=")[1].split("&")[0]) if "page=" in url else 1
        ads = [fake_ad(100 + page)]
        return HttpResponse(url, 200, apollo_page(ads, page_count=2))

    f, t = fetcher({f"{BASE}/robots.txt": HttpResponse("", 200, ROBOTS), f"{BASE}/search": search})
    src = RevolicoSource(fetcher=f, max_pages=5, now_fn=lambda: NOW)
    obs = src.fetch(TargetProduct(name="aceite de girasol", provinces=["La Habana"]), None, None)
    assert [o.listing_id for o in obs] == ["101", "102"]
    searches = [url for url, _ in t.calls if "/search" in url]
    assert len(searches) == 2  # se detiene en pageCount
    assert "province=la-habana" in searches[0] and "q=aceite+de+girasol" in searches[0]
    assert "page=2" in searches[1]


def test_registry_records_block_as_failed_source():
    f, _ = fetcher({f"{BASE}/robots.txt": HttpResponse("", 200, ROBOTS),
                    f"{BASE}/search": HttpResponse("", 403, CHALLENGE)})
    res = SourceRegistry([RevolicoSource(fetcher=f)]).fetch_all(TargetProduct(name="aceite"))
    assert res[0].status == "error" and "SourceBlockedError" in res[0].error
    assert res[0].observations == []


def test_auth_headers_are_sent(monkeypatch):
    monkeypatch.setenv("REVOLICO_AUTH_HEADERS", json.dumps({"Authorization": "Bearer acuerdo"}))
    t = FakeTransport({f"{BASE}/robots.txt": HttpResponse("", 200, ROBOTS),
                       f"{BASE}/search": HttpResponse("", 200, next_data_page([]))})
    src = RevolicoSource(now_fn=lambda: NOW)
    src.fetcher.transport = t
    src.fetcher._sleep = lambda s: None
    src.fetch(TargetProduct(name="aceite"), None, None)
    assert all(h.get("Authorization") == "Bearer acuerdo" for _, h in t.calls)


@pytest.mark.parametrize("path,allowed", [
    ("/search?q=aceite", True),
    ("/checkout/pago", False),
    ("/account", False),
    ("/accounting-tips", False),  # prefijo: /account también cubre /accounting…
    ("/favorites/1", False),
    ("/compra-venta", True),
])
def test_robots_longest_match_with_real_revolico_rules(path, allowed):
    from controlador_mercado.adapters.http import RobotsRules
    real = ("User-agent: *\nAllow: /\nDisallow: /checkout/\nDisallow: /cdn-cgi/\nDisallow: /account\n"
            "Disallow: /auth\nDisallow: /favorites/")
    assert RobotsRules(real).can_fetch("AgenteControladorMercado/0.1", BASE + path) is allowed


def test_robots_wildcards():
    from controlador_mercado.adapters.http import RobotsRules
    rules = RobotsRules("User-agent: *\nDisallow: /*carro$\nDisallow: /api/\nAllow: /api/publico")
    assert not rules.can_fetch("x", "https://s.com/es/carro")
    assert rules.can_fetch("x", "https://s.com/es/carro/ver")
    assert not rules.can_fetch("x", "https://s.com/api/privado")
    assert rules.can_fetch("x", "https://s.com/api/publico/1")


def test_seller_key_is_persisted_between_runs(monkeypatch, tmp_path):
    from controlador_mercado.adapters import revolico

    monkeypatch.delenv("CONTROLADOR_SELLER_HASH_KEY", raising=False)
    monkeypatch.setenv("CONTROLADOR_CACHE_DIR", str(tmp_path))
    first = revolico._seller_key()
    assert first == revolico._seller_key()
    assert (tmp_path / "seller_hash_key").read_text().strip().encode() == first
