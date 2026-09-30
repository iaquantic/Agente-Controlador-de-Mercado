"""Interfaz de línea de comandos.

    controlador-mercado analizar --producto '{"name": "aceite de girasol", "quantity": 1, "unit": "L"}' \
        --fuente datos/revolico.jsonl:revolico --inicio 2026-09-23 --salida informe.json

    controlador-mercado agente "Analiza el aceite de girasol de 1 L en La Habana" --fuente datos/
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path

from .analyzer import AnalyzerConfig, MarketAnalyzer
from .models import ReferenceCost, TargetProduct, parse_datetime
from .sources import ExchangeRateProvider, JsonFileSource, SourceRegistry


def _registry(specs: list[str] | None, web: list[str] | None = None) -> SourceRegistry:
    registry = SourceRegistry()
    for spec in specs or []:
        path, _, source_id = spec.partition(":")
        registry.add(JsonFileSource(path, source_id=source_id or None))
    for name in web or []:
        if name == "revolico":
            from .adapters import RevolicoSource

            registry.add(RevolicoSource())
    if not registry.adapters:
        raise SystemExit("Indica al menos una fuente: --fuente o --web.")
    return registry


def _write(data: dict, out: str | None) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    if out:
        Path(out).write_text(text + "\n", encoding="utf-8")
        print(f"Informe guardado en {out}", file=sys.stderr)
    else:
        print(text)


def cmd_analizar(args: argparse.Namespace) -> int:
    product = json.loads(Path(args.producto[1:]).read_text("utf-8")) if args.producto.startswith("@") \
        else json.loads(args.producto)
    target = TargetProduct.from_dict(product)
    registry = _registry(args.fuente, args.web)
    end = parse_datetime(args.fin)
    start = parse_datetime(args.inicio)
    history_start = parse_datetime(args.historial_desde)
    if history_start is None and end is not None:
        history_start = end - timedelta(weeks=12)
    fetched = registry.fetch_all(target, history_start, end)
    rates = ExchangeRateProvider.from_file(args.tipos_cambio) if args.tipos_cambio else ExchangeRateProvider()
    cost = None
    if args.coste_referencia:
        amount, currency = args.coste_referencia.split(":", 1)
        cost = ReferenceCost(float(amount), currency.upper(), source="linea_de_comandos")
    result = MarketAnalyzer(AnalyzerConfig(granularity=args.granularidad)).analyze(
        target, [o for f in fetched for o in f.observations],
        analysis_start=start, analysis_end=end, now=parse_datetime(args.ahora),
        exchange_rates=rates.rates, convert_to=args.convertir_a, reference_cost=cost,
        source_status=[f.status_dict() for f in fetched],
    )
    if args.sin_traza:
        result.pop("observations")
    _write(result, args.salida)
    return 0


def cmd_agente(args: argparse.Namespace) -> int:
    from .agent import AgentError, MarketControllerAgent

    rates = ExchangeRateProvider.from_file(args.tipos_cambio) if args.tipos_cambio else ExchangeRateProvider()
    agent = MarketControllerAgent(_registry(args.fuente, args.web), rate_provider=rates, model=args.modelo, effort=args.esfuerzo)
    try:
        report = agent.run(args.solicitud)
    except AgentError as exc:
        print(f"Error del agente: {exc}", file=sys.stderr)
        _write({"error": str(exc), "tool_log": agent.tool_log}, args.salida)
        return 2
    _write(report.to_dict(include_traces=not args.sin_traza), args.salida)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="controlador-mercado", description="Agente Controlador de Mercado")
    sub = parser.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--fuente", action="append",
                        help="Archivo o directorio JSON/JSONL autorizado, opcionalmente 'ruta:source_id'. Repetible.")
    common.add_argument("--web", action="append", choices=["revolico"],
                        help="Adaptador web a consultar en vivo. Repetible.")
    common.add_argument("--tipos-cambio", help="JSON con tipos de cambio autorizados y fechados.")
    common.add_argument("--salida", help="Archivo de salida (por defecto, stdout).")
    common.add_argument("--sin-traza", action="store_true", help="Omitir la traza por observación.")

    a = sub.add_parser("analizar", parents=[common], help="Análisis determinista (sin LLM).")
    a.add_argument("--producto", required=True, help="JSON del producto objetivo o @archivo.json.")
    a.add_argument("--inicio", help="Inicio del periodo (ISO-8601).")
    a.add_argument("--fin", help="Fin del periodo (ISO-8601).")
    a.add_argument("--historial-desde", help="Inicio del histórico para tendencias (ISO-8601).")
    a.add_argument("--ahora", help="Momento del análisis (ISO-8601); por defecto, ahora.")
    a.add_argument("--granularidad", default="week", choices=["day", "week", "month"])
    a.add_argument("--convertir-a", help="Moneda destino (requiere --tipos-cambio).")
    a.add_argument("--coste-referencia", help="Coste autorizado 'importe:MONEDA' para simular margen potencial.")
    a.set_defaults(func=cmd_analizar)

    g = sub.add_parser("agente", parents=[common], help="Agente completo sobre la Claude API.")
    g.add_argument("solicitud", help="Solicitud del Agente Central.")
    g.add_argument("--modelo", default=None)
    g.add_argument("--esfuerzo", default=None, choices=["low", "medium", "high", "xhigh", "max"])
    g.set_defaults(func=cmd_agente)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "modelo", "x") is None:
        from .agent import DEFAULT_MODEL
        args.modelo = DEFAULT_MODEL
    if getattr(args, "esfuerzo", "x") is None:
        from .agent import DEFAULT_EFFORT
        args.esfuerzo = DEFAULT_EFFORT
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
