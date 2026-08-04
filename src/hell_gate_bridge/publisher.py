"""Shared, provider-agnostic publish layer.

Takes resolved `VehicleUpdate`s (from any `Source`) and POSTs them to cafe-car's
ingest seam: positions to `/ingest/position`, per-stop predictions to
`/ingest/trip-update`. Speed is metres/second and timestamps are epoch seconds,
matching the `vehicle:*` contract cafe-car serves from.
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


def _position_body(config: Config, v: VehicleUpdate) -> dict[str, object]:
    body: dict[str, object] = {
        "tracker_id": v.tracker_id,
        "trip_id": v.trip_id,
    }
    if v.vehicle_id is not None:
        body["vehicle_id"] = v.vehicle_id
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


async def publish(
    config: Config, http: httpx.AsyncClient, updates: list[VehicleUpdate]
) -> tuple[int, int]:
    """POST positions + trip-updates. Returns (positions, trip_updates) counts."""
    if not config.ingest_url:
        log.error("CAFE_CAR_INGEST_URL not set, cannot publish")
        return 0, 0

    base = config.ingest_url.rstrip("/")
    position_url = f"{base}/ingest/position"
    trip_update_url = f"{base}/ingest/trip-update"
    headers = {"Authorization": f"Bearer {config.ingest_token}"}

    positions = 0
    trip_updates = 0
    for v in updates:
        try:
            resp = await http.post(
                position_url, json=_position_body(config, v), headers=headers
            )
            resp.raise_for_status()
            positions += 1
        except httpx.HTTPError as exc:
            log.error("position POST failed for %s: %r", v.trip_id, exc)
            continue

        if not v.stop_time_updates:
            continue
        body = {
            "trip_id": v.trip_id,
            "tracker_id": v.tracker_id,
            "timestamp": v.timestamp,
            "stop_time_updates": [
                _stop_time_update_body(u) for u in v.stop_time_updates
            ],
        }
        if v.vehicle_id is not None:
            body["vehicle_id"] = v.vehicle_id
        if v.vehicle_label is not None:
            body["vehicle_label"] = v.vehicle_label
        if v.start_date is not None:
            body["start_date"] = v.start_date
        try:
            resp = await http.post(trip_update_url, json=body, headers=headers)
            resp.raise_for_status()
            trip_updates += 1
        except httpx.HTTPError as exc:
            log.error("trip-update POST failed for %s: %r", v.trip_id, exc)

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
        "tracker_id": config.vehicle_id,
        "alerts": [_alert_body(a) for a in alerts],
    }
    try:
        resp = await http.post(f"{base}/ingest/alerts", json=body, headers=headers)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        log.error("alerts sync POST failed: %r", exc)
        return 0
    return len(alerts)
