"""buswhere per-route live client.

buswhere serves a JSON snapshot per route at
`/columbiacountyny/routes/{slug}?initial=true`. A dormant route redirects to the
default route (so the response isn't the JSON we asked for) — we treat that, and
any inactive/suspended/vehicle-less route, as "nothing running".
"""

from __future__ import annotations

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
    """One running vehicle on a buswhere route."""

    lat: float
    lon: float
    timestamp: int  # epoch seconds (device fix time)
    # buswhere stop_id (str) -> seconds until the bus next reaches that stop.
    stop_eta: dict[str, float]


async def fetch_route(
    http: httpx.AsyncClient, slug: str, base_url: str = BASE_URL
) -> BuswhereObservation | None:
    """Fetch a route's live snapshot, or None if nothing is running on it."""
    resp = await http.get(
        f"{base_url}/{slug}",
        params={"initial": "true", "filter": ""},
        headers=_HEADERS,
        follow_redirects=False,
    )
    if resp.status_code != 200:
        return None  # dormant routes 302-redirect to the default route
    try:
        data = resp.json()
    except ValueError:
        return None

    if not data.get("active") or data.get("suspended"):
        return None
    devices = data.get("devices") or []
    if not devices:
        return None

    device = devices[0]
    pos = device.get("position") or data.get("current") or {}
    lat, lon = pos.get("lat"), pos.get("lon")
    if lat is None or lon is None:
        return None

    # Upstream stop_eta values are a number, null, or a sentinel string such as
    # "departed" (the bus already passed that stop this trip). Anything that
    # isn't a numeric ETA simply means "no upcoming arrival to predict here".
    stop_eta: dict[str, float] = {}
    for sid, secs in (data.get("stop_eta") or {}).items():
        try:
            stop_eta[str(sid)] = float(secs)
        except (TypeError, ValueError):
            continue
    return BuswhereObservation(
        lat=float(lat),
        lon=float(lon),
        timestamp=int(device.get("updated_at", 0)),
        stop_eta=stop_eta,
    )
