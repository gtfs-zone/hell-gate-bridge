"""buswhere per-route live client.

buswhere serves a JSON snapshot per route at
`/columbiacountyny/routes/{slug}?initial=true`. A dormant route redirects to the
default route (so the response isn't the JSON we asked for), and we treat that, and
any inactive/suspended/vehicle-less route, as "nothing running".

A snapshot's `devices` list is not confined to the route asked for: buses running
other routes are echoed into it. So this module returns one observation *per
device* and leaves attribution to the source, which sees the whole cycle. Two
fields make that possible: `device_id`, the only stable discriminator across
routes, and `route_current` (the snapshot's `current`), which tracks the route's
own bus.

ETAs are likewise per device. The top-level `stop_eta` is an aggregate over every
device in the snapshot, so on a multi-device snapshot it blends buses; the
per-device breakdown lives in `other_routes_stop_eta`, keyed by stop with
`{eta, device, device_id, route_id}` entries.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx

BASE_URL = "https://buswhere.com/columbiacountyny/routes"

_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}


@dataclass
class BuswhereObservation:
    """One device seen in a buswhere route snapshot.

    Seen in it, not necessarily running it: the caller decides which snapshot
    owns this device (see `route_current`).
    """

    lat: float
    lon: float
    timestamp: int  # epoch seconds (device fix time)
    # buswhere stop_id (str) -> seconds until this bus next reaches that stop.
    stop_eta: dict[str, float]
    # buswhere's own per-bus identifier (e.g. "C5"), when the feed carries one.
    vehicle_name: str | None = None
    # buswhere's internal device id; the discriminator when one device is
    # echoed into several routes' snapshots.
    device_id: int | None = None
    # The snapshot's `current` position, which tracks the bus actually running
    # this route (a fix or so stale). Same value on every observation from one
    # snapshot.
    route_current: tuple[float, float] | None = None


def _numeric_etas(raw: dict[str, object] | None) -> dict[str, float]:
    """Coerce a {stop_id: eta} mapping, dropping anything that isn't a number.

    Upstream ETA values are a number, null, or a sentinel string such as
    "departed"/"arrived" (the bus already passed that stop this trip). Anything
    that isn't numeric simply means "no upcoming arrival to predict here".
    """
    etas: dict[str, float] = {}
    for sid, secs in (raw or {}).items():
        try:
            etas[str(sid)] = float(secs)
        except (TypeError, ValueError):
            continue
    return etas


def _etas_for_device(
    other: dict[str, list[dict]] | None, device_id: int | None
) -> dict[str, float]:
    """Pick one device's ETAs out of `other_routes_stop_eta`."""
    etas: dict[str, float] = {}
    for sid, entries in (other or {}).items():
        for entry in entries or []:
            if entry.get("device_id") != device_id:
                continue
            with contextlib.suppress(TypeError, ValueError):
                etas[str(sid)] = float(entry.get("eta"))
            break
    return etas


async def fetch_route(
    http: httpx.AsyncClient, slug: str, base_url: str = BASE_URL
) -> list[BuswhereObservation]:
    """Fetch a route's live snapshot as one observation per device.

    Empty when nothing is running on the route.
    """
    resp = await http.get(
        f"{base_url}/{slug}",
        params={"initial": "true", "filter": ""},
        headers=_HEADERS,
        follow_redirects=False,
    )
    if resp.status_code != 200:
        return []  # dormant routes 302-redirect to the default route
    try:
        data = resp.json()
    except ValueError:
        return []

    if not data.get("active") or data.get("suspended"):
        return []
    devices = data.get("devices") or []
    if not devices:
        return []

    current = data.get("current") or {}
    route_current = (
        (float(current["lat"]), float(current["lon"]))
        if current.get("lat") is not None and current.get("lon") is not None
        else None
    )
    other = data.get("other_routes_stop_eta")
    # The aggregate is only unambiguous when it describes a single bus, and it
    # is the sole source when the per-device breakdown is missing.
    aggregate = _numeric_etas(data.get("stop_eta"))

    observations: list[BuswhereObservation] = []
    for device in devices:
        pos = device.get("position") or (current if len(devices) == 1 else {})
        lat, lon = pos.get("lat"), pos.get("lon")
        if lat is None or lon is None:
            continue
        device_id = device.get("device_id")
        if other and len(devices) > 1:
            stop_eta = _etas_for_device(other, device_id)
        elif other:
            stop_eta = _etas_for_device(other, device_id) or aggregate
        else:
            stop_eta = aggregate
        name = device.get("name")
        observations.append(
            BuswhereObservation(
                lat=float(lat),
                lon=float(lon),
                timestamp=int(device.get("updated_at", 0)),
                stop_eta=stop_eta,
                # buswhere pads some names (" C1"); the raw value would reach
                # the GTFS-RT VehicleDescriptor.label verbatim.
                vehicle_name=name.strip() if isinstance(name, str) else name,
                device_id=device_id,
                route_current=route_current,
            )
        )
    return observations
