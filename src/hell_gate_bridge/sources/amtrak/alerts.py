"""Scrape amtrak.com/service-alerts-and-notices into neutral `Alert`s.

Amtrak's rider-facing alerts page — not the encrypted getTrainsData feed
`client.py` talks to — is a normal server-rendered page, so this is a plain
HTML scrape rather than anything cryptographic. It has no API, so the
selectors here are inherently brittle against a site redesign; that risk is
accepted rather than engineered around, since a scrape that silently returns
nothing just means the next sync publishes zero alerts (see `build_alerts`).
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING
from urllib.parse import urljoin

from bs4 import BeautifulSoup

if TYPE_CHECKING:
    from zoneinfo import ZoneInfo

    import httpx

    from hell_gate_bridge.gtfs import GtfsResolver

URL = "https://www.amtrak.com/service-alerts-and-notices"

# Amtrak's site blocks requests with no browser-shaped User-Agent.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64; rv:149.0) Gecko/20100101 Firefox/149.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
}

_STATION_CODE_RE = re.compile(r"\(([A-Za-z0-9]+)\)\s*$")
_WEEKDAY_RE = re.compile(
    r"\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b,?\s*",
    re.IGNORECASE,
)
_WEEKDAY_RANGE_RE = re.compile(
    r"\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
    r"\s*[-\u2013]\s*"
    r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b",
    re.IGNORECASE,
)
_MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name} | {
    abbr.lower(): i for i, abbr in enumerate(calendar.month_abbr) if abbr
}
_MONTH_WORD = "|".join(sorted(_MONTHS, key=len, reverse=True))
_SAME_MONTH_RANGE_RE = re.compile(
    rf"^(?P<month>{_MONTH_WORD})\.?\s+(?P<d1>\d{{1,2}})\s*[-\u2013]\s*"
    rf"(?P<d2>\d{{1,2}}),\s*(?P<year>\d{{4}})$",
    re.IGNORECASE,
)
_CROSS_MONTH_RANGE_RE = re.compile(
    rf"^(?P<month1>{_MONTH_WORD})\.?\s+(?P<d1>\d{{1,2}})\s*[-\u2013]\s*"
    rf"(?P<month2>{_MONTH_WORD})\.?\s+(?P<d2>\d{{1,2}}),\s*(?P<year>\d{{4}})$",
    re.IGNORECASE,
)
_SINGLE_DATE_RE = re.compile(
    rf"^(?P<month>{_MONTH_WORD})\.?\s+(?P<d>\d{{1,2}}),\s*(?P<year>\d{{4}})$",
    re.IGNORECASE,
)


async def fetch_alert_html(http: httpx.AsyncClient) -> str:
    resp = await http.get(URL, headers=_HEADERS)
    resp.raise_for_status()
    return resp.text


def parse_passenger_advisories(soup: BeautifulSoup) -> list[dict]:
    section = soup.find(
        "div", class_="na-advisories-section__tab_content_passengerAdvisories"
    )
    if section is None:
        return []

    advisories = []
    for option in section.find_all("div", class_="na-service-alert__option"):
        header = option.find(class_="na-service-alert__option-wrapper")
        tag = header.find("h3") if header else None
        routes = (
            [
                p.get_text(strip=True)
                for p in header.find_all(class_="tooltip__text_content")
            ]
            if header
            else []
        )
        title_el = option.find(class_="na-service-alert__option_title")
        date_el = option.find(class_="na-service-alert__option_date")
        advisories.append(
            {
                "tag": tag.get_text(strip=True) if tag else "",
                "routes": routes,
                "title": title_el.get_text(strip=True) if title_el else "",
                "effective": date_el.get_text(strip=True) if date_el else "",
                "link": title_el.get("data-href") if title_el else None,
            }
        )
    return advisories


def parse_station_notices(soup: BeautifulSoup) -> list[dict]:
    section = soup.find(
        "div", class_="na-advisories-section__tab_content_stationAdvisories"
    )
    if section is None:
        return []

    notices = []
    for li in section.find_all("li", class_="na-service-alert__stations_ul_li"):
        station = li.find(class_="na-service-alert__stations_ul_li_header")
        title_el = li.find(class_="na-service-alert__stations_ul_li_details_alert_link")
        date_el = li.find(class_="na-service-alert__stations_ul_li_details_alert_date")
        if title_el is None:
            # Station listed with no active notice attached.
            continue
        notices.append(
            {
                "station": station.get_text(strip=True) if station else "",
                "title": title_el.get_text(strip=True),
                "effective": date_el.get_text(strip=True) if date_el else "",
                "link": title_el.get("data-href"),
            }
        )
    return notices


def _parse_effective_window(text: str, tz: ZoneInfo) -> tuple[int | None, int | None]:
    """Best-effort parse of a scraped "Effective ..." string.

    Handles the common shapes: a single date, a same-month day range, and a
    cross-month date range, each optionally prefixed with weekday names
    ("Effective Thursday, August 6 - Monday, August 10, 2026"). Recurring
    weekly patterns ("Monday - Friday"), "Effective Immediately", and
    multi-range strings ("... and ...") aren't representable as one
    active_period, so they fall back to (None, None) — GTFS-RT then treats
    the alert as always active, and the raw text is still visible in the
    alert's description.
    """
    if _WEEKDAY_RANGE_RE.search(text) or " and " in text.lower():
        return None, None

    cleaned = text.strip()
    if cleaned.lower().startswith("effective"):
        cleaned = cleaned[len("effective") :].strip(" :")
    cleaned = _WEEKDAY_RE.sub("", cleaned).strip()

    if m := _SAME_MONTH_RANGE_RE.match(cleaned):
        month = _MONTHS[m["month"].lower()]
        year = int(m["year"])
        start = datetime(year, month, int(m["d1"]), tzinfo=tz)
        end = datetime(year, month, int(m["d2"]), 23, 59, 59, tzinfo=tz)
        return int(start.timestamp()), int(end.timestamp())

    if m := _CROSS_MONTH_RANGE_RE.match(cleaned):
        year = int(m["year"])
        month1 = _MONTHS[m["month1"].lower()]
        month2 = _MONTHS[m["month2"].lower()]
        start = datetime(year, month1, int(m["d1"]), tzinfo=tz)
        end = datetime(year, month2, int(m["d2"]), 23, 59, 59, tzinfo=tz)
        return int(start.timestamp()), int(end.timestamp())

    if m := _SINGLE_DATE_RE.match(cleaned):
        month = _MONTHS[m["month"].lower()]
        year = int(m["year"])
        start = datetime(year, month, int(m["d"]), tzinfo=tz)
        return int(start.timestamp()), None

    return None, None


@dataclass
class AlertEntity:
    """A GTFS-RT EntitySelector; exactly one of these is normally set."""

    agency_id: str | None = None
    route_id: str | None = None
    stop_id: str | None = None


@dataclass
class Alert:
    """A neutral service alert, ready for `publisher.publish_alerts`."""

    header_text: str
    description_text: str
    url: str | None = None
    active_period_start: int | None = None  # epoch seconds
    active_period_end: int | None = None  # epoch seconds
    entities: list[AlertEntity] = field(default_factory=list)


def _route_entities(
    routes: list[str], resolver: GtfsResolver, agency_id: str
) -> list[AlertEntity]:
    route_ids = {
        rid for name in routes if (rid := resolver.route_id_for_name(name)) is not None
    }
    if route_ids:
        return [AlertEntity(route_id=rid) for rid in sorted(route_ids)]
    # No route in this advisory matched the static feed — publish it anyway,
    # scoped to the whole agency, rather than dropping it silently.
    return [AlertEntity(agency_id=agency_id)]


def build_alerts(html: str, resolver: GtfsResolver, agency_id: str) -> list[Alert]:
    soup = BeautifulSoup(html, "html.parser")
    tz = resolver.timezone
    alerts: list[Alert] = []

    for advisory in parse_passenger_advisories(soup):
        if not advisory["title"]:
            continue
        start, end = _parse_effective_window(advisory["effective"], tz)
        alerts.append(
            Alert(
                header_text=advisory["title"],
                description_text=(
                    f"{advisory['tag']}: {advisory['effective']}".strip(": ")
                ),
                url=urljoin(URL, advisory["link"]) if advisory["link"] else None,
                active_period_start=start,
                active_period_end=end,
                entities=_route_entities(advisory["routes"], resolver, agency_id),
            )
        )

    for notice in parse_station_notices(soup):
        if not notice["title"]:
            continue
        start, end = _parse_effective_window(notice["effective"], tz)
        code_match = _STATION_CODE_RE.search(notice["station"])
        entities = (
            [AlertEntity(stop_id=code_match.group(1))]
            if code_match
            else [AlertEntity(agency_id=agency_id)]
        )
        alerts.append(
            Alert(
                header_text=notice["title"],
                description_text=f"{notice['station']}: {notice['effective']}".strip(
                    ": "
                ),
                url=urljoin(URL, notice["link"]) if notice["link"] else None,
                active_period_start=start,
                active_period_end=end,
                entities=entities,
            )
        )

    return alerts
