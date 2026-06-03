"""Disk cache for SciSports API responses.

Single source of caching for every script that talks to SciSports. Cache hits
cost zero API budget — favour aggressively.

Cache layout: data/scisports_cache/{sha256(endpoint + sorted(params))}.json
Each file is a JSON object:
  {
    "fetched_at":    ISO timestamp,
    "ttl_seconds":   int,
    "endpoint":      "/api/v2/...",
    "params":        {...},      # for human inspection only
    "source":        "live" | "manual_seed",
    "data":          {...}       # the actual response
  }

TTLs (per docs):
  30 days — leagues, teams metadata
  24 hours — squad rosters (per-team player lists)
  7 days — sciskill, transfer fees (manual_seed entries also use 7d)
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = PROJECT_ROOT / "data" / "scisports_cache"

# TTL constants in seconds
TTL_LEAGUES_TEAMS_META = 30 * 24 * 3600
TTL_SQUAD_ROSTERS = 24 * 3600
TTL_SCISKILL = 7 * 24 * 3600
TTL_TRANSFER_FEES = 7 * 24 * 3600


def _ensure_cache_dir() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def cache_key(endpoint: str, params: dict | None) -> str:
    """SHA-256 over endpoint + sorted JSON of params (None and empty equiv)."""
    norm_params = dict(sorted((params or {}).items()))
    raw = endpoint + "|" + json.dumps(norm_params, default=str, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def cache_path(endpoint: str, params: dict | None) -> Path:
    _ensure_cache_dir()
    return CACHE_DIR / f"{cache_key(endpoint, params)}.json"


def _parse_iso(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def read_cached(endpoint: str, params: dict | None,
                accept_expired: bool = False) -> tuple[dict | list, dict] | None:
    """Return (data, meta) if cache fresh, else None.

    meta = {fetched_at, ttl_seconds, source}. accept_expired=True returns the
    payload even past TTL — useful for the manual_seed override path.
    """
    p = cache_path(endpoint, params)
    if not p.exists():
        return None
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    fetched_at = _parse_iso(obj["fetched_at"])
    ttl = int(obj.get("ttl_seconds", 0))
    age = (dt.datetime.now(dt.timezone.utc) - fetched_at).total_seconds()
    if (age > ttl) and not accept_expired:
        return None
    return obj["data"], {
        "fetched_at": obj["fetched_at"],
        "ttl_seconds": ttl,
        "source": obj.get("source", "live"),
        "age_seconds": age,
    }


def write_cached(endpoint: str, params: dict | None, data,
                 ttl_seconds: int, source: str = "live") -> Path:
    p = cache_path(endpoint, params)
    obj = {
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "ttl_seconds": ttl_seconds,
        "endpoint": endpoint,
        "params": params or {},
        "source": source,
        "data": data,
    }
    p.write_text(json.dumps(obj, default=str), encoding="utf-8")
    return p


def get_or_fetch(client, endpoint: str, params: dict | None,
                 ttl_seconds: int) -> tuple[dict | list, str]:
    """Read cache first; on miss, call client.get and store. Returns (data, source).

    source is 'cache_hit', 'manual_seed' (cache hit from seeded entry),
    or 'live' (fresh network).
    """
    hit = read_cached(endpoint, params)
    if hit is not None:
        data, meta = hit
        return data, ("manual_seed" if meta["source"] == "manual_seed" else "cache_hit")
    data = client.get(endpoint, **(params or {}))
    write_cached(endpoint, params, data, ttl_seconds, source="live")
    return data, "live"


def stats() -> dict:
    """Quick filesystem inventory of the cache."""
    _ensure_cache_dir()
    files = list(CACHE_DIR.glob("*.json"))
    by_source: dict[str, int] = {}
    total_size = 0
    for f in files:
        try:
            obj = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        src = obj.get("source", "live")
        by_source[src] = by_source.get(src, 0) + 1
        total_size += f.stat().st_size
    return {
        "files": len(files),
        "total_kb": round(total_size / 1024, 1),
        "by_source": by_source,
    }
