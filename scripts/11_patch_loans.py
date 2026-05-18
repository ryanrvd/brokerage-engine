"""
Step 11 — Day 3.5 patch: detect loans and populate parent_club / parent_club_id / on_loan.

For every player in player_universe, fetch their TM profile page (cache-aware)
and parse the "On loan from X" marker. Updates the three loan columns on
player_universe. Players without a loan marker keep parent_club = current_club
and on_loan = 0 (the default seeded by 03/06).

Why the fetch is needed: dcaribou has no loan field and the transfers table
mostly skips loan moves. TM profile pages have a reliable
   <a title="On loan from {Club} until {DD/MM/YYYY}" href="/.../verein/{id}/...">
attribute that gives us both the parent club name and its TM club_id.

For top-tier dcaribou players we look up the TM URL from dcaribou's players.url.
For second-tier players the Day 2 scrape already cached profile_{player_id}.html.
"""

import re
import sqlite3
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import certifi
import duckdb
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
SLEEP_BETWEEN = 1.5
CACHE_DIR = Path("data/tm_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)
_ssl_context = ssl.create_default_context(cafile=certifi.where())


def fetch_profile(player_id: int, url: str | None) -> str | None:
    """Return profile HTML from cache; fall back to live fetch if URL provided."""
    cache_path = CACHE_DIR / f"profile_{player_id}.html"
    if cache_path.exists():
        return cache_path.read_text(encoding="utf-8")
    if not url:
        return None
    time.sleep(SLEEP_BETWEEN)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, context=_ssl_context, timeout=30) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code in (429, 503):
            print(f"  ! HTTP {e.code} on player {player_id}, backing off 30s")
            time.sleep(30)
            with urllib.request.urlopen(req, context=_ssl_context, timeout=30) as resp:
                html = resp.read().decode("utf-8", errors="replace")
        else:
            print(f"  ! HTTP {e.code} on player {player_id}: skipping")
            return None
    cache_path.write_text(html, encoding="utf-8")
    return html


def parse_loan(html: str) -> tuple[str | None, str | None]:
    """Return (parent_club_name, parent_club_id) or (None, None) if not on loan."""
    soup = BeautifulSoup(html, "lxml")

    # Primary: link with title="On loan from {Club} until {date}".
    for a in soup.find_all("a", title=True):
        t = a.get("title", "")
        m = re.match(r"On loan from\s+(.+?)\s+until\s+", t, re.IGNORECASE)
        if m:
            club_name = m.group(1).strip()
            href = a.get("href", "")
            cid_match = re.search(r"/verein/(\d+)", href)
            return club_name, cid_match.group(1) if cid_match else None

    # Fallback: info-table label "On loan from:".
    for lbl in soup.select(".info-table__content--regular"):
        if "loan from" in lbl.get_text().lower():
            val = (lbl.find_next_sibling("span", class_="info-table__content--bold")
                   or lbl.find_next("span", class_="info-table__content--bold"))
            if val:
                link = val.find("a", href=True)
                if link:
                    href = link.get("href", "")
                    m = re.search(r"/verein/(\d+)", href)
                    cid = m.group(1) if m else None
                    name = link.get("title") or link.get_text(strip=True)
                    return name.strip(), cid
                return val.get_text(strip=True), None

    return None, None


def main() -> None:
    # Pull universe rows + dcaribou URLs.
    with sqlite3.connect(config.SQLITE_FILE) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute("""
            SELECT player_id, name, current_club, current_club_id, league_id, data_source
            FROM player_universe ORDER BY league_id, name
        """).fetchall()

    # Map of player_id → dcaribou TM URL for the dcaribou rows.
    dca_ids = [r["player_id"] for r in rows if r["data_source"] == "dcaribou"]
    url_map: dict[int, str] = {}
    if dca_ids:
        src = duckdb.connect(config.DUCKDB_FILE, read_only=True)
        ids_sql = "(" + ",".join(str(i) for i in dca_ids) + ")"
        for pid, url in src.execute(f"SELECT player_id, url FROM players WHERE player_id IN {ids_sql}").fetchall():
            if url:
                # dcaribou URLs sometimes use http://; normalise.
                url_map[int(pid)] = url.replace("http://", "https://")

    # Count fresh vs cached.
    cached = sum(1 for r in rows if (CACHE_DIR / f"profile_{r['player_id']}.html").exists())
    print(f"Profile cache: {cached}/{len(rows)} hit; {len(rows) - cached} fresh fetches needed")
    print()

    updates: list[tuple[str | None, str | None, int, int]] = []
    loaned: list[dict] = []
    missing_html: list[tuple[int, str]] = []

    for r in rows:
        pid = r["player_id"]
        url = url_map.get(pid)
        html = fetch_profile(pid, url)
        if html is None:
            missing_html.append((pid, r["name"]))
            continue
        parent_name, parent_id = parse_loan(html)
        if parent_name:
            # Loan detected — overwrite parent_*, set on_loan=1.
            updates.append((parent_name, parent_id, 1, pid))
            loaned.append({
                "player_id": pid,
                "name": r["name"],
                "league_id": r["league_id"],
                "current_club": r["current_club"],
                "parent_club": parent_name,
                "parent_club_id": parent_id,
            })
        else:
            # No loan — keep defaults (parent = current), set on_loan=0 explicitly
            # (handles re-runs after a previously-detected loan ended).
            updates.append((r["current_club"], r["current_club_id"], 0, pid))

    with sqlite3.connect(config.SQLITE_FILE) as con:
        con.executemany("""
            UPDATE player_universe
               SET parent_club = ?, parent_club_id = ?, on_loan = ?
             WHERE player_id = ?
        """, updates)
        con.commit()

    # === Print sanity check ===
    print(f"Loaned players detected: {len(loaned)}/{len(rows)}")
    print()
    if missing_html:
        print(f"WARNING — {len(missing_html)} players had no profile HTML and no fetch URL:")
        for pid, nm in missing_html[:10]:
            print(f"  {pid:>9d}  {nm}")
        print()

    print("Loan-affected players, per league:")
    from collections import Counter
    lc = Counter(L["league_id"] for L in loaned)
    for lid in sorted(lc):
        print(f"  {lid:6s}  {lc[lid]}")
    print()

    print("Per-loan detail (player → current club → parent club):")
    print(f"  {'player':28s} {'lg':5s} {'loan club':32s} {'parent club':32s} {'pid':>9s}")
    print(f"  {'-'*28} {'-'*5} {'-'*32} {'-'*32} {'-'*9}")
    for L in loaned:
        print(f"  {L['name'][:28]:28s} {L['league_id']:5s} "
              f"{(L['current_club'] or '')[:32]:32s} "
              f"{(L['parent_club'] or '')[:32]:32s} "
              f"{(L['parent_club_id'] or '—'):>9s}")


if __name__ == "__main__":
    main()
