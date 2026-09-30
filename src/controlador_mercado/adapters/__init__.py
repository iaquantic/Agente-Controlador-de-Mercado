"""Adaptadores web para fuentes de mercado."""

from .http import PoliteFetcher, RobotsDisallowedError, SourceBlockedError
from .cuballama import CuballamaSource
from .cubamax import CubamaxSource
from .cubatel import ConsentRequiredError, CubatelSource
from .revolico import RevolicoSource

__all__ = ["ConsentRequiredError", "CuballamaSource", "CubamaxSource", "CubatelSource", "PoliteFetcher", "RevolicoSource", "RobotsDisallowedError", "SourceBlockedError"]
