from controlador_mercado.matching import PresentationStatus, match_observation
from controlador_mercado.models import MatchLevel, Observation, TargetProduct


def obs(title, **kw):
    return Observation.from_dict({"source_id": "s", "title": title, **kw})


COCA = TargetProduct(name="refresco cola", brand="Coca-Cola", quantity=330, unit="ml")


def test_exact_match():
    m = match_observation(COCA, obs("Refresco Coca Cola lata 330 ml"))
    assert m.level == MatchLevel.EXACT
    assert m.presentation_status == PresentationStatus.MISMA


def test_different_presentation_is_not_exact():
    m = match_observation(COCA, obs("Refresco Coca-Cola cola 1,5 L"))
    assert m.level == MatchLevel.MEDIUM
    assert m.presentation_status == PresentationStatus.DIFERENTE


def test_brand_conflict_is_no_match():
    m = match_observation(COCA, obs("Refresco cola 330 ml", brand="Tukola"))
    assert m.level == MatchLevel.NO_MATCH


def test_brand_not_verifiable_caps_at_high():
    m = match_observation(COCA, obs("Refresco cola 330 ml"))
    assert m.level == MatchLevel.HIGH


def test_unknown_presentation_caps_at_medium():
    m = match_observation(COCA, obs("Refresco Coca Cola"))
    assert m.level == MatchLevel.MEDIUM
    assert m.presentation_status == PresentationStatus.DESCONOCIDA


def test_dimension_mismatch_is_no_match():
    target = TargetProduct(name="arroz", quantity=1, unit="kg")
    m = match_observation(target, obs("Arroz 1 L"))
    assert m.level == MatchLevel.NO_MATCH


def test_exclude_keywords():
    target = TargetProduct(name="aceite", quantity=1, unit="L", exclude_keywords=["motor"])
    assert match_observation(target, obs("Aceite de motor 1 L")).level == MatchLevel.NO_MATCH


def test_low_coverage():
    target = TargetProduct(name="leche en polvo entera", quantity=1, unit="kg")
    assert match_observation(target, obs("Polvo de hornear 1 kg")).level.rank <= MatchLevel.LOW.rank


def test_condition_mismatch():
    target = TargetProduct(name="split inverter", brand="Royal", condition="nuevo")
    m = match_observation(target, obs("Split Royal inverter", condition="usado"))
    assert m.level == MatchLevel.LOW


def test_target_without_presentation_cannot_be_exact():
    target = TargetProduct(name="aceite de girasol")
    m = match_observation(target, obs("Aceite de girasol 1 L"))
    assert m.level == MatchLevel.HIGH
    assert m.presentation_status == PresentationStatus.NO_ESPECIFICADA


def test_missing_defining_term_is_low():
    target = TargetProduct(name="aceite de girasol", quantity=1, unit="L")
    assert match_observation(target, obs("Aceite capilar de romero 30 ml")).level == MatchLevel.LOW
    assert match_observation(target, obs("Aceite de girasol 900ml")).level == MatchLevel.MEDIUM


def test_one_missing_term_of_many_is_medium_or_high():
    target = TargetProduct(name="leche en polvo entera instantanea", quantity=1, unit="kg")
    assert match_observation(target, obs("Leche en polvo entera 1 kg")).level == MatchLevel.HIGH
    assert match_observation(target, obs("Leche en polvo 1 kg")).level == MatchLevel.LOW
