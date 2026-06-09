"""Backfill player_universe.date_of_birth from TM profile pages.

Why: the TM↔SciSports linker (29_ingest_squads_via_api.py) relies on DOB to
distinguish same-named players. A NULL DOB forces the linker into its loose
Pass-3 fallback, which was the source of the Kalajdzic→Lukić wrong-link
class of bug. 446 NULL-DOB rows in player_universe currently have a linked
sci_id; another 737 are unlinked. This script backfills both groups so the
next linker run has DOB available for the strict passes.

Approach: for every row with NULL date_of_birth, fetch the TM profile via
the stub URL https://www.transfermarkt.com/-/profil/spieler/{pid} (which
redirects to the canonical name-slug URL), cache the HTML to data/tm_cache/
matching script 11's convention, and parse DOB from <span itemprop="birthDate">
(DD/MM/YYYY format) with an info-table label fallback.

Polite: 1.5s sleep between fresh fetches, 30s backoff on HTTP 429/503,
cache-first so re-runs are instant.
"""
from __future__ import annotations

import re
import sqlite3
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import certifi
from bs4 import BeautifulSoup

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
import config

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
SLEEP_BETWEEN = 1.5
CACHE_DIR = PROJECT_ROOT / "data" / "tm_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
_ssl_context = ssl.create_default_context(cafile=certifi.where())


def fetch_profile(player_id: int) -> str | None:
    """Cache-first profile fetch using TM's stub URL."""
    cache_path = CACHE_DIR / f"profile_{player_id}.html"
    if cache_path.exists():
        return cache_path.read_text(encoding="utf-8")
    url = f"https://www.transfermarkt.com/-/profil/spieler/{player_id}"
    time.sleep(SLEEP_BETWEEN)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, context=_ssl_context, timeout=30) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code in (429, 503):
            print(f"  ! HTTP {e.code} on player {player_id}, backing off 30s")
            time.sleep(30)
            try:
                with urllib.request.urlopen(req, context=_ssl_context, timeout=30) as resp:
                    html = resp.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as e2:
                print(f"  ! HTTP {e2.code} on player {player_id} (retry): skipping")
                return None
        else:
            print(f"  ! HTTP {e.code} on player {player_id}: skipping")
            return None
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"  ! network error on player {player_id}: {e} — skipping")
        return None
    cache_path.write_text(html, encoding="utf-8")
    return html


_DOB_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")


def parse_dob(html: str) -> str | None:
    """Return ISO date (YYYY-MM-DD) or None.

    Primary: <span itemprop="birthDate">DD/MM/YYYY (age)</span>
    Fallback: info-table label "Date of birth/Age:" sibling.
    """
    soup = BeautifulSoup(html, "lxml")

    # Primary
    el = soup.find("span", itemprop="birthDate")
    if el:
        m = _DOB_RE.search(el.get_text(strip=True))
        if m:
            d, mo, y = m.groups()
            return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"

    # Fallback — info-table labels
    for lbl in soup.select(".info-table__content--regular"):
        if "date of birth" in lbl.get_text(strip=True).lower():
            val = (lbl.find_next_sibling("span")
                   or lbl.find_next("span"))
            if val:
                m = _DOB_RE.search(val.get_text(strip=True))
                if m:
                    d, mo, y = m.groups()
                    return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
    return None


def main() -> None:
    with sqlite3.connect(config.SQLITE_FILE) as con:
        rows = con.execute("""
            SELECT player_id, name, data_source
            FROM player_universe
            WHERE date_of_birth IS NULL
            ORDER BY player_id
        """).fetchall()

    n = len(rows)
    cached = sum(1 for (pid, _, _) in rows
                 if (CACHE_DIR / f"profile_{pid}.html").exists())
    print(f"NULL-DOB rows: {n}")
    print(f"  cached profile HTML available: {cached}")
    print(f"  fresh fetches needed:          {n - cached}")
    if n - cached:
        eta_min = round((n - cached) * SLEEP_BETWEEN / 60, 1)
        print(f"  estimated wall time:           ~{eta_min} min")
    print()

    filled: list[tuple[str, int]] = []  # (iso_date, player_id)
    still_null: list[tuple[int, str]] = []  # (pid, name)
    bad_html: list[tuple[int, str]] = []
    progress_step = max(50, n // 20)

    for i, (pid, name, ds) in enumerate(rows, 1):
        html = fetch_profile(pid)
        if html is None:
            bad_html.append((pid, name))
            continue
        iso = parse_dob(html)
        if iso:
            filled.append((iso, pid))
        else:
            still_null.append((pid, name))
        if i % progress_step == 0:
            print(f"  ... {i}/{n}  filled={len(filled)}  null={len(still_null)}  bad={len(bad_html)}")

    # Apply updates in batch
    with sqlite3.connect(config.SQLITE_FILE) as con:
        con.executemany(
            "UPDATE player_universe SET date_of_birth = ? WHERE player_id = ?",
            filled,
        )
        con.commit()

    print()
    print("=" * 60)
    print(f"Backfilled DOBs:           {len(filled)} / {n}  ({len(filled)/n:.1%})")
    print(f"Profile fetched but no DOB found: {len(still_null)}")
    print(f"Profile fetch failed:      {len(bad_html)}")
    if still_null[:5]:
        print()
        print("Sample of profile-fetched-but-no-DOB (first 5):")
        for pid, nm in still_null[:5]:
            print(f"  {pid:>9d}  {nm}")
    if bad_html[:5]:
        print()
        print("Sample of fetch-failed (first 5):")
        for pid, nm in bad_html[:5]:
            print(f"  {pid:>9d}  {nm}")


if __name__ == "__main__":
    main()
