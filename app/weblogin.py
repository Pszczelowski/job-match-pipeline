"""The login of the web app (v0.74.0, goal.md 5.7, 5.9a): password hash, signed session cookie, throttle, safe `next`.

Only the standard library. The password hash is `hashlib.scrypt` (N=2^17, r=8, p=1, 16-byte salt, 32-byte key) in the text
form `scrypt:17:8:1:<salt>:<key>` (base64url, no dollar sign, so docker compose and dotenv leave it alone). It lives in the
untracked `.env` as RECRUITMENT_WEB_PASSWORD_HASH. The session cookie is stateless and signed:
`v1.<payload>.<HMAC-SHA256>`, the payload is base64url JSON `{u, iat, exp}`, the signing key is derived from the hash string,
so a new password (a new salt) ends every session. Nothing here writes a file or touches the database.
"""
from __future__ import annotations

import base64
import binascii
import collections
import hashlib
import hmac
import json
import math
import re
import secrets
import threading
import time

SCRYPT_LOG2 = 17                    # N = 2^17 (OWASP minimum for scrypt with r=8, p=1)
SCRYPT_MIN_LOG2 = 15                # the parser refuses anything weaker
SCRYPT_MAX_LOG2 = 17                # N = 2^18 with r=8 needs more than MAXMEM (256 MiB), so every login would fail
SCRYPT_R, SCRYPT_P = 8, 1
SALT_BYTES, KEY_BYTES = 16, 32
MAXMEM = 256 * 1024 * 1024
MIN_PASSWORD = 12

COOKIE_NAME = "recruitment_session"
COOKIE_VERSION = "v1"
SESSION_SECONDS = 12 * 3600         # a session cookie (no "stay logged in"): the payload expires after 12 hours
SIGN_CONTEXT = b"recruitment-web-session-v1"

IP_FAILURES, IP_WINDOW, LOCK_SECONDS = 5, 15 * 60, 15 * 60
GLOBAL_FAILURES, GLOBAL_WINDOW = 20, 60 * 60
MAX_TRACKED = 1000
KNOWN_GOOD_SECONDS = 30 * 86400     # an address with a successful login this recently is not stopped by the global lock

_B64 = re.compile(r"[A-Za-z0-9_-]+")
_NEXT = re.compile(r"/[A-Za-z0-9/_?=&.-]*")
NEXT_MAX = 200


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    if not _B64.fullmatch(text):
        raise ValueError("not base64url")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        raise ValueError("not base64url") from None


def hash_password(password: str, n_log2: int = SCRYPT_LOG2) -> str:
    """The storable hash of a password, e.g. `scrypt:17:8:1:SALT:KEY`."""
    if not SCRYPT_MIN_LOG2 <= n_log2 <= SCRYPT_MAX_LOG2:
        raise ValueError(f"the scrypt cost must be between 2^{SCRYPT_MIN_LOG2} and 2^{SCRYPT_MAX_LOG2}")
    salt = secrets.token_bytes(SALT_BYTES)
    key = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=1 << n_log2, r=SCRYPT_R, p=SCRYPT_P, dklen=KEY_BYTES, maxmem=MAXMEM)
    return f"scrypt:{n_log2}:{SCRYPT_R}:{SCRYPT_P}:{_b64(salt)}:{_b64(key)}"


def parse_hash(text: str) -> tuple[int, int, int, bytes, bytes]:
    """(log2 N, r, p, salt, key) of a hash string, or ValueError when it is malformed or weaker than the minimum."""
    parts = text.split(":") if isinstance(text, str) else []
    if len(parts) != 6 or parts[0] != "scrypt":
        raise ValueError("not a scrypt hash in the form scrypt:N:r:p:salt:key")
    try:
        n_log2, r, p = (int(x) for x in parts[1:4])
    except ValueError:
        raise ValueError("the scrypt numbers are not integers") from None
    if not SCRYPT_MIN_LOG2 <= n_log2 <= SCRYPT_MAX_LOG2 or r != SCRYPT_R or p != SCRYPT_P:
        raise ValueError("the scrypt parameters are outside the accepted range")
    salt, key = _unb64(parts[4]), _unb64(parts[5])
    if len(salt) < SALT_BYTES or len(key) != KEY_BYTES:
        raise ValueError("the salt or the key has the wrong length")
    return n_log2, r, p, salt, key


def verify_password(password: str, stored: str) -> bool:
    """True when the password matches the stored hash; a malformed hash is False, never an exception."""
    try:
        n_log2, r, p, salt, key = parse_hash(stored)
        got = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=1 << n_log2, r=r, p=p, dklen=len(key), maxmem=MAXMEM)
    except (ValueError, MemoryError, OverflowError):
        return False
    return hmac.compare_digest(got, key)


def safe_next(value) -> str:
    """The page to go to after the login: a local path of simple characters, else the top page."""
    if (isinstance(value, str) and 0 < len(value) <= NEXT_MAX and _NEXT.fullmatch(value) and not value.startswith("//")):
        return value
    return "/"


class LoginThrottle:
    """Failed logins, in memory. Per address: 5 failures within 15 minutes lock that address for 15 minutes (the right password
    is refused too). Global: 20 failures within an hour lock everybody for 15 minutes (the backstop when the container sees one
    gateway address for all). A success clears its address and remembers it for 30 days: such a known address is not stopped by
    the global lock (its own per-address lock still applies). At most 1000 addresses are tracked in each list. A restart clears
    everything."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.failures: collections.OrderedDict[str, list[float]] = collections.OrderedDict()
        self.locked: dict[str, float] = {}
        self.all_failures: collections.deque[float] = collections.deque()
        self.global_locked_until = 0.0
        self.known_good: collections.OrderedDict[str, float] = collections.OrderedDict()
        self._lock = threading.Lock()

    def check(self, ip: str) -> int:
        """0 when a login attempt may be checked, else the seconds to wait."""
        with self._lock:
            now = self.clock()
            seen = self.known_good.get(ip)
            known = seen is not None and now - seen < KNOWN_GOOD_SECONDS
            wait = max(self.locked.get(ip, 0.0), 0.0 if known else self.global_locked_until) - now
            return math.ceil(wait) if wait > 0 else 0

    def record_failure(self, ip: str) -> None:
        with self._lock:
            now = self.clock()
            self.locked = {k: v for k, v in self.locked.items() if v > now}
            times = [t for t in self.failures.pop(ip, []) if now - t < IP_WINDOW] + [now]
            if len(times) >= IP_FAILURES:
                self.locked[ip] = now + LOCK_SECONDS
                times = []
            self.failures[ip] = times
            while len(self.failures) > MAX_TRACKED:
                self.failures.popitem(last=False)
            self.all_failures.append(now)
            while self.all_failures and now - self.all_failures[0] >= GLOBAL_WINDOW:
                self.all_failures.popleft()
            if len(self.all_failures) >= GLOBAL_FAILURES:
                self.global_locked_until = now + LOCK_SECONDS
                self.all_failures.clear()

    def record_success(self, ip: str) -> None:
        with self._lock:
            self.failures.pop(ip, None)
            self.locked.pop(ip, None)
            now = self.clock()
            self.known_good.pop(ip, None)
            self.known_good[ip] = now
            while len(self.known_good) > MAX_TRACKED:
                self.known_good.popitem(last=False)


class WebLogin:
    """The configured user and password hash, the signed cookie, and the throttle."""

    def __init__(self, username: str, pwhash: str, stay_days: int = 90, clock=time.time, throttle: LoginThrottle | None = None):
        parse_hash(pwhash)                                  # ValueError on a bad hash: refuse at start, not at the first login
        self.username, self.hash, self.stay_days, self.clock = username, pwhash, stay_days, clock
        self.key = hmac.new(pwhash.encode("utf-8"), SIGN_CONTEXT, hashlib.sha256).digest()
        self.throttle = throttle or LoginThrottle(clock=lambda: self.clock())
        self._scrypt_lock = threading.Lock()                # one 128 MiB hash at a time

    def check(self, username: str, password: str) -> bool:
        """True for the right pair. The hash is computed for a wrong user name too, so the time tells nothing."""
        with self._scrypt_lock:
            password_ok = verify_password(password, self.hash)
        user_ok = hmac.compare_digest(username.encode("utf-8"), self.username.encode("utf-8"))
        return password_ok and user_ok

    def _sign(self, payload: str) -> str:
        return _b64(hmac.new(self.key, f"{COOKIE_VERSION}.{payload}".encode("ascii"), hashlib.sha256).digest())

    def make_cookie(self, stay: bool) -> tuple[str, int | None]:
        """(cookie value, Max-Age in seconds or None for a browser-session cookie)."""
        now = int(self.clock())
        seconds = self.stay_days * 86400 if stay else SESSION_SECONDS
        payload = _b64(json.dumps({"u": self.username, "iat": now, "exp": now + seconds}, separators=(",", ":")).encode("utf-8"))
        return f"{COOKIE_VERSION}.{payload}.{self._sign(payload)}", (seconds if stay else None)

    def read_cookie(self, value) -> bool:
        """True for a cookie this app signed with the current password, for the configured user, and not expired."""
        if not isinstance(value, str) or len(value) > 1000:
            return False
        parts = value.split(".")
        if len(parts) != 3 or parts[0] != COOKIE_VERSION or not _B64.fullmatch(parts[1]) or not _B64.fullmatch(parts[2]):
            return False
        if not hmac.compare_digest(parts[2].encode("ascii"), self._sign(parts[1]).encode("ascii")):
            return False
        try:
            data = json.loads(_unb64(parts[1]))
            return (isinstance(data, dict) and data.get("u") == self.username and isinstance(data.get("exp"), int)
                    and not isinstance(data.get("exp"), bool) and data["exp"] > self.clock())
        except ValueError:
            return False
