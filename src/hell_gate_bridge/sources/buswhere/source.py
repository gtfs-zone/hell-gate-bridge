"""buswhere (Columbia County) source.

buswhere reports a route and per-stop ETAs but no trip_id, so this source
resolves the trip itself against the Columbia County GTFS (`resolve_by_route`)
and rewrites buswhere's internal stop IDs to GTFS stop IDs via the committed
mapping. It never trusts a device-supplied trip.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

from hell_gate_bridge.gtfs import GtfsResolver, fetch_gtfs
from hell_gate_bridge.sources.base import Source, StopTimeUpdate, VehicleUpdate

from .client import fetch_route

if TYPE_CHECKING:
    import httpx

    from hell_gate_bridge.config import Config

    from .client import BuswhereObservation

log = logging.getLogger(__name__)

# buswhere reports the ETA to each stop's *next* visit. On a loop, stops the bus
# already passed this trip resolve to the next loop (~a loop-length away), so we
# only keep a stop when its predicted arrival lands near this trip's scheduled
# time for it. This window is comfortably below a loop length and above any
# plausible lateness, and it also assigns a twice-visited stop (loop origin =
# terminus) to the correct occurrence.
_MAX_SCHEDULE_SKEW = 1800  # seconds


def _load_mapping() -> tuple[dict[str, str], dict[str, str]]:
    """(routes: slug->route_id, stops: buswhere_stop_id->gtfs_stop_id)."""
    raw = json.loads(
        resources.files(__package__).joinpath("mapping.json").read_text("utf-8")
    )
    return raw.get("routes", {}), raw.get("stops", {})


class BuswhereSource(Source):
    name = "buswhere"

    def __init__(self, config: Config) -> None:
        self._config = config
        self._resolver: GtfsResolver | None = None
        self._routes, self._stops = _load_mapping()
        # Poll the configured slugs, or every slug we have a route mapping for.
        self._slugs = config.buswhere_routes or list(self._routes)

    async def startup(self, http: httpx.AsyncClient) -> None:
        gtfs_path = await fetch_gtfs(
            self._config.gtfs_url, Path(self._config.gtfs_path), http
        )
        self._resolver = GtfsResolver(gtfs_path)

    def _build(
        self, slug: str, obs: BuswhereObservation, now: datetime
    ) -> VehicleUpdate | None:
        resolver = self._resolver
        assert resolver is not None
        route_id = self._routes.get(slug)
        if route_id is None:
            log.warning("no route mapping for buswhere slug %s", slug)
            return None
        resolved = resolver.resolve_by_route(route_id, now)
        if resolved is None:
            return None
        trip_id, start_date = resolved

        # buswhere ETA keyed by GTFS stop (min ETA if two buswhere stops collapse
        # onto one GTFS stop).
        eta_by_stop: dict[str, float] = {}
        for bid, eta_seconds in obs.stop_eta.items():
            gtfs_stop = self._stops.get(bid)
            if gtfs_stop is None:
                continue  # buswhere stop with no GTFS counterpart
            if gtfs_stop not in eta_by_stop or eta_seconds < eta_by_stop[gtfs_stop]:
                eta_by_stop[gtfs_stop] = eta_seconds

        # Walk the trip's scheduled stops in order; emit the ones whose predicted
        # arrival lands near their scheduled time (i.e. still ahead this loop).
        stop_time_updates: list[StopTimeUpdate] = []
        for seq, gtfs_stop, scheduled in resolver.trip_schedule(trip_id, start_date):
            eta_seconds = eta_by_stop.get(gtfs_stop)
            if eta_seconds is None:
                continue
            arrival_time = obs.timestamp + int(eta_seconds)
            if abs(arrival_time - scheduled) > _MAX_SCHEDULE_SKEW:
                continue  # next-loop arrival, not this trip's visit
            stop_time_updates.append(
                StopTimeUpdate(
                    stop_id=gtfs_stop,
                    stop_sequence=seq,
                    arrival_time=arrival_time,
                    arrival_delay=arrival_time - scheduled,
                )
            )

        # The surviving updates are this trip's remaining visits, in schedule
        # order, so the first one is the stop the bus is running towards.
        # buswhere reports an ETA to every stop and nothing about arrival, so
        # IN_TRANSIT_TO is all we can honestly claim — never STOPPED_AT.
        next_stop = stop_time_updates[0] if stop_time_updates else None

        return VehicleUpdate(
            tracker_id=self._config.vehicle_id,
            # buswhere gives no stable per-device id we can trust for
            # uniqueness (the same physical device has been observed echoed
            # across two different routes' live snapshots at once), so this
            # is display-only; cafe-car's ingest guarantees a unique
            # VehicleDescriptor.id regardless of whether this is set.
            vehicle_label=obs.vehicle_name or slug,
            trip_id=trip_id,
            start_date=start_date,
            timestamp=obs.timestamp,
            lat=obs.lat,
            lon=obs.lon,
            route_id=route_id,
            # buswhere's `speed` unit is ambiguous (GPS knots vs mph) and its
            # feed carries no heading, so we publish neither rather than emit a
            # wrong-unit value.
            speed_mps=None,
            bearing=None,
            current_stop_sequence=next_stop.stop_sequence if next_stop else None,
            current_stop_id=next_stop.stop_id if next_stop else None,
            current_status="IN_TRANSIT_TO" if next_stop else None,
            stop_time_updates=stop_time_updates,
        )

    async def fetch(self, http: httpx.AsyncClient) -> list[VehicleUpdate]:
        assert self._resolver is not None, "startup() must run before fetch()"

        updates: list[VehicleUpdate] = []
        unresolved: list[str] = []
        for slug in self._slugs:
            try:
                obs = await fetch_route(http, slug)
            except Exception:
                # One malformed/unreachable route must not abort the whole
                # cycle and suppress the routes that parse cleanly.
                log.exception("buswhere: route %s failed, skipping", slug)
                continue
            if obs is None:
                continue  # dormant / nothing running
            now = (
                datetime.fromtimestamp(obs.timestamp, tz=UTC)
                if obs.timestamp
                else datetime.now(UTC)
            )
            update = self._build(slug, obs, now)
            if update is None:
                unresolved.append(slug)
                continue
            updates.append(update)

        if unresolved:
            log.warning(
                "buswhere: %d route(s) running but no scheduled trip matched (%s)",
                len(unresolved),
                ", ".join(unresolved),
            )
        return updates
