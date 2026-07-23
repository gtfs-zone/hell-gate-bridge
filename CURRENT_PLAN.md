# Plan: Publish correct GTFS trip_ids (issue #53 continuation)

## Summary

hell-gate-bridge currently publishes MQTT messages using the Amtrak train number (e.g., "174") as
the OwnTracks device ID. trip-updogger interprets that device ID as the GTFS `trip_id`, but the
real GTFS `trip_id` for train 174 running today is something like "258711". The fix is to load a
GTFS feed at startup, resolve `train_num → trip_id` for the current time, and use the resolved
`trip_id` as the MQTT device ID.

## Relevant Context

### GTFS schema
- `trips.txt`: `route_id, service_id, trip_id, trip_short_name, direction_id, shape_id, trip_headsign`
  — `trip_short_name` == Amtrak train number (e.g., "174")
- `calendar.txt`: `service_id, monday…sunday, start_date, end_date`
  — dates are `YYYYMMDD` integers; days-of-week are `"0"/"1"` strings
- `stop_times.txt`: `trip_id, arrival_time, departure_time, stop_id, stop_sequence, …`
  — times use GTFS extended format: can exceed `23:59` to represent times past midnight of the
    service date (e.g., `25:09:00` = 01:09 AM the next calendar day); first stop departure is
    always within `[0, 24h)`
- No `calendar_dates.txt` in the Amtrak feed; no exception handling needed

### Why simple date lookup isn't enough
- 367 trips have arrivals past midnight; max trip duration is **80 hours** (Texas Eagle #421)
- A daily train departing 23:00 and arriving 03:00: at 01:00 AM, the Amtrak API still shows it
  running, but today's calendar lookup would return today's (not-yet-departed) trip_id
- Fix: look back up to **4 days** (ceil(80/24)) and use stop_times first/last times to verify the
  current wall-clock time falls within the trip's actual window

### Duplicate service_id edge case
- One instance found in real data: train 364 on a Friday has two active service_ids with identical
  route, direction, and 17:00 departure — a data artifact of overlapping calendar windows
- Resolution: if multiple candidates remain after the window check, pick the one with the
  lexicographically smallest `trip_id` for determinism

### No-match trains
- ~30% of GTFS train numbers have no active service on a given weekday (weekend-only trains, etc.)
- The Amtrak live API only returns trains that are actually running, so a no-match in practice
  means either an expired GTFS feed or a train number not in the GTFS (charter, special)
- Resolution: log a warning and skip publishing that train

### Trains 174 / 184 (the ones from the logs)
- Both are daytime NEC trains: 174 runs 10:35→19:04, 184 runs 13:25→21:36 (no overnight)
- Today (Wed Apr 22 2026): train 174 → trip_id `258711`, train 184 → trip_id `258743`

## Phases

---

### Phase 1: GTFS loader module

Create `src/hell_gate_bridge/gtfs.py`. This module:

1. Accepts a path to a GTFS **directory** or **`.zip` file** (`zipfile` stdlib handles both uniformly)
2. Reads `trips.txt`, `calendar.txt`, and `stop_times.txt`
3. Builds an in-memory index at construction time:
   - `_trips`: `dict[str, list[tuple[str, str]]]` — train_num → list of `(trip_id, service_id)`
   - `_calendar`: `dict[str, CalendarRow]` — service_id → parsed row (days bitmask + date ints)
   - `_windows`: `dict[str, tuple[int, int]]` — trip_id → `(first_dep_secs, last_arr_secs)`
     where seconds are measured from midnight of the service date
4. Exposes one method: `resolve(train_num: str, now: datetime) -> str | None`

**`resolve` algorithm:**
```
for lookback in range(5):                        # 0 = today … 4 = four days ago
    d = now.date() - timedelta(days=lookback)
    for (trip_id, service_id) in _trips[train_num]:
        if service_id not active on d: continue
        (first_dep, last_arr) = _windows[trip_id]
        service_midnight = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        window_start = service_midnight + timedelta(seconds=first_dep)
        window_end   = service_midnight + timedelta(seconds=last_arr)
        if window_start <= now <= window_end:
            return trip_id
return None  # log warning upstream
```

If multiple trip_ids pass the window check (the duplicate edge case), return the
lexicographically smallest.

**Timezone note:** stop_times are in local clock time but we use UTC midnight as the anchor. The
maximum US timezone offset is 8 hours west. Since we check `window_start ≤ now ≤ window_end`, the
worst-case error is ±8h on the boundaries — acceptable because trip windows are much wider than 8h
and there is no real ambiguity within the ±8h slop.

**No new dependencies** — stdlib only (`csv`, `zipfile`, `datetime`).

**Checklist**
- [x] `gtfs.py` — `GtfsResolver` class with `__init__(path: str | Path)` and
      `resolve(train_num: str, now: datetime) -> str | None`
- [x] Handles both directory and `.zip` input
- [x] Parses GTFS time strings correctly (hours ≥ 24 → seconds > 86400)
- [x] No external dependencies

**Gotchas**
- `stop_times.txt` has 31k rows; load once at startup, don't re-read per poll
- `trip_short_name` and the Amtrak API's `TrainNum` are both plain strings — compare directly
- `service_id` date fields are `YYYYMMDD` integers — compare as `int`, not `str`

---

### Phase 2: Wire GTFS resolver into the publish pipeline

1. **`config.py`**: Add `self.gtfs_path: str = os.environ["GTFS_PATH"]` (required; fail loudly at
   startup if missing, same pattern as `MQTT_BROKER`)
2. **`main.py`**: Construct `GtfsResolver(config.gtfs_path)` once before the poll loop, pass it
   into `publish_positions`
3. **`publisher.py`**: In `publish_positions`, call
   `trip_id = resolver.resolve(train.train_num, train.timestamp)` and use `trip_id` (not
   `train.train_num`) as the MQTT device ID; if `None`, log a warning and `continue` (skip that
   train)
4. **`CLAUDE.md`**: Add `GTFS_PATH` row to the env var table

The `Train` model does **not** need a `trip_id` field — resolve at publish time, keeping models
as pure data from the Amtrak API.

**Checklist**
- [x] `Config.gtfs_path` wired from `GTFS_PATH` env var
- [x] `GtfsResolver` constructed once in `main.py`, passed to `publish_positions`
- [x] `publish_positions` signature updated to accept `resolver: GtfsResolver`
- [x] MQTT topic uses resolved `trip_id`; unresolved trains are warned + skipped
- [x] `CLAUDE.md` env var table updated with `GTFS_PATH`

**Gotchas**
- `train.timestamp` from the Amtrak API is already a UTC `datetime`; pass it directly to `resolve`
- The resolver is constructed once and never reloaded — if the GTFS feed is updated on disk, a
  restart is required (acceptable; operators redeploy the container with the new feed)
- `publish_positions` is currently `async`; the resolver is pure Python and synchronous —
  no changes to async structure needed
