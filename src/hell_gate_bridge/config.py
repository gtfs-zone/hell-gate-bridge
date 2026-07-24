import os


class Config:
    def __init__(self) -> None:
        # Which upstream tracker this process runs. One source per process keeps
        # the two isolated (separate GTFS, vehicle_id, token scope); deploy one
        # container each.
        self.source: str = os.environ.get("SOURCE", "amtrak").lower()
        self.poll_interval: int = int(os.environ.get("POLL_INTERVAL", "15"))
        # httpx defaults to 5s; Amtrak's getTrainsData blob is slow and large, so
        # be explicit rather than inheriting a default that silently times out.
        self.http_timeout: float = float(os.environ.get("HTTP_TIMEOUT", "20"))
        # cafe-car ingest seam (positions + trip-updates over HTTP). vehicle_id
        # must match the feed's Driver.username so cafe-car's
        # `vehicle:{username}:*` scan finds it.
        self.ingest_url: str | None = os.environ.get("CAFE_CAR_INGEST_URL")
        self.ingest_token: str | None = os.environ.get("INGEST_API_TOKEN")
        self.vehicle_id: str = os.environ.get("INGEST_VEHICLE_ID", "amtrakdriver")
        self.gtfs_url: str = os.environ.get(
            "GTFS_URL", "https://content.amtrak.com/content/gtfs/GTFS.zip"
        )
        self.gtfs_path: str = os.environ.get("GTFS_PATH", "/app/beat/gtfs_cache.zip")
        route_filter = os.environ.get("ROUTE_FILTER")
        self.route_filter: list[str] | None = (
            [r.strip() for r in route_filter.split(",")] if route_filter else None
        )
