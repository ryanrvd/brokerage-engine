"""
Step 13 — Day 3.6 patch: backfill last_fee_paid_eur via TM's transferHistory JSON.

Why this exists: dcaribou's transfers table has ~93% NULL fees and is missing the
entire recent-permanent-move record for our universe (every one of the 163 players
had last_fee_paid_eur = NULL pre-scrape). The right_priced filter is the brief's
first-listed criterion ("bought at right price") so it has to fire on real data,
not on the NULL-pass academy default.

Endpoint discovered (undocumented but stable):
   https://www.transfermarkt.com/ceapi/transferHistory/list/{player_id}
Returns JSON with an ordered transfer list. We pick the most recent
   - upcoming = false (already happened)
   - fee starts with '€'  (permanent, fee > 0 — loans show 'loan transfer' /
     'End of loan'; free transfers show 'free transfer' or '-' / '?')

~163 requests × 1.5s ≈ 4 minutes wall time. Cached forever at data/tm_cache/fees_*.json.
"""

import json
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

FEE_RE = re.compile(r"^€\s*([\d.,]+)\s*([mk]?)\s*$", re.IGNORECASE)


def fetch_fees(player_id: int) -> dict | None:
    """Fetch TM transferHistory JSON for a player. Cached at fees_{id}.json."""
    cache = CACHE_DIR / f"fees_{player_id}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    url = f"https://www.transfermarkt.com/ceapi/transferHistory/list/{player_id}"
    time.sleep(SLEEP_BETWEEN)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, context=_ssl_context, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code in (429, 503):
            print(f"  ! HTTP {e.code} on {player_id}, backing off 30s")
            time.sleep(30)
            with urllib.request.urlopen(req, context=_ssl_context, timeout=30) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        else:
            print(f"  ! HTTP {e.code} on {player_id}: skipping")
            return None
    cache.write_text(raw, encoding="utf-8")
    return json.loads(raw)


def parse_fee_eur(fee_str: str | None) -> int | None:
    """Return euros (int) for permanent-fee strings; None for loans/free/empty."""
    if not fee_str:
        return None
    m = FEE_RE.match(fee_str.strip())
    if not m:
        return None
    amount = float(m.group(1).replace(",", "."))
    unit = m.group(2).lower()
    mult = 1_000_000 if unit == "m" else (1_000 if unit == "k" else 1)
    return int(amount * mult)


def last_permanent_fee(history: dict) -> tuple[int | None, date | None]:
    """Walk transfers (most-recent first), return (fee_eur, date) for first match."""
    for t in history.get("transfers", []):
        if t.get("upcoming"):
            continue
        fee_eur = parse_fee_eur(t.get("fee"))
        if fee_eur is None or fee_eur <= 0:
            continue
        try:
            d = datetime.strptime(t.get("dateUnformatted", ""), "%Y-%m-%d").date()
        except ValueError:
            d = None
        return fee_eur, d
    return None, None


def main() -> None:
    with sqlite3.connect(config.SQLITE_FILE) as con:
        con.row_factory = sqlite3.Row
        # Skip pl_squad_full players — they don't go through the filtered
        # right_priced pipeline (TM kader has no fee data, and TM's JSON
        # transferHistory endpoint is currently blocked by CloudFront).
        rows = con.execute(
            "SELECT player_id, name, current_tm_value_eur, right_priced "
            "FROM player_universe "
            "WHERE data_source != 'pl_squad_full' "
            "ORDER BY player_id"
        ).fetchall()
    print(f"Universe: {len(rows)} players")
    cached = sum(1 for r in rows if (CACHE_DIR / f"fees_{r['player_id']}.json").exists())
    print(f"Cache: {cached}/{len(rows)} hit; {len(rows) - cached} fresh fetches needed")
    print()

    # Snapshot pre-state for the before/after delta.
    before_right_priced_1 = sum(1 for r in rows if r["right_priced"] == 1)

    updates: list[tuple[int | None, str | None, int, int]] = []
    missing_history: list[int] = []
    fee_found = 0
    for i, r in enumerate(rows, 1):
        pid = r["player_id"]
        history = fetch_fees(pid)
        if history is None:
            missing_history.append(pid)
            continue
        fee_eur, fee_date = last_permanent_fee(history)
        if fee_eur is not None:
            fee_found += 1
        mv = r["current_tm_value_eur"]
        new_right_priced = 1 if (fee_eur is None or (mv is not None and mv >= fee_eur)) else 0
        updates.append((
            fee_eur,
            fee_date.isoformat() if fee_date else None,
            new_right_priced,
            pid,
        ))
        if i % 25 == 0:
            print(f"  …processed {i}/{len(rows)}")

    with sqlite3.connect(config.SQLITE_FILE) as con:
        con.executemany("""
            UPDATE player_universe
               SET last_fee_paid_eur = ?, last_fee_paid_date = ?, right_priced = ?
             WHERE player_id = ?
        """, updates)
        con.commit()

        # Before/after summary.
        after = con.execute("""
            SELECT
                SUM(CASE WHEN right_priced = 1 THEN 1 ELSE 0 END) AS rp1,
                SUM(CASE WHEN right_priced = 0 THEN 1 ELSE 0 END) AS rp0,
                SUM(CASE WHEN last_fee_paid_eur IS NOT NULL THEN 1 ELSE 0 END) AS with_fee,
                SUM(CASE WHEN last_fee_paid_eur IS NULL THEN 1 ELSE 0 END) AS without_fee
            FROM player_universe
        """).fetchone()

        print()
        print("─" * 70)
        print(f"right_priced=1 before: {before_right_priced_1}/{len(rows)}")
        print(f"right_priced=1 after:  {after[0]}/{len(rows)}")
        print(f"right_priced=0 after:  {after[1]}/{len(rows)}   ← players now failing the filter")
        print(f"With fee data: {after[2]}/{len(rows)};  without (academy/no record): {after[3]}/{len(rows)}")
        if missing_history:
            print(f"Profile fetch failures: {len(missing_history)}")
        print()

        # Top 10 players whose right_priced flipped 1 → 0.
        flipped = con.execute("""
            SELECT name, current_club, current_tm_value_eur, last_fee_paid_eur, last_fee_paid_date
            FROM player_universe
            WHERE right_priced = 0
            ORDER BY (last_fee_paid_eur - current_tm_value_eur) DESC
            LIMIT 10
        """).fetchall()
        if flipped:
            print("Top 10 right_priced flipped (1 → 0) — biggest absolute overpaid gaps:")
            print(f"  {'player':28s} {'club':28s} {'TM value':>11s} {'last fee':>11s} {'gap':>11s}  date")
            for row in flipped:
                gap = (row[3] or 0) - (row[2] or 0)
                print(f"  {(row[0] or '')[:28]:28s} {(row[1] or '')[:28]:28s} "
                      f"€{(row[2] or 0)/1e6:>9.2f}m €{(row[3] or 0)/1e6:>9.2f}m "
                      f"€{gap/1e6:>+9.2f}m  {row[4]}")
        # Show fee-data coverage by source.
        print()
        breakdown = con.execute("""
            SELECT data_source,
                   COUNT(*) AS n,
                   SUM(CASE WHEN last_fee_paid_eur IS NOT NULL THEN 1 ELSE 0 END) AS with_fee
            FROM player_universe GROUP BY 1
        """).fetchall()
        print("Fee-data coverage by source:")
        for r in breakdown:
            print(f"  {r[0]:12s}  n={r[1]:>3d}  with_fee={r[2]}")


if __name__ == "__main__":
    main()
