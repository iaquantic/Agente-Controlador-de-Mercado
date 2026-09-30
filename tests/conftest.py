from datetime import datetime, timedelta, timezone

import pytest

from controlador_mercado.models import Observation

NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)


def make_obs(i: int, **kw) -> Observation:
    data = {
        "source_id": "s1",
        "title": "Aceite de girasol Ole 1 L",
        "seller": f"v{i}",
        "price": 1000,
        "currency": "CUP",
        "province": "La Habana",
        "captured_at": (NOW - timedelta(days=1)).isoformat(),
    }
    data.update(kw)
    return Observation.from_dict(data, i)


@pytest.fixture
def now():
    return NOW
