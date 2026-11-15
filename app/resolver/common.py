"""What the platform modules and the resolver share."""
from __future__ import annotations


from ..config import DISCOVERY_PLATFORMS as BOARD_PLATFORMS      # the one list of public job-board platforms

# `description_resolutions.platform` of the discovery step (v0.73.0): the boards and the local twin
DISCOVERY_PLATFORMS = (*BOARD_PLATFORMS, "local")


class PlatformError(Exception):
    """A page or answer of a site is not what the platform module expects."""


class Expired(Exception):
    """The posting page shows the site's notice that the posting is closed: there is no text to read."""


class Refused(Exception):
    """A request was not made or not answered: robots.txt disallows it, or the host is not on the allowlist."""


class StopRun(Exception):
    """The site refused (HTTP 403, 429, 5xx) or the network failed: nothing more is requested in this run."""

    def __init__(self, message: str, http_status: int | None = None):
        super().__init__(message)
        self.http_status = http_status
