import asyncio
import logging

import aiomqtt
import httpx

from hell_gate_bridge.amtrak import fetch_trains
from hell_gate_bridge.config import Config
from hell_gate_bridge.gtfs import GtfsResolver
from hell_gate_bridge.publisher import publish_positions

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


async def _poll_loop(config: Config, resolver: GtfsResolver) -> None:
    async with httpx.AsyncClient() as http:
        while True:
            reconnect_interval = 1
            try:
                async with aiomqtt.Client(
                    hostname=config.mqtt_hostname,
                    port=config.mqtt_port,
                    username=config.mqtt_username,
                    password=config.mqtt_password,
                ) as mqtt:
                    while True:
                        try:
                            trains = await fetch_trains(http)
                            if config.route_filter:
                                trains = [
                                    t for t in trains if t.route in config.route_filter
                                ]
                            await publish_positions(config, mqtt, trains, resolver)
                            log.info("published %d trains", len(trains))
                        except Exception as exc:
                            log.error("fetch error: %s", exc)
                        await asyncio.sleep(config.poll_interval)
            except aiomqtt.MqttError as exc:
                log.warning(
                    "MQTT error: %s — reconnecting in %ds", exc, reconnect_interval
                )
                await asyncio.sleep(reconnect_interval)
                reconnect_interval = min(reconnect_interval * 2, 60)


async def main() -> None:
    config = Config()
    resolver = GtfsResolver(config.gtfs_path)
    try:
        await _poll_loop(config, resolver)
    except asyncio.CancelledError:
        log.info("shutting down")


if __name__ == "__main__":
    asyncio.run(main())
