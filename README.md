# Hell Gate Bridge

Sidecar worker that polls the Amtrak live tracker and publishes MQTT position messages

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

