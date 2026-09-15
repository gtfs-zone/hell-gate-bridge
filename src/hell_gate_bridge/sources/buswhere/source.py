"""buswhere (Columbia County) source.

buswhere reports a route and per-stop ETAs but no trip_id, so this source
resolves the trip itself against the Columbia County GTFS (`resolve_by_route`)
and rewrites buswhere's internal stop IDs to GTFS stop IDs via the committed
mapping. It never trusts a device-supplied trip.

A route's snapshot also lists buses running *other* routes, so each cycle first
attributes devices to routes (`_attribute`) and only then builds updates.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

from hell_gate_bridge.gtfs import GtfsResolver, fetch_gtfs
from hell_gate_bridge.sources.base import Source, StopTimeUpdate, VehicleUpdate

from .client import BuswhereObservation, fetch_route

if TYPE_CHECKING:
    import httpx

    from hell_gate_bridge.config import Config

log = logging.getLogger(__name__)

# buswhere reports the ETA to each stop's *next* visit. On a loop, stops the bus
# already passed this trip resolve to the next loop (~a loop-length away), so we
# only keep a stop when its predicted arrival lands near this trip's scheduled
# time for it. This window is comfortably below a loop length and above any
# plausible lateness, and it also assigns a twice-visited stop (loop origin =
# terminus) to the correct occurrence.
_MAX_SCHEDULE_SKEW = 1800  # seconds


@dataclass(frozen=True)
class _RouteMapping:
    """A buswhere slug's GTFS route, and optionally which of its trips to use."""

    route_id: str
    # Set when several slugs share one route_id and their scheduled windows
    # overlap; None means "any trip on the route".
    allowed_trips: frozenset[str] | None = None


def _load_mapping() -> tuple[dict[str, _RouteMapping], dict[str, str], set[str]]:
    """(routes, stops: buswhere_stop_id->gtfs_stop_id, reviewed-as-unmappable)."""
    raw = json.loads(
        resources.files(__package__).joinpath("mapping.json").read_text("utf-8")
    )
    routes: dict[str, _RouteMapping] = {}
    for slug, entry in raw.get("routes", {}).items():
        # A bare string is the common case: one slug, one route, no ambiguity.
        if isinstance(entry, str):
            routes[slug] = _RouteMapping(entry)
        else:
            trips = entry.get("trips")
            routes[slug] = _RouteMapping(
                entry["route_id"], frozenset(trips) if trips else None
            )
    return routes, raw.get("stops", {}), set(raw.get("unmapped", []))


def _distance_sq(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Squared degree distance. Only ever compared, never reported."""
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2


def _attribute(
    snapshots: dict[str, list[BuswhereObservation]],
) -> dict[str, list[BuswhereObservation]]:
    """Assign each device to the route it is actually running.

    A snapshot's `current` tracks the bus running that route, so the device
    nearest it owns the route. An owned device is dropped from every other
    snapshot, which is what removes the echoes. Devices no route claims are left
    where they were found, so a route genuinely running a second bus keeps it.

    A bus that has just switched runs is briefly the nearest device to both
    snapshots' `current`, the route it left having stopped updating. The tighter
    match is the route it is on now, so the claims are compared by distance
    rather than resolved by whichever route was polled last.
    """
    # device_id -> (distance to the claiming route's `current`, slug)
    claims: dict[int, tuple[float, str]] = {}
    for slug, observations in snapshots.items():
        current = next((o.route_current for o in observations if o.route_current), None)
        if current is None:
            continue
        owner = min(observations, key=lambda o: _distance_sq((o.lat, o.lon), current))
        if owner.device_id is None:
            continue
        claim = (_distance_sq((owner.lat, owner.lon), current), slug)
        existing = claims.get(owner.device_id)
        if existing is None or claim < existing:
            claims[owner.device_id] = claim
    owners = {device_id: slug for device_id, (_, slug) in claims.items()}

    return {
        slug: [
            o
            for o in observations
            if o.device_id is None or owners.get(o.device_id, slug) == slug
        ]
        for slug, observations in snapshots.items()
    }


class BuswhereSource(Source):
    name = "buswhere"

    def __init__(self, config: Config) -> None:
        self._config = config
        self._resolver: GtfsResolver | None = None
        self._routes, self._stops, self._unmappable = _load_mapping()
        # Poll the configured slugs, or every slug we have a route mapping for.
        self._slugs = config.buswhere_routes or list(self._routes)

    async def startup(self, http: httpx.AsyncClient) -> None:
        gtfs_path = await fetch_gtfs(
            self._config.gtfs_url, Path(self._config.gtfs_path), http
        )
        self._resolver = GtfsResolver(gtfs_path)

    def _build(
        self,
        slug: str,
        obs: BuswhereObservation,
        now: datetime,
        unmapped_stops: set[str] | None = None,
    ) -> VehicleUpdate | None:
        resolver = self._resolver
        assert resolver is not None
        mapping = self._routes.get(slug)
        if mapping is None:
            log.warning("no route mapping for buswhere slug %s", slug)
            return None
        route_id = mapping.route_id
        resolved = resolver.resolve_by_route(route_id, now, mapping.allowed_trips)
        if resolved is None:
            return None
        trip_id, start_date = resolved

        # buswhere ETA keyed by GTFS stop (min ETA if two buswhere stops collapse
        # onto one GTFS stop).
        eta_by_stop: dict[str, float] = {}
        for bid, eta_seconds in obs.stop_eta.items():
            gtfs_stop = self._stops.get(bid)
            if gtfs_stop is None:
                # Stops reviewed and found to have no GTFS counterpart at all are
                # expected; only an unreviewed one is worth a warning.
                if unmapped_stops is not None and bid not in self._unmappable:
                    unmapped_stops.add(bid)
                continue
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
        # IN_TRANSIT_TO is all we can honestly claim, never STOPPED_AT.
        next_stop = stop_time_updates[0] if stop_time_updates else None

        return VehicleUpdate(
            tracker_id=self._config.vehicle_id,
            # buswhere's device_id is stable within a snapshot, which is all
            # cafe-car needs to keep concurrent buses on one tracker apart.
            vehicle_id=(
                f"{slug}:{obs.device_id}" if obs.device_id is not None else None
            ),
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

        snapshots: dict[str, list[BuswhereObservation]] = {}
        for slug in self._slugs:
            try:
                snapshots[slug] = await fetch_route(http, slug)
            except Exception:
                # One malformed/unreachable route must not abort the whole
                # cycle and suppress the routes that parse cleanly.
                log.exception("buswhere: route %s failed, skipping", slug)

        owned = _attribute(snapshots)

        updates: list[VehicleUpdate] = []
        unresolved: set[str] = set()
        unmapped_stops: set[str] = set()
        for slug, observations in owned.items():
            dropped = len(snapshots[slug]) - len(observations)
            if dropped:
                log.debug(
                    "buswhere: %s dropped %d device(s) running another route",
                    slug,
                    dropped,
                )
            for obs in observations:
                now = (
                    datetime.fromtimestamp(obs.timestamp, tz=UTC)
                    if obs.timestamp
                    else datetime.now(UTC)
                )
                update = self._build(slug, obs, now, unmapped_stops)
                if update is None:
                    unresolved.add(slug)
                    continue
                updates.append(update)

        if unresolved:
            log.warning(
                "buswhere: %d route(s) running but no scheduled trip matched (%s)",
                len(unresolved),
                ", ".join(sorted(unresolved)),
            )
        if unmapped_stops:
            log.warning(
                "buswhere: %d stop id(s) with no GTFS mapping this cycle (%s)",
                len(unmapped_stops),
                ", ".join(sorted(unmapped_stops)),
            )
        return updates
