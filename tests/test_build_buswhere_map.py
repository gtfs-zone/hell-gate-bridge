"""build_buswhere_map.py: HTML stop extraction, fuzzy tiebreak, review-file
round-trip, coverage report, and exit-code behavior, all offline."""

import importlib.util
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parent.parent / "scripts" / "build_buswhere_map.py"
)
_spec = importlib.util.spec_from_file_location("build_buswhere_map", _MODULE_PATH)
bbm = importlib.util.module_from_spec(_spec)
sys.modules["build_buswhere_map"] = bbm
_spec.loader.exec_module(bbm)


# -- _extract_stops -----------------------------------------------------------


def test_extract_stops_basic():
    html = (
        '<script>var x = {"stops":[{"id":1,"lat":42.1,"lon":-73.7,'
        '"address":"A"}]};</script>'
    )
    stops = bbm._extract_stops(html)
    assert stops == [{"id": 1, "lat": 42.1, "lon": -73.7, "address": "A"}]


def test_extract_stops_address_with_bracket_survives():
    html = (
        '{"stops":[{"id":1,"lat":42.1,"lon":-73.7,"address":"Plaza [north]"},'
        '{"id":2,"lat":42.2,"lon":-73.8,"address":"B"}]}'
    )
    stops = bbm._extract_stops(html)
    assert len(stops) == 2
    assert stops[0]["address"] == "Plaza [north]"


def test_extract_stops_malformed_json_skipped():
    html = '{"stops":[{"id":1,"lat":42.1,]}'  # broken
    assert bbm._extract_stops(html) == []


def test_extract_stops_picks_longest_valid_array():
    html = (
        '{"stops":[{"id":1,"lat":1}]} ... {"stops":[{"id":1,"lat":1},{"id":2,"lat":2}]}'
    )
    stops = bbm._extract_stops(html)
    assert len(stops) == 2


# -- fuzzy matching -------------------------------------------------------------


def test_fuzzy_score_matches_real_duplicate_pair():
    # "Front & Warren St." vs "N Front St & Warren St": a real duplicate-
    # coordinate pair in the Columbia County GTFS.
    assert bbm._fuzzy_score("Front & Warren St.", "Front & Warren St.") > 0.9
    assert bbm._fuzzy_score(
        "Front & Warren St.", "N Front St & Warren St"
    ) > bbm._fuzzy_score("Front & Warren St.", "ShopRite of Hudson")


def test_normalize_text_strips_punctuation_and_case():
    assert bbm._normalize_text("2939 Rt. 9 - Valatie") == bbm._normalize_text(
        "2939 RT 9 VALATIE"
    )


# -- _match_stop / cluster tiebreak --------------------------------------------

_DUP_COORD_STOPS = [
    ("STOP-A", "Front & Warren St.", 42.256552, -73.795788),
    ("STOP-B", "N Front St & Warren St", 42.256517, -73.795909),
    ("STOP-C", "Elsewhere", 42.30, -73.70),
]


def test_match_stop_decisive_fuzzy_winner_auto_accepts():
    result = bbm._match_stop(
        42.256552, -73.795788, "Front & Warren St.", _DUP_COORD_STOPS
    )
    assert result.outcome == "matched"
    assert result.stop_id == "STOP-A"


def test_match_stop_indecisive_tie_goes_to_review():
    # Neither buswhere-side text resembles either GTFS name closely.
    result = bbm._match_stop(42.256552, -73.795788, "xyz123", _DUP_COORD_STOPS)
    assert result.outcome == "review"
    assert result.reason == "TIE"


def test_match_stop_far_from_everything_is_rejected():
    result = bbm._match_stop(50.0, -73.795788, "anything", _DUP_COORD_STOPS)
    assert result.outcome == "review"
    assert result.reason == "REJECT"


def test_match_stop_clear_nearest_within_reject_is_matched():
    result = bbm._match_stop(42.30, -73.70, "Elsewhere", _DUP_COORD_STOPS)
    assert result.outcome == "matched"
    assert result.stop_id == "STOP-C"


def test_match_stop_beyond_warn_but_within_reject_is_flagged():
    # A single, unambiguous nearest stop 150m off (past WARN_METERS=90 but
    # under REJECT_METERS=300) still auto-matches, but must carry the warn
    # flag so it's logged loudly instead of blending in with clean matches.
    far_single = [("STOP-X", "Somewhere", 42.30, -73.70)]
    # ~150m north of STOP-X.
    result = bbm._match_stop(42.3013, -73.70, "Somewhere", far_single)
    assert result.outcome == "matched"
    assert result.stop_id == "STOP-X"
    assert result.warn is True


def test_match_stop_within_warn_is_not_flagged():
    close_single = [("STOP-X", "Somewhere", 42.30, -73.70)]
    result = bbm._match_stop(42.3001, -73.70, "Somewhere", close_single)
    assert result.outcome == "matched"
    assert result.warn is False


def test_match_stop_close_but_name_mismatch_goes_to_review():
    # The real "Greenport" bug: a single dominant nearest candidate 116m away
    # whose name bears no resemblance to buswhere's generic address must NOT
    # auto-accept just because it's under REJECT_METERS; needs a name match
    # too, or it goes to review instead of silently mapping the wrong stop.
    stops = [("STOP-X", "Fairview Plaza", 42.2570, -73.7650)]
    result = bbm._match_stop(42.2580, -73.7650, "Greenport", stops)
    assert result.outcome == "review"
    assert result.reason == "LOW_CONFIDENCE"


def test_match_stop_within_sure_meters_skips_name_check():
    # Close enough (<=25m) that distance alone is trusted even if the name
    # doesn't resemble it at all (e.g. buswhere's generic address).
    stops = [("STOP-X", "Fairview Plaza", 42.2570, -73.7650)]
    result = bbm._match_stop(42.2570, -73.76495, "Greenport", stops)
    assert result.outcome == "matched"
    assert result.stop_id == "STOP-X"


def test_search_gtfs_stops_finds_far_but_name_matching_stop():
    # The nearest-N-by-distance list can miss the right answer entirely when
    # buswhere reuses one generic address (e.g. "Greenport") across several
    # spread-out GTFS stops; search must find it by name regardless of
    # distance.
    stops = [
        ("STOP-NEAR", "Fairview Plaza", 42.256, -73.765),  # near, wrong name
        ("STOP-FAR", "Greenport", 42.30, -73.90),  # far, right name
    ]
    results = bbm._search_gtfs_stops("Greenport", 42.256, -73.765, stops)
    assert results[0].stop_id == "STOP-FAR"


# -- review file round-trip ------------------------------------------------------


def test_load_save_review_round_trip(tmp_path):
    path = tmp_path / "review.json"
    review = bbm._load_review(path)
    assert review == {"pending": {}, "resolved": {}}
    review["pending"]["123"] = {"route": "chatham", "reason": "REJECT"}
    review["resolved"]["456"] = {"decision": "no_match", "decided_at": "now"}
    bbm._save_review(review, path)

    reloaded = bbm._load_review(path)
    assert reloaded["pending"]["123"]["reason"] == "REJECT"
    assert reloaded["resolved"]["456"]["decision"] == "no_match"


def test_prune_stale_resolutions_drops_missing_stop_id():
    review = {
        "pending": {},
        "resolved": {
            "keep": {"decision": "map", "stop_id": "STOP-EXISTS"},
            "drop": {"decision": "map", "stop_id": "STOP-GONE"},
            "no_match_untouched": {"decision": "no_match"},
        },
    }
    bbm._prune_stale_resolutions(review, {"STOP-EXISTS"})
    assert set(review["resolved"]) == {"keep", "no_match_untouched"}


# -- _map_route: review-file interaction ------------------------------------------


def test_map_route_rejects_go_to_pending(tmp_path):
    mapping = {"routes": dict(bbm.ROUTES), "stops": {}}
    review = {"pending": {}, "resolved": {}}
    stops = [{"id": 999, "lat": 50.0, "lon": -73.0, "address": "Nowhere"}]
    bbm._map_route(
        "chatham", stops, _DUP_COORD_STOPS, mapping, review, interactive=False
    )
    assert "999" not in mapping["stops"]
    assert "999" in review["pending"]
    assert review["pending"]["999"]["reason"] == "REJECT"


def test_map_route_uses_prior_resolution(monkeypatch):
    mapping = {"routes": dict(bbm.ROUTES), "stops": {}}
    review = {
        "pending": {},
        "resolved": {
            "999": {"decision": "map", "stop_id": "STOP-C", "decided_at": "now"}
        },
    }
    stops = [{"id": 999, "lat": 50.0, "lon": -73.0, "address": "Nowhere"}]
    matched = bbm._map_route(
        "chatham", stops, _DUP_COORD_STOPS, mapping, review, interactive=False
    )
    assert matched == 1
    assert mapping["stops"]["999"] == "STOP-C"
    assert "999" not in review["pending"]


def test_map_route_no_match_resolution_stays_unmapped():
    mapping = {"routes": dict(bbm.ROUTES), "stops": {}}
    review = {
        "pending": {},
        "resolved": {"999": {"decision": "no_match", "decided_at": "now"}},
    }
    stops = [{"id": 999, "lat": 50.0, "lon": -73.0, "address": "Nowhere"}]
    matched = bbm._map_route(
        "chatham", stops, _DUP_COORD_STOPS, mapping, review, interactive=False
    )
    assert matched == 0
    assert "999" not in mapping["stops"]
    assert "999" not in review["pending"]


# -- coverage report ---------------------------------------------------------------


def test_coverage_report_finds_missing_scheduled_stop():
    mapping = {
        "routes": {"chatham": "R1"},
        "stops": {"bw1": "STOP-A"},  # STOP-B scheduled but unmapped
    }
    gtfs_route_stops = {"R1": {"STOP-A", "STOP-B"}}
    gtfs_stops_by_id = {"STOP-A": "Alpha", "STOP-B": "Beta"}
    gaps = bbm._coverage_report(mapping, gtfs_route_stops, gtfs_stops_by_id)
    assert gaps == {"R1": [("STOP-B", "Beta")]}


def test_coverage_report_clean_when_fully_mapped():
    mapping = {"routes": {"chatham": "R1"}, "stops": {"bw1": "STOP-A"}}
    gtfs_route_stops = {"R1": {"STOP-A"}}
    gtfs_stops_by_id = {"STOP-A": "Alpha"}
    assert bbm._coverage_report(mapping, gtfs_route_stops, gtfs_stops_by_id) == {}


# -- GTFS zip parsing (route/stop_times) -------------------------------------------


def _make_gtfs_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "stops.txt",
            "stop_id,stop_name,stop_lat,stop_lon\n"
            "STOP-A,Alpha,42.1,-73.1\n"
            "STOP-B,Beta,42.2,-73.2\n",
        )
        zf.writestr(
            "trips.txt",
            "route_id,service_id,trip_id\nR1,WK,T1\n",
        )
        zf.writestr(
            "stop_times.txt",
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            "T1,08:00:00,08:00:00,STOP-A,0\n"
            "T1,08:10:00,08:10:00,STOP-B,1\n",
        )
    return buf.getvalue()


def test_load_gtfs_stops_and_route_stop_ids():
    data = _make_gtfs_zip()
    stops = bbm._load_gtfs_stops(data)
    assert ("STOP-A", "Alpha", 42.1, -73.1) in stops
    route_stops = bbm._load_route_stop_ids(data)
    assert route_stops == {"R1": {"STOP-A", "STOP-B"}}


# -- exit-code behavior via main() -------------------------------------------------


def test_main_exits_nonzero_when_pending_backlog(tmp_path, monkeypatch):
    gtfs_zip = tmp_path / "gtfs.zip"
    gtfs_zip.write_bytes(_make_gtfs_zip())
    mapping_path = tmp_path / "mapping.json"
    review_path = tmp_path / "review.json"
    monkeypatch.setattr(bbm, "MAPPING_PATH", mapping_path)

    def fake_fetch(client, slug):
        return [{"id": 1, "lat": 50.0, "lon": -73.0, "address": "Nowhere"}]

    monkeypatch.setattr(bbm, "_fetch_route_stops", fake_fetch)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_buswhere_map.py",
            "--gtfs",
            str(gtfs_zip),
            "--routes",
            "chatham",
            "--review-file",
            str(review_path),
        ],
    )
    assert bbm.main() == 1
    assert json.loads(review_path.read_text())["pending"]


def test_main_exits_zero_on_clean_match(tmp_path, monkeypatch):
    gtfs_zip = tmp_path / "gtfs.zip"
    gtfs_zip.write_bytes(_make_gtfs_zip())
    mapping_path = tmp_path / "mapping.json"
    review_path = tmp_path / "review.json"
    monkeypatch.setattr(bbm, "MAPPING_PATH", mapping_path)

    def fake_fetch(client, slug):
        return [{"id": 1, "lat": 42.1, "lon": -73.1, "address": "Alpha"}]

    monkeypatch.setattr(bbm, "_fetch_route_stops", fake_fetch)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_buswhere_map.py",
            "--gtfs",
            str(gtfs_zip),
            "--routes",
            "chatham",
            "--review-file",
            str(review_path),
        ],
    )
    assert bbm.main() == 0


def test_main_strict_fails_on_coverage_gap(tmp_path, monkeypatch):
    gtfs_zip = tmp_path / "gtfs.zip"
    gtfs_zip.write_bytes(_make_gtfs_zip())
    mapping_path = tmp_path / "mapping.json"
    review_path = tmp_path / "review.json"
    monkeypatch.setattr(bbm, "MAPPING_PATH", mapping_path)

    # Only STOP-A gets mapped; STOP-B is scheduled on R1 (route_id for
    # "chatham" is "Chatham-Hudson" per ROUTES, so point trips.txt there).
    gtfs_buf = io.BytesIO()
    with zipfile.ZipFile(gtfs_buf, "w") as zf:
        zf.writestr(
            "stops.txt",
            "stop_id,stop_name,stop_lat,stop_lon\n"
            "STOP-A,Alpha,42.1,-73.1\n"
            "STOP-B,Beta,42.2,-73.2\n",
        )
        zf.writestr("trips.txt", "route_id,service_id,trip_id\nChatham-Hudson,WK,T1\n")
        zf.writestr(
            "stop_times.txt",
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            "T1,08:00:00,08:00:00,STOP-A,0\n"
            "T1,08:10:00,08:10:00,STOP-B,1\n",
        )
    gtfs_zip.write_bytes(gtfs_buf.getvalue())

    def fake_fetch(client, slug):
        return [{"id": 1, "lat": 42.1, "lon": -73.1, "address": "Alpha"}]

    monkeypatch.setattr(bbm, "_fetch_route_stops", fake_fetch)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_buswhere_map.py",
            "--gtfs",
            str(gtfs_zip),
            "--routes",
            "chatham",
            "--review-file",
            str(review_path),
            "--strict",
        ],
    )
    assert bbm.main() == 1


def test_interactive_and_watch_mutually_exclusive(monkeypatch, tmp_path):
    monkeypatch.setattr(
        sys, "argv", ["build_buswhere_map.py", "--interactive", "--watch"]
    )
    with pytest.raises(SystemExit):
        bbm.main()
