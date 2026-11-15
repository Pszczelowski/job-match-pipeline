"""Record real answers of the travel-time services as test fixtures (goal.md 5.3.1, 7.2).

Run by hand, not by the tests:  .venv\\Scripts\\python scripts\\record_travel_fixtures.py

It makes about 30 requests to transport.opendata.ch and router.project-osrm.org, one per second, with the
honest User-Agent from the config, and writes the answers to tests/fixtures/travel/. The tests replay these
files; they never use the network. The fixed "today" (31 Oct 2026, a Saturday) makes the train lookup date
Monday 2 Nov 2026, so the recorded URLs stay the same whenever the tests run.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db                                    # noqa: E402
from app.config import load_config                    # noqa: E402
from app.location import check_location               # noqa: E402
from app.travel import HttpFetcher, OsrmProvider, Providers, TransportProvider  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "travel"
TODAY = date(2026, 10, 31)
PLACES = ["Zürich", "Winterthur", "Opfikon", "Basel", "Genève", "Frankfurt am Main", "Bern", "Lugano", "Thalwil",
          "Xyzzyville Nowhere"]


class Recorder:
    def __init__(self, inner):
        self.inner, self.index = inner, {}

    def __call__(self, url):
        data = self.inner(url)
        name = hashlib.sha1(url.encode()).hexdigest()[:16] + ".json"
        (OUT / name).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        self.index[url] = name
        return data


def main() -> None:
    cfg = load_config()
    p = cfg["location"]["providers"]
    OUT.mkdir(parents=True, exist_ok=True)
    rec = Recorder(HttpFetcher(p["user_agent"], p["request_delay_seconds"], p["timeout_seconds"]))
    providers = Providers(TransportProvider(rec, p["transport_url"]), OsrmProvider(rec, p["osrm_url"]), today=TODAY)
    conn = db.connect(":memory:")
    for place in PLACES:
        check = check_location(conn, providers, cfg, place)
        print(f"{place:22} {check.outcome:12} {check.reason or '':26} {check.comment}")
    (OUT / "index.json").write_text(json.dumps(rec.index, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    print(f"{len(rec.index)} responses saved to {OUT}")


if __name__ == "__main__":
    main()
