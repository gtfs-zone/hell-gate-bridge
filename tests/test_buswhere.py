"""resolve_by_route + BuswhereSource: route-window resolution and loop-aware
stop-time construction against the Columbia County GTFS shape."""

import asyncio
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from hell_gate_bridge.config import Config
from hell_gate_bridge.gtfs import GtfsResolver
from hell_gate_bridge.sources.buswhere import source as buswhere_source_mod
from hell_gate_bridge.sources.buswhere.client import BuswhereObservation, fetch_route
from hell_gate_bridge.sources.buswhere.source import BuswhereSource

TZ = ZoneInfo("America/New_York")

_AGENCY = (
    "agency_id,agency_name,agency_url,agency_timezone\n"
    "CCPT,CC,https://x,America/New_York\n"
)
_CALENDAR = (
    "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
    "WK,1,1,1,1,1,0,0,20240101,20241231\n"
)


def _write(tmp_path, trips: str, stop_times: str, calendar_dates: str | None = None):
    (tmp_path / "agency.txt").write_text(_AGENCY)
    (tmp_path / "calendar.txt").write_text(_CALENDAR)
    (tmp_path / "trips.txt").write_text(trips)
    (tmp_path / "stop_times.txt").write_text(stop_times)
    if calendar_dates is not None:
        (tmp_path / "calendar_dates.txt").write_text(calendar_dates)
    return GtfsResolver(tmp_path)


# Two back-to-back trips on one route: 08:00-09:00 and 09:00-10:00.
_TWO_TRIPS = (
    "route_id,service_id,trip_id,trip_short_name,shape_id\n"
    "R,WK,T1,loop,S\n"
    "R,WK,T2,loop,S\n"
)
_TWO_TIMES = (
    "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
    "T1,08:00:00,08:00:00,A,0\n"
    "T1,09:00:00,09:00:00,B,1\n"
    "T2,09:00:00,09:00:00,A,0\n"
    "T2,10:00:00,10:00:00,B,1\n"
)


def test_resolve_by_route_picks_running_trip(tmp_path):
    r = _write(tmp_path, _TWO_TRIPS, _TWO_TIMES)
    assert r.resolve_by_route("R", datetime(2024, 1, 2, 8, 30, tzinfo=TZ)) == (
        "T1",
        "20240102",
    )
    assert r.resolve_by_route("R", datetime(2024, 1, 2, 9, 30, tzinfo=TZ)) == (
        "T2",
        "20240102",
    )


def test_resolve_by_route_boundary_prefers_starting_trip(tmp_path):
    # At the shared 09:00 boundary both windows contain now; prefer the trip
    # just starting, not the one just ending.
    r = _write(tmp_path, _TWO_TRIPS, _TWO_TIMES)
    assert r.resolve_by_route("R", datetime(2024, 1, 2, 9, 0, tzinfo=TZ)) == (
        "T2",
        "20240102",
    )


def test_resolve_by_route_none_outside_service_hours(tmp_path):
    r = _write(tmp_path, _TWO_TRIPS, _TWO_TIMES)
    assert r.resolve_by_route("R", datetime(2024, 1, 2, 6, 0, tzinfo=TZ)) is None


def test_resolve_by_route_honors_calendar_dates_removal(tmp_path):
    # 2024-01-03 (Wed) is normally served, but a type-2 exception removes it.
    r = _write(
        tmp_path,
        _TWO_TRIPS,
        _TWO_TIMES,
        calendar_dates="service_id,date,exception_type\nWK,20240103,2\n",
    )
    assert r.resolve_by_route("R", datetime(2024, 1, 3, 8, 30, tzinfo=TZ)) is None
    assert r.resolve_by_route("R", datetime(2024, 1, 2, 8, 30, tzinfo=TZ)) is not None


# One 60-minute loop that returns to its origin stop A (seq 0 and seq 3).
_LOOP_TRIP = "route_id,service_id,trip_id,trip_short_name,shape_id\nR,WK,LOOP,loop,S\n"
_LOOP_TIMES = (
    "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
    "LOOP,08:00:00,08:00:00,A,0\n"
    "LOOP,08:20:00,08:20:00,B,1\n"
    "LOOP,08:40:00,08:40:00,C,2\n"
    "LOOP,09:00:00,09:00:00,A,3\n"
)


def _buswhere_source(tmp_path, monkeypatch):
    monkeypatch.setenv("SOURCE", "buswhere")
    monkeypatch.setenv("INGEST_VEHICLE_ID", "ccbus")
    src = BuswhereSource(Config())
    src._resolver = _write(tmp_path, _LOOP_TRIP, _LOOP_TIMES)
    src._routes = {"testslug": "R"}
    src._stops = {"bwA": "A", "bwB": "B", "bwC": "C"}
    return src


def test_buswhere_build_loop_keeps_upcoming_only(tmp_path, monkeypatch):
    src = _buswhere_source(tmp_path, monkeypatch)
    # Now = 08:25 (just left B, heading to C). buswhere ETAs to each stop's *next*
    # visit: C in 15m (+3m late), A(origin) next reached at the 09:00 terminus,
    # B not until the next loop (~09:20).
    now = datetime(2024, 1, 2, 8, 25, tzinfo=TZ)
    ts = int(now.timestamp())
    obs = BuswhereObservation(
        lat=42.25,
        lon=-73.79,
        timestamp=ts,
        stop_eta={
            "bwC": 15 * 60 + 180,  # 08:43 (scheduled 08:40 → +180s)
            "bwA": 35 * 60,  # 09:00 terminus
            "bwB": 55 * 60,  # 09:20 next loop → dropped
        },
    )

    v = src._build("testslug", obs, now)
    assert v is not None
    assert (v.trip_id, v.start_date, v.route_id) == ("LOOP", "20240102", "R")
    assert v.speed_mps is None and v.bearing is None

    by_seq = {u.stop_sequence: u for u in v.stop_time_updates}
    # Passed origin (seq 0) and next-loop B (seq 1) are filtered out; the upcoming
    # C (seq 2) and the terminus A (seq 3) remain.
    assert set(by_seq) == {2, 3}
    assert by_seq[2].stop_id == "C"
    assert by_seq[2].arrival_delay == 180
    assert by_seq[2].arrival_time == ts + 15 * 60 + 180
    assert by_seq[3].stop_id == "A"
    assert by_seq[3].arrival_delay == 0


def test_buswhere_build_labels_from_device_name(tmp_path, monkeypatch):
    # buswhere's device name has no uniqueness guarantee (seen echoed across
    # two routes at once), so it's display-only: cafe-car derives the actual
    # unique VehicleDescriptor.id, hence no vehicle_id here.
    src = _buswhere_source(tmp_path, monkeypatch)
    now = datetime(2024, 1, 2, 8, 25, tzinfo=TZ)
    obs = BuswhereObservation(
        lat=42.25,
        lon=-73.79,
        timestamp=int(now.timestamp()),
        stop_eta={"bwC": 900},
        vehicle_name="C5",
    )

    v = src._build("testslug", obs, now)
    assert v is not None
    assert v.vehicle_id is None
    assert v.vehicle_label == "C5"

    obs_unnamed = BuswhereObservation(
        lat=42.25, lon=-73.79, timestamp=int(now.timestamp()), stop_eta={"bwC": 900}
    )
    v2 = src._build("testslug", obs_unnamed, now)
    assert v2 is not None
    assert v2.vehicle_label == "testslug"


def test_buswhere_build_current_stop_is_next_visit(tmp_path, monkeypatch):
    # Same 08:25 fix as above: the first surviving update (C, seq 2) is the stop
    # the bus is running towards, and an ETA feed can only claim IN_TRANSIT_TO.
    src = _buswhere_source(tmp_path, monkeypatch)
    now = datetime(2024, 1, 2, 8, 25, tzinfo=TZ)
    ts = int(now.timestamp())
    obs = BuswhereObservation(
        lat=42.25,
        lon=-73.79,
        timestamp=ts,
        stop_eta={"bwC": 15 * 60 + 180, "bwA": 35 * 60, "bwB": 55 * 60},
    )

    v = src._build("testslug", obs, now)
    assert v is not None
    assert (v.current_stop_sequence, v.current_stop_id, v.current_status) == (
        2,
        "C",
        "IN_TRANSIT_TO",
    )


def test_buswhere_build_no_current_stop_without_predictions(tmp_path, monkeypatch):
    # Every ETA belongs to the next loop, so nothing survives the filter and the
    # bus is published with no claim about where it is.
    src = _buswhere_source(tmp_path, monkeypatch)
    now = datetime(2024, 1, 2, 8, 25, tzinfo=TZ)
    obs = BuswhereObservation(
        lat=42.25,
        lon=-73.79,
        timestamp=int(now.timestamp()),
        stop_eta={"bwB": 55 * 60},
    )

    v = src._build("testslug", obs, now)
    assert v is not None
    assert v.stop_time_updates == []
    assert v.current_stop_sequence is None
    assert v.current_stop_id is None
    assert v.current_status is None


def test_buswhere_build_unresolved_when_no_trip_running(tmp_path, monkeypatch):
    src = _buswhere_source(tmp_path, monkeypatch)
    now = datetime(2024, 1, 2, 6, 0, tzinfo=TZ)  # before service
    obs = BuswhereObservation(
        lat=42.25, lon=-73.79, timestamp=int(now.timestamp()), stop_eta={}
    )
    assert src._build("testslug", obs, now) is None


def test_buswhere_build_unmapped_slug(tmp_path, monkeypatch):
    src = _buswhere_source(tmp_path, monkeypatch)
    now = datetime(2024, 1, 2, 8, 25, tzinfo=TZ)
    obs = BuswhereObservation(
        lat=42.25, lon=-73.79, timestamp=int(now.timestamp()), stop_eta={}
    )
    assert src._build("nope", obs, now) is None


def test_now_from_utc_timestamp_resolves_local_day(tmp_path, monkeypatch):
    # A device fix carried as a UTC epoch must resolve on the agency-local day.
    src = _buswhere_source(tmp_path, monkeypatch)
    now = datetime(2024, 1, 2, 8, 25, tzinfo=TZ).astimezone(UTC)
    obs = BuswhereObservation(
        lat=42.25, lon=-73.79, timestamp=int(now.timestamp()), stop_eta={"bwC": 900}
    )
    v = src._build("testslug", obs, now)
    assert v is not None and v.start_date == "20240102"


class _FakeResponse:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload

    def json(self):
        return self._payload


class _FakeHttp:
    def __init__(self, payload):
        self._payload = payload

    async def get(self, *args, **kwargs):
        return _FakeResponse(self._payload)


def test_fetch_route_tolerates_non_numeric_stop_eta():
    # Upstream mixes numbers, null, and sentinel strings like "departed" in
    # stop_eta; only numeric ETAs (including numeric strings) survive.
    payload = {
        "active": True,
        "devices": [
            {"position": {"lat": 42.25, "lon": -73.79}, "updated_at": 1700000000}
        ],
        "stop_eta": {
            "150769331": "departed",
            "150769332": None,
            "150769333": 120,
            "150769334": "300",
            "150769335": "arriving",
        },
    }
    obs = asyncio.run(fetch_route(_FakeHttp(payload), "hudson__albany_b_pm"))
    assert obs is not None
    assert obs.stop_eta == {"150769333": 120.0, "150769334": 300.0}


def test_fetch_failure_on_one_route_does_not_abort_cycle(tmp_path, monkeypatch, caplog):
    # A route whose fetch raises must be skipped, not kill the whole poll cycle.
    src = _buswhere_source(tmp_path, monkeypatch)
    src._routes = {"bad": "R", "good": "R"}
    src._slugs = ["bad", "good"]
    now = datetime(2024, 1, 2, 8, 25, tzinfo=TZ)
    good_obs = BuswhereObservation(
        lat=42.25,
        lon=-73.79,
        timestamp=int(now.timestamp()),
        stop_eta={"bwC": 900},
    )

    async def fake_fetch_route(http, slug, base_url=None):
        if slug == "bad":
            raise ValueError("could not convert string to float: 'departed'")
        return good_obs

    monkeypatch.setattr(buswhere_source_mod, "fetch_route", fake_fetch_route)
    updates = asyncio.run(src.fetch(None))
    assert [u.trip_id for u in updates] == ["LOOP"]
    assert "route bad failed" in caplog.text
