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

`--watch` does that accreting for you: it parks on every route that isn't mapped
yet — including unconfirmed candidate slugs — and captures each one the moment it
wakes up, saving after every capture. Leave it running across a service day and
it fills in the map on its own.

Usage:
    uv run python scripts/build_buswhere_map.py            # one pass, live routes
    uv run python scripts/build_buswhere_map.py --watch    # until all are caught
    uv run python scripts/build_buswhere_map.py --watch --routes chatham
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

# Curated buswhere slug -> GTFS route_id. Small and hand-maintained; a 302 means
# "dormant right now", NOT "not a real route", so only add a slug once you've
# seen it serve stops during its own window.
#
# buswhere splits the Albany commuter into one slug per run, while the GTFS has a
# single Albany-Commuter route with eight weekday trips (A_AM 06:15, C_AM 07:00,
# B_PM 14:30, D_PM 16:00, each NB/SB). That's fine: resolve_by_route picks the
# trip by route + scheduled window, so every run slug maps to the one route_id.
# Note the inconsistent separator — AM runs double the underscore, PM runs don't.
ROUTES: dict[str, str] = {
    "shopping_shuttle": "Shopping",
    "hudson__albany_c__am": "Albany-Commuter",
    "hudson__albany_b_pm": "Albany-Commuter",
    "chatham": "Chatham-Hudson",
}

# Slugs we believe exist but have never seen serve stops — the GTFS has A_AM and
# D_PM runs, and the confirmed slugs imply this spelling. --watch polls them; the
# first one that answers is promoted into mapping.json's routes automatically, so
# a guess that's wrong just stays dormant forever and costs nothing.
CANDIDATE_ROUTES: dict[str, str] = {
    "hudson__albany_a__am": "Albany-Commuter",
    "hudson__albany_d_pm": "Albany-Commuter",
}

ALL_ROUTES: dict[str, str] = {**ROUTES, **CANDIDATE_ROUTES}

MAPPING_PATH = (
    Path(__file__).resolve().parent.parent
    / "src/hell_gate_bridge/sources/buswhere/mapping.json"
)

# Nearest-stop match distance: warn above this (needs a human look), reject above
# the hard cap (almost certainly a wrong match). Columbia County stops are blocks
# apart, so even the loose cap is unambiguous.
WARN_METERS = 90.0
REJECT_METERS = 300.0

# buswhere reports the same lat/lon for both roadside platforms of this
# divided-highway stop (Rt. 9 in Valatie), so nearest-distance can't tell them
# apart — it picks the closer GTFS stop for both, leaving the other completely
# unmapped. The GTFS splits this stop by direction and buswhere's own address
# text does too, so match on that first.
STOP_ADDRESS_OVERRIDES: dict[str, str] = {
    "2939 US-9": "STOP-ad8f5dff-5acc-4af6-b1dd-7d567ed433ab",
    "2939 Rt. 9 - Valatie": "STOP-9b2779a3-6fd5-492c-8304-cb4a5f3ffd7f",
}


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


def _load_mapping() -> dict[str, dict]:
    """The committed mapping, or an empty one. Only confirmed routes are seeded."""
    mapping: dict[str, dict] = {"routes": dict(ROUTES), "stops": {}}
    if MAPPING_PATH.exists():
        existing = json.loads(MAPPING_PATH.read_text())
        mapping["stops"] = existing.get("stops", {})
        mapping["routes"].update(existing.get("routes", {}))
    return mapping


def _save_mapping(mapping: dict[str, dict]) -> None:
    mapping["stops"] = dict(sorted(mapping["stops"].items()))
    MAPPING_PATH.parent.mkdir(parents=True, exist_ok=True)
    MAPPING_PATH.write_text(json.dumps(mapping, indent=2) + "\n")


def _map_route(
    slug: str,
    stops: list[dict],
    gtfs_stops: list[tuple[str, str, float, float]],
    mapping: dict[str, dict],
) -> int:
    """Match one route's stops into `mapping`. Returns how many were accepted."""
    print(f"[{slug}] {len(stops)} buswhere stops")
    matched = 0
    for s in stops:
        bid = str(s["id"])
        addr = s.get("address", "")
        override = STOP_ADDRESS_OVERRIDES.get(addr)
        if override is not None:
            gname = next((g[1] for g in gtfs_stops if g[0] == override), "?")
            print(f"  {bid} {addr!r:32} -> {override} {gname!r} (override)")
            mapping["stops"][bid] = override
            matched += 1
            continue
        blat, blon = float(s["lat"]), float(s["lon"])
        gid, gname, dist = min(
            ((g[0], g[1], _haversine_m(blat, blon, g[2], g[3])) for g in gtfs_stops),
            key=lambda x: x[2],
        )
        flag = ""
        if dist > REJECT_METERS:
            flag = " !! REJECTED (too far)"
        elif dist > WARN_METERS:
            flag = " ! review"
        print(f"  {bid} {addr!r:32} -> {gid} {gname!r} ({dist:.0f} m){flag}")
        if dist <= REJECT_METERS:
            mapping["stops"][bid] = gid
            matched += 1
    # A candidate slug that actually served stops is a real route: record it so
    # the runtime source starts polling it too.
    mapping["routes"].setdefault(slug, ALL_ROUTES[slug])
    return matched


def _watch(
    client: httpx.Client,
    slugs: list[str],
    gtfs_stops: list[tuple[str, str, float, float]],
    mapping: dict[str, dict],
    interval: float,
    deadline: float | None,
) -> list[str]:
    """Poll `slugs` until each has been captured, mapping them as they wake up.

    Routes go live on their own schedule and there's no calendar to consult, so
    the only reliable way to map them all is to keep asking. Each capture is
    written immediately — Ctrl-C after six hours keeps everything caught so far.
    Returns the slugs still dormant when the loop ends.
    """
    pending = list(slugs)
    cycle = 0
    try:
        while pending:
            cycle += 1
            for slug in list(pending):
                stops = _fetch_route_stops(client, slug)
                time.sleep(1.0)  # be polite
                if stops is None:
                    continue
                pending.remove(slug)
                _map_route(slug, stops, gtfs_stops, mapping)
                _save_mapping(mapping)
                print(
                    f"  ↳ captured {slug}; {len(mapping['stops'])} total stop "
                    f"mappings written"
                )
            if not pending:
                break
            now = time.time()
            if deadline is not None and now >= deadline:
                print(f"[watch] timed out with {len(pending)} route(s) still dormant")
                break
            nap = interval if deadline is None else min(interval, deadline - now)
            print(
                f"[watch] {time.strftime('%H:%M:%S')} cycle {cycle}: waiting on "
                f"{', '.join(pending)} — next check in {nap / 60:.0f} min "
                f"(Ctrl-C to stop)"
            )
            time.sleep(nap)
    except KeyboardInterrupt:
        print("\n[watch] interrupted — everything captured so far is saved")
    return pending


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Build the buswhere→Columbia County GTFS ID map.",
        epilog="Routes are only mappable while they run, so --watch is the way "
        "to fill in the map without babysitting the clock.",
    )
    ap.add_argument("--gtfs", default=GTFS_URL, help="GTFS .zip path or URL")
    ap.add_argument(
        "--watch",
        action="store_true",
        help="keep running, capturing each route as it goes live (including the "
        "unconfirmed candidate slugs) instead of one pass over whatever is up now",
    )
    ap.add_argument(
        "--routes",
        metavar="SLUGS",
        help="comma-separated slugs to limit this run to (default: every known "
        "route, plus candidates when --watch is set)",
    )
    ap.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="minutes between --watch checks (default 5)",
    )
    ap.add_argument(
        "--timeout",
        type=float,
        default=0.0,
        help="give up watching after N minutes (default 0 = run until every "
        "route has been captured)",
    )
    args = ap.parse_args()

    # Candidates are guesses, so they're only worth the extra polling in --watch,
    # where a dormant route costs nothing but another cycle.
    slugs = list(ALL_ROUTES) if args.watch else list(ROUTES)
    if args.routes:
        slugs = [s.strip() for s in args.routes.split(",") if s.strip()]
        unknown = [s for s in slugs if s not in ALL_ROUTES]
        if unknown:
            ap.error(
                f"unknown slug(s): {', '.join(unknown)}. Known: {', '.join(ALL_ROUTES)}"
            )

    mapping = _load_mapping()
    with httpx.Client(follow_redirects=False, timeout=30) as client:
        gtfs_stops = _load_gtfs_stops(args.gtfs)
        print(f"loaded {len(gtfs_stops)} GTFS stops")

        if args.watch:
            print(f"watching {len(slugs)} route(s): {', '.join(slugs)}")
            deadline = time.time() + args.timeout * 60 if args.timeout else None
            dormant = _watch(
                client, slugs, gtfs_stops, mapping, args.interval * 60, deadline
            )
        else:
            dormant = []
            matched = 0
            for slug in slugs:
                stops = _fetch_route_stops(client, slug)
                time.sleep(1.0)  # be polite
                if stops is None:
                    dormant.append(slug)
                    print(f"[skip] {slug}: dormant (retry when active, or --watch)")
                    continue
                matched += _map_route(slug, stops, gtfs_stops, mapping)
            _save_mapping(mapping)
            print(f"\n{matched} stop matches this run")

    print(f"{MAPPING_PATH}: {len(mapping['stops'])} total stop mappings")
    if dormant:
        print(f"still unmapped: {', '.join(dormant)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
