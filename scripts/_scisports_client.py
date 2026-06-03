"""Sci Sports API client — matcher build.

Ported from ~/market-movement-maps/src/mmm/sync/scisports_api.py with
STRENGTHENED rate-limit discipline. The matcher and the maps repo share a
single client_id and a single 1000-req/60s budget. SciSports has already
issued one written warning for over-use. A second incident likely means
service restrictions, so this client is deliberately paranoid.

Key auth finding (from maps repo, costly to re-discover): the OAuth2 password
grant requires `scope=api recruitment`. The `scope` value binds the JWT
`aud` claim. Without 'recruitment' the API returns 401 'audience empty is
invalid' on every endpoint.

Auth flow:
  POST identity.scisports.app/connect/token
  grant_type=password, scope='api recruitment'
  + client_id, client_secret, username, password

Pacing rules (binding floors, not aspirations):
  * Maximum sustained rate: 8 req/sec (half of documented 16/sec ceiling)
  * Burst protection: ≤30 requests in any rolling 5-second window
  * Minimum inter-request gap: 250ms (defensive floor regardless of quota)
  * Soft throttle: X-RateLimit-Remaining < 500 → sleep 500ms before next call
  * Hard throttle: X-RateLimit-Remaining < 300 → sleep 5s before next call
  * Emergency stop: X-RateLimit-Remaining < 100 → halt run, raise loudly
  * On HTTP 429: read Retry-After, sleep, retry ONCE only. Second 429 = halt.

Every request is logged to logs/scisports_api.log with timestamp + endpoint
+ status + remaining-quota for audit trail.

Credentials read from os.environ (loaded from .env at repo root). Never
echoed, never committed.
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import logging
import os
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import certifi

logger = logging.getLogger(__name__)

BASE_URL = "https://api-recruitment.scisports.app"
IDENTITY_URL = "https://identity.scisports.app"
TOKEN_PATH = "/connect/token"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
LOG_PATH = LOG_DIR / "scisports_api.log"

_ssl_context = ssl.create_default_context(cafile=certifi.where())

# ─── Rate-limit floors (binding) ─────────────────────────────────────────────
MAX_REQ_PER_SEC = 8                   # half the documented 16/sec
MIN_INTER_REQUEST_MS = 250            # 250ms defensive floor between any two requests
BURST_WINDOW_SECONDS = 5
BURST_MAX_REQUESTS = 30               # ≤30 requests in any rolling 5s window

SOFT_THRESHOLD = 500                  # remaining < this → 500ms sleep
HARD_THRESHOLD = 300                  # remaining < this → 5s sleep
EMERGENCY_THRESHOLD = 100             # remaining < this → HALT

SOFT_SLEEP_S = 0.5
HARD_SLEEP_S = 5.0


class ScisportsAuthError(RuntimeError):
    pass


class ScisportsRateLimitEmergency(RuntimeError):
    """Raised when X-RateLimit-Remaining drops below EMERGENCY_THRESHOLD.

    The client halts immediately to protect the shared budget. The user
    investigates before re-running.
    """


class ScisportsRateLimitedError(RuntimeError):
    """Raised when a 429 retry itself returns 429 — second incident on the
    same call, treat as protocol breach, halt entire run."""


def _load_dotenv() -> None:
    """Load .env from project root into os.environ if not already set."""
    dotenv = PROJECT_ROOT / ".env"
    if not dotenv.exists():
        return
    for line in dotenv.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def _ensure_log_dir() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def _audit(line: str) -> None:
    """Append one line to logs/scisports_api.log. ISO timestamp prepended."""
    _ensure_log_dir()
    ts = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(f"{ts}  {line}\n")


# ─── Defensive rate limiter ──────────────────────────────────────────────────

class ConservativeLimiter:
    """Three-layer defensive limiter:
       1) Minimum 250ms gap between requests (defensive floor)
       2) ≤30 requests in any rolling 5-second window (burst protection)
       3) ≤8 req/sec average sustained rate

    Plus dynamic post-response throttle based on X-RateLimit-Remaining
    (see SOFT/HARD/EMERGENCY thresholds at module top).
    """

    def __init__(self):
        self.timestamps: collections.deque[float] = collections.deque()
        self._last_request_ts: float = 0.0
        self._lock = threading.Lock()

    def wait_for_slot(self) -> None:
        with self._lock:
            now = time.monotonic()

            # Layer 1: minimum gap since last request
            gap = now - self._last_request_ts
            min_gap = MIN_INTER_REQUEST_MS / 1000.0
            if gap < min_gap:
                time.sleep(min_gap - gap)
                now = time.monotonic()

            # Layer 2: burst protection — drop stamps older than burst window
            while self.timestamps and self.timestamps[0] <= now - BURST_WINDOW_SECONDS:
                self.timestamps.popleft()
            if len(self.timestamps) >= BURST_MAX_REQUESTS:
                sleep_until = self.timestamps[0] + BURST_WINDOW_SECONDS + 0.1
                wait = sleep_until - now
                if wait > 0:
                    logger.debug("Burst window full (%d/%d in %ss), sleeping %.1fs",
                                 len(self.timestamps), BURST_MAX_REQUESTS,
                                 BURST_WINDOW_SECONDS, wait)
                    time.sleep(wait)
                    now = time.monotonic()
                    while self.timestamps and self.timestamps[0] <= now - BURST_WINDOW_SECONDS:
                        self.timestamps.popleft()

            # Layer 3: sustained rate (≤MAX_REQ_PER_SEC avg)
            # Trim window to 1 second and enforce count
            one_sec_ago = now - 1.0
            recent = [t for t in self.timestamps if t > one_sec_ago]
            if len(recent) >= MAX_REQ_PER_SEC:
                sleep_until = recent[0] + 1.0
                wait = sleep_until - now
                if wait > 0:
                    time.sleep(wait)
                    now = time.monotonic()

            self.timestamps.append(now)
            self._last_request_ts = now

    def apply_post_response_throttle(self, remaining: int | None) -> None:
        """After reading X-RateLimit-Remaining from the response, sleep
        proportionally so the NEXT request honours the soft/hard thresholds."""
        if remaining is None:
            return
        if remaining < HARD_THRESHOLD:
            time.sleep(HARD_SLEEP_S)
        elif remaining < SOFT_THRESHOLD:
            time.sleep(SOFT_SLEEP_S)


# ─── Client ──────────────────────────────────────────────────────────────────

class ScisportsClient:
    """Single-run client. Auth-on-first-call; keeps the bearer token in memory.

    Auth: OAuth2 password grant, scope='api recruitment'.
    Required env vars: SCISPORTS_CLIENT_ID, SCISPORTS_CLIENT_SECRET,
                       SCISPORTS_USERNAME, SCISPORTS_PASSWORD.
    """

    def __init__(self,
                 client_id: str | None = None,
                 client_secret: str | None = None,
                 username: str | None = None,
                 password: str | None = None,
                 base_url: str = BASE_URL,
                 identity_url: str = IDENTITY_URL):
        _load_dotenv()
        self.client_id = client_id or os.environ.get("SCISPORTS_CLIENT_ID")
        self.client_secret = client_secret or os.environ.get("SCISPORTS_CLIENT_SECRET")
        self.username = username or os.environ.get("SCISPORTS_USERNAME")
        self.password = password or os.environ.get("SCISPORTS_PASSWORD")
        missing = [k for k, v in [
            ("SCISPORTS_CLIENT_ID", self.client_id),
            ("SCISPORTS_CLIENT_SECRET", self.client_secret),
            ("SCISPORTS_USERNAME", self.username),
            ("SCISPORTS_PASSWORD", self.password),
        ] if not v]
        if missing:
            raise ScisportsAuthError(
                f"Missing required env var(s): {', '.join(missing)}. "
                "Add them to .env at the repo root."
            )
        self.base_url = base_url.rstrip("/")
        self.identity_url = identity_url.rstrip("/")
        self._token: str | None = None
        self._token_expiry_ts: float = 0.0
        self._token_endpoint: str | None = None
        self._limiter = ConservativeLimiter()
        self._requests_made = 0
        self._last_remaining: int | None = None
        self._min_remaining_seen: int | None = None

    # ─── auth ───────────────────────────────────────────────────────────────

    def authenticate(self) -> None:
        if self._token and time.time() < self._token_expiry_ts - 30:
            return
        url = f"{self.identity_url}{TOKEN_PATH}"
        body = urllib.parse.urlencode({
            "grant_type": "password",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "username": self.username,
            "password": self.password,
            # CRITICAL: scope='api recruitment' binds the JWT `aud` claim. The
            # recruitment API validates aud; missing it = 401 audience-empty.
            # (Discovered the hard way in the maps repo — see scisports_api.py
            # comment.)
            "scope": "api recruitment",
        }).encode("utf-8")
        req = urllib.request.Request(
            url, data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, context=_ssl_context, timeout=20) as resp:
                text = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
            raise ScisportsAuthError(
                f"Token endpoint returned HTTP {e.code} at {url}. Body: {err_body[:300]}"
            )
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise ScisportsAuthError(
                f"Token endpoint returned non-JSON at {url}: {text[:200]!r}"
            )
        token = data.get("access_token") or data.get("accessToken") or data.get("token")
        if not token:
            raise ScisportsAuthError(
                f"Token response missing access_token. Keys: {list(data.keys())}"
            )
        self._token = token
        self._token_expiry_ts = time.time() + int(data.get("expires_in") or 3600)
        self._token_endpoint = url
        _audit(f"AUTH OK token_endpoint={url} expires_in={data.get('expires_in')}")

    @property
    def token_endpoint(self) -> str | None:
        return self._token_endpoint

    @property
    def last_remaining(self) -> int | None:
        return self._last_remaining

    @property
    def min_remaining_seen(self) -> int | None:
        return self._min_remaining_seen

    @property
    def requests_made(self) -> int:
        return self._requests_made

    # ─── request plumbing ──────────────────────────────────────────────────

    def _request(self, method: str, path: str, params: dict | None = None,
                 body: dict | None = None) -> dict | list:
        self._limiter.wait_for_slot()
        self.authenticate()
        url = f"{self.base_url}{path}"
        if params:
            q = urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None}, doseq=True,
            )
            url = f"{url}?{q}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"

        retry_429_used = False
        start = time.monotonic()
        for attempt in range(3):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, context=_ssl_context, timeout=30) as resp:
                    text = resp.read().decode("utf-8", errors="replace")
                    remaining_hdr = resp.headers.get("X-RateLimit-Remaining")
                    limit_hdr = resp.headers.get("X-RateLimit-Limit")
                    remaining = (
                        int(remaining_hdr) if remaining_hdr and remaining_hdr.isdigit()
                        else None
                    )
                    elapsed_ms = int((time.monotonic() - start) * 1000)
                    self._requests_made += 1
                    self._last_remaining = remaining
                    if remaining is not None:
                        if (self._min_remaining_seen is None
                                or remaining < self._min_remaining_seen):
                            self._min_remaining_seen = remaining

                    _audit(
                        f"REQ {method} {path} status={resp.status} "
                        f"elapsed_ms={elapsed_ms} remaining={remaining} "
                        f"limit={limit_hdr}"
                    )

                    # EMERGENCY STOP — halt the run, do not continue.
                    if remaining is not None and remaining < EMERGENCY_THRESHOLD:
                        msg = (
                            f"X-RateLimit-Remaining={remaining} < {EMERGENCY_THRESHOLD} "
                            "(emergency threshold). Halting to protect the shared "
                            "client_id budget. Investigate before re-running."
                        )
                        _audit(f"EMERGENCY_HALT {msg}")
                        raise ScisportsRateLimitEmergency(msg)

                    # Soft/hard throttle for the NEXT call
                    self._limiter.apply_post_response_throttle(remaining)

                    return json.loads(text) if text else {}

            except urllib.error.HTTPError as e:
                elapsed_ms = int((time.monotonic() - start) * 1000)
                remaining_hdr = e.headers.get("X-RateLimit-Remaining") if e.headers else None
                remaining = (
                    int(remaining_hdr) if remaining_hdr and remaining_hdr.isdigit()
                    else None
                )
                _audit(
                    f"REQ {method} {path} status={e.code} elapsed_ms={elapsed_ms} "
                    f"remaining={remaining} ERROR"
                )
                if e.code == 401 and attempt == 0:
                    self._token = None
                    self.authenticate()
                    continue
                if e.code == 429:
                    if retry_429_used:
                        # Second 429 = halt entire run.
                        msg = (
                            "Second HTTP 429 on the same call. Halting to "
                            "protect the shared client_id budget."
                        )
                        _audit(f"DOUBLE_429_HALT {path}")
                        raise ScisportsRateLimitedError(msg)
                    retry_after = e.headers.get("Retry-After") if e.headers else None
                    sleep_for = int(retry_after) if retry_after and retry_after.isdigit() else 30
                    _audit(f"RETRY_429 path={path} sleeping={sleep_for}s "
                           f"Retry-After={retry_after}")
                    time.sleep(sleep_for)
                    retry_429_used = True
                    continue
                if e.code in (502, 503, 504) and attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise
            except (urllib.error.URLError, TimeoutError) as e:
                _audit(f"REQ {method} {path} NETWORK_ERROR {type(e).__name__}")
                if attempt < 2:
                    time.sleep(1 + attempt)
                    continue
                raise

        raise RuntimeError(f"request {method} {path} failed after retries")

    def get(self, path: str, **params) -> dict | list:
        return self._request("GET", path, params=params)

    # ─── typed convenience helpers (verified against swagger v2) ───────────

    def get_leagues(self, offset: int = 0, limit: int = 10, **filters) -> dict:
        return self.get("/api/v2/Leagues", Offset=offset, Limit=limit, **filters)

    def get_league(self, league_id: int) -> dict:
        return self.get(f"/api/v2/Leagues/{league_id}")

    def get_teams(self, offset: int = 0, limit: int = 10, **filters) -> dict:
        return self.get("/api/v2/Teams", Offset=offset, Limit=limit, **filters)

    def get_team(self, team_id: int) -> dict:
        return self.get(f"/api/v2/Teams/{team_id}")

    def get_players(self, offset: int = 0, limit: int = 10, **filters) -> dict:
        return self.get("/api/v2/Players", Offset=offset, Limit=limit, **filters)

    def get_player(self, player_id: int) -> dict:
        return self.get(f"/api/v2/Players/{player_id}")

    def get_player_sciskills(self, offset: int = 0, limit: int = 10, **filters) -> dict:
        return self.get("/api/v2/metrics/players/sciskill",
                        Offset=offset, Limit=limit, **filters)

    def get_player_career_stats(self, offset: int = 0, limit: int = 10, **filters) -> dict:
        return self.get("/api/v2/metrics/career-stats/players",
                        Offset=offset, Limit=limit, **filters)

    def fetch_all(self, endpoint_fn, page_size: int = 10, **filters) -> list[dict]:
        """Paginate through any of the get_* helpers. page_size is forced
        ≤ 10 — API rejects larger values silently with empty payload."""
        items: list[dict] = []
        offset = 0
        while True:
            resp = endpoint_fn(offset=offset, limit=page_size, **filters)
            page_items = resp.get("items") or resp.get("data") or []
            items.extend(page_items)
            total = resp.get("total") or resp.get("totalCount") or len(items)
            if len(items) >= total or not page_items:
                break
            offset += page_size
        return items

    # ─── coordination pre-flight ───────────────────────────────────────────

    def preflight_baseline(self, halt_threshold: int = 800) -> dict:
        """Fetch a single lightweight endpoint and read X-RateLimit-Remaining.

        Returns dict with status_code, remaining, looks_fresh (bool).
        Logs to logs/scisports_api.log so we have an audit trail.

        If remaining < halt_threshold (default 800), the caller should prompt
        the user to confirm no concurrent operation is running in the maps
        repo before proceeding.
        """
        _audit("PREFLIGHT start /api/v2/Leagues?Offset=0&Limit=1")
        resp = self.get_leagues(offset=0, limit=1)
        remaining = self._last_remaining
        items = resp.get("items") or resp.get("data") or []
        sample = items[0] if items else None
        looks_fresh = (remaining is None) or (remaining >= halt_threshold)
        _audit(
            f"PREFLIGHT done remaining={remaining} looks_fresh={looks_fresh} "
            f"sample_league={(sample or {}).get('name') if isinstance(sample, dict) else None}"
        )
        return {
            "status_code": 200,
            "remaining": remaining,
            "looks_fresh": looks_fresh,
            "sample_league_name": (sample or {}).get("name") if isinstance(sample, dict) else None,
            "halt_threshold": halt_threshold,
        }
