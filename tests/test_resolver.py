"""GtfsResolver per-instance resolution.

A multi-day train has several instances live at once, all sharing one train
number. They must resolve to distinct GTFS trips by the service day they
departed origin, otherwise they collapse onto one trip_id (and one Redis key
downstream), which is how live trains went missing.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from hell_gate_bridge.gtfs import GtfsResolver

TZ = ZoneInfo("America/New_York")

# A single 2-day train "5": one GTFS trip per service day, each departing origin
# 08:00 and arriving the next day 20:00 (GTFS 44:00:00), so consecutive-day
# instances overlap in wall-clock time.
_AGENCY = "agency_id,agency_name,agency_url,agency_timezone\n1,Amtrak,https://amtrak.com,America/New_York\n"
_CALENDAR = (
    "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
    "SVC_MON,1,0,0,0,0,0,0,20240101,20241231\n"
    "SVC_TUE,0,1,0,0,0,0,0,20240101,20241231\n"
)
_TRIPS = (
    "route_id,service_id,trip_id,trip_short_name,shape_id\n"
    "R,SVC_MON,T_MON,5,SH\n"
    "R,SVC_TUE,T_TUE,5,SH\n"
)
_STOP_TIMES = (
    "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
    "T_MON,08:00:00,08:00:00,CHI,1\n"
    "T_MON,44:00:00,44:00:00,NYP,2\n"
    "T_TUE,08:00:00,08:00:00,CHI,1\n"
    "T_TUE,44:00:00,44:00:00,NYP,2\n"
)


def _make_resolver(tmp_path):
    (tmp_path / "agency.txt").write_text(_AGENCY)
    (tmp_path / "calendar.txt").write_text(_CALENDAR)
    (tmp_path / "trips.txt").write_text(_TRIPS)
    (tmp_path / "stop_times.txt").write_text(_STOP_TIMES)
    return GtfsResolver(tmp_path)


def test_origin_date_distinguishes_overlapping_instances(tmp_path):
    resolver = _make_resolver(tmp_path)

    # Tuesday 2024-01-02 10:00: both Monday's and Tuesday's instances are en route.
    now = datetime(2024, 1, 2, 10, 0, tzinfo=TZ)
    mon_origin = datetime(2024, 1, 1, 8, 0, tzinfo=TZ)
    tue_origin = datetime(2024, 1, 2, 8, 0, tzinfo=TZ)

    # Distinct trip_id *and* distinct start_date: the pair is what keeps
    # concurrent instances from colliding downstream.
    assert resolver.resolve("5", now, origin=mon_origin) == ("T_MON", "20240101")
    assert resolver.resolve("5", now, origin=tue_origin) == ("T_TUE", "20240102")


def test_same_trip_id_split_by_start_date(tmp_path):
    # The Floridian case: a single daily >24h trip_id, so both live instances
    # resolve to the same trip_id but must carry different start_dates.
    (tmp_path / "agency.txt").write_text(_AGENCY)
    (tmp_path / "calendar.txt").write_text(
        "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
        "SVC_DAILY,1,1,1,1,1,1,1,20240101,20241231\n"
    )
    (tmp_path / "trips.txt").write_text(
        "route_id,service_id,trip_id,trip_short_name,shape_id\nR,SVC_DAILY,T_DAILY,41,SH\n"
    )
    (tmp_path / "stop_times.txt").write_text(
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "T_DAILY,08:00:00,08:00:00,CHI,1\n"
        "T_DAILY,44:00:00,44:00:00,MIA,2\n"
    )
    resolver = GtfsResolver(tmp_path)

    now = datetime(2024, 1, 2, 10, 0, tzinfo=TZ)
    day1 = resolver.resolve("41", now, origin=datetime(2024, 1, 1, 8, 0, tzinfo=TZ))
    day2 = resolver.resolve("41", now, origin=datetime(2024, 1, 2, 8, 0, tzinfo=TZ))

    assert day1 == ("T_DAILY", "20240101")
    assert day2 == ("T_DAILY", "20240102")
    assert day1 != day2  # same trip_id, different instance


def test_without_origin_still_resolves_to_a_trip(tmp_path):
    # With no origin the now-based fallback still returns a (trip_id, start_date)
    # pair, no regression for producers that can't supply an origin.
    resolver = _make_resolver(tmp_path)
    now = datetime(2024, 1, 2, 10, 0, tzinfo=TZ)

    result = resolver.resolve("5", now)
    assert result is not None
    assert isinstance(result, tuple) and len(result) == 2


def test_unknown_train_number_returns_none(tmp_path):
    resolver = _make_resolver(tmp_path)
    now = datetime(2024, 1, 2, 10, 0, tzinfo=TZ)
    assert resolver.resolve("999", now, origin=now) is None
