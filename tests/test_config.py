"""Config reads one tracker id under two names during the rename."""

from hell_gate_bridge.config import Config


def test_the_tracker_id_comes_from_ingest_tracker_id(monkeypatch):
    monkeypatch.setenv("INGEST_TRACKER_ID", "new-name")
    monkeypatch.delenv("INGEST_VEHICLE_ID", raising=False)

    assert Config().tracker_id == "new-name"


def test_the_old_ingest_vehicle_id_still_works(monkeypatch):
    """Kept for one release so a deploy can rename on its own schedule."""
    monkeypatch.delenv("INGEST_TRACKER_ID", raising=False)
    monkeypatch.setenv("INGEST_VEHICLE_ID", "old-name")

    assert Config().tracker_id == "old-name"


def test_the_new_name_wins_when_both_are_set(monkeypatch):
    monkeypatch.setenv("INGEST_TRACKER_ID", "new-name")
    monkeypatch.setenv("INGEST_VEHICLE_ID", "old-name")

    assert Config().tracker_id == "new-name"
