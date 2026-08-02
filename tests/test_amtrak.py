"""AmtrakSource._current_stop: Amtrak's per-stop statuses → a GTFS-RT
VehicleStopStatus plus the stop it describes."""

from datetime import UTC, datetime

from hell_gate_bridge.sources.amtrak.models import StopTime, Train, TrainStop
from hell_gate_bridge.sources.amtrak.source import _current_stop

# The resolved trip's {station_code: stop_sequence}, as GtfsResolver returns it.
_SEQS = {"WAS": 1, "BWI": 2, "BAL": 3, "PHL": 4}


def _train(*statuses: tuple[str, str]) -> Train:
    return Train(
        train_num="123",
        route="Northeast Regional",
        heading="NE",
        lat=39.0,
        lon=-76.7,
        speed_mph=60.0,
        amtrak_id="123",
        timestamp=datetime(2024, 1, 2, 12, 0, tzinfo=UTC),
        stops=[
            TrainStop(
                station_code=code,
                bus=False,
                timezone="America/New_York",
                status=status,
                arrival=StopTime(),
                departure=StopTime(),
            )
            for code, status in statuses
        ],
    )


def test_enroute_stop_is_in_transit_to():
    train = _train(("WAS", "departed"), ("BWI", "enroute"), ("BAL", "scheduled"))
    assert _current_stop(train, _SEQS) == (2, "BWI", "IN_TRANSIT_TO")


def test_arrived_stop_is_stopped_at():
    # client.py demotes enroute to scheduled once anything is arrived, so the
    # train standing at BWI has no enroute stop to be confused with.
    train = _train(("WAS", "departed"), ("BWI", "arrived"), ("BAL", "scheduled"))
    assert _current_stop(train, _SEQS) == (2, "BWI", "STOPPED_AT")


def test_latest_arrived_stop_wins():
    train = _train(("WAS", "arrived"), ("BWI", "arrived"), ("BAL", "scheduled"))
    assert _current_stop(train, _SEQS) == (2, "BWI", "STOPPED_AT")


def test_not_yet_departed_reports_nothing():
    train = _train(("WAS", "scheduled"), ("BWI", "scheduled"))
    assert _current_stop(train, _SEQS) is None


def test_finished_trip_reports_nothing():
    train = _train(("WAS", "departed"), ("BWI", "departed"))
    assert _current_stop(train, _SEQS) is None


def test_station_missing_from_trip_reports_nothing():
    # Amtrak occasionally reports a stop the static trip does not carry; there is
    # no sequence to place it at, so nothing is claimed.
    train = _train(("WAS", "departed"), ("XXX", "enroute"))
    assert _current_stop(train, _SEQS) is None


def test_no_stops_reports_nothing():
    assert _current_stop(_train(), _SEQS) is None
