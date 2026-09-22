"""Shared, provider-agnostic publish layer.

Takes resolved `VehicleUpdate`s (from any `Source`) and POSTs them to cafe-car's
ingest seam: positions to `/ingest/positions`, per-stop predictions to
`/ingest/trip-updates`. Speed is metres/second and timestamps are epoch seconds,
matching the `vehicle:*` contract cafe-car serves from.

A cycle goes out in chunks rather than one request per vehicle: Amtrak is ~53
trains every 15s, which was over a hundred round trips a cycle. cafe-car
validates a batch as a whole, so a chunk is the blast radius of one malformed
record.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from hell_gate_bridge.config import Config
    from hell_gate_bridge.sources.amtrak.alerts import Alert
    from hell_gate_bridge.sources.base import StopTimeUpdate, VehicleUpdate

log = logging.getLogger(__name__)


# Positions are small and cafe-car writes them one at a time, so the chunk size
# is about bounding what a single bad record costs, not about request size.
CHUNK_SIZE = 25


def _position_body(v: VehicleUpdate) -> dict[str, object]:
    body: dict[str, object] = {
        "tracker_id": v.tracker_id,
        "vehicle_id": v.vehicle_id,
        "trip_id": v.trip_id,
    }
    if v.vehicle_label is not None:
        body["vehicle_label"] = v.vehicle_label
    if v.start_date is not None:
        body["start_date"] = v.start_date
    body["lat"] = v.lat
    body["lon"] = v.lon
    if v.speed_mps is not None:
        body["speed"] = v.speed_mps
    body["timestamp"] = v.timestamp
    if v.bearing is not None:
        body["bearing"] = v.bearing
    if v.route_id is not None:
        body["route_id"] = v.route_id
    # All three together or none: cafe-car rejects a status with no stop to
    # describe. Sequence 0 is a real stop_sequence, so test against None.
    if v.current_stop_sequence is not None:
        body["current_stop_sequence"] = v.current_stop_sequence
    if v.current_stop_id is not None:
        body["stop_id"] = v.current_stop_id
    if v.current_status is not None:
        body["current_status"] = v.current_status
    return body


def _stop_time_update_body(u: StopTimeUpdate) -> dict[str, object]:
    body: dict[str, object] = {}
    if u.stop_id is not None:
        body["stop_id"] = u.stop_id
    if u.stop_sequence is not None:
        body["stop_sequence"] = u.stop_sequence
    if u.arrival_time is not None:
        body["arrival_time"] = u.arrival_time
    if u.arrival_delay is not None:
        body["arrival_delay"] = u.arrival_delay
    if u.departure_time is not None:
        body["departure_time"] = u.departure_time
    if u.departure_delay is not None:
        body["departure_delay"] = u.departure_delay
    return body


def _trip_update_body(v: VehicleUpdate) -> dict[str, object]:
    body: dict[str, object] = {
        "trip_id": v.trip_id,
        "tracker_id": v.tracker_id,
        "vehicle_id": v.vehicle_id,
        "timestamp": v.timestamp,
        "stop_time_updates": [_stop_time_update_body(u) for u in v.stop_time_updates],
    }
    if v.vehicle_label is not None:
        body["vehicle_label"] = v.vehicle_label
    if v.start_date is not None:
        body["start_date"] = v.start_date
    return body


async def _post_chunks(
    http: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    field: str,
    bodies: list[dict[str, object]],
) -> int:
    """POST `bodies` in chunks under one JSON key. Returns the count accepted.

    A chunk cafe-car rejects is logged and skipped, and the rest of the cycle
    still ships: one unresolvable record must not cost a whole poll.
    """
    sent = 0
    for start in range(0, len(bodies), CHUNK_SIZE):
        chunk = bodies[start : start + CHUNK_SIZE]
        try:
            resp = await http.post(url, json={field: chunk}, headers=headers)
            resp.raise_for_status()
            sent += len(chunk)
        except httpx.HTTPError as exc:
            log.error("%s POST failed for %d records: %r", field, len(chunk), exc)
    return sent


async def publish(
    config: Config, http: httpx.AsyncClient, updates: list[VehicleUpdate]
) -> tuple[int, int]:
    """POST positions + trip-updates. Returns (positions, trip_updates) counts."""
    if not config.ingest_url:
        log.error("CAFE_CAR_INGEST_URL not set, cannot publish")
        return 0, 0

    base = config.ingest_url.rstrip("/")
    headers = {"Authorization": f"Bearer {config.ingest_token}"}

    # The two are independent now that they are batched: a position chunk that
    # fails no longer suppresses those vehicles' predictions, which are useful
    # on their own and outlive a single fix anyway (300s TTL vs 60s).
    positions = await _post_chunks(
        http,
        f"{base}/ingest/positions",
        headers,
        "positions",
        [_position_body(v) for v in updates],
    )
    trip_updates = await _post_chunks(
        http,
        f"{base}/ingest/trip-updates",
        headers,
        "trip_updates",
        [_trip_update_body(v) for v in updates if v.stop_time_updates],
    )
    return positions, trip_updates


def _alert_body(a: Alert) -> dict[str, object]:
    body: dict[str, object] = {
        "header_text": a.header_text,
        "description_text": a.description_text,
        "entities": [
            {
                k: v
                for k, v in (
                    ("agency_id", e.agency_id),
                    ("route_id", e.route_id),
                    ("stop_id", e.stop_id),
                )
                if v is not None
            }
            for e in a.entities
        ],
    }
    if a.url is not None:
        body["url"] = a.url
    if a.active_period_start is not None:
        body["active_period_start"] = a.active_period_start
    if a.active_period_end is not None:
        body["active_period_end"] = a.active_period_end
    return body


async def publish_alerts(
    config: Config, http: httpx.AsyncClient, alerts: list[Alert]
) -> int:
    """POST a full-replace sync of the current alert set. Returns the count sent.

    Unlike `publish`, this is one batch call: cafe-car's `/ingest/alerts`
    replaces the producer's entire alert set in one transaction, so a stale
    alert (removed from amtrak.com) disappears on the next sync without any
    separate expiry logic here.
    """
    if not config.ingest_url:
        log.error("CAFE_CAR_INGEST_URL not set, cannot publish alerts")
        return 0

    base = config.ingest_url.rstrip("/")
    headers = {"Authorization": f"Bearer {config.ingest_token}"}
    body = {
        "tracker_id": config.tracker_id,
        "alerts": [_alert_body(a) for a in alerts],
    }
    try:
        resp = await http.post(f"{base}/ingest/alerts", json=body, headers=headers)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        log.error("alerts sync POST failed: %r", exc)
        return 0
    return len(alerts)
