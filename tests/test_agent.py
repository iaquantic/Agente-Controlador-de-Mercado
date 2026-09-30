"""Pruebas del bucle del agente con un cliente simulado (sin llamadas a la API)."""

import json
from types import SimpleNamespace

import pytest

from controlador_mercado.agent import TOOLS, AgentError, MarketControllerAgent
from controlador_mercado.models import Observation
from controlador_mercado.sources import SourceAdapter, SourceRegistry

from .conftest import NOW, make_obs


class ListSource(SourceAdapter):
    def __init__(self, source_id, observations):
        self.source_id = source_id
        self.source_name = f"Fuente {source_id}"
        self._obs = observations

    def fetch(self, target, since, until):
        return list(self._obs)


class BrokenSource(SourceAdapter):
    source_id = "rota"

    def fetch(self, target, since, until):
        raise TimeoutError("sin respuesta")


def text_block(text):
    return SimpleNamespace(type="text", text=text)


def tool_block(tool_id, name, input_):
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=input_)


def response(content, stop):
    return SimpleNamespace(content=content, stop_reason=stop, model="modelo-simulado", stop_details=None,
                           usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0))


class FakeClient:
    """Devuelve respuestas programadas; la segunda depende del analysis_id devuelto."""

    def __init__(self, script):
        self.script = script
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **params):
        self.calls.append(params)
        return self.script(len(self.calls), params)


def _last_tool_result(params):
    return params["messages"][-1]["content"][0]


def _registry():
    obs = [make_obs(i, price=1000 + i * 10, source_id="s1") for i in range(12)]
    return SourceRegistry([ListSource("s1", obs), BrokenSource()])


def _agent(script, **kw):
    return MarketControllerAgent(_registry(), client=FakeClient(script), now_fn=lambda: NOW, **kw)


PRODUCT = {"name": "aceite de girasol", "brand": "Ole", "quantity": 1, "unit": "L"}


def final_json(aid, **item):
    body = {
        "resumen": "Mediana observada 1055 CUP.",
        "analisis": [{
            "analysis_id": aid,
            "conclusiones": [{"tipo": "ESTADISTICA_CALCULADA", "texto": "Mediana 1055 CUP (n=12)."}],
            "advertencias": [],
            "confianza_ajustada": None,
            "motivo_ajuste_confianza": None,
            **item,
        }],
        "solicitudes_no_resueltas": [],
    }
    return json.dumps(body, ensure_ascii=False)


def test_full_loop_and_merge():
    def script(n, params):
        if n == 1:
            return response([tool_block("t1", "analizar_producto", {"product": PRODUCT})], "tool_use")
        result = json.loads(_last_tool_result(params)["content"])
        return response([text_block(final_json(result["analysis_id"]))], "end_turn")

    agent = _agent(script)
    report = agent.run("Analiza el aceite de girasol Ole 1 L").to_dict()
    assert len(report["analyses"]) == 1
    analysis = report["analyses"][0]
    assert analysis["market_summary"]["conclusions"][0]["type"] == "ESTADISTICA_CALCULADA"
    # La fuente rota se registra, no se inventa.
    rota = next(s for s in analysis["sources"] if s["source_id"] == "rota")
    assert rota["status"] == "error" and "TimeoutError" in rota["error"]
    assert analysis["confidence"] != "HIGH"
    # El prompt del sistema se envía con caché y las herramientas son las declaradas.
    first = agent.client.calls[0]
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert first["tools"] is TOOLS
    assert first["thinking"] == {"type": "adaptive"}
    assert first["fallbacks"] == "default"
    assert "tool_choice" not in first


def test_model_cannot_raise_confidence_and_decision_language_flagged():
    def script(n, params):
        if n == 1:
            return response([tool_block("t1", "analizar_producto", {"product": PRODUCT})], "tool_use")
        aid = json.loads(_last_tool_result(params)["content"])["analysis_id"]
        return response([text_block(final_json(
            aid, confianza_ajustada="HIGH",
            conclusiones=[{"tipo": "INFERENCIA", "texto": "El negocio debería comprar 100 unidades."}],
        ))], "end_turn")

    report = _agent(script).run("x")
    analysis = report.analyses[0]
    assert analysis["confidence"] != "HIGH"
    assert any("elevar la confianza" in w for w in report.warnings)
    assert "boundary_warning" in analysis["market_summary"]["conclusions"][0]


def test_model_can_lower_confidence():
    def script(n, params):
        if n == 1:
            return response([tool_block("t1", "analizar_producto", {"product": PRODUCT})], "tool_use")
        aid = json.loads(_last_tool_result(params)["content"])["analysis_id"]
        return response([text_block(final_json(aid, confianza_ajustada="LOW",
                                               motivo_ajuste_confianza="coincidencias dudosas"))], "end_turn")

    report = _agent(script).run("x")
    assert report.analyses[0]["confidence"] == "LOW"


def test_tool_errors_are_reported_to_model():
    def script(n, params):
        if n == 1:
            return response([tool_block("t1", "inspeccionar_observaciones", {"analysis_id": "an_inexistente"}),
                             tool_block("t2", "obtener_tipo_cambio", {"from_currency": "USD", "to_currency": "CUP"})],
                            "tool_use")
        results = params["messages"][-1]["content"]
        assert len(results) == 2  # todos los resultados en un único mensaje
        assert results[0]["is_error"] is True
        assert json.loads(results[1]["content"])["status"] == "DATO_NO_DISPONIBLE"
        return response([text_block(json.dumps({"resumen": "sin datos", "analisis": [],
                                                "solicitudes_no_resueltas": ["x"]}))], "end_turn")

    agent = _agent(script)
    report = agent.run("x")
    assert report.unresolved == ["x"]
    assert agent.tool_log[0]["ok"] is False


def test_refusal_raises():
    def script(n, params):
        r = response([], "refusal")
        r.stop_details = SimpleNamespace(category="otro", explanation="n/a")
        return r

    with pytest.raises(AgentError):
        _agent(script).run("x")


def test_unauthorized_reference_cost_is_rejected():
    def script(n, params):
        if n == 1:
            return response([tool_block("t1", "analizar_producto",
                                        {"product": PRODUCT, "reference_cost_key": "inventado"})], "tool_use")
        assert _last_tool_result(params)["is_error"] is True
        return response([text_block(json.dumps({"resumen": "", "analisis": [], "solicitudes_no_resueltas": []}))],
                        "end_turn")

    _agent(script).run("x")


def test_observation_trace_available_via_inspection():
    def script(n, params):
        if n == 1:
            return response([tool_block("t1", "analizar_producto", {"product": PRODUCT})], "tool_use")
        if n == 2:
            aid = json.loads(_last_tool_result(params)["content"])["analysis_id"]
            return response([tool_block("t2", "inspeccionar_observaciones",
                                        {"analysis_id": aid, "status": "valida", "limit": 3})], "tool_use")
        payload = json.loads(_last_tool_result(params)["content"])
        assert len(payload["observaciones"]) == 3
        assert "DATOS" in payload["aviso"]
        return response([text_block(json.dumps({"resumen": "", "analisis": [], "solicitudes_no_resueltas": []}))],
                        "end_turn")

    _agent(script).run("x")


def test_observation_model_is_not_mutated_by_registry():
    obs = Observation.from_dict({"source_id": "s", "title": "Aceite de girasol Ole 1 L", "price": "1000"})
    assert obs.raw["price"] == "1000"
