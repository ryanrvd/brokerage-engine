"""
Step 06 — Lightweight Transfermarkt scraper for 5 second-tier leagues.

dcaribou's published dataset is top-tier-only, so for ENG-2, FRA-2, ESP-2, ITA-2,
and GER-2 we hit TM URLs directly. For each league:

  1. Paginate the per-league market-value ranking, collect U24 players ≥ value floor.
  2. For each candidate, fetch their profile page → contract, club, agent, DOB, etc.
  3. Apply the Day 1 filters and write rows to player_universe with data_source='tm_scrape'.

What we DON'T scrape on Day 2 (deferred):
  - Per-match minutes for 2024/25 + 2025/26 → finished_product = NULL for these rows.
  - Transfer history & fees → last_fee_paid_eur = NULL (treated as academy = passes).

Caching: every fetched page is cached under data/tm_cache/ so re-runs are free.
Politeness: ~1.5s sleep between live (un-cached) requests.
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

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
SLEEP_BETWEEN = 1.5
MAX_PAGES_PER_LEAGUE = 30

CACHE_DIR = Path("data/tm_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

_ssl_context = ssl.create_default_context(cafile=certifi.where())


# ────────────────────────────────────────────────────────────────────────────────
# HTTP + cache
# ────────────────────────────────────────────────────────────────────────────────

def fetch(url: str, cache_key: str) -> str:
    """Fetch URL; cache HTML on disk so reruns don't re-hit TM."""
    cache_path = CACHE_DIR / f"{cache_key}.html"
    if cache_path.exists():
        return cache_path.read_text(encoding="utf-8")
    time.sleep(SLEEP_BETWEEN)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, context=_ssl_context, timeout=30) as response:
            html = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code in (429, 503):
            # Server rate-limit or transient — wait and retry once.
            print(f"  ! HTTP {e.code} on {cache_key}, backing off 30s and retrying")
            time.sleep(30)
            with urllib.request.urlopen(req, context=_ssl_context, timeout=30) as response:
                html = response.read().decode("utf-8", errors="replace")
        else:
            raise
    cache_path.write_text(html, encoding="utf-8")
    return html


# ────────────────────────────────────────────────────────────────────────────────
# Parsers
# ────────────────────────────────────────────────────────────────────────────────

def parse_market_value(text: str) -> int | None:
    """Convert '€28.00m' / '€800k' / '-' into euros (int) or None."""
    text = (text or "").strip()
    m = re.match(r"€\s*([\d.,]+)\s*([mk])", text, re.IGNORECASE)
    if not m:
        return None
    amount = float(m.group(1).replace(",", "."))
    unit = m.group(2).lower()
    return int(amount * (1_000_000 if unit == "m" else 1_000))


def parse_date_dmy(text: str) -> date | None:
    """Parse 'DD/MM/YYYY' (with optional trailing '(age)')."""
    if not text:
        return None
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def parse_height(text: str) -> int | None:
    """Convert '1,78 m' into 178 cm."""
    m = re.match(r"(\d+)[,.](\d+)\s*m", (text or "").strip())
    if not m:
        return None
    return int(m.group(1)) * 100 + int(m.group(2))


# ────────────────────────────────────────────────────────────────────────────────
# Phase 1 — list candidates per league
# ────────────────────────────────────────────────────────────────────────────────

def list_candidates(league_id: str, slug: str) -> list[dict]:
    """Paginate market-value ranking. Return U24 players ≥ floor with basic info."""
    candidates: list[dict] = []
    for page in range(1, MAX_PAGES_PER_LEAGUE + 1):
        url = f"https://www.transfermarkt.com/{slug}/marktwerte/wettbewerb/{league_id}/page/{page}"
        html = fetch(url, f"list_{league_id}_p{page}")
        soup = BeautifulSoup(html, "lxml")
        table = soup.select_one("table.items")
        if not table:
            break
        rows = table.select("tbody > tr")
        if not rows:
            break
        added_this_page = 0
        last_mv = None
        for r in rows:
            cells = r.find_all("td", recursive=False)
            if len(cells) < 6:
                continue
            link = cells[1].select_one("a[href*='/profil/spieler/']")
            if not link:
                continue
            m = re.search(r"/([^/]+)/profil/spieler/(\d+)", link["href"])
            if not m:
                continue
            slug_name = m.group(1)
            player_id = int(m.group(2))
            name = link.text.strip()
            position = cells[1].get_text(" ", strip=True).replace(name, "").strip()
            try:
                age = int(cells[3].get_text(strip=True))
            except ValueError:
                age = None
            mv = parse_market_value(cells[5].get_text(strip=True))
            if mv is None:
                continue
            last_mv = mv
            if mv < config.TM_VALUE_MIN_EUR:
                continue
            if age is None or not (config.AGE_MIN <= age <= config.AGE_MAX):
                continue
            candidates.append({
                "player_id": player_id,
                "slug_name": slug_name,
                "name": name,
                "position_combined": position,
                "age_on_listing": age,
                "market_value_in_eur": mv,
                "league_id": league_id,
            })
            added_this_page += 1
        # Stop paginating once the last row on the page is below the floor.
        if last_mv is not None and last_mv < config.TM_VALUE_MIN_EUR:
            break
    return candidates


# ────────────────────────────────────────────────────────────────────────────────
# Phase 2 — profile detail per candidate
# ────────────────────────────────────────────────────────────────────────────────

def scrape_profile(player_id: int, slug_name: str) -> dict:
    """Scrape the player's profile page and return a dict of structured fields."""
    url = f"https://www.transfermarkt.com/{slug_name}/profil/spieler/{player_id}"
    html = fetch(url, f"profile_{player_id}")
    soup = BeautifulSoup(html, "lxml")

    # Build a label→value map from the info table.
    info: dict[str, str] = {}
    labels = soup.select(".info-table__content--regular")
    for lbl in labels:
        # Each label has a sibling .info-table__content--bold with the value.
        val = lbl.find_next_sibling("span", class_="info-table__content--bold") \
              or lbl.find_next("span", class_="info-table__content--bold")
        if val:
            key = lbl.get_text(strip=True).rstrip(":").strip()
            info[key] = val.get_text(" ", strip=True)

    # Current club from the data-header (more reliable than info table for club_id).
    club_link = soup.select_one(".data-header__club a")
    club_name = club_link.get_text(strip=True) if club_link else info.get("Current club")
    club_id = None
    if club_link and club_link.get("href"):
        m = re.search(r"/verein/(\d+)", club_link["href"])
        if m:
            club_id = m.group(1)

    pos = info.get("Position", "")
    primary_pos, _, sub_pos = pos.partition(" - ") if " - " in pos else (pos, "", "")
    sub_pos = sub_pos.strip() or pos.strip()

    dob = parse_date_dmy(info.get("Date of birth/Age", ""))
    contract_end = parse_date_dmy(info.get("Contract expires", ""))
    citizenship = (info.get("Citizenship", "").split() or [None])[0]

    return {
        "name": info.get("Name in home country") or info.get("Full name"),
        "current_club": club_name,
        "current_club_id": club_id,
        "primary_position": primary_pos.strip() or None,
        "sub_position": sub_pos or None,
        "date_of_birth": dob,
        "contract_end_date": contract_end,
        "agent_name": info.get("Player agent") or None,
        "foot": info.get("Foot") or None,
        "height_cm": parse_height(info.get("Height", "")),
        "nationality": citizenship,
    }


# ────────────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────────────

def main() -> None:
    snapshot = config.SNAPSHOT_DATE
    contract_cutoff = config.end_of_season_plus(snapshot, config.CONTRACT_MAX_YEARS_AHEAD)
    leverage_cutoff = config.end_of_season_plus(snapshot, config.CONTRACT_LEVERAGED_YEARS)

    print(f"Snapshot: {snapshot}   Contract cutoff: {contract_cutoff}")
    print()

    all_rows = []
    per_league_counts = {}
    for league in SECOND_TIER_LEAGUES:
        league_id = league["id"]
        print(f"=== {league_id} ({league['country']}) ===")
        candidates = list_candidates(league_id, league["slug"])
        print(f"  {len(candidates)} candidates after age + value floor")
        if not candidates:
            per_league_counts[league_id] = 0
            continue

        passing = []
        for i, cand in enumerate(candidates, start=1):
            try:
                prof = scrape_profile(cand["player_id"], cand["slug_name"])
            except Exception as e:
                print(f"    ! skipped {cand['name']}: {e}")
                continue
            ce = prof["contract_end_date"]
            if ce is None or ce > contract_cutoff:
                continue  # contract too long or unknown
            # age — recompute from DOB if we have it (more reliable than listing).
            dob = prof["date_of_birth"]
            if dob:
                age = snapshot.year - dob.year - ((snapshot.month, snapshot.day) < (dob.month, dob.day))
            else:
                age = cand["age_on_listing"]
            if age is None or not (config.AGE_MIN <= age <= config.AGE_MAX):
                continue
            row = {
                "player_id": cand["player_id"],
                "name": prof["name"] or cand["name"],
                "current_club": prof["current_club"],
                "current_club_id": prof["current_club_id"],
                "league_id": league_id,
                "league_display": config.LEAGUE_DISPLAY.get(league_id, league_id),
                "primary_position": prof["primary_position"],
                "sub_position": prof["sub_position"],
                "age": age,
                "date_of_birth": dob,
                "current_tm_value_eur": cand["market_value_in_eur"],
                "contract_end_date": ce,
                "agency": prof["agent_name"],
                "foot": prof["foot"],
                "height_cm": prof["height_cm"],
                "nationality": prof["nationality"],
                "contract_leveraged": 1 if ce <= leverage_cutoff else 0,
            }
            passing.append(row)
            print(f"    ✓ {row['name']:<28s} {row['current_club']:<32s} contract {ce}  {row['agency'] or '—'}")
        print(f"  {len(passing)} pass full filters")
        per_league_counts[league_id] = len(passing)
        all_rows.extend(passing)

    # Write to SQLite — scrape data is authoritative (current league walk).
    # If a player_id already exists from the dcaribou pass with a stale league tag,
    # the scrape replaces it. This corrects e.g. relegated players still tagged top-tier.
    with sqlite3.connect(config.SQLITE_FILE) as dst:
        dst.execute("DELETE FROM player_universe WHERE data_source = 'tm_scrape'")
        for r in all_rows:
            dst.execute(
                "INSERT OR REPLACE INTO player_universe VALUES (" + ",".join(["?"] * 32) + ")",
                (
                    r["player_id"],
                    r["name"],
                    r["current_club"],
                    r["current_club_id"],
                    r["league_display"],
                    r["league_id"],
                    r["primary_position"],
                    r["sub_position"],
                    r["age"],
                    str(r["date_of_birth"]) if r["date_of_birth"] else None,
                    r["current_tm_value_eur"],
                    str(r["contract_end_date"]),
                    None,  # last_fee_paid_eur — deferred
                    None,  # last_fee_paid_date — deferred
                    None,  # minutes_last_18m — deferred
                    None,  # appearances_last_18m — deferred
                    None,  # minutes_available_18m — deferred
                    None,  # minutes_share_pct — deferred
                    r["agency"],
                    r["foot"],
                    r["height_cm"],
                    r["nationality"],
                    1,     # right_priced — NULL last_fee passes the rule
                    None,  # finished_product — NULL, minutes not verified
                    r["contract_leveraged"],
                    None,  # position_bucket — set by 09_compute_sellability.py
                    None,  # sellability_score — set by 09_compute_sellability.py
                    r["current_club"],     # parent_club — default = current_club
                    r["current_club_id"],  # parent_club_id — default = current_club_id
                    0,                     # on_loan — default 0; flipped by 11
                    "tm_scrape",
                    str(snapshot),
                ),
            )
        dst.commit()

    print()
    print("Per-league summary:")
    for lid, n in per_league_counts.items():
        print(f"  {config.LEAGUE_DISPLAY.get(lid, lid):<24s} {n}")
    print(f"  {'─' * 50}")
    print(f"  {'Total (scraped)':<24s} {sum(per_league_counts.values())}")


if __name__ == "__main__":
    main()
