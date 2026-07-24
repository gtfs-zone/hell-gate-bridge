import asyncio
import logging

import httpx

from hell_gate_bridge.config import Config
from hell_gate_bridge.publisher import publish
from hell_gate_bridge.sources.base import Source

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def build_source(config: Config) -> Source:
    if config.source == "amtrak":
        from hell_gate_bridge.sources.amtrak import AmtrakSource

        return AmtrakSource(config)
    if config.source == "buswhere":
        from hell_gate_bridge.sources.buswhere import BuswhereSource

        return BuswhereSource(config)
    raise ValueError(f"unknown SOURCE {config.source!r} (expected amtrak|buswhere)")


async def _poll_loop(config: Config, source: Source, http: httpx.AsyncClient) -> None:
    while True:
        try:
            updates = await source.fetch(http)
            positions, trip_updates = await publish(config, http, updates)
            # Report what actually shipped, not what upstream returned — vehicles
            # whose trip_id won't resolve are dropped by the source.
            log.info(
                "%s: %d vehicles → %d positions, %d trip-updates published",
                source.name,
                len(updates),
                positions,
                trip_updates,
            )
        except Exception as exc:
            # httpx timeouts stringify to "", which made this log line blank.
            # %r always carries the exception type.
            log.error("poll cycle failed: %r", exc)
        await asyncio.sleep(config.poll_interval)


async def main() -> None:
    config = Config()
    source = build_source(config)
    log.info("starting source %s", source.name)
    try:
        async with httpx.AsyncClient(timeout=config.http_timeout) as http:
            await source.startup(http)
            await _poll_loop(config, source, http)
    except asyncio.CancelledError:
        log.info("shutting down")


if __name__ == "__main__":
    asyncio.run(main())
