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
        # cafe-car ingest seam (positions over HTTP). vehicle_id must match the
        # feed's Driver.username so cafe-car's `vehicle:{username}:*` scan finds it.
        self.ingest_url: str | None = os.environ.get("CAFE_CAR_INGEST_URL")
        self.ingest_token: str | None = os.environ.get("INGEST_API_TOKEN")
        self.vehicle_id: str = os.environ.get("INGEST_VEHICLE_ID") or (
            self.mqtt_username or "amtrakdriver"
        )
        self.gtfs_url: str = os.environ.get(
            "GTFS_URL", "https://content.amtrak.com/content/gtfs/GTFS.zip"
        )
        self.gtfs_path: str = os.environ.get("GTFS_PATH", "/app/beat/gtfs_cache.zip")
        route_filter = os.environ.get("ROUTE_FILTER")
        self.route_filter: list[str] | None = (
            [r.strip() for r in route_filter.split(",")] if route_filter else None
        )
