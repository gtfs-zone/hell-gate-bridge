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


@dataclass
class _CalendarRow:
    days_bitmask: int  # bit i = weekday i (0=Mon…6=Sun), matching date.weekday()
    start_date: int  # YYYYMMDD
    end_date: int  # YYYYMMDD


class GtfsResolver:
    def __init__(self, path: str | Path) -> None:
        path = Path(path)
        self._trips: dict[str, list[tuple[str, str]]] = {}
        self._calendar: dict[str, _CalendarRow] = {}
        self._windows: dict[str, tuple[int, int]] = {}
        # trip_id -> {stop_id: stop_sequence}. Amtrak GTFS stop_id == station code
        # (CHI, NYP, …), so this doubles as the station-code validity check when
        # building trip-updates.
        self._trip_stops: dict[str, dict[str, int]] = {}
        # stop_times.txt values are agency-local, so the service day must be
        # anchored in the agency's zone — not UTC.
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

        for row in csv.DictReader(io.StringIO(self._read_file(path, "trips.txt"))):
            train_num = row["trip_short_name"]
            self._trips.setdefault(train_num, []).append(
                (row["trip_id"], row["service_id"])
            )

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
            self._trip_stops.setdefault(tid, {})[row["stop_id"]] = int(
                row["stop_sequence"]
            )

        for tid in first_dep:
            self._windows[tid] = (first_dep[tid], last_arr[tid])

    def _is_active(self, service_id: str, d: date) -> bool:
        cal = self._calendar.get(service_id)
        if cal is None:
            return False
        date_int = int(d.strftime("%Y%m%d"))
        if not (cal.start_date <= date_int <= cal.end_date):
            return False
        return bool(cal.days_bitmask & (1 << d.weekday()))

    def stop_sequences(self, trip_id: str) -> dict[str, int]:
        """{stop_id: stop_sequence} for a resolved trip; empty if unknown."""
        return self._trip_stops.get(trip_id, {})

    def resolve(self, train_num: str, now: datetime) -> str | None:
        candidates = self._trips.get(train_num)
        if not candidates:
            return None

        local_today = now.astimezone(self._tz).date()
        for lookback in range(5):
            d = local_today - timedelta(days=lookback)
            matches: list[str] = []
            for trip_id, service_id in candidates:
                if not self._is_active(service_id, d):
                    continue
                first_dep, last_arr = self._windows.get(trip_id, (0, 0))
                # GTFS defines the service day as noon minus 12h, which keeps
                # DST-transition days an honest 23 or 25 hours long.
                service_midnight = datetime(
                    d.year, d.month, d.day, 12, tzinfo=self._tz
                ) - timedelta(hours=12)
                window_start = service_midnight + timedelta(seconds=first_dep)
                window_end = service_midnight + timedelta(seconds=last_arr)
                if window_start <= now <= window_end:
                    matches.append(trip_id)
            if matches:
                return min(matches)

        return None
