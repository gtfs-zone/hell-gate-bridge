#!/usr/bin/env python3
"""Build (or extend) the buswhere→Columbia County GTFS ID map.

buswhere exposes its own numeric stop IDs; we serve the Columbia County GTFS we
control. This script matches each buswhere stop to the nearest GTFS stop by
coordinate (with a name-similarity tiebreak for stops that share coordinates)
and writes `sources/buswhere/mapping.json`.

buswhere only serves a route's HTML (with the embedded `stops` array) **while
that route is active** — a dormant route 302-redirects to the default route. So
run this when the routes you care about are running (e.g. the Albany commuter in
the morning); it MERGES into any existing mapping.json, so several runs across
the day accrete the full map. Review the logged report before committing.

`--watch` does that accreting for you: it parks on every route that isn't mapped
yet — including unconfirmed candidate slugs — and captures each one the moment it
wakes up, saving after every capture. Leave it running across a service day and
it fills in the map on its own.

Stops that can't be matched confidently (too far from any GTFS stop, or tied
between two GTFS stops with no decisive name match) are written to
`buswhere_map_review.json` instead of being silently dropped. Run with
`--interactive` to resolve them by hand; the exit code stays non-zero until
every pending stop has been resolved or explicitly marked "no match".

Usage:
    uv run python scripts/build_buswhere_map.py                  # one pass, live routes
    uv run python scripts/build_buswhere_map.py --watch          # until all are caught
    uv run python scripts/build_buswhere_map.py --watch --routes chatham
    uv run python scripts/build_buswhere_map.py --interactive    # resolve the backlog
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import math
import re
import sys
import time
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path

import httpx

log = logging.getLogger("build_buswhere_map")

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
REVIEW_PATH = Path(__file__).resolve().parent / "buswhere_map_review.json"

# Nearest-stop match distance: warn above this (needs a human look), reject above
# the hard cap (almost certainly a wrong match). Columbia County stops are blocks
# apart, so even the loose cap is unambiguous — except for the handful of GTFS
# stops that share (near-)identical coordinates under different names; those are
# handled by the cluster/fuzzy-name tiebreak below, not this threshold.
WARN_METERS = 90.0
REJECT_METERS = 300.0

# Within this, distance alone is trusted (no name check) — GPS jitter, not a
# different physical stop. Beyond it, a single nearest candidate ALSO needs a
# decent name match to auto-accept; otherwise it's the "Greenport" trap: one
# generic buswhere address covering several distinct, spread-out GTFS stops,
# where nearest-by-distance quietly picks the wrong one every time.
SURE_METERS = 25.0
MIN_SINGLE_FUZZY_SCORE = 0.45

# GTFS stops within this of the single nearest candidate are treated as a tied
# cluster requiring a name-based tiebreak rather than a plain nearest-neighbor
# pick. The confirmed duplicate-coordinate pairs in this GTFS are ~0m apart;
# genuinely distinct stops are blocks apart, so this can't false-trigger.
CLUSTER_METERS = 20.0

# A cluster tie is only auto-resolved when the top fuzzy-name match is both
# decent on its own and clearly ahead of the runner-up; otherwise it goes to
# manual review rather than guessing.
MIN_FUZZY_SCORE = 0.35
MIN_FUZZY_MARGIN = 0.1

# buswhere reports the same lat/lon for both roadside platforms of this
# divided-highway stop (Rt. 9 in Valatie), so nearest-distance can't tell them
# apart — it picks the closer GTFS stop for both, leaving the other completely
# unmapped. The GTFS splits this stop by direction and buswhere's own address
# text does too, so match on that first.
STOP_ADDRESS_OVERRIDES: dict[str, str] = {
    "2939 US-9": "STOP-ad8f5dff-5acc-4af6-b1dd-7d567ed433ab",
    "2939 Rt. 9 - Valatie": "STOP-9b2779a3-6fd5-492c-8304-cb4a5f3ffd7f",
}


@dataclass
class Candidate:
    stop_id: str
    stop_name: str
    dist_m: float
    fuzzy_score: float


@dataclass
class MatchResult:
    outcome: str  # "matched" | "review"
    stop_id: str | None = None
    candidates: list[Candidate] = field(default_factory=list)
    reason: str = ""  # "REJECT" | "TIE", set when outcome == "review"
    warn: bool = False  # matched, but past WARN_METERS — still worth a human glance


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _normalize_text(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _fuzzy_score(a: str, b: str) -> float:
    return SequenceMatcher(None, _normalize_text(a), _normalize_text(b)).ratio()


def _fetch_gtfs_bytes(gtfs: str) -> bytes:
    if gtfs.startswith("http"):
        return httpx.get(gtfs, follow_redirects=True, timeout=60).content
    return Path(gtfs).read_bytes()


def _load_gtfs_stops(data: bytes) -> list[tuple[str, str, float, float]]:
    """Return [(stop_id, stop_name, lat, lon)] from GTFS zip bytes."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        text = zf.read("stops.txt").decode("utf-8")
    return [
        (
            row["stop_id"],
            row["stop_name"],
            float(row["stop_lat"]),
            float(row["stop_lon"]),
        )
        for row in csv.DictReader(io.StringIO(text))
    ]


def _load_route_stop_ids(data: bytes) -> dict[str, set[str]]:
    """route_id -> set of stop_ids scheduled on it, from trips.txt + stop_times.txt."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        trips_text = zf.read("trips.txt").decode("utf-8")
        stop_times_text = zf.read("stop_times.txt").decode("utf-8")
    trip_to_route = {
        row["trip_id"]: row["route_id"]
        for row in csv.DictReader(io.StringIO(trips_text))
    }
    out: dict[str, set[str]] = {}
    for row in csv.DictReader(io.StringIO(stop_times_text)):
        route_id = trip_to_route.get(row["trip_id"])
        if route_id is None:
            continue
        out.setdefault(route_id, set()).add(row["stop_id"])
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


def _load_review(path: Path) -> dict[str, dict]:
    review = json.loads(path.read_text()) if path.exists() else {}
    review.setdefault("pending", {})
    review.setdefault("resolved", {})
    return review


def _save_review(review: dict[str, dict], path: Path) -> None:
    review["pending"] = dict(sorted(review["pending"].items()))
    review["resolved"] = dict(sorted(review["resolved"].items()))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(review, indent=2) + "\n")


def _prune_stale_resolutions(review: dict[str, dict], gtfs_stop_ids: set[str]) -> None:
    """Drop "map" resolutions pointing at a stop_id that no longer exists in the
    current GTFS (regenerated feeds can renumber stop_ids), so they're re-reviewed
    instead of silently trusted."""
    stale = [
        bid
        for bid, r in review["resolved"].items()
        if r.get("decision") == "map" and r.get("stop_id") not in gtfs_stop_ids
    ]
    for bid in stale:
        log.warning(
            "review: resolved mapping for %s points at a stop_id no longer in "
            "the GTFS (%s) — treating as unresolved again",
            bid,
            review["resolved"][bid].get("stop_id"),
        )
        del review["resolved"][bid]


def _rank_candidates(
    blat: float,
    blon: float,
    text: str,
    gtfs_stops: list[tuple[str, str, float, float]],
    top_n: int = 5,
) -> list[Candidate]:
    ranked = sorted(
        (
            Candidate(
                stop_id=g[0],
                stop_name=g[1],
                dist_m=_haversine_m(blat, blon, g[2], g[3]),
                fuzzy_score=_fuzzy_score(text, g[1]),
            )
            for g in gtfs_stops
        ),
        key=lambda c: c.dist_m,
    )
    return ranked[:top_n]


def _match_stop(
    blat: float,
    blon: float,
    text: str,
    gtfs_stops: list[tuple[str, str, float, float]],
) -> MatchResult:
    candidates = _rank_candidates(blat, blon, text, gtfs_stops)
    nearest = candidates[0]
    cluster = [c for c in candidates if c.dist_m <= nearest.dist_m + CLUSTER_METERS]

    if len(cluster) > 1:
        by_fuzzy = sorted(cluster, key=lambda c: c.fuzzy_score, reverse=True)
        best, runner_up = by_fuzzy[0], by_fuzzy[1]
        if (
            best.fuzzy_score >= MIN_FUZZY_SCORE
            and best.fuzzy_score - runner_up.fuzzy_score >= MIN_FUZZY_MARGIN
        ):
            return MatchResult(
                outcome="matched",
                stop_id=best.stop_id,
                candidates=candidates,
                warn=best.dist_m > WARN_METERS,
            )
        return MatchResult(outcome="review", candidates=candidates, reason="TIE")

    if nearest.dist_m > REJECT_METERS:
        return MatchResult(outcome="review", candidates=candidates, reason="REJECT")
    if nearest.dist_m <= SURE_METERS:
        # Close enough that this is almost certainly the same physical stop
        # regardless of how buswhere worded the address.
        return MatchResult(
            outcome="matched", stop_id=nearest.stop_id, candidates=candidates
        )
    if nearest.fuzzy_score >= MIN_SINGLE_FUZZY_SCORE:
        return MatchResult(
            outcome="matched",
            stop_id=nearest.stop_id,
            candidates=candidates,
            warn=nearest.dist_m > WARN_METERS,
        )
    # Close enough to be plausible, but the name doesn't back it up — likely a
    # generic buswhere address (e.g. "Greenport") shared across several
    # distinct GTFS stops, only one of which this actually is.
    return MatchResult(outcome="review", candidates=candidates, reason="LOW_CONFIDENCE")


def _search_gtfs_stops(
    query: str,
    blat: float,
    blon: float,
    gtfs_stops: list[tuple[str, str, float, float]],
    top_n: int = 10,
) -> list[Candidate]:
    """Every GTFS stop ranked by name similarity to `query`, not by distance —
    for finding a correct-but-far-away match the nearest-N list missed (e.g.
    buswhere's generic "Greenport" address covering several distinct, spread
    out GTFS stops, only one of which is actually named "Greenport")."""
    ranked = sorted(
        (
            Candidate(
                stop_id=g[0],
                stop_name=g[1],
                dist_m=_haversine_m(blat, blon, g[2], g[3]),
                fuzzy_score=_fuzzy_score(query, g[1]),
            )
            for g in gtfs_stops
        ),
        key=lambda c: c.fuzzy_score,
        reverse=True,
    )
    return ranked[:top_n]


def _prompt_interactive(
    bid: str,
    addr: str,
    candidates: list[Candidate],
    blat: float,
    blon: float,
    gtfs_stops: list[tuple[str, str, float, float]],
) -> tuple[str, str | None]:
    """Prompt the user for one review-backlog stop.

    Returns (decision, stop_id) where decision is "map", "no_match", or "skip".
    Nearest-distance candidates aren't guaranteed to include the right answer
    (buswhere sometimes uses one generic address for several spread-out GTFS
    stops), so typing anything that isn't a number/k/Enter re-searches every
    GTFS stop by name instead of just the nearby ones.
    """
    current = candidates
    while True:
        print(f"\nbuswhere stop {bid} ({addr!r})")
        for i, c in enumerate(current, start=1):
            print(
                f"  [{i}] {c.stop_id} {c.stop_name!r} — "
                f"{c.dist_m:.0f}m, fuzzy {c.fuzzy_score:.2f}"
            )
        print(
            "  [k] no correct match here   [Enter] skip for now   "
            "(or type a name to search all stops)"
        )
        choice = input("choice: ").strip()
        if not choice:
            return "skip", None
        if choice.lower() == "k":
            return "no_match", None
        if choice.isdigit() and 1 <= int(choice) <= len(current):
            return "map", current[int(choice) - 1].stop_id
        results = _search_gtfs_stops(choice, blat, blon, gtfs_stops)
        if not results:
            print("  (no stops matched that search, try again)")
            continue
        current = results


def _map_route(
    slug: str,
    stops: list[dict],
    gtfs_stops: list[tuple[str, str, float, float]],
    mapping: dict[str, dict],
    review: dict[str, dict],
    interactive: bool,
) -> int:
    """Match one route's stops into `mapping`. Returns how many were accepted."""
    log.info("[%s] %d buswhere stops", slug, len(stops))
    now_iso = datetime.now(UTC).isoformat()
    matched = 0
    for s in stops:
        bid = str(s["id"])
        addr = s.get("address", "") or s.get("name", "")
        override = STOP_ADDRESS_OVERRIDES.get(addr)
        if override is not None:
            gname = next((g[1] for g in gtfs_stops if g[0] == override), "?")
            log.info("  %s %r -> %s %r (override)", bid, addr, override, gname)
            mapping["stops"][bid] = override
            review["pending"].pop(bid, None)
            matched += 1
            continue

        resolved = review["resolved"].get(bid)
        if resolved is not None:
            if resolved["decision"] == "map":
                mapping["stops"][bid] = resolved["stop_id"]
                matched += 1
            # "no_match": deliberately left unmapped, no further action.
            review["pending"].pop(bid, None)
            continue

        blat, blon = float(s["lat"]), float(s["lon"])
        result = _match_stop(blat, blon, addr or bid, gtfs_stops)

        if result.outcome == "matched":
            gname = next((g[1] for g in gtfs_stops if g[0] == result.stop_id), "?")
            winner = next(
                (c for c in result.candidates if c.stop_id == result.stop_id),
                result.candidates[0],
            )
            log_fn = log.warning if result.warn else log.info
            flag = " ! review" if result.warn else ""
            log_fn(
                "  %s %r -> %s %r (%.0fm)%s",
                bid,
                addr,
                result.stop_id,
                gname,
                winner.dist_m,
                flag,
            )
            mapping["stops"][bid] = result.stop_id
            review["pending"].pop(bid, None)
            matched += 1
            continue

        # Needs review.
        if interactive:
            decision, stop_id = _prompt_interactive(
                bid, addr, result.candidates, blat, blon, gtfs_stops
            )
            if decision == "map":
                assert stop_id is not None
                mapping["stops"][bid] = stop_id
                review["resolved"][bid] = {
                    "decision": "map",
                    "stop_id": stop_id,
                    "decided_at": now_iso,
                }
                review["pending"].pop(bid, None)
                matched += 1
                continue
            if decision == "no_match":
                review["resolved"][bid] = {
                    "decision": "no_match",
                    "decided_at": now_iso,
                }
                review["pending"].pop(bid, None)
                continue
            # "skip": falls through to the pending write below.

        log.error(
            "  %s %r -> NEEDS REVIEW (%s); nearest candidates: %s",
            bid,
            addr,
            result.reason,
            ", ".join(
                f"{c.stop_name!r} {c.dist_m:.0f}m" for c in result.candidates[:3]
            ),
        )
        entry = review["pending"].setdefault(
            bid,
            {
                "route": slug,
                "address": addr,
                "lat": blat,
                "lon": blon,
                "reason": result.reason,
                "first_seen": now_iso,
            },
        )
        entry["route"] = slug
        entry["address"] = addr
        entry["lat"] = blat
        entry["lon"] = blon
        entry["reason"] = result.reason
        entry["last_seen"] = now_iso
        entry["candidates"] = [
            {
                "stop_id": c.stop_id,
                "stop_name": c.stop_name,
                "dist_m": round(c.dist_m, 1),
                "fuzzy_score": round(c.fuzzy_score, 3),
            }
            for c in result.candidates
        ]

    # A candidate slug that actually served stops is a real route: record it so
    # the runtime source starts polling it too.
    mapping["routes"].setdefault(slug, ALL_ROUTES[slug])
    return matched


def _coverage_report(
    mapping: dict[str, dict],
    gtfs_route_stops: dict[str, set[str]],
    gtfs_stops_by_id: dict[str, str],
) -> dict[str, list[tuple[str, str]]]:
    """route_id -> [(stop_id, stop_name), ...] for scheduled stops with no
    buswhere counterpart, restricted to routes we've actually captured."""
    mapped_gtfs_stops = set(mapping["stops"].values())
    gaps: dict[str, list[tuple[str, str]]] = {}
    for route_id in sorted(set(mapping["routes"].values())):
        scheduled = gtfs_route_stops.get(route_id, set())
        missing = scheduled - mapped_gtfs_stops
        if missing:
            gaps[route_id] = sorted(
                (stop_id, gtfs_stops_by_id.get(stop_id, "?")) for stop_id in missing
            )
        if scheduled:
            log.info(
                "coverage: %s — %d/%d scheduled stops mapped%s",
                route_id,
                len(scheduled) - len(missing),
                len(scheduled),
                f"; missing: {', '.join(n for _, n in gaps[route_id])}"
                if missing
                else "",
            )
    return gaps


def _watch(
    client: httpx.Client,
    slugs: list[str],
    gtfs_stops: list[tuple[str, str, float, float]],
    mapping: dict[str, dict],
    review: dict[str, dict],
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
                _map_route(slug, stops, gtfs_stops, mapping, review, interactive=False)
                _save_mapping(mapping)
                _save_review(review, REVIEW_PATH)
                log.info(
                    "  ↳ captured %s; %d total stop mappings written",
                    slug,
                    len(mapping["stops"]),
                )
            if not pending:
                break
            now = time.time()
            if deadline is not None and now >= deadline:
                log.warning(
                    "[watch] timed out with %d route(s) still dormant", len(pending)
                )
                break
            nap = interval if deadline is None else min(interval, deadline - now)
            log.info(
                "[watch] cycle %d: waiting on %s — next check in %.0f min "
                "(Ctrl-C to stop)",
                cycle,
                ", ".join(pending),
                nap / 60,
            )
            time.sleep(nap)
    except KeyboardInterrupt:
        log.info("[watch] interrupted — everything captured so far is saved")
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
        "--interactive",
        action="store_true",
        help="prompt to resolve stops that need review (rejected/tied matches) "
        "instead of just logging them to the review backlog",
    )
    ap.add_argument(
        "--strict",
        action="store_true",
        help="also fail (non-zero exit) if the GTFS-stop coverage report finds "
        "scheduled stops with no buswhere counterpart on a captured route",
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
    ap.add_argument(
        "--verbose",
        action="store_true",
        help="DEBUG-level logging (per-candidate detail)",
    )
    ap.add_argument(
        "--review-file",
        type=Path,
        default=REVIEW_PATH,
        help="override the review-backlog file location (mainly for tests)",
    )
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    if args.interactive and args.watch:
        ap.error("--interactive and --watch are mutually exclusive")

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
    review = _load_review(args.review_file)

    with httpx.Client(follow_redirects=False, timeout=30) as client:
        gtfs_bytes = _fetch_gtfs_bytes(args.gtfs)
        gtfs_stops = _load_gtfs_stops(gtfs_bytes)
        gtfs_route_stops = _load_route_stop_ids(gtfs_bytes)
        gtfs_stops_by_id = {g[0]: g[1] for g in gtfs_stops}
        log.info("loaded %d GTFS stops", len(gtfs_stops))

        _prune_stale_resolutions(review, set(gtfs_stops_by_id))

        if args.watch:
            log.info("watching %d route(s): %s", len(slugs), ", ".join(slugs))
            deadline = time.time() + args.timeout * 60 if args.timeout else None
            dormant = _watch(
                client, slugs, gtfs_stops, mapping, review, args.interval * 60, deadline
            )
        else:
            dormant = []
            matched = 0
            for slug in slugs:
                stops = _fetch_route_stops(client, slug)
                time.sleep(1.0)  # be polite
                if stops is None:
                    dormant.append(slug)
                    log.info("[skip] %s: dormant (retry when active, or --watch)", slug)
                    continue
                matched += _map_route(
                    slug,
                    stops,
                    gtfs_stops,
                    mapping,
                    review,
                    interactive=args.interactive,
                )
            _save_mapping(mapping)
            _save_review(review, args.review_file)
            log.info("%d stop matches this run", matched)

    gaps = _coverage_report(mapping, gtfs_route_stops, gtfs_stops_by_id)

    pending_count = len(review["pending"])
    log.info(
        "summary: %d total stop mappings, %d pending review, "
        "%d route(s) with coverage gaps",
        len(mapping["stops"]),
        pending_count,
        len(gaps),
    )
    if dormant:
        log.info("still unmapped/dormant: %s", ", ".join(dormant))
    if pending_count:
        log.error(
            "%d stop(s) awaiting manual review in %s — run with --interactive to "
            "resolve them",
            pending_count,
            args.review_file,
        )

    if pending_count:
        return 1
    if args.strict and gaps:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
