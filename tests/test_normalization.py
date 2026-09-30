import pytest

from controlador_mercado.models import Observation
from controlador_mercado.normalization import (
    build_presentation,
    normalize_availability,
    normalize_currency,
    parse_pack_count,
    parse_size,
    same_presentation,
)


@pytest.mark.parametrize("raw,expected", [
    ("CUP", "CUP"), ("mn", "CUP"), ("Peso cubano", "CUP"), ("usd", "USD"), ("US$", "USD"),
    ("MLC", "MLC"), ("€", "EUR"), ("$", None), ("", None), (None, None), ("xyz", None),
])
def test_normalize_currency(raw, expected):
    assert normalize_currency(raw) == expected


def test_parse_size_variants():
    assert parse_size("Coca-Cola 1,5 L") == (1.5, "l")
    assert parse_size("Refresco 330ml lata") == (330.0, "ml")
    assert parse_size("Arroz 5 libras") == (5.0, "libras")
    assert parse_size("Pollo sin tamaño") is None


def test_pack_count():
    assert parse_pack_count("Cerveza Cristal 24 x 355 ml") == 24
    assert parse_pack_count("Caja de 12 latas") == 12
    assert parse_pack_count("Huevos 30 u") == 30
    assert parse_pack_count("Aceite 1 L") is None


def test_presentation_standardization():
    pack = build_presentation(None, None, None, "Refresco Tukola 6 x 330 ml")
    assert pack.pack_count == 6
    assert pack.standard_unit == "L"
    assert pack.standard_quantity == pytest.approx(1.98)
    lb = build_presentation(5, "lb", None)
    assert lb.standard_unit == "kg"
    assert lb.standard_quantity == pytest.approx(2.26796185)
    unknown = build_presentation(None, None, None, "Aceite de girasol")
    assert not unknown.known


def test_same_presentation():
    a = build_presentation(1.5, "L", None)
    b = build_presentation(1500, "ml", None)
    c = build_presentation(330, "ml", None)
    d = build_presentation(1, "kg", None)
    assert same_presentation(a, b) is True
    assert same_presentation(a, c) is False
    assert same_presentation(a, d) is False
    assert same_presentation(a, build_presentation(None, None, None)) is None


def test_availability():
    assert normalize_availability("Agotado") == "AGOTADO"
    assert normalize_availability("Disponible") == "DISPONIBLE"
    assert normalize_availability("consultar") is None


def test_observation_parses_localized_price():
    obs = Observation.from_dict({"source_id": "x", "title": "t", "price": "1.500,50"})
    assert obs.price == 1500.50
    assert obs.raw["price"] == "1.500,50"


@pytest.mark.parametrize("title,std_qty,unit", [
    ("GALON DE ACEITE GIRASOL (17.3 LITROS) PRECIO : 4.40 USD EL LITRO", 1.0, "L"),
    ("Carne de cerdo 350 CUP/lb", 0.45359237, "kg"),
    ("Queso a 900 la libra", 0.45359237, "kg"),
])
def test_price_per_unit_in_text(title, std_qty, unit):
    from controlador_mercado.normalization import observation_presentation

    pres = observation_presentation(Observation.from_dict({"source_id": "x", "title": title}))
    assert pres.origin == "texto_precio_por_unidad"
    assert pres.standard_quantity == pytest.approx(std_qty) and pres.standard_unit == unit


def test_package_size_is_not_price_basis():
    from controlador_mercado.normalization import observation_presentation

    pres = observation_presentation(Observation.from_dict({"source_id": "x", "title": "Arroz 5 libras"}))
    assert pres.origin == "texto" and pres.standard_quantity == pytest.approx(2.26796185)


@pytest.mark.parametrize("title,expected", [
    ("Aceite vegetal puro Mazeite (1. 89 l)", (1.89, "l")),
    ("aceite vegetal puro – 48 oz (1, 42 litros)", (1.42, "litros")),
    ("Aceite 17, 3 l", (17.3, "l")),
    ("Aceite x 3, 900 ml", (900.0, "ml")),
])
def test_spaced_decimals(title, expected):
    assert parse_size(title) == expected
