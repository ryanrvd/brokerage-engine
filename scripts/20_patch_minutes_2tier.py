"""
Step 20 — scrape minutes data for tm_scrape-sourced players.

dcaribou's appearances table covers top tiers only — second-tier players (GB2,
FR2, ES2, IT2, L2) and players sourced via tm_scrape are stuck with NULL
minutes_share_pct / minutes_last_18m, which means their `finished_product`
flag is NULL and the matcher can't score them on that signal.

This script fetches each affected player's TM "Detailed performance data"
(leistungsdaten) page, sums league minutes for the current season across
all of the player's clubs, and computes:

  - minutes_last_18m       (sum of league minutes this season)
  - appearances_last_18m   (sum of league appearances this season)
  - minutes_available_18m  (matchdays in player's main league × 90)
  - minutes_share_pct      (numerator ÷ denominator × 100)
  - finished_product       (1 if share ≥ 50, else 0)

Polite scraper: 1.5s delay between live fetches, browser user-agent,
file-cached HTML at data/tm_cache/stats_{player_id}.html. SA1 players are
intentionally skipped (Saudi Pro League has no appearances data anywhere).
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

# Matchdays per season for the leagues we care about. Used as denominator for
# minutes_share_pct calculation (matchdays × 90 = max possible minutes per
# squad player). End-of-25/26 totals — snapshot is 2026-05-12, season is over.
LEAGUE_MATCHDAYS = {
    "GB1": 38, "ES1": 38, "IT1": 38, "L1": 34, "FR1": 34,
    "PO1": 34, "NL1": 34, "BE1": 30, "TR1": 38, "DK1": 32,
    "SC1": 38, "GR1": 26,
    "GB2": 46, "FR2": 34, "ES2": 42, "IT2": 38, "L2": 34,
}

# Map TM competition labels seen on the stats page → our league_id.
# Used to identify which competition rows count as "league" minutes.
TM_COMPETITION_TO_LEAGUE = {
    "Premier League":   "GB1",
    "Championship":     "GB2",
    "LaLiga":           "ES1",
    "LaLiga2":          "ES2",
    "Serie A":          "IT1",
    "Serie B":          "IT2",
    "Bundesliga":       "L1",
    "2. Bundesliga":    "L2",
    "Ligue 1":          "FR1",
    "Ligue 2":          "FR2",
    "Liga Portugal":    "PO1",
    "Eredivisie":       "NL1",
    "Jupiler Pro Lge":  "BE1",
    "Süper Lig":        "TR1",
    "Superliga":        "DK1",
    "Premiership":      "SC1",
    "Super League 1":   "GR1",
}


def fetch_stats(player_id: int) -> str | None:
    """Fetch the TM leistungsdaten (detailed performance data) page for a player.
    Cached at data/tm_cache/stats_{player_id}.html. Returns HTML text or None on error."""
    cache_path = CACHE_DIR / f"stats_{player_id}.html"
    if cache_path.exists():
        return cache_path.read_text(encoding="utf-8")
    # TM accepts a placeholder slug; redirects to the canonical URL.
    url = f"https://www.transfermarkt.com/x/leistungsdaten/spieler/{player_id}/plus/1"
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
            except Exception as e2:
                print(f"  ! retry failed on {player_id}: {e2}")
                return None
        else:
            print(f"  ! HTTP {e.code} on player {player_id}: skipping")
            return None
    except Exception as e:
        print(f"  ! fetch error on {player_id}: {e}")
        return None
    cache_path.write_text(html, encoding="utf-8")
    return html


_MINUTE_RE = re.compile(r"([\d.,]+)\s*'")  # matches "2,880'" or "2.880'" or "1.234'"


def _parse_minutes(cell_text: str) -> int:
    """Parse a TM minutes cell like "2,880'" → 2880."""
    if not cell_text:
        return 0
    m = _MINUTE_RE.search(cell_text)
    if not m:
        return 0
    digits = m.group(1).replace(",", "").replace(".", "")
    try:
        return int(digits)
    except ValueError:
        return 0


def _parse_apps(cell_text: str) -> int:
    """Parse appearances cell — typically a plain integer, sometimes empty."""
    if not cell_text:
        return 0
    m = re.search(r"\d+", cell_text.strip())
    return int(m.group()) if m else 0


def parse_current_season_stats(html: str) -> dict:
    """Walk the leistungsdaten table and sum the current season's league minutes.

    The TM page renders one row per (season, competition) combination. The
    current season (25/26) rows sit at the top; we identify league rows by
    matching their competition name to our TM_COMPETITION_TO_LEAGUE map.
    Returns:
      { league_minutes: int, league_apps: int, all_comp_minutes: int,
        all_comp_apps: int, leagues_seen: list[str] }"""
    soup = BeautifulSoup(html, "lxml")
    out = {
        "league_minutes": 0,
        "league_apps":    0,
        "all_comp_minutes": 0,
        "all_comp_apps":    0,
        "leagues_seen":   [],
    }

    # The "Detailed stats" table has class "items". First grouping row is
    # the current season header; subsequent rows until the next season-header
    # are per-competition lines.
    table = soup.find("table", class_="items")
    if not table:
        return out

    in_current_season = False
    for tr in table.find_all("tr"):
        cls = tr.get("class") or []
        # Section header rows: TM uses <tr class="bg_blau_20"> or similar for
        # season headers like "25/26". Use header text to detect.
        text = tr.get_text(" ", strip=True)
        season_match = re.match(r"^(\d{2})/(\d{2})\b", text)
        if season_match:
            in_current_season = season_match.group(0) in ("25/26", "2025/26")
            continue
        if not in_current_season:
            continue

        # Data rows have multiple <td>s. Skip totals row ("Total : Squad…").
        if "Total" in text and "Squad" in text:
            continue

        tds = tr.find_all("td")
        if len(tds) < 4:
            continue

        # Competition cell — find link or first plain text cell
        comp_link = tr.find("a", href=re.compile(r"/wettbewerb/"))
        if comp_link:
            comp_label = comp_link.get_text(strip=True)
        else:
            comp_label = tds[0].get_text(strip=True) if tds else ""

        # Last cell is typically minutes ("2,880'"), the cell before is goals/assists
        # Actual column layout varies; minutes cells contain an apostrophe.
        mins_cell = ""
        for td in reversed(tds):
            txt = td.get_text(strip=True)
            if "'" in txt and any(c.isdigit() for c in txt):
                mins_cell = txt
                break
        mins = _parse_minutes(mins_cell)

        # Apps cell — typically the 3rd or 4th column with an integer link inside
        apps = 0
        for td in tds[2:6]:
            cell_text = td.get_text(strip=True)
            if cell_text.isdigit():
                apps = int(cell_text)
                break

        if mins == 0 and apps == 0:
            continue

        out["all_comp_minutes"] += mins
        out["all_comp_apps"] += apps

        league_code = TM_COMPETITION_TO_LEAGUE.get(comp_label)
        if league_code:
            out["league_minutes"] += mins
            out["league_apps"] += apps
            if league_code not in out["leagues_seen"]:
                out["leagues_seen"].append(league_code)

    return out


def main() -> None:
    with sqlite3.connect(config.SQLITE_FILE) as con:
        con.row_factory = sqlite3.Row
        targets = con.execute("""
            SELECT player_id, name, current_club, league_id, data_source, finished_product
            FROM player_universe
            WHERE data_source = 'tm_scrape'
              AND minutes_share_pct IS NULL
              AND league_id NOT IN ('SA1', 'MLS1')
            ORDER BY league_id, name
        """).fetchall()

    print(f"Players needing minutes scrape: {len(targets)}")
    cached = sum(1 for r in targets if (CACHE_DIR / f"stats_{r['player_id']}.html").exists())
    print(f"  stats-cache hits: {cached}; fresh fetches: {len(targets) - cached}")
    print()

    updates: list[tuple] = []  # (mins_total, apps_total, mins_share, mins_avail, finished, pid)
    skipped: list[tuple[int, str, str]] = []  # (pid, name, reason)
    moved: list[dict] = []

    for r in targets:
        pid = r["player_id"]
        league_id = r["league_id"]
        html = fetch_stats(pid)
        if html is None:
            skipped.append((pid, r["name"], "fetch failed"))
            continue

        stats = parse_current_season_stats(html)
        if stats["all_comp_minutes"] == 0:
            skipped.append((pid, r["name"], "no minutes parsed"))
            continue

        # Denominator — matchdays for player's main league × 90.
        # If player is in player_universe under league X but actually played most
        # minutes in league Y this season (e.g. mid-season transfer or loan move),
        # we prefer Y. Default to league_id from player_universe.
        denom_league = stats["leagues_seen"][0] if stats["leagues_seen"] else league_id
        matchdays = LEAGUE_MATCHDAYS.get(denom_league, 38)
        denom = matchdays * 90

        share = round((stats["league_minutes"] / denom) * 100, 1) if denom else None
        finished = 1 if (share is not None and share >= 50) else 0

        updates.append((
            stats["all_comp_minutes"],
            stats["all_comp_apps"],
            share,
            denom,
            finished,
            pid,
        ))
        moved.append({
            "player_id": pid,
            "name": r["name"],
            "league_id": league_id,
            "denom_league": denom_league,
            "minutes_total": stats["all_comp_minutes"],
            "league_minutes": stats["league_minutes"],
            "league_apps": stats["league_apps"],
            "share_pct": share,
            "finished_product": finished,
            "prev_finished": r["finished_product"],
        })

    with sqlite3.connect(config.SQLITE_FILE) as con:
        con.executemany("""
            UPDATE player_universe
               SET minutes_last_18m       = ?,
                   appearances_last_18m   = ?,
                   minutes_share_pct      = ?,
                   minutes_available_18m  = ?,
                   finished_product       = ?
             WHERE player_id              = ?
        """, updates)
        con.commit()

    # ── Summary ──
    print(f"Players updated: {len(updates)}")
    print(f"Players skipped: {len(skipped)}")
    if skipped:
        print("  Skipped detail:")
        for pid, nm, reason in skipped:
            print(f"    {pid:>9d}  {nm:<28s}  {reason}")
    print()

    print(f"Finished-product flag transitions (NULL → 0 or 1):")
    new_true = sum(1 for m in moved if m["finished_product"] == 1)
    new_false = sum(1 for m in moved if m["finished_product"] == 0)
    print(f"  → TRUE  (≥50% league share): {new_true}")
    print(f"  → FALSE (<50% league share): {new_false}")
    print()

    print("Per-player detail (current season league minutes / share):")
    print(f"  {'player':<28s} {'lg':<5s} {'tot mins':>8s} {'lg mins':>8s} {'lg apps':>7s} {'share':>6s} {'fp':>3s}")
    print(f"  {'-'*28} {'-'*5} {'-'*8} {'-'*8} {'-'*7} {'-'*6} {'-'*3}")
    for m in sorted(moved, key=lambda x: (x["league_id"], -(x["share_pct"] or 0))):
        share_str = f"{m['share_pct']:.0f}%" if m['share_pct'] is not None else "—"
        print(f"  {m['name'][:28]:<28s} {m['league_id']:<5s} "
              f"{m['minutes_total']:>8d} {m['league_minutes']:>8d} {m['league_apps']:>7d} "
              f"{share_str:>6s} {m['finished_product']:>3d}")


if __name__ == "__main__":
    main()
