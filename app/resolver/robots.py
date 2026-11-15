"""robots.txt per host, read once per run through the same fetcher as the pages (goal.md 5.1a).

No robots.txt means everything is allowed: a 4xx answer (404, 410, and since v0.75.0 also 401 and the other client errors,
RFC 9309 2.3.1.3) is "no rules". The exceptions are 403 and 429, which stay refusals like every other failure (5xx, a network
error): nothing is allowed for that host, and `refusal` tells the caller why (the run stops on 403, 429 and 5xx, like every
other request).
"""
from __future__ import annotations

import urllib.parse
from urllib.robotparser import RobotFileParser

from ..descriptions_fetch import STOP_STATUSES, FetchError


class Robots:
    def __init__(self, fetch, user_agent: str):
        self.fetch, self.user_agent = fetch, user_agent
        self._parsers: dict[str, RobotFileParser | None] = {}              # None: nothing is allowed
        self._refusals: dict[str, tuple[int | None, str]] = {}
        self.requests = 0                                                  # robots.txt files fetched

    def _load(self, host: str) -> RobotFileParser | None:
        if host in self._parsers:
            return self._parsers[host]
        parser = RobotFileParser()
        self.requests += 1
        try:
            text = self.fetch(f"https://{host}/robots.txt")
        except FetchError as exc:
            status = exc.http_status
            if status is not None and 400 <= status < 500 and status not in STOP_STATUSES:
                parser.parse([])                                           # no (readable) robots.txt, a 4xx: no rules, allowed
            else:
                self._refusals[host] = (exc.http_status, f"robots.txt of {host} could not be read ({exc})")
                parser = None
        else:
            parser.parse(text.splitlines())
        self._parsers[host] = parser
        return parser

    def allowed(self, url: str) -> bool:
        parser = self._load(urllib.parse.urlsplit(url).netloc)
        return parser is not None and parser.can_fetch(self.user_agent, url)

    def crawl_delay(self, host: str) -> float:
        parser = self._load(host)
        delay = parser.crawl_delay(self.user_agent) if parser is not None else None
        return float(delay) if delay else 0.0

    def refusal(self, host: str) -> tuple[int | None, str] | None:
        """(HTTP status or None, message) when robots.txt of the host could not be read, else None."""
        self._load(host)
        return self._refusals.get(host)
