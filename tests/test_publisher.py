"""The publish layer: the payload shapes are the whole contract with cafe-car.

Nothing else in either repo checks that a key cafe-car reads is a key this repo
writes, so the bodies are asserted field by field. The batching rules matter for
the same reason: a cycle is chunked so one record cafe-car rejects costs a chunk
rather than a poll.
"""

import asyncio

import httpx
import pytest

from hell_gate_bridge.config import Config
from hell_gate_bridge.publisher import CHUNK_SIZE, publish
from hell_gate_bridge.sources.base import StopTimeUpdate, VehicleUpdate


class FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)


class FakeHttp:
    """Records every POST. `fail_on` rejects the nth call to a given path."""

    def __init__(self, fail_on: set[tuple[str, int]] | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._fail_on = fail_on or set()
        self._seen: dict[str, int] = {}

    async def post(self, url: str, json: dict, headers: dict) -> FakeResponse:
        self.calls.append((url, json))
        path = url.rsplit("/", 1)[-1]
        n = self._seen.get(path, 0)
        self._seen[path] = n + 1
        return FakeResponse(500 if (path, n) in self._fail_on else 200)

    def bodies(self, path: str) -> list[dict]:
        field = path.replace("-", "_")
        return [
            body
            for url, payload in self.calls
            if url.endswith(path)
            for body in payload[field]
        ]


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("CAFE_CAR_INGEST_URL", "http://cafe-car/")
    monkeypatch.setenv("INGEST_API_TOKEN", "tok")
    return Config()


def _update(**overrides) -> VehicleUpdate:
    base = {
        "tracker_id": "gently-tender-oyster",
        "vehicle_id": "449:20260921",
        "vehicle_label": "449",
        "trip_id": "T1",
        "start_date": "20260921",
        "timestamp": 1_700_000_000,
        "lat": 42.0,
        "lon": -73.0,
        "speed_mps": 12.5,
        "bearing": 90.0,
        "route_id": "R",
        "current_stop_sequence": 3,
        "current_stop_id": "S3",
        "current_status": "IN_TRANSIT_TO",
    }
    base.update(overrides)
    return VehicleUpdate(**base)


def test_the_position_body_is_what_cafe_car_reads(config):
    http = FakeHttp()

    sent = asyncio.run(publish(config, http, [_update()]))

    assert sent == (1, 0)
    assert http.bodies("positions") == [
        {
            "tracker_id": "gently-tender-oyster",
            "vehicle_id": "449:20260921",
            "vehicle_label": "449",
            "trip_id": "T1",
            "start_date": "20260921",
            "lat": 42.0,
            "lon": -73.0,
            "speed": 12.5,
            "timestamp": 1_700_000_000,
            "bearing": 90.0,
            "route_id": "R",
            "current_stop_sequence": 3,
            "stop_id": "S3",
            "current_status": "IN_TRANSIT_TO",
        }
    ]


def test_an_absent_optional_is_omitted_not_nulled(config):
    # cafe-car's serialiser reads every optional field with .get(), so an
    # explicit null and an absent key are not the same thing.
    http = FakeHttp()

    asyncio.run(
        publish(
            config,
            http,
            [
                _update(
                    vehicle_label=None,
                    start_date=None,
                    speed_mps=None,
                    bearing=None,
                    route_id=None,
                    current_stop_sequence=None,
                    current_stop_id=None,
                    current_status=None,
                )
            ],
        )
    )

    assert http.bodies("positions") == [
        {
            "tracker_id": "gently-tender-oyster",
            "vehicle_id": "449:20260921",
            "trip_id": "T1",
            "lat": 42.0,
            "lon": -73.0,
            "timestamp": 1_700_000_000,
        }
    ]


def test_sequence_zero_is_a_real_stop_sequence(config):
    http = FakeHttp()

    asyncio.run(publish(config, http, [_update(current_stop_sequence=0)]))

    assert http.bodies("positions")[0]["current_stop_sequence"] == 0


def test_the_trip_update_body_carries_the_per_stop_predictions(config):
    http = FakeHttp()
    update = _update(
        stop_time_updates=[
            StopTimeUpdate(
                stop_id="S4",
                stop_sequence=4,
                arrival_time=1_700_000_600,
                arrival_delay=60,
            )
        ]
    )

    sent = asyncio.run(publish(config, http, [update]))

    assert sent == (1, 1)
    assert http.bodies("trip-updates") == [
        {
            "trip_id": "T1",
            "tracker_id": "gently-tender-oyster",
            "vehicle_id": "449:20260921",
            "timestamp": 1_700_000_000,
            "stop_time_updates": [
                {
                    "stop_id": "S4",
                    "stop_sequence": 4,
                    "arrival_time": 1_700_000_600,
                    "arrival_delay": 60,
                }
            ],
            "vehicle_label": "449",
            "start_date": "20260921",
        }
    ]


def test_a_vehicle_with_no_predictions_sends_no_trip_update(config):
    http = FakeHttp()

    asyncio.run(publish(config, http, [_update()]))

    assert [url for url, _ in http.calls] == ["http://cafe-car/ingest/positions"]


def test_a_cycle_goes_out_in_chunks(config):
    http = FakeHttp()
    updates = [_update(vehicle_id=str(i)) for i in range(CHUNK_SIZE + 1)]

    positions, _ = asyncio.run(publish(config, http, updates))

    assert positions == CHUNK_SIZE + 1
    sizes = [len(payload["positions"]) for _, payload in http.calls]
    assert sizes == [CHUNK_SIZE, 1]


def test_a_rejected_chunk_does_not_cost_the_rest_of_the_cycle(config):
    http = FakeHttp(fail_on={("positions", 0)})
    updates = [_update(vehicle_id=str(i)) for i in range(CHUNK_SIZE + 1)]

    positions, _ = asyncio.run(publish(config, http, updates))

    assert positions == 1  # the second chunk still shipped
    assert len(http.calls) == 2


def test_predictions_ship_even_when_the_positions_fail(config):
    # They are independent calls now: a prediction outlives the fix that
    # produced it (300s TTL vs 60s), so it is worth sending on its own.
    http = FakeHttp(fail_on={("positions", 0)})
    update = _update(stop_time_updates=[StopTimeUpdate(stop_sequence=4)])

    positions, trip_updates = asyncio.run(publish(config, http, [update]))

    assert (positions, trip_updates) == (0, 1)


def test_publishing_without_an_ingest_url_is_a_no_op(monkeypatch):
    monkeypatch.delenv("CAFE_CAR_INGEST_URL", raising=False)
    http = FakeHttp()

    assert asyncio.run(publish(Config(), http, [_update()])) == (0, 0)
    assert http.calls == []
