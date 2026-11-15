"""Travel-time providers (goal.md 5.3.1): train from transport.opendata.ch, car from the public OSRM server,
and a marked approximation as the last fallback.

Providers take a `fetch(url) -> dict` function, so tests replay recorded responses and never touch the
network. `HttpFetcher` is the real one: honest User-Agent, a pause between requests, a timeout, and no
retry loop. Any failure (HTTP error, timeout, bad JSON, unexpected shape) raises ProviderError; callers
turn that into a conditional outcome, never a silent pass or fail.

Data sources and their conditions: train data from opentransportdata.swiss (shown on the Health page);
car routes from OSRM with OpenStreetMap data (best effort, no stated rate limit beyond "no excessive use").
"""
from __future__ import annotations

import http.client
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, timedelta

# Rough bounding box of Switzerland, used only to prefer a Swiss station when a name is ambiguous.
CH_LAT = (45.82, 47.81)
CH_LON = (5.95, 10.50)


class ProviderError(Exception):
    """A travel-time service did not give a usable answer."""


@dataclass(frozen=True)
class Place:
    name: str
    station_id: str
    latitude: float
    longitude: float
    in_switzerland: bool


class HttpFetcher:
    """GET a URL and parse JSON. Waits `delay` seconds between requests."""

    def __init__(self, user_agent: str, delay: float = 1.5, timeout: float = 15.0):
        self.user_agent, self.delay, self.timeout = user_agent, delay, timeout
        self._last = 0.0

    def __call__(self, url: str) -> dict:
        wait = self._last + self.delay - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        request = urllib.request.Request(url, headers={"User-Agent": self.user_agent, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise ProviderError(f"HTTP {exc.code} from {urllib.parse.urlsplit(url).netloc}") from exc
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError, ValueError) as exc:
            raise ProviderError(f"request to {urllib.parse.urlsplit(url).netloc} failed: {exc}") from exc
        finally:
            self._last = time.monotonic()


def next_weekday(today: date) -> date:
    """The next Monday to Friday after `today` (the day the train connection is looked up for)."""
    day = today + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def is_swiss(station_id: str, lat: float, lon: float) -> bool:
    """A Swiss station: its id starts with 85 (the Swiss UIC country code) and it lies in the box of Switzerland. The box
    alone also holds stations in Germany, France and Italy ("Lörrach", "Saint-Genis")."""
    return (str(station_id).startswith("85") and CH_LAT[0] <= lat <= CH_LAT[1] and CH_LON[0] <= lon <= CH_LON[1])


class TransportProvider:
    """Station search and train durations from transport.opendata.ch."""

    name = "transport.opendata.ch"

    def __init__(self, fetch, base_url: str):
        self.fetch, self.base = fetch, base_url.rstrip("/")

    def find_station(self, query: str) -> Place | None:
        """Best station for a place name: the first Swiss one, else the first one with coordinates. None if no match."""
        data = self.fetch(f"{self.base}/locations?" + urllib.parse.urlencode({"query": query, "type": "station"}))
        stations = data.get("stations") if isinstance(data, dict) else None
        if not isinstance(stations, list):
            raise ProviderError("unexpected answer from the station search")
        places = []
        for s in stations:
            coord = s.get("coordinate") or {}
            # the API puts latitude in `x` and longitude in `y`
            if s.get("id") and s.get("name") and isinstance(coord.get("x"), (int, float)) and isinstance(coord.get("y"), (int, float)):
                places.append(Place(s["name"], str(s["id"]), float(coord["x"]), float(coord["y"]),
                                    is_swiss(str(s["id"]), coord["x"], coord["y"])))
        if not places:
            return None
        return next((p for p in places if p.in_switzerland), places[0])

    def train_minutes(self, origin: str, destination: Place, day: date, arrival: str) -> float:
        """Duration of the connection that arrives at `arrival` (HH:MM) on `day`, station to station."""
        params = {"from": origin, "to": destination.station_id, "date": day.isoformat(), "time": arrival,
                  "isArrivalTime": 1, "limit": 1}
        data = self.fetch(f"{self.base}/connections?" + urllib.parse.urlencode(params))
        connections = data.get("connections") if isinstance(data, dict) else None
        if not connections:
            raise ProviderError("no train connection returned")
        m = re.fullmatch(r"(\d+)d(\d{2}):(\d{2}):(\d{2})", str(connections[0].get("duration", "")))
        if not m:
            raise ProviderError(f"unreadable duration {connections[0].get('duration')!r}")
        days, hours, minutes, seconds = (int(g) for g in m.groups())
        return days * 1440 + hours * 60 + minutes + seconds / 60


class OsrmProvider:
    """Free-flow driving time between two coordinates from the public OSRM server."""

    name = "router.project-osrm.org"

    def __init__(self, fetch, base_url: str):
        self.fetch, self.base = fetch, base_url.rstrip("/")

    def car_minutes(self, origin: Place, destination: Place) -> float:
        url = (f"{self.base}/route/v1/driving/{origin.longitude:.5f},{origin.latitude:.5f};"
               f"{destination.longitude:.5f},{destination.latitude:.5f}?overview=false")
        data = self.fetch(url)
        if not isinstance(data, dict) or data.get("code") != "Ok" or not data.get("routes"):
            raise ProviderError(f"no route: {data.get('code') if isinstance(data, dict) else 'bad answer'}")
        seconds = data["routes"][0].get("duration")
        if not isinstance(seconds, (int, float)) or seconds < 0:
            raise ProviderError("unreadable route duration")
        return seconds / 60


def haversine_km(a: Place, b: Place) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a.latitude, a.longitude, b.latitude, b.longitude))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def approximate_minutes(a: Place, b: Place, detour_factor: float, speed_kmh: float) -> float:
    """Straight-line distance times a detour factor, divided by an average speed. Always marked approximate."""
    return haversine_km(a, b) * detour_factor / speed_kmh * 60


class OfflineTransport:
    """Stands in for the timetable service in `recheck-travel`: every call raises ProviderError and is recorded."""

    name = "offline"

    def __init__(self):
        self.calls: list[tuple] = []

    def find_station(self, query: str):
        self.calls.append(("find", query))
        raise ProviderError("offline: no service is called")

    def train_minutes(self, origin, destination, day, arrival):
        self.calls.append(("train", destination.name))
        raise ProviderError("offline: no service is called")


class OfflineOsrm:
    """Stands in for the car-route service in `recheck-travel`: every call raises ProviderError and is recorded."""

    name = "offline"

    def __init__(self):
        self.calls: list[str] = []

    def car_minutes(self, origin, destination):
        self.calls.append(destination.name)
        raise ProviderError("offline: no service is called")


@dataclass
class Providers:
    """The services a location check needs, plus the clock (injectable for tests).

    `cache_any_age` makes the place and travel caches count even after their expiry (used offline by `recheck-travel`)."""
    transport: TransportProvider
    osrm: OsrmProvider
    today: date | None = None
    cache_any_age: bool = False

    @classmethod
    def offline(cls) -> "Providers":
        return cls(OfflineTransport(), OfflineOsrm(), cache_any_age=True)

    @classmethod
    def live(cls, cfg: dict) -> "Providers":
        p = cfg["location"]["providers"]
        fetch = HttpFetcher(p["user_agent"], p["request_delay_seconds"], p["timeout_seconds"])
        return cls(TransportProvider(fetch, p["transport_url"]), OsrmProvider(fetch, p["osrm_url"]))
