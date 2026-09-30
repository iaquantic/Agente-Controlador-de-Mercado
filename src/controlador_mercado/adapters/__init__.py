"""Adaptadores web para fuentes de mercado."""

from .http import PoliteFetcher, RobotsDisallowedError, SourceBlockedError
from .cubatel import ConsentRequiredError, CubatelSource
from .revolico import RevolicoSource

__all__ = ["ConsentRequiredError", "CubatelSource", "PoliteFetcher", "RevolicoSource", "RobotsDisallowedError", "SourceBlockedError"]
