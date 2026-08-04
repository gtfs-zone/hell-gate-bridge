from __future__ import annotations

import csv
import io
import logging
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    import httpx

log = logging.getLogger(__name__)


async def fetch_gtfs(url: str, cache_path: Path, client: httpx.AsyncClient) -> Path:
    try:
        async with client.stream("GET", url) as r:
            r.raise_for_status()
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            with cache_path.open("wb") as f:
                async for chunk in r.aiter_bytes():
                    f.write(chunk)
        log.info("fetched GTFS from %s → %s", url, cache_path)
    except Exception as exc:
        if cache_path.exists():
            log.warning("GTFS fetch failed (%s), using cached %s", exc, cache_path)
        else:
            raise RuntimeError(
                f"GTFS fetch failed and no cache at {cache_path}"
            ) from exc
    return cache_path


def _parse_gtfs_time(s: str) -> int:
    h, m, sec = s.split(":")
    return int(h) * 3600 + int(m) * 60 + int(sec)


def _normalize_route_name(name: str) -> str:
    """Casefold, collapse whitespace (incl. NBSP/narrow-NBSP), drop an "amtrak" prefix.

    Amtrak's scraped alert page and its own GTFS route names disagree on
    exactly this kind of cosmetic noise (e.g. "Amtrak Hartford Line" on the
    alerts page vs. "Hartford Line" in routes.txt, or a narrow no-break space
    between two route names run together).
    """
    normalized = " ".join(name.replace("\u202f", " ").replace("\xa0", " ").split())
    normalized = normalized.casefold()
    if normalized.startswith("amtrak "):
        normalized = normalized[len("amtrak ") :]
    return normalized


@dataclass
class _CalendarRow:
    days_bitmask: int  # bit i = weekday i (0=Mon…6=Sun), matching date.weekday()
    start_date: int  # YYYYMMDD
    end_date: int  # YYYYMMDD


class GtfsResolver:
    def __init__(self, path: str | Path) -> None:
        path = Path(path)
        self._trips: dict[str, list[tuple[str, str]]] = {}
        # trip_id -> service_id, and route_id -> [trip_id], for resolving a
        # vehicle by route (buswhere) rather than by trip_short_name (Amtrak).
        self._trip_service: dict[str, str] = {}
        self._trips_by_route: dict[str, list[str]] = {}
        self._calendar: dict[str, _CalendarRow] = {}
        # service_id -> {YYYYMMDD: exception_type} (1=added, 2=removed).
        self._calendar_exceptions: dict[str, dict[int, int]] = {}
        self._windows: dict[str, tuple[int, int]] = {}
        # trip_id -> GTFS route_id, so the feed can carry the static route_id
        # (which joins to routes.txt) rather than Amtrak's display route name.
        self._trip_routes: dict[str, str] = {}
        # normalized route name (long or short) -> route_id, for matching
        # Amtrak's scraped alert route names (e.g. "Amtrak Hartford Line")
        # against the static feed.
        self._route_names: dict[str, str] = {}
        # trip_id -> {stop_id: stop_sequence}. Amtrak GTFS stop_id == station code
        # (CHI, NYP, …), so this doubles as the station-code validity check when
        # building trip-updates.
        self._trip_stops: dict[str, dict[str, int]] = {}
        # trip_id -> ordered [(stop_sequence, stop_id, arrival_secs)], agency-local
        # seconds-since-service-midnight. A list (not a dict) so loop routes,
        # which visit the same stop twice, keep both occurrences, needed to place
        # a predicted arrival on the correct scheduled visit.
        self._trip_schedule: dict[str, list[tuple[int, str, int]]] = {}
        # stop_times.txt values are agency-local, so the service day must be
        # anchored in the agency's zone, not UTC.
        self._tz: ZoneInfo = ZoneInfo("America/New_York")
        self._load(path)

    def _read_file(self, path: Path, name: str) -> str:
        if path.suffix == ".zip":
            with zipfile.ZipFile(path) as zf:
                return zf.read(name).decode("utf-8")
        return (path / name).read_text(encoding="utf-8")

    def _load(self, path: Path) -> None:
        day_names = [
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
        ]

        try:
            agencies = csv.DictReader(io.StringIO(self._read_file(path, "agency.txt")))
            tz_name = next(
                (r["agency_timezone"] for r in agencies if r.get("agency_timezone")),
                None,
            )
            if tz_name:
                self._tz = ZoneInfo(tz_name)
        except (KeyError, OSError, ValueError) as exc:
            log.warning("no agency_timezone (%s), assuming %s", exc, self._tz)

        for row in csv.DictReader(io.StringIO(self._read_file(path, "calendar.txt"))):
            bitmask = sum((1 << i) for i, d in enumerate(day_names) if row[d] == "1")
            self._calendar[row["service_id"]] = _CalendarRow(
                days_bitmask=bitmask,
                start_date=int(row["start_date"]),
                end_date=int(row["end_date"]),
            )

        # calendar_dates.txt is optional (Amtrak omits it; the Columbia County
        # feed uses it for holiday exceptions), so tolerate its absence.
        try:
            for row in csv.DictReader(
                io.StringIO(self._read_file(path, "calendar_dates.txt"))
            ):
                self._calendar_exceptions.setdefault(row["service_id"], {})[
                    int(row["date"])
                ] = int(row["exception_type"])
        except (KeyError, OSError, ValueError) as exc:
            log.debug("no calendar_dates.txt (%s)", exc)

        try:
            for row in csv.DictReader(io.StringIO(self._read_file(path, "routes.txt"))):
                route_id = row["route_id"]
                for name in (row.get("route_long_name"), row.get("route_short_name")):
                    normalized = _normalize_route_name(name) if name else ""
                    if normalized:
                        self._route_names.setdefault(normalized, route_id)
        except (KeyError, OSError) as exc:
            log.warning("no routes.txt (%s), route-name lookup disabled", exc)

        for row in csv.DictReader(io.StringIO(self._read_file(path, "trips.txt"))):
            trip_id = row["trip_id"]
            service_id = row["service_id"]
            self._trips.setdefault(row["trip_short_name"], []).append(
                (trip_id, service_id)
            )
            self._trip_service[trip_id] = service_id
            if row.get("route_id"):
                self._trip_routes[trip_id] = row["route_id"]
                self._trips_by_route.setdefault(row["route_id"], []).append(trip_id)

        first_dep: dict[str, int] = {}
        last_arr: dict[str, int] = {}
        for row in csv.DictReader(io.StringIO(self._read_file(path, "stop_times.txt"))):
            tid = row["trip_id"]
            dep = _parse_gtfs_time(row["departure_time"])
            arr = _parse_gtfs_time(row["arrival_time"])
            if tid not in first_dep or dep < first_dep[tid]:
                first_dep[tid] = dep
            if tid not in last_arr or arr > last_arr[tid]:
                last_arr[tid] = arr
            seq = int(row["stop_sequence"])
            self._trip_stops.setdefault(tid, {})[row["stop_id"]] = seq
            self._trip_schedule.setdefault(tid, []).append((seq, row["stop_id"], arr))

        for tid in first_dep:
            self._windows[tid] = (first_dep[tid], last_arr[tid])
        for sched in self._trip_schedule.values():
            sched.sort()

    def _is_active(self, service_id: str, d: date) -> bool:
        date_int = int(d.strftime("%Y%m%d"))
        # calendar_dates exceptions override calendar.txt: type 1 adds service on
        # a date, type 2 removes it (holidays, one-offs).
        exc = self._calendar_exceptions.get(service_id, {}).get(date_int)
        if exc is not None:
            return exc == 1
        cal = self._calendar.get(service_id)
        if cal is None:
            return False
        if not (cal.start_date <= date_int <= cal.end_date):
            return False
        return bool(cal.days_bitmask & (1 << d.weekday()))

    def _service_midnight(self, d: date) -> datetime:
        """Service-day midnight in the agency zone (DST-honest: noon minus 12h).

        Keeping the anchor at noon-minus-12h means DST-transition days stay an
        honest 23 or 25 hours long.
        """
        return datetime(d.year, d.month, d.day, 12, tzinfo=self._tz) - timedelta(
            hours=12
        )

    @property
    def timezone(self) -> ZoneInfo:
        return self._tz

    def stop_sequences(self, trip_id: str) -> dict[str, int]:
        """{stop_id: stop_sequence} for a resolved trip; empty if unknown."""
        return self._trip_stops.get(trip_id, {})

    def route_for(self, trip_id: str) -> str | None:
        """GTFS route_id for a resolved trip; None if unknown."""
        return self._trip_routes.get(trip_id)

    def route_id_for_name(self, name: str) -> str | None:
        """Best-effort match of a display route name to a GTFS route_id.

        For mapping scraped alert route names (e.g. "Amtrak Hartford Line")
        onto routes.txt, which the alerts page and the static feed don't
        always spell identically. Tries an exact normalized match first, then
        falls back to substring matching in either direction. None if nothing
        matches closely enough to trust.
        """
        normalized = _normalize_route_name(name)
        if not normalized:
            return None
        exact = self._route_names.get(normalized)
        if exact is not None:
            return exact
        for candidate_name, route_id in self._route_names.items():
            if normalized in candidate_name or candidate_name in normalized:
                return route_id
        return None

    def trip_schedule(
        self, trip_id: str, start_date: str
    ) -> list[tuple[int, str, int]]:
        """Ordered [(stop_sequence, stop_id, scheduled_arrival_epoch)] for a trip.

        Absolute epochs are anchored to `start_date` (YYYYMMDD). Repeated stops
        (loop routes) appear once per visit, so a predicted arrival can be placed
        on the matching scheduled occurrence. Empty if the trip is unknown.
        """
        sched = self._trip_schedule.get(trip_id)
        if not sched:
            return []
        d = date(int(start_date[:4]), int(start_date[4:6]), int(start_date[6:8]))
        midnight = self._service_midnight(d)
        return [
            (seq, stop_id, int((midnight + timedelta(seconds=arr)).timestamp()))
            for seq, stop_id, arr in sched
        ]

    def resolve_by_route(self, route_id: str, now: datetime) -> tuple[str, str] | None:
        """Resolve the trip on `route_id` whose scheduled window contains `now`.

        For providers that report a route but not a trip (buswhere): among trips
        on the route whose service is active, pick the one whose
        [first_dep, last_arr] window contains `now`. Trips on these routes run
        back-to-back, so at most one is running; at a shared boundary second we
        prefer the just-starting trip (latest window_start). Returns
        (trip_id, start_date) or None when nothing is scheduled to be running.
        """
        trip_ids = self._trips_by_route.get(route_id)
        if not trip_ids:
            return None

        local_today = now.astimezone(self._tz).date()
        best: tuple[datetime, str, str] | None = None  # (window_start, trip, sdate)
        # A run can start the previous service day and cross midnight, so look
        # back one day as well as today.
        for lookback in range(2):
            d = local_today - timedelta(days=lookback)
            service_midnight = self._service_midnight(d)
            start_date = d.strftime("%Y%m%d")
            for trip_id in trip_ids:
                service_id = self._trip_service.get(trip_id)
                if service_id is None or not self._is_active(service_id, d):
                    continue
                first_dep, last_arr = self._windows.get(trip_id, (0, 0))
                window_start = service_midnight + timedelta(seconds=first_dep)
                window_end = service_midnight + timedelta(seconds=last_arr)
                if not (window_start <= now <= window_end):
                    continue
                # Prefer the most recently started trip; break exact ties on the
                # boundary second deterministically by trip_id.
                key = (window_start, trip_id, start_date)
                if (
                    best is None
                    or key[0] > best[0]
                    or (key[0] == best[0] and trip_id < best[1])
                ):
                    best = key
        if best is None:
            return None
        return best[1], best[2]

    def resolve(
        self, train_num: str, now: datetime, origin: datetime | None = None
    ) -> tuple[str, str] | None:
        """Resolve to (trip_id, start_date), the GTFS-RT trip instance.

        Amtrak models a >24h daily train as one trip_id running every day, so
        several instances of that trip_id are en route at once. start_date
        (YYYYMMDD, the service day the train departed its origin) is what tells
        them apart, both here and in the Redis key downstream. Returns None when
        the train number isn't in the GTFS or no instance is currently running.
        """
        candidates = self._trips.get(train_num)
        if not candidates:
            return None

        # The service day the train actually departed its origin on is the most
        # reliable discriminator, so match that first when we have it.
        if origin is not None:
            service_date = origin.astimezone(self._tz).date()
            day_matches = [
                trip_id
                for trip_id, service_id in candidates
                if self._is_active(service_id, service_date)
            ]
            if day_matches:
                return min(day_matches), service_date.strftime("%Y%m%d")

        local_today = now.astimezone(self._tz).date()
        for lookback in range(5):
            d = local_today - timedelta(days=lookback)
            matches: list[str] = []
            for trip_id, service_id in candidates:
                if not self._is_active(service_id, d):
                    continue
                first_dep, last_arr = self._windows.get(trip_id, (0, 0))
                service_midnight = self._service_midnight(d)
                window_start = service_midnight + timedelta(seconds=first_dep)
                window_end = service_midnight + timedelta(seconds=last_arr)
                if window_start <= now <= window_end:
                    matches.append(trip_id)
            if matches:
                return min(matches), d.strftime("%Y%m%d")

        return None
