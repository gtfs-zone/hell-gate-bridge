import hashlib
import json
import re
from base64 import b64decode
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from .models import StopTime, Train, TrainStop

_ROUTES_URL = "https://maps.amtrak.com/rttl/js/RoutesList.json"
_ROUTES_V_URL = "https://maps.amtrak.com/rttl/js/RoutesList.v.json"
_TRAINS_URL = "https://maps.amtrak.com/services/MapDataService/trains/getTrainsData"

_MASTER_SEGMENT = 88

_crypto_cache: dict | None = None

_TZ_MAP = {
    "P": "America/Los_Angeles",
    "M": "America/Denver",
    "C": "America/Chicago",
    "E": "America/New_York",
}


async def _get_crypto_initializers(client: httpx.AsyncClient) -> dict:
    global _crypto_cache
    if _crypto_cache is not None:
        return _crypto_cache

    routes = (await client.get(_ROUTES_URL)).json()
    master_zoom = sum(r.get("ZoomLevel", 0) or 0 for r in routes)

    crypto_data = (await client.get(_ROUTES_V_URL)).json()
    public_key: str = crypto_data["arr"][master_zoom]
    salt = bytes.fromhex(crypto_data["s"][len(crypto_data["s"][0])])
    iv = bytes.fromhex(crypto_data["v"][len(crypto_data["v"][0])])

    _crypto_cache = {"public_key": public_key, "salt": salt, "iv": iv}
    return _crypto_cache


def _derive_key(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha1", password.encode(), salt, 1000, dklen=16)


def _aes_decrypt(ciphertext_b64: str, key: bytes, iv: bytes) -> str:
    ciphertext = b64decode(ciphertext_b64)
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = PKCS7(128).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode()


def _parse_date(ugly_date: str | None, tz: str) -> datetime | None:
    if not ugly_date:
        return None
    zone = ZoneInfo(_TZ_MAP.get(tz.upper(), "America/New_York"))
    local = datetime.strptime(ugly_date, "%m/%d/%Y %H:%M:%S").replace(tzinfo=zone)
    return local.astimezone(ZoneInfo("UTC"))


def _parse_stop(station_json: str, tz_hint: str = "E") -> TrainStop | None:
    data = json.loads(station_json)
    code: str = data.get("code", "")
    if code == "CBN":
        return None
    tz = data.get("tz", tz_hint)

    arrival = StopTime(
        scheduled=_parse_date(data.get("scharr"), tz),
        estimated=_parse_date(data.get("estarr"), tz),
        actual=_parse_date(data.get("postarr"), tz),
    )
    departure = StopTime(
        scheduled=_parse_date(data.get("schdep"), tz),
        estimated=_parse_date(data.get("estdep"), tz),
        actual=_parse_date(data.get("postdep"), tz),
    )

    return TrainStop(
        station_code=code,
        bus=bool(data.get("bus", False)),
        timezone=_TZ_MAP.get(tz.upper(), "America/New_York"),
        status="",
        arrival=arrival,
        departure=departure,
    )


def _parse_feature(feature: dict) -> Train | None:
    geometry = feature.get("geometry")
    if not geometry or not geometry.get("coordinates"):
        return None

    lon, lat = geometry["coordinates"]
    props = feature["properties"]

    # Parse stops from Station1..N keys
    station_keys = sorted(
        [k for k in props if re.match(r"^Station\d{1,2}$", k) and props[k]],
        key=lambda k: int(re.sub(r"\D", "", k)),
    )
    stops: list[TrainStop] = []
    for key in station_keys:
        stop = _parse_stop(props[key])
        if stop is not None:
            stops.append(stop)

    # Infer stop statuses (mirror JS logic)
    if stops and stops[0].status == "":
        for i, stop in enumerate(stops):
            data = json.loads(props[station_keys[i]])
            first = i == 0
            if first and not data.get("postdep"):
                stop.status = "scheduled"
            elif data.get("postdep"):
                stop.status = "departed"
            elif data.get("postarr"):
                stop.status = "arrived"
            else:
                stop.status = "enroute"

    if stops and stops[0].status == "scheduled":
        for s in stops:
            s.status = "scheduled"

    if any(s.status == "arrived" for s in stops):
        for s in stops:
            if s.status == "enroute":
                s.status = "scheduled"

    enroute_idx = next((i for i, s in enumerate(stops) if s.status == "enroute"), -1)
    if enroute_idx >= 0:
        for s in stops[enroute_idx + 1 :]:
            s.status = "scheduled"

    last_val = props.get("LastValTS")
    if last_val:
        try:
            timestamp = _parse_date(last_val, "E") or datetime.now(UTC)
        except ValueError:
            timestamp = datetime.now(UTC)
    else:
        timestamp = datetime.now(UTC)

    return Train(
        train_num=str(props.get("TrainNum", "")),
        route=str(props.get("RouteName", "")),
        heading=str(props.get("Heading", "")),
        lat=lat,
        lon=lon,
        speed_mph=float(props.get("Speed") or 0),
        amtrak_id=str(props.get("ID", "")),
        timestamp=timestamp,
        stops=stops,
    )


async def fetch_trains(client: httpx.AsyncClient) -> list[Train]:
    crypto = await _get_crypto_initializers(client)
    public_key: str = crypto["public_key"]
    salt: bytes = crypto["salt"]
    iv: bytes = crypto["iv"]

    blob: str = (await client.get(_TRAINS_URL)).text

    private_key_cipher = blob[-_MASTER_SEGMENT:]
    ciphertext = blob[:-_MASTER_SEGMENT]

    key1 = _derive_key(public_key, salt)
    private_key = _aes_decrypt(private_key_cipher, key1, iv).split("|")[0]

    key2 = _derive_key(private_key, salt)
    plaintext = _aes_decrypt(ciphertext, key2, iv)

    geojson = json.loads(plaintext)

    trains: list[Train] = []
    for feature in geojson.get("features", []):
        train = _parse_feature(feature)
        if train is not None:
            trains.append(train)
    return trains
