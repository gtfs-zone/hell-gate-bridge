import asyncio
import logging

import httpx

from hell_gate_bridge.config import Config
from hell_gate_bridge.publisher import publish, publish_alerts
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


async def _alerts_poll_loop(
    config: Config, source: Source, http: httpx.AsyncClient
) -> None:
    """Scrape+sync amtrak.com's rider alerts page, on its own slower cadence.

    A courtesy scrape of a marketing site, not a live tracker — no reason to
    hit it as often as `_poll_loop` hits the live train feed.
    """
    from hell_gate_bridge.sources.amtrak import AmtrakSource
    from hell_gate_bridge.sources.amtrak.alerts import build_alerts, fetch_alert_html

    if not isinstance(source, AmtrakSource):
        return

    while True:
        try:
            html = await fetch_alert_html(http)
            alerts = build_alerts(html, source.resolver, config.amtrak_agency_id)
            count = await publish_alerts(config, http, alerts)
            log.info("alerts: %d scraped → %d synced", len(alerts), count)
        except Exception as exc:
            log.error("alerts poll cycle failed: %r", exc)
        await asyncio.sleep(config.alerts_poll_interval)


async def main() -> None:
    config = Config()
    source = build_source(config)
    log.info("starting source %s", source.name)
    try:
        async with httpx.AsyncClient(timeout=config.http_timeout) as http:
            await source.startup(http)
            await asyncio.gather(
                _poll_loop(config, source, http),
                _alerts_poll_loop(config, source, http),
            )
    except asyncio.CancelledError:
        log.info("shutting down")


if __name__ == "__main__":
    asyncio.run(main())
