"""Shared helpers of the public-ATS reader tests (v0.70.0): fixtures, a fake fetcher that serves robots.txt, a config."""
from __future__ import annotations

from pathlib import Path

from app import db
from app.config import load_config

FIX = Path(__file__).parent / "fixtures"
NOW_ISO = "2026-11-04T06:00:00Z"
ROBOTS_OPEN = "User-agent: *\nDisallow: /embed/\nDisallow: /refreshFacet/\nDisallow: /services/\n"


def fx(*parts: str) -> str:
    return FIX.joinpath(*parts).read_text(encoding="utf-8")


def make_cfg(tmp_path, monkeypatch, *keys: str) -> dict:
    monkeypatch.delenv("RECRUITMENT_CONFIG", raising=False)
    cfg = load_config()
    cfg["paths"].update(exchange=str(tmp_path / "exchange"), lock=str(tmp_path / "run.lock"), logs=str(tmp_path / "logs"))
    for key in keys:
        cfg["adapters"][key]["enabled"] = True
        cfg["adapters"][key]["decision_note"] = "4 Nov 2026: test, the risk is accepted"
    return cfg


def make_conn(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    db.start_run(conn, "run1")
    return conn


class Site:
    """A fake fetcher `(url, data=None) -> str` that also serves robots.txt and records every call with the pauses.

    `events` holds ("sleep", seconds) and ("get", url) / ("post", url) in the order they happened. `lists` maps the
    `offset` of a JSON POST body to the answer; `pages` maps a url to a text or an exception; `default` answers the rest.
    """

    def __init__(self, *, robots: str | Exception = ROBOTS_OPEN, lists: dict | None = None, pages: dict | None = None, default=None):
        self.robots, self.lists, self.pages, self.default = robots, lists or {}, pages or {}, default
        self.events: list[tuple] = []

    def sleep(self, seconds) -> None:
        self.events.append(("sleep", seconds))

    def __call__(self, url, data=None):
        self.events.append(("post" if data is not None else "get", url))
        if url.endswith("/robots.txt"):
            if isinstance(self.robots, Exception):
                raise self.robots
            return self.robots
        if data is not None and "offset" in data:
            answer = self.lists[data["offset"]]
        else:
            answer = self.pages[url] if url in self.pages else self.default
            if answer is None:
                raise AssertionError(f"unexpected request {url}")
        if isinstance(answer, Exception):
            raise answer
        return answer

    @property
    def requests(self) -> list[tuple]:
        return [e for e in self.events if e[0] != "sleep"]

    @property
    def pauses(self) -> list:
        return [e[1] for e in self.events if e[0] == "sleep"]
