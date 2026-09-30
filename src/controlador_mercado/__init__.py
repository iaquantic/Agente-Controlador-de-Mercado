"""Agente Controlador de Mercado: inteligencia de mercado externa para el mercado cubano."""

__version__ = "0.1.0"

from .analyzer import AnalyzerConfig, MarketAnalyzer  # noqa: E402
from .models import ExchangeRate, MatchLevel, Observation, ReferenceCost, TargetProduct  # noqa: E402
from .sources import ExchangeRateProvider, JsonFileSource, SourceAdapter, SourceRegistry  # noqa: E402

__all__ = [
    "AnalyzerConfig",
    "ExchangeRate",
    "ExchangeRateProvider",
    "JsonFileSource",
    "MarketAnalyzer",
    "MatchLevel",
    "Observation",
    "ReferenceCost",
    "SourceAdapter",
    "SourceRegistry",
    "TargetProduct",
    "__version__",
]
