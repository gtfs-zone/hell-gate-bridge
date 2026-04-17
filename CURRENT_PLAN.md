# Plan: Issue #53 — Implement Amtrak Tracker Poller

## Summary

Implement the full worker loop: fetch and decrypt the Amtrak live tracker API,
extract vehicle positions, and publish them to MQTT in OwnTracks format so
vehicle-poser can consume them. The amtrak-api repo (`~/Documents/amtrak-api`)
is JavaScript and is used for **inspiration only** — the decryption logic is
reimplemented in Python.

Redis is not needed here — this worker only publishes.

## Relevant Context

### Amtrak API (from amtrak-api reverse engineering)

Three HTTP endpoints:

1. `GET https://maps.amtrak.com/rttl/js/RoutesList.json`  
   Array of route objects each with a `ZoomLevel`. `masterZoom = sum of all ZoomLevel values`.

2. `GET https://maps.amtrak.com/rttl/js/RoutesList.v.json`  
   JSON object:
   - `arr[masterZoom]` → PUBLIC_KEY (string)
   - `s[s[0].length]` → CRYPTO_SALT (hex string → bytes)
   - `v[v[0].length]` → CRYPTO_IV (hex string → bytes)

3. `GET https://maps.amtrak.com/services/MapDataService/trains/getTrainsData`  
   Encrypted blob (text string):
   - Last 88 chars: base64-encoded private-key cipher (encrypted with PUBLIC_KEY)
   - Remainder: AES-128-CBC ciphertext (encrypted with per-response private key)

**Decryption:**
1. `key = PBKDF2-HMAC-SHA1(PUBLIC_KEY, CRYPTO_SALT, iterations=1000, dklen=16)`
2. Decrypt last-88-char slice → pipe-delimited string → take first segment as `private_key`
3. `key2 = PBKDF2-HMAC-SHA1(private_key, CRYPTO_SALT, iterations=1000, dklen=16)`
4. Decrypt prefix with `key2` + `CRYPTO_IV` → JSON string → parse as GeoJSON FeatureCollection

Each GeoJSON feature:
- `geometry.coordinates`: `[lon, lat]` — current train position
- `properties`: raw Amtrak fields including `TrainNum`, `RouteName`, `Heading`,
  `ID`, `Speed`, `LastValTS`, `Station1..N` (JSON-encoded stop data), and more

### MQTT message format (from vehicle-poser)

vehicle-poser subscribes to `owntracks/+/+` and expects OwnTracks location messages.

**Topic:** `owntracks/amtrak/{train_num}`  
(vehicle-poser parses `parts[1]` → `driver = "amtrak"`, `parts[2]` → `trip_id = train_num`)

**Payload:**
```json
{
  "_type": "location",
  "lat": 40.123,
  "lon": -74.456,
  "cog": 45,
  "vel": 120,
  "tst": 1713312000
}
```
- `cog`: bearing in degrees (convert Amtrak `Heading` string like `"NE"` → `45`)
- `vel`: speed in **km/h** (Amtrak `Speed` is mph → multiply by 1.60934)
- `tst`: Unix timestamp (from `LastValTS` or `time.time()`)

### Data model philosophy

The `amtrak.py` module should return rich, fully-populated `Train` dataclasses
containing **all** API data (stops with scheduled/actual/estimated times,
station codes, etc.) so future workers can use it without re-fetching. The MQTT
publisher only reads the subset it needs.

### Heading → degrees conversion

| String | Degrees |
|--------|---------|
| N      | 0       |
| NE     | 45      |
| E      | 90      |
| SE     | 135     |
| S      | 180     |
| SW     | 225     |
| W      | 270     |
| NW     | 315     |

### Config env vars

| Variable        | Default   | Description                        |
|-----------------|-----------|------------------------------------|
| `MQTT_BROKER`   | required  | `tcp://host:port`                  |
| `MQTT_USERNAME` | None      | Optional MQTT auth                 |
| `MQTT_PASSWORD` | None      | Optional MQTT auth                 |
| `POLL_INTERVAL` | `15`      | Seconds between polls              |
| `ROUTE_FILTER`  | None      | Comma-separated route names to include; omit for all trains |

### Current codebase state

- `src/hell_gate_bridge/__init__.py`: empty
- Dockerfile CMD: `python -m hell_gate_bridge.main`
- No runtime dependencies yet in `pyproject.toml`

---

## Phase 1: Dependencies and project skeleton

### Tasks

- [ ] Add runtime dependencies to `pyproject.toml`:
  - `httpx` — async HTTP
  - `aiomqtt` — async MQTT (already used by vehicle-poser, same pattern)
  - `cryptography` — AES-128-CBC (PBKDF2 via stdlib `hashlib`)
- [ ] Run `uv sync` to update lockfile
- [ ] Create `src/hell_gate_bridge/config.py` — read all env vars, parse
  `MQTT_BROKER` with `urllib.parse.urlparse`, expose typed config object
- [ ] Create `src/hell_gate_bridge/main.py` — entry point stub

### Gotchas

- `MQTT_BROKER` is `tcp://host:port`; aiomqtt needs `hostname` and `port` separately.
- `cryptography` needs IV/salt as `bytes`; the API returns hex strings — decode with `bytes.fromhex(...)`.

---

## Phase 2: Amtrak API client + data model

### Tasks

- [ ] Create `src/hell_gate_bridge/models.py` — dataclasses for `StopTime`,
  `TrainStop`, and `Train`:
  ```
  StopTime: scheduled, estimated, actual (all datetime | None)
  TrainStop: station_code, bus, timezone, status, arrival, departure (each StopTime)
  Train: train_num, route, heading, lat, lon, speed_mph, amtrak_id, timestamp, stops: list[TrainStop]
  ```
- [ ] Create `src/hell_gate_bridge/amtrak.py`:
  - `_get_crypto_initializers(client)` — fetches both RoutesList endpoints,
    caches result module-level (crypto params don't change between polls)
  - `_decrypt(data_b64, password, salt, iv) -> str` — PBKDF2 key derivation +
    AES-128-CBC decrypt
  - `_parse_feature(feature) -> Train | None` — maps one GeoJSON feature to a
    `Train`, parsing all station stops, returning `None` if geometry is missing
  - `fetch_trains(client) -> list[Train]` — orchestrates fetch + two-layer
    decrypt + `_parse_feature` for each feature

### Gotchas

- Slice the blob as a **string** (characters): `blob[:-88]` and `blob[-88:]`.
- GeoJSON coordinates are `[longitude, latitude]` — don't swap them.
- Some features have `null` geometry (train not yet reporting) — skip with `None`.
- `Station1..N` property values are **double-encoded JSON** (strings containing JSON) — `json.loads(train["Station1"])`.
- Filter out station code `"CBN"` (Amtrak internal, no real station data).

---

## Phase 3: MQTT publisher

### Tasks

- [ ] Create `src/hell_gate_bridge/publisher.py`:
  - `heading_to_degrees(heading: str) -> int | None` — converts `"NE"` → `45` etc.
  - `publish_positions(client: aiomqtt.Client, trains: list[Train])` — for each
    train, builds OwnTracks payload and publishes to `owntracks/amtrak/{train_num}`

---

## Phase 4: Main polling loop

### Tasks

- [ ] Implement `main.py`:
  - Read config
  - Connect `httpx.AsyncClient` and `aiomqtt.Client` (keep both alive across polls)
  - Loop: `fetch_trains` → optional route filter → `publish_positions` → sleep
  - Log train count per poll; log and continue on fetch errors (don't crash)
  - Graceful shutdown via `asyncio.run(main())` + `CancelledError` handling
  - Reconnect loop on `MqttError` (mirror vehicle-poser's exponential backoff)
- [ ] Update `.env.example` to add `POLL_INTERVAL` and `ROUTE_FILTER`

### Gotchas

- Keep `httpx.AsyncClient` alive for the whole process — don't recreate per poll.
- The crypto initializers cache is module-level, so it survives across polls automatically.
