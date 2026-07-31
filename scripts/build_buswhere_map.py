#!/usr/bin/env python3
"""Build (or extend) the buswhere→Columbia County GTFS ID map.

buswhere exposes its own numeric stop IDs; we serve the Columbia County GTFS we
control. This script matches each buswhere stop to the nearest GTFS stop by
coordinate and writes `sources/buswhere/mapping.json`.

buswhere only serves a route's HTML (with the embedded `stops` array) **while
that route is active** — a dormant route 302-redirects to the default route. So
run this when the routes you care about are running (e.g. the Albany commuter in
the morning); it MERGES into any existing mapping.json, so several runs across
the day accrete the full map. Review the printed report before committing.

`--wait` parks on a route until it wakes up, so you can start the script before
service begins instead of watching the clock.

Usage:
    uv run python scripts/build_buswhere_map.py [--gtfs PATH_OR_URL]
    uv run python scripts/build_buswhere_map.py --wait hudson__albany_c__am
"""

from __future__ import annotations

import argparse
import io
import json
import math
import re
import sys
import time
import zipfile
from pathlib import Path

import httpx

GTFS_URL = (
    "https://github.com/columbia-county-ny-transit/gtfs-generator/"
    "raw/refs/heads/main/columbia_county_gtfs.zip"
)
BUSWHERE_BASE = "https://buswhere.com/columbiacountyny/routes"

# Curated buswhere slug -> GTFS route_id. Small and hand-maintained; a slug that
# 302-redirects when fetched isn't a real route (e.g. a guessed `_pm`).
ROUTES: dict[str, str] = {
    "shopping_shuttle": "Shopping",
    "hudson__albany_c__am": "Albany-Commuter",
    "chatham": "Chatham-Hudson",
}

MAPPING_PATH = (
    Path(__file__).resolve().parent.parent
    / "src/hell_gate_bridge/sources/buswhere/mapping.json"
)

# Nearest-stop match distance: warn above this (needs a human look), reject above
# the hard cap (almost certainly a wrong match). Columbia County stops are blocks
# apart, so even the loose cap is unambiguous.
WARN_METERS = 90.0
REJECT_METERS = 300.0


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _load_gtfs_stops(gtfs: str) -> list[tuple[str, str, float, float]]:
    """Return [(stop_id, stop_name, lat, lon)] from a GTFS path or URL."""
    if gtfs.startswith("http"):
        data = httpx.get(gtfs, follow_redirects=True, timeout=60).content
    else:
        data = Path(gtfs).read_bytes()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        text = zf.read("stops.txt").decode("utf-8")
    import csv

    out = []
    for row in csv.DictReader(io.StringIO(text)):
        out.append(
            (
                row["stop_id"],
                row["stop_name"],
                float(row["stop_lat"]),
                float(row["stop_lon"]),
            )
        )
    return out


def _extract_stops(html: str) -> list[dict]:
    """Pull the embedded `stops` JSON array out of a route HTML page.

    Balanced bracket scan (string-aware) so an address containing `]` can't cut
    the array short. Returns the longest valid array found, or [].
    """
    best: list[dict] = []
    for m in re.finditer(r'"stops":\[', html):
        start = m.end() - 1
        depth = 0
        j = start
        in_str = False
        esc = False
        while j < len(html):
            c = html[j]
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = not in_str
            elif not in_str:
                if c == "[":
                    depth += 1
                elif c == "]":
                    depth -= 1
                    if depth == 0:
                        break
            j += 1
        try:
            arr = json.loads(html[start : j + 1])
        except json.JSONDecodeError:
            continue
        if (
            isinstance(arr, list)
            and len(arr) > len(best)
            and all("id" in s and "lat" in s for s in arr)
        ):
            best = arr
    return best


def _fetch_route_stops(client: httpx.Client, slug: str) -> list[dict] | None:
    """buswhere stops for a route, or None if the route is dormant/unavailable."""
    resp = client.get(
        f"{BUSWHERE_BASE}/{slug}",
        headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"},
    )
    if resp.status_code in (301, 302) or not resp.text.strip():
        return None  # dormant route redirects to the default route
    stops = _extract_stops(resp.text)
    return stops or None


def _wait_for_routes(
    client: httpx.Client,
    slugs: list[str],
    interval: float,
    deadline: float | None,
) -> dict[str, list[dict]]:
    """Poll `slugs` until each serves stops. Returns whatever went live.

    Stops fetched here are handed to the build pass, so waking a route costs one
    fetch, not two. Ctrl-C returns early with whatever is already in hand.
    """
    live: dict[str, list[dict]] = {}
    pending = list(slugs)
    attempt = 0
    try:
        while pending:
            attempt += 1
            for slug in list(pending):
                stops = _fetch_route_stops(client, slug)
                if stops is not None:
                    live[slug] = stops
                    pending.remove(slug)
                    print(f"[wake] {slug}: live with {len(stops)} stops")
                time.sleep(1.0)  # be polite
            if not pending:
                break
            now = time.time()
            if deadline is not None and now >= deadline:
                print(f"[wait] timed out with {', '.join(pending)} still dormant")
                break
            stamp = time.strftime("%H:%M:%S")
            nap = interval if deadline is None else min(interval, deadline - now)
            print(
                f"[wait] {stamp} check #{attempt}: {', '.join(pending)} still "
                f"dormant — retrying in {nap / 60:.1f} min (Ctrl-C to stop)"
            )
            time.sleep(nap)
    except KeyboardInterrupt:
        print("\n[wait] interrupted — building with what we have")
    return live


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gtfs", default=GTFS_URL, help="GTFS .zip path or URL")
    ap.add_argument(
        "--wait",
        metavar="SLUGS",
        help="comma-separated route slugs to poll until they go live before "
        "building (e.g. hudson__albany_c__am)",
    )
    ap.add_argument(
        "--wait-interval",
        type=float,
        default=5.0,
        help="minutes between --wait checks (default 5)",
    )
    ap.add_argument(
        "--wait-timeout",
        type=float,
        default=0.0,
        help="give up waiting after N minutes (default 0 = wait indefinitely)",
    )
    args = ap.parse_args()

    wait_slugs: list[str] = []
    if args.wait:
        wait_slugs = [s.strip() for s in args.wait.split(",") if s.strip()]
        unknown = [s for s in wait_slugs if s not in ROUTES]
        if unknown:
            ap.error(
                f"unknown slug(s): {', '.join(unknown)}. Known: {', '.join(ROUTES)}"
            )

    mapping: dict[str, dict] = {"routes": dict(ROUTES), "stops": {}}
    if MAPPING_PATH.exists():
        existing = json.loads(MAPPING_PATH.read_text())
        mapping["stops"] = existing.get("stops", {})
        mapping["routes"].update(existing.get("routes", {}))

    matched = 0
    dormant: list[str] = []
    with httpx.Client(follow_redirects=False, timeout=30) as client:
        prefetched: dict[str, list[dict]] = {}
        if wait_slugs:
            print(f"waiting for {', '.join(wait_slugs)} to go live…")
            deadline = (
                time.time() + args.wait_timeout * 60 if args.wait_timeout else None
            )
            prefetched = _wait_for_routes(
                client, wait_slugs, args.wait_interval * 60, deadline
            )

        # Fetched after any wait so an overnight park doesn't build against a
        # GTFS snapshot from hours ago.
        gtfs_stops = _load_gtfs_stops(args.gtfs)
        print(f"loaded {len(gtfs_stops)} GTFS stops")

        for slug in ROUTES:
            stops = prefetched.pop(slug, None)
            if stops is None:
                stops = _fetch_route_stops(client, slug)
                time.sleep(1.0)  # be polite
            if stops is None:
                dormant.append(slug)
                print(f"[skip] {slug}: dormant/unavailable (run when active)")
                continue
            print(f"[{slug}] {len(stops)} buswhere stops")
            for s in stops:
                bid = str(s["id"])
                blat, blon = float(s["lat"]), float(s["lon"])
                gid, gname, dist = min(
                    (
                        (g[0], g[1], _haversine_m(blat, blon, g[2], g[3]))
                        for g in gtfs_stops
                    ),
                    key=lambda x: x[2],
                )
                flag = ""
                if dist > REJECT_METERS:
                    flag = " !! REJECTED (too far)"
                elif dist > WARN_METERS:
                    flag = " ! review"
                addr = s.get("address", "")
                print(f"  {bid} {addr!r:32} -> {gid} {gname!r} ({dist:.0f} m){flag}")
                if dist <= REJECT_METERS:
                    mapping["stops"][bid] = gid
                    matched += 1

    mapping["stops"] = dict(sorted(mapping["stops"].items()))
    MAPPING_PATH.parent.mkdir(parents=True, exist_ok=True)
    MAPPING_PATH.write_text(json.dumps(mapping, indent=2) + "\n")
    print(
        f"\nwrote {MAPPING_PATH} — {len(mapping['stops'])} total stop mappings "
        f"({matched} matched this run)"
    )
    if dormant:
        print(f"dormant routes not updated this run: {', '.join(dormant)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
