# Hell Gate Bridge

Sidecar worker that polls an upstream live tracker (Amtrak or buswhere/Columbia
County), resolves each vehicle to a GTFS trip, and POSTs positions + per-stop
trip-updates to the cafe-car ingest API. Select the source with `SOURCE`.

## Overview

<!-- Describe the service's role in the overall gtfs.zone system here -->

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

