import json
from datetime import datetime, timedelta, timezone

from controlador_mercado.adapters.eltoque import eltoque_rates, parse_eltoque
from controlador_mercado.adapters.http import HttpResponse, PoliteFetcher
from controlador_mercado.adapters.store import RecordingSource, SnapshotStore
from controlador_mercado.models import Observation, TargetProduct
from controlador_mercado.sources import SourceAdapter

PAYLOAD = {"date": "2026-10-01", "hour": 10, "minutes": 22, "seconds": 5,
           "tasas": {"BTC": 765.16, "ECU": 860.0, "MLC": 495.01, "USD": 760.0, "USDT_TRC20": 767.37}}


def test_parse_eltoque_keeps_fiat_and_dates_in_havana_time():
    rates = {r.from_currency: r for r in parse_eltoque(PAYLOAD, window_hours=24)}
    assert set(rates) == {"USD", "EUR", "MLC"}
    usd = rates["USD"]
    assert usd.to_currency == "CUP" and usd.rate == 760.0
    assert usd.as_of == datetime(2026, 10, 1, 14, 22, 5, tzinfo=timezone.utc)  # 10:22 en La Habana (UTC-4)
    assert "informal" in usd.source


def test_parse_eltoque_without_data_returns_nothing():
    assert parse_eltoque({}, window_hours=24) == []


def test_eltoque_rates_sends_bearer_and_window():
    seen = []

    def transport(url, headers, timeout, data=None):
        seen.append((url, headers))
        if url.endswith("/robots.txt"):
            return HttpResponse(url, 404, "")
        return HttpResponse(url, 200, json.dumps(PAYLOAD))

    fetcher = PoliteFetcher(transport=transport, sleep=lambda s: None)
    provider = eltoque_rates(api_key="clave", until=datetime(2026, 10, 1, 18, tzinfo=timezone.utc), fetcher=fetcher)
    url, headers = seen[-1]
    assert headers["Authorization"] == "Bearer clave"
    assert "date_from=2026-09-30+14%3A00%3A00" in url and "date_to=2026-10-01+14%3A00%3A00" in url
    assert provider.lookup("CUP", "USD")[0].rate == 760.0


class _Live(SourceAdapter):
    source_id = "viva"

    def __init__(self):
        self.price = 10.0

    def fetch(self, target, since, until):
        return [Observation(observation_id=f"viva:1:{self.price}", source_id="viva", title="Aceite 1 L",
                            listing_id="1", price=self.price, currency="USD",
                            captured_at=self.now, raw={"nota": "x"})]


def test_recording_source_accumulates_captures(tmp_path):
    live = _Live()
    src = RecordingSource(live, SnapshotStore(tmp_path))
    t0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
    live.now = t0
    assert len(src.fetch(TargetProduct(name="aceite"), None, None)) == 1
    live.now, live.price = t0 + timedelta(days=7), 12.0
    obs = src.fetch(TargetProduct(name="aceite"), None, None)
    assert [o.price for o in obs] == [10.0, 12.0]
    assert obs[0].captured_at == t0 and obs[0].raw == {"nota": "x"}
    assert [o.price for o in src.fetch(TargetProduct(name="aceite"), t0 + timedelta(days=1), None)] == [12.0, 12.0]


def test_recording_source_propagates_failures(tmp_path):
    class Broken(SourceAdapter):
        source_id = "rota"

        def fetch(self, target, since, until):
            raise RuntimeError("bloqueada")

    src = RecordingSource(Broken(), SnapshotStore(tmp_path))
    try:
        src.fetch(TargetProduct(name="x"), None, None)
    except RuntimeError as exc:
        assert "bloqueada" in str(exc)
    else:
        raise AssertionError("debía fallar")
