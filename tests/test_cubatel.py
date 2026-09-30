"""Pruebas del adaptador de Cubatel con páginas SINTÉTICAS (misma forma JSON-LD que el sitio)."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from controlador_mercado import MarketAnalyzer
from controlador_mercado.adapters.cubatel import ConsentRequiredError, CubatelSource, parse_cubatel_product
from controlador_mercado.adapters.http import HttpResponse, PoliteFetcher
from controlador_mercado.adapters.store import SnapshotStore
from controlador_mercado.models import TargetProduct
from controlador_mercado.sources import SourceRegistry

BASE = "https://www.cubatel.com"
SITEMAP = f"{BASE}/market-products-sitemap.xml"
ROBOTS = "User-agent: *\nDisallow: /blog/\nDisallow: /perfil/\n"
T0 = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)


def product_page(sku, name, price, availability="InStock", seller="Mercadito XL"):
    ld = {"@context": "https://schema.org", "@type": "Product", "name": name, "sku": sku,
          "brand": {"@type": "Brand", "name": f"Vendido por {seller}"},
          "offers": {"@type": "Offer", "priceCurrency": "USD", "price": price,
                     "availability": f"https://schema.org/{availability}",
                     "itemCondition": "https://schema.org/NewCondition"}}
    return f'<script type="application/ld+json">{json.dumps(ld)}</script>'


def sitemap(skus):
    locs = "".join(f"<url><loc>{BASE}/market/productos-compras-cuba-online/cubatel/{s}</loc></url>" for s in skus)
    return f'<?xml version="1.0"?><urlset>{locs}</urlset>'


class Site:
    """Sitio simulado cuyo catálogo y precios pueden cambiar entre ejecuciones."""

    def __init__(self, products):
        self.products = dict(products)  # sku -> (nombre, precio, disponibilidad)
        self.calls = []

    def __call__(self, url, headers, timeout):
        self.calls.append(url)
        if url.endswith("/robots.txt"):
            return HttpResponse(url, 200, ROBOTS)
        if url == SITEMAP:
            return HttpResponse(url, 200, sitemap(self.products))
        sku = url.rsplit("/", 1)[-1]
        if sku in self.products:
            name, price, avail = self.products[sku]
            return HttpResponse(url, 200, product_page(sku, name, price, avail))
        return HttpResponse(url, 404, "")


def make_source(site, tmp_path, clock, **kw):
    fetcher = PoliteFetcher(transport=site, sleep=lambda s: None, min_delay_seconds=0)
    return CubatelSource(consent_reference="CONSENT-2026-001", fetcher=fetcher,
                         store=SnapshotStore(tmp_path), now_fn=lambda: clock[0], **kw)


def test_parse_product_jsonld():
    rec = parse_cubatel_product(product_page("9c57dc3a", "Arroz Importado (1 kg)", 1.8), "u", T0)
    assert (rec["title"], rec["price"], rec["currency"]) == ("Arroz Importado (1 kg)", 1.8, "USD")
    assert rec["seller"] == "Mercadito XL" and rec["brand"] is None
    assert rec["availability"] == "disponible" and rec["condition"] == "nuevo"
    oos = parse_cubatel_product(product_page("x", "Aceite 1 L", 4, "OutOfStock"), "u", T0)
    assert oos["availability"] == "agotado"
    assert parse_cubatel_product("<html>sin datos</html>", "u", T0) is None


def test_requires_written_consent(tmp_path, monkeypatch):
    monkeypatch.delenv("CUBATEL_CONSENT_REF", raising=False)
    site = Site({"a": ("Arroz 1 kg", 1.8, "InStock")})
    src = CubatelSource(fetcher=PoliteFetcher(transport=site, min_delay_seconds=0), store=SnapshotStore(tmp_path))
    with pytest.raises(ConsentRequiredError):
        src.fetch(TargetProduct(name="arroz"), None, None)
    assert site.calls == []  # ni siquiera se consulta el sitio
    res = SourceRegistry([src]).fetch_all(TargetProduct(name="arroz"))
    assert res[0].status == "error" and "consentimiento" in res[0].error


def test_cache_ttl_and_fetch_limit(tmp_path):
    clock = [T0]
    site = Site({s: (f"Producto {s}", 1.0, "InStock") for s in "abcde"})
    src = make_source(site, tmp_path, clock, max_fetches_per_run=3)
    assert src.refresh()["fetched"] == 3 and src.last_run["pending"] == 2
    clock[0] = T0 + timedelta(hours=1)
    stats = src.refresh()
    assert stats["fetched"] == 2 and stats["cached"] == 3  # solo lo pendiente
    before = len(site.calls)
    clock[0] = T0 + timedelta(hours=2)
    assert src.refresh()["fetched"] == 0
    assert site.calls[before:] == [SITEMAP]  # todo en caché: solo se consulta el sitemap
    clock[0] = T0 + timedelta(hours=30)
    assert src.refresh()["fetched"] == 3  # caducados, respetando el límite


def test_removed_products_leave_catalog_but_keep_history(tmp_path):
    clock = [T0]
    site = Site({"a": ("Arroz 1 kg", 1.8, "InStock"), "b": ("Aceite 1 L", 4.0, "InStock")})
    src = make_source(site, tmp_path, clock)
    src.refresh()
    del site.products["b"]
    clock[0] = T0 + timedelta(days=2)
    assert src.refresh()["removed"] == 1
    assert not any(k.endswith("/b") for k in src.store.catalog)
    assert any(r["listing_id"] == "b" for r in src.store.history())


def test_history_feeds_price_trend(tmp_path):
    clock = [T0]
    site = Site({f"s{i}": (f"Aceite de girasol 1 L tienda {i}", 4.0, "InStock") for i in range(6)})
    src = make_source(site, tmp_path, clock)
    for week in range(4):
        clock[0] = T0 + timedelta(weeks=week)
        for sku in site.products:
            name, _, avail = site.products[sku]
            site.products[sku] = (name, round(4.0 * (1 + 0.1 * week), 2), avail)
        src.refresh()
    target = TargetProduct(name="aceite de girasol", quantity=1, unit="L")
    now = T0 + timedelta(weeks=3, days=1)
    obs = src.fetch(target, None, now)
    res = MarketAnalyzer().analyze(target, obs, now=now, analysis_start=now - timedelta(days=7))
    assert res["price_statistics"]["by_currency"]["USD"]["presentation_price"]["n"] == 6  # sin duplicar capturas
    assert res["trends"]["price_trend"]["USD"]["classification"] == "TENDENCIA"
    assert any(s["type"] == "PRICE_INCREASE" for s in res["market_signals"])
