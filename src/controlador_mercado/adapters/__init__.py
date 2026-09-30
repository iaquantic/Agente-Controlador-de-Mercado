"""Adaptadores web para fuentes de mercado."""

from .http import PoliteFetcher, RobotsDisallowedError, SourceBlockedError
from .revolico import RevolicoSource

__all__ = ["PoliteFetcher", "RevolicoSource", "RobotsDisallowedError", "SourceBlockedError"]
