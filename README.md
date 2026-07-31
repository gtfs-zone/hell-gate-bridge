# Hell Gate Bridge

Sidecar worker that polls an upstream live tracker (Amtrak or buswhere/Columbia
County), resolves each vehicle to a GTFS trip, and POSTs positions + per-stop
trip-updates to the cafe-car ingest API. Select the source with `SOURCE`.

## Overview

One process runs one source (`SOURCE=amtrak|buswhere`); deploy one container per
source. Each source polls its upstream tracker, resolves the vehicle to a GTFS
trip instance via the shared `GtfsResolver`, and hands provider-neutral
`VehicleUpdate`s to `publisher.py`, which POSTs them to cafe-car. cafe-car serves
the resulting GTFS-RT feeds; schedule-foamer loads the static schedules the
resolver matches against.

## Running Locally


```bash
cp .env.example .env  # edit as needed
uv sync

python -m hell_gate_bridge.main

```


## Development


```bash
uv sync              # install dependencies
ruff check .         # lint
ruff format .        # format
pre-commit install   # install git hooks (run once after clone)
```

