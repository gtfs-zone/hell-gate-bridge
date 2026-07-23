import asyncio
import logging
from pathlib import Path

import httpx

from hell_gate_bridge.amtrak import fetch_trains
from hell_gate_bridge.config import Config
from hell_gate_bridge.gtfs import GtfsResolver, fetch_gtfs
from hell_gate_bridge.publisher import publish_positions

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


async def _poll_loop(config: Config, resolver: GtfsResolver) -> None:
    async with httpx.AsyncClient() as http:
        while True:
            try:
                trains = await fetch_trains(http)
                if config.route_filter:
                    trains = [t for t in trains if t.route in config.route_filter]
                await publish_positions(config, http, trains, resolver)
                log.info("published %d trains", len(trains))
            except Exception as exc:
                log.error("fetch error: %s", exc)
            await asyncio.sleep(config.poll_interval)


async def main() -> None:
    config = Config()
    async with httpx.AsyncClient() as client:
        gtfs_path = await fetch_gtfs(config.gtfs_url, Path(config.gtfs_path), client)
    resolver = GtfsResolver(gtfs_path)
    try:
        await _poll_loop(config, resolver)
    except asyncio.CancelledError:
        log.info("shutting down")


if __name__ == "__main__":
    asyncio.run(main())
