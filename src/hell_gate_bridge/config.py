import os
from urllib.parse import urlparse


class Config:
    def __init__(self) -> None:
        broker_url = os.environ["MQTT_BROKER"]
        parsed = urlparse(broker_url)
        self.mqtt_hostname: str = parsed.hostname or ""
        self.mqtt_port: int = parsed.port or 1883
        self.mqtt_username: str | None = os.environ.get("MQTT_USERNAME")
        self.mqtt_password: str | None = os.environ.get("MQTT_PASSWORD")
        self.poll_interval: int = int(os.environ.get("POLL_INTERVAL", "15"))
        self.gtfs_path: str = os.environ["GTFS_PATH"]
        route_filter = os.environ.get("ROUTE_FILTER")
        self.route_filter: list[str] | None = (
            [r.strip() for r in route_filter.split(",")] if route_filter else None
        )
