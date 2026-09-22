import os

_AMTRAK_GTFS = "https://content.amtrak.com/content/gtfs/GTFS.zip"
_COLUMBIA_GTFS = (
    "https://github.com/columbia-county-ny-transit/gtfs-generator/"
    "raw/refs/heads/main/columbia_county_gtfs.zip"
)


class Config:
    def __init__(self) -> None:
        # Which upstream tracker this process runs. One source per process keeps
        # the two isolated (separate GTFS, tracker, token scope); deploy one
        # container each.
        self.source: str = os.environ.get("SOURCE", "amtrak").lower()
        self.poll_interval: int = int(os.environ.get("POLL_INTERVAL", "15"))
        # amtrak-only: the rider-facing alerts page is a courtesy scrape of a
        # marketing site, not a live tracker, so poll it far less often than
        # positions. GTFS-RT informed_entity fallback when a scraped route
        # name doesn't match routes.txt (see GtfsResolver.route_id_for_name).
        self.alerts_poll_interval: int = int(
            os.environ.get("ALERTS_POLL_INTERVAL", "1800")
        )
        # "51" is Amtrak's own agency_id in its GTFS agency.txt (a numeric
        # code there, not the "AMTK" short code riders might expect).
        self.amtrak_agency_id: str = os.environ.get("AMTRAK_AGENCY_ID", "51")
        # httpx defaults to 5s; Amtrak's getTrainsData blob is slow and large, so
        # be explicit rather than inheriting a default that silently times out.
        self.http_timeout: float = float(os.environ.get("HTTP_TIMEOUT", "20"))
        # cafe-car ingest seam (positions + trip-updates over HTTP). This must
        # match a feed's `Tracker.id` so cafe-car's `vehicle:{tracker_id}:*`
        # scan finds the records. It is a surrogate, not a secret: the request
        # is authenticated by INGEST_API_TOKEN, and the tracker's own credential
        # (`device_key`) never comes near this process. It is a per-tracker id,
        # not a vehicle id: one tracker can carry many concurrent vehicles, each
        # supplying its own public vehicle_id (see AmtrakSource).
        self.ingest_url: str | None = os.environ.get("CAFE_CAR_INGEST_URL")
        self.ingest_token: str | None = os.environ.get("INGEST_API_TOKEN")
        # INGEST_VEHICLE_ID is the old name for the same value, kept working for
        # one release so a deploy can set the new name on its own schedule.
        self.tracker_id: str = os.environ.get("INGEST_TRACKER_ID") or os.environ.get(
            "INGEST_VEHICLE_ID", "amtrakdriver"
        )
        # GTFS default follows the source: Amtrak's national feed vs. the
        # Columbia County feed we control (not the one buswhere uses internally).
        default_gtfs = _COLUMBIA_GTFS if self.source == "buswhere" else _AMTRAK_GTFS
        self.gtfs_url: str = os.environ.get("GTFS_URL", default_gtfs)
        self.gtfs_path: str = os.environ.get("GTFS_PATH", "/app/beat/gtfs_cache.zip")
        route_filter = os.environ.get("ROUTE_FILTER")
        # Amtrak-only: allowlist of RouteName values.
        self.route_filter: list[str] | None = (
            [r.strip() for r in route_filter.split(",")] if route_filter else None
        )
        # buswhere-only: which route slugs to poll. Empty → the source polls
        # every slug in its committed mapping.
        buswhere_routes = os.environ.get("BUSWHERE_ROUTES")
        self.buswhere_routes: list[str] | None = (
            [r.strip() for r in buswhere_routes.split(",")] if buswhere_routes else None
        )
