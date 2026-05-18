"""
Step 07 — Extend roster to all senior players across the 19 leagues.

For the 14 dcaribou-covered top-tier leagues: filter dcaribou's players table on
clubs currently in our competition set (no age cap). Joins appearances for
minutes_last_18m.

For the 5 second-tier leagues (GB2/FR2/ES2/IT2/L2): scrape TM directly.
  Phase A: one request per league to extract the club list (5 requests).
  Phase B: one squad page per club (~85 requests) to extract roster.
Total: ~90 polite requests; cached so reruns are free.

Reuses the Day 2 scraper's cache dir (data/tm_cache/), user-agent, 1.5s sleep
and 30s backoff. Writes to the senior_roster table.
"""

import re
import sqlite3
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path

import certifi
import duckdb
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

SECOND_TIER_LEAGUES = [
    {"id": "GB2", "slug": "championship",   "country": "England"},
    {"id": "FR2", "slug": "ligue-2",        "country": "France"},
    {"id": "ES2", "slug": "laliga2",        "country": "Spain"},
    {"id": "IT2", "slug": "serie-b",        "country": "Italy"},
    {"id": "L2",  "slug": "2-bundesliga",   "country": "Germany"},
]

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
SLEEP_BETWEEN = 1.5
SEASON = "2025"  # 25/26 season

CACHE_DIR = Path("data/tm_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)
_ssl_context = ssl.create_default_context(cafile=certifi.where())


# ────────────────────────────────────────────────────────────────────────────────
# HTTP + cache (same pattern as 06)
# ────────────────────────────────────────────────────────────────────────────────

def fetch(url: str, cache_key: str) -> str:
    cache_path = CACHE_DIR / f"{cache_key}.html"
    if cache_path.exists():
        return cache_path.read_text(encoding="utf-8")
    time.sleep(SLEEP_BETWEEN)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, context=_ssl_context, timeout=30) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code in (429, 503):
            print(f"  ! HTTP {e.code} on {cache_key}, backing off 30s and retrying")
            time.sleep(30)
            with urllib.request.urlopen(req, context=_ssl_context, timeout=30) as resp:
                html = resp.read().decode("utf-8", errors="replace")
        else:
            raise
    cache_path.write_text(html, encoding="utf-8")
    return html


def months_before(d: date, months: int) -> date:
    y, m = d.year, d.month - months
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, d.day)


# ────────────────────────────────────────────────────────────────────────────────
# Phase A — discover clubs per second-tier league
# ────────────────────────────────────────────────────────────────────────────────

def fetch_league_clubs(league_id: str, slug: str) -> list[dict]:
    url = f"https://www.transfermarkt.com/{slug}/startseite/wettbewerb/{league_id}"
    html = fetch(url, f"league_{league_id}")
    soup = BeautifulSoup(html, "lxml")
    clubs: list[dict] = []
    seen: set[str] = set()
    table = soup.select_one("table.items")
    if not table:
        return clubs
    for row in table.select("tbody > tr"):
        link = row.select_one("a[href*='/startseite/verein/']")
        if not link:
            continue
        m = re.search(r"/([^/]+)/startseite/verein/(\d+)", link.get("href", ""))
        if not m:
            continue
        cslug, cid = m.group(1), m.group(2)
        if cid in seen:
            continue
        seen.add(cid)
        name = link.get("title") or link.text.strip() or cslug
        clubs.append({"club_id": cid, "slug": cslug, "name": name.strip()})
    return clubs


# ────────────────────────────────────────────────────────────────────────────────
# Phase B — parse a club's squad page
# ────────────────────────────────────────────────────────────────────────────────

# Multiple date formats appear on kader pages depending on TM's locale at render time.
_DATE_FORMATS = ("%b %d, %Y", "%d.%m.%Y", "%d/%m/%Y", "%d %b %Y")


def _parse_contract_date(text: str) -> date | None:
    text = (text or "").strip()
    if not text or text == "-":
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    # Fallback: search for "Jun 30, 2027"-style substring anywhere in text.
    m = re.search(r"([A-Z][a-z]{2})\s+(\d{1,2}),?\s+(\d{4})", text)
    if m:
        try:
            return datetime.strptime(f"{m.group(1)} {m.group(2)}, {m.group(3)}", "%b %d, %Y").date()
        except ValueError:
            pass
    return None


def fetch_club_squad(
    club_id: str,
    slug: str,
    league_id: str,
    league_display: str,
    leverage_cutoff: date,
) -> tuple[str, list[dict]]:
    url = (
        f"https://www.transfermarkt.com/{slug}/kader/verein/{club_id}"
        f"/saison_id/{SEASON}/plus/1"
    )
    html = fetch(url, f"squad_{club_id}")
    soup = BeautifulSoup(html, "lxml")

    # Resolve canonical club name from the page header.
    h1 = soup.select_one("h1.data-header__headline-wrapper") or soup.find("h1")
    club_name = h1.get_text(" ", strip=True) if h1 else slug

    rows_out: list[dict] = []
    table = soup.select_one("#yw1 table.items") or soup.select_one("table.items")
    if not table:
        return club_name, rows_out

    # Map header labels to column indices so contract & age extraction is robust to
    # column-reorder changes on TM.
    headers = [th.get_text(" ", strip=True).lower() for th in table.select("thead th")]

    def first_col(*needles: str) -> int | None:
        for i, h in enumerate(headers):
            for n in needles:
                if n in h:
                    return i
        return None

    age_col = first_col("age")
    contract_col = first_col("contract", "vertrag")
    dob_col = first_col("date of birth", "geburtsdatum", "born")

    for r in table.select("tbody > tr.odd, tbody > tr.even"):
        cells = r.find_all("td", recursive=False)
        if not cells:
            continue
        # Player link → id + name
        plink = r.select_one("td.hauptlink a[href*='/profil/spieler/']") \
            or r.select_one("a[href*='/profil/spieler/']")
        if not plink:
            continue
        m = re.search(r"/profil/spieler/(\d+)", plink["href"])
        if not m:
            continue
        player_id = int(m.group(1))
        name = plink.get_text(strip=True)

        # Sub-position: inside the inline-table second row inside the name cell.
        sub_position = None
        inline = r.select_one("table.inline-table")
        if inline:
            trs = inline.select("tr")
            if len(trs) >= 2:
                sub_position = trs[1].get_text(strip=True) or None

        # Age — TM renders DOB and age together as "DD/MM/YYYY (NN)".
        # Require the (NN) parens form to avoid grabbing the day of the date or shirt #.
        age = None
        for col in (age_col, dob_col):
            if col is not None and col < len(cells):
                txt = cells[col].get_text(" ", strip=True)
                am = re.search(r"\((\d{1,2})\)", txt)
                if am:
                    age = int(am.group(1))
                    break
                # bare numeric "Age" column (no parens)
                if txt.isdigit() and 14 <= int(txt) <= 50:
                    age = int(txt)
                    break
        if age is None:
            for c in cells:
                am = re.search(r"\((\d{1,2})\)", c.get_text(" ", strip=True))
                if am:
                    val = int(am.group(1))
                    if 14 <= val <= 50:
                        age = val
                        break

        # Contract end date.
        contract_end = None
        if contract_col is not None and contract_col < len(cells):
            contract_end = _parse_contract_date(cells[contract_col].get_text(" ", strip=True))

        contract_leveraged = (
            1 if contract_end and contract_end <= leverage_cutoff
            else (0 if contract_end else None)
        )

        rows_out.append({
            "player_id": player_id,
            "name": name,
            "club_id": club_id,
            "club_name": club_name,
            "league_id": league_id,
            "league": league_display,
            "sub_position": sub_position,
            "age": age,
            "contract_end_date": contract_end,
            "contract_leveraged": contract_leveraged,
        })
    return club_name, rows_out


# ────────────────────────────────────────────────────────────────────────────────
# Phase: pull dcaribou senior rosters for 14 top-tier leagues
# ────────────────────────────────────────────────────────────────────────────────

def pull_dcaribou_seniors(snapshot: date, lookback_start: date) -> list[tuple]:
    src = duckdb.connect(config.DUCKDB_FILE, read_only=True)
    dcaribou_ids = [l["id"] for l in config.LEAGUES if not l.get("external")]
    ids_sql = "(" + ",".join(f"'{i}'" for i in dcaribou_ids) + ")"

    src.execute(f"""
        CREATE OR REPLACE TEMP TABLE player_minutes AS
        SELECT a.player_id, SUM(a.minutes_played) AS minutes_in_window
        FROM appearances a
        JOIN competitions c ON c.competition_id = a.competition_id
        WHERE a.date >= DATE '{lookback_start}'
          AND a.date <= DATE '{snapshot}'
          AND c.type != 'national_team_competition'
        GROUP BY a.player_id
    """)

    rows = src.execute(f"""
        SELECT
            p.player_id, p.name,
            CAST(p.current_club_id AS VARCHAR) AS club_id,
            p.current_club_name,
            p.current_club_domestic_competition_id AS league_id,
            p.sub_position,
            CAST(EXTRACT(YEAR FROM AGE(DATE '{snapshot}', CAST(p.date_of_birth AS DATE))) AS INTEGER) AS age,
            CAST(p.contract_expiration_date AS DATE) AS contract_end,
            pm.minutes_in_window
        FROM players p
        JOIN clubs c ON c.club_id = CAST(p.current_club_id AS VARCHAR)
        LEFT JOIN player_minutes pm ON pm.player_id = p.player_id
        WHERE p.current_club_domestic_competition_id IN {ids_sql}
          AND c.domestic_competition_id IN {ids_sql}
          AND c.last_season >= '2025'
          AND p.current_club_id IS NOT NULL
          AND p.date_of_birth IS NOT NULL
    """).fetchall()
    return rows


# ────────────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────────────

def main() -> None:
    snapshot = config.SNAPSHOT_DATE
    lookback_start = months_before(snapshot, config.MINUTES_LOOKBACK_MONTHS)
    leverage_cutoff = config.end_of_season_plus(snapshot, config.CONTRACT_LEVERAGED_YEARS)

    print(f"Snapshot: {snapshot}   Lookback start: {lookback_start}")
    print(f"Leverage cutoff: {leverage_cutoff}")
    print()

    # === Top-tier seniors from dcaribou ===
    print("=== dcaribou senior rosters (14 top-tier leagues) ===")
    dca_rows = pull_dcaribou_seniors(snapshot, lookback_start)
    by_league: dict[str, int] = {}
    for r in dca_rows:
        by_league[r[4]] = by_league.get(r[4], 0) + 1
    for lid in sorted(by_league):
        print(f"  {lid:6s} {by_league[lid]:5d}")
    print(f"  total: {len(dca_rows):,}")

    # === Second-tier scrape ===
    print()
    print("=== Scraping second-tier squads (5 leagues, ~85 club pages) ===")
    scrape_rows: list[dict] = []
    for league in SECOND_TIER_LEAGUES:
        lid = league["id"]
        print(f"--- {lid} ({league['country']}) ---")
        clubs = fetch_league_clubs(lid, league["slug"])
        print(f"  {len(clubs)} clubs")
        total_for_league = 0
        for club in clubs:
            try:
                resolved_name, roster = fetch_club_squad(
                    club["club_id"], club["slug"],
                    lid, config.LEAGUE_DISPLAY[lid],
                    leverage_cutoff,
                )
            except Exception as e:
                print(f"    ! skipped {club['name']}: {type(e).__name__}: {e}")
                continue
            scrape_rows.extend(roster)
            total_for_league += len(roster)
            disp = (resolved_name or club["name"])[:34]
            print(f"    {disp:34s}  {len(roster)} players")
        print(f"  league total: {total_for_league}")

    # === Write to SQLite ===
    with sqlite3.connect(config.SQLITE_FILE) as dst:
        dst.execute("DELETE FROM senior_roster")
        for r in dca_rows:
            (player_id, name, club_id, club_name, league_id, sub_pos,
             age, contract_end, minutes_18m) = r
            contract_leveraged = (
                1 if contract_end and contract_end <= leverage_cutoff
                else (0 if contract_end else None)
            )
            dst.execute(
                "INSERT OR REPLACE INTO senior_roster VALUES (" + ",".join(["?"] * 14) + ")",
                (
                    int(player_id),
                    name,
                    str(club_id) if club_id is not None else None,
                    club_name,
                    league_id,
                    config.LEAGUE_DISPLAY.get(league_id, league_id),
                    sub_pos,
                    None,  # position_bucket — filled by 08
                    int(age) if age is not None else None,
                    str(contract_end) if contract_end else None,
                    int(minutes_18m) if minutes_18m is not None else None,
                    contract_leveraged,
                    "dcaribou",
                    str(snapshot),
                ),
            )
        for r in scrape_rows:
            dst.execute(
                "INSERT OR REPLACE INTO senior_roster VALUES (" + ",".join(["?"] * 14) + ")",
                (
                    r["player_id"],
                    r["name"],
                    r["club_id"],
                    r["club_name"],
                    r["league_id"],
                    r["league"],
                    r["sub_position"],
                    None,
                    r["age"],
                    str(r["contract_end_date"]) if r["contract_end_date"] else None,
                    None,
                    r["contract_leveraged"],
                    "tm_squad_scrape",
                    str(snapshot),
                ),
            )
        dst.commit()
        n = dst.execute("SELECT COUNT(*) FROM senior_roster").fetchone()[0]

    print()
    print(f"Wrote {n:,} rows to senior_roster")
    with sqlite3.connect(config.SQLITE_FILE) as con:
        rows = con.execute("""
            SELECT league_id, COUNT(*) AS players, COUNT(DISTINCT club_id) AS clubs,
                   SUM(CASE WHEN contract_leveraged=1 THEN 1 ELSE 0 END) AS leveraged,
                   SUM(CASE WHEN minutes_last_18m IS NULL THEN 1 ELSE 0 END) AS no_min
            FROM senior_roster GROUP BY 1 ORDER BY 1
        """).fetchall()
        print(f"  {'league':6s}  {'players':>8s} {'clubs':>6s} {'leveraged':>10s} {'no-min':>7s}")
        for r in rows:
            print(f"  {r[0]:6s}  {r[1]:>8d} {r[2]:>6d} {r[3]:>10d} {r[4]:>7d}")


if __name__ == "__main__":
    main()
