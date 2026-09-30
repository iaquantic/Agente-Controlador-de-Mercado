import pytest

from controlador_mercado.dedup import deduplicate
from controlador_mercado.stats import concentration, detect_outliers, price_summary
from controlador_mercado.temporal import classify_series

from .conftest import make_obs


def test_dedup_same_url_keeps_one():
    a = make_obs(1, url="https://www.site.cu/anuncio/1?utm_source=x")
    b = make_obs(2, url="http://site.cu/anuncio/1", seller="otro")
    res = deduplicate([a, b])
    assert len(res.unique) == 1
    assert "misma URL" in next(iter(res.duplicates.values())).reasons


def test_dedup_replicated_listing():
    a = make_obs(1, seller="Tienda X")
    b = make_obs(2, seller="tienda x", source_id="s2")
    assert len(deduplicate([a, b]).unique) == 1


def test_weak_duplicate_is_flagged_not_removed():
    a = make_obs(1, seller="A")
    b = make_obs(2, seller="B")
    res = deduplicate([a, b])
    assert len(res.unique) == 2
    assert res.possible_duplicates


def test_outliers_flagged():
    values = [1000, 1010, 990, 1005, 995, 9000]
    flags, method = detect_outliers(values)
    assert method == "mad_zscore"
    assert [f.index for f in flags] == [5]
    assert flags[0].direction == "ALTO"


def test_outliers_not_evaluated_on_tiny_sample():
    flags, method = detect_outliers([10, 1000])
    assert flags == [] and method.startswith("no_evaluado")


def test_close_prices_not_flagged():
    flags, _ = detect_outliers([1080, 1085, 1090, 1085, 900])
    assert flags == []


def test_price_summary():
    s = price_summary([100, 200, 300, 400])
    assert s["price_median"] == 250
    assert s["price_range"] == 300
    assert s["price_spread_pct"] == 120.0
    assert price_summary([])["price_median"] is None


def test_concentration():
    c = concentration(["a", "a", "a", "b", None])
    assert c["top_seller_share_pct"] == 75.0
    assert c["level"] == "ALTA"
    assert c["listings_without_seller"] == 1
    assert concentration(["a"])["hhi"] is None


@pytest.mark.parametrize("series,expected", [
    ([100], "INSUFICIENTE"),
    ([100, 120], "CAMBIO_PUNTUAL"),
    ([100, 103], "ESTABLE"),
    ([100, 110, 120, 130], "TENDENCIA"),
    ([100, 101, 99, 125], "MOVIMIENTO_RECIENTE"),
    ([0, 0, 5], "APARICION"),
    ([5, 4, 0], "DESAPARICION"),
])
def test_classify_series(series, expected):
    assert classify_series(series, 10.0)["classification"] == expected


def test_trend_direction():
    res = classify_series([130, 120, 110, 100], 10.0)
    assert res["direction"] == "BAJA"
    assert res["change_pct_total"] == pytest.approx(-23.1)
