"""Cliente HTTP respetuoso para adaptadores web.

- Consulta robots.txt antes de cada ruta y no accede a lo que prohíbe.
- Espacia las peticiones (``min_delay_seconds``) y se identifica con un User-Agent propio.
- Detecta desafíos anti-bot (Cloudflare, etc.) y los notifica como ``SourceBlockedError``.
  NO intenta resolverlos ni evadirlos: el acceso debe estar autorizado por el sitio.
"""

from __future__ import annotations

import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlsplit

DEFAULT_USER_AGENT = os.environ.get(
    "CONTROLADOR_USER_AGENT", "AgenteControladorMercado/0.1 (+analisis de mercado; contacto en la configuracion)"
)

_CHALLENGE_MARKERS = (
    "just a moment...",
    "un momento…",
    "attention required! | cloudflare",
    "cf-chl-",
    "challenge-platform",
    "cf_chl_opt",
)


class RobotsRules:
    """robots.txt según RFC 9309: gana la regla más larga que coincide; ante empate, Allow.

    (``urllib.robotparser`` aplica la primera regla que coincide, lo que con
    "Allow: /" al principio permitiría rutas que el sitio prohíbe.)
    """

    def __init__(self, text: str) -> None:
        self.groups: list[tuple[list[str], list[tuple[bool, str]]]] = []
        agents: list[str] = []
        rules: list[tuple[bool, str]] = []
        last_was_agent = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = (x.strip() for x in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if not last_was_agent and agents:
                    self.groups.append((agents, rules))
                    agents, rules = [], []
                agents.append(value.lower())
                last_was_agent = True
            elif key in ("allow", "disallow"):
                last_was_agent = False
                if agents and value:
                    rules.append((key == "allow", value))
        if agents:
            self.groups.append((agents, rules))

    @staticmethod
    def _matches(pattern: str, path: str) -> bool:
        regex = re.escape(pattern).replace(r"\*", ".*")
        if regex.endswith(r"\$"):
            regex = regex[:-2] + "$"
        return re.match(regex, path) is not None

    def can_fetch(self, user_agent: str, url: str) -> bool:
        parts = urlsplit(url)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        ua = user_agent.lower()
        specific = [r for agents, r in self.groups if any(a != "*" and a in ua for a in agents)]
        rules = specific[0] if specific else next((r for agents, r in self.groups if "*" in agents), [])
        best: tuple[int, bool] | None = None
        for allow, pattern in rules:
            if self._matches(pattern, path):
                cand = (len(pattern), allow)
                if best is None or cand[0] > best[0] or (cand[0] == best[0] and allow):
                    best = cand
        return True if best is None else best[1]


class SourceBlockedError(RuntimeError):
    """El sitio bloquea el acceso automatizado (desafío anti-bot o 401/403)."""


class RobotsDisallowedError(RuntimeError):
    """robots.txt del sitio prohíbe la ruta solicitada."""


@dataclass
class HttpResponse:
    url: str
    status: int
    text: str
    headers: dict[str, str] = field(default_factory=dict)


Transport = Callable[[str, dict[str, str], float], HttpResponse]


def urllib_transport(url: str, headers: dict[str, str], timeout: float) -> HttpResponse:
    """Transporte por defecto (stdlib); respeta HTTPS_PROXY y la CA del sistema."""
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode(resp.headers.get_content_charset() or "utf-8", errors="replace")
            return HttpResponse(resp.geturl(), resp.status, body, dict(resp.headers.items()))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        return HttpResponse(url, exc.code, body, dict(exc.headers.items()) if exc.headers else {})


def looks_like_challenge(resp: HttpResponse) -> bool:
    head = resp.text[:20000].lower()
    if resp.status in (403, 429, 503) and any(m in head for m in _CHALLENGE_MARKERS):
        return True
    return resp.headers.get("cf-mitigated", "").lower() == "challenge"


class PoliteFetcher:
    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        extra_headers: dict[str, str] | None = None,
        min_delay_seconds: float = 5.0,
        timeout: float = 30.0,
        respect_robots: bool = True,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.user_agent = user_agent
        self.extra_headers = dict(extra_headers or {})
        self.min_delay_seconds = min_delay_seconds
        self.timeout = timeout
        self.respect_robots = respect_robots
        self.transport = transport or urllib_transport
        self._sleep = sleep
        self._clock = clock
        self._last_request: dict[str, float] = {}
        self._robots: dict[str, RobotsRules | None] = {}

    def _headers(self) -> dict[str, str]:
        return {"User-Agent": self.user_agent, "Accept-Language": "es-ES,es;q=0.9", **self.extra_headers}

    def _throttle(self, host: str) -> None:
        last = self._last_request.get(host)
        if last is not None:
            wait = self.min_delay_seconds - (self._clock() - last)
            if wait > 0:
                self._sleep(wait)
        self._last_request[host] = self._clock()

    def _robots_for(self, url: str) -> RobotsRules | None:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            robots_url = origin + "/robots.txt"
            self._throttle(parts.netloc)
            resp = self.transport(robots_url, self._headers(), self.timeout)
            if resp.status == 200 and not looks_like_challenge(resp):
                self._robots[origin] = RobotsRules(resp.text)
            elif resp.status in (404, 410):
                self._robots[origin] = None  # sin robots.txt: sin restricciones declaradas
            else:
                raise SourceBlockedError(f"no se pudo leer {robots_url} (HTTP {resp.status}); no se continúa")
        return self._robots[origin]

    def get(self, url: str) -> HttpResponse:
        if self.respect_robots:
            robots = self._robots_for(url)
            if robots is not None and not robots.can_fetch(self.user_agent, url):
                raise RobotsDisallowedError(f"robots.txt prohíbe {url}")
        self._throttle(urlsplit(url).netloc)
        resp = self.transport(url, self._headers(), self.timeout)
        if looks_like_challenge(resp):
            raise SourceBlockedError(
                f"{urlsplit(url).netloc} respondió con un desafío anti-bot (HTTP {resp.status}). "
                "Se requiere acceso autorizado por el sitio (API, IP permitida o credenciales); "
                "el agente no intenta evadirlo."
            )
        if resp.status in (401, 403):
            raise SourceBlockedError(f"acceso denegado por {urlsplit(url).netloc} (HTTP {resp.status})")
        if resp.status >= 400:
            raise RuntimeError(f"HTTP {resp.status} en {url}")
        return resp
