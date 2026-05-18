"""
Step 03 — Build the filtered player universe from dcaribou's dataset.

Applies the Day 2 filters and writes results to SQLite:
  - Current club in one of our configured leagues (excluding `external` ones — those come
    from the second-tier scraper run, merged in by a later step)
  - Age 17-24 inclusive (as of SNAPSHOT_DATE)
  - Current TM value within band
  - Contract end ≤ SNAPSHOT_DATE + CONTRACT_MAX_YEARS_AHEAD
  - ≥ MIN_MINUTES_SHARE of available minutes across all senior club competitions in
    the last MINUTES_LOOKBACK_MONTHS — relaxed for leagues flagged `relaxed_minutes`
    (Saudi/MLS, where dcaribou has no appearances)
  - Current TM value ≥ last permanent fee (NULL fee passes — academy graduates)

Computes three flag columns: right_priced, finished_product, contract_leveraged.

Also seeds `club_pressure` with all clubs in the same league set; scores left NULL.
"""

from datetime import date
import sqlite3
import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config


def months_before(d: date, months: int) -> date:
    """Subtract a whole number of calendar months from a date."""
    y, m = d.year, d.month - months
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, d.day)


def _sql_set(ids):
    return "(" + ",".join(f"'{i}'" for i in ids) + ")" if ids else "(NULL)"


def main() -> None:
    snapshot = config.SNAPSHOT_DATE
    lookback_start = months_before(snapshot, config.MINUTES_LOOKBACK_MONTHS)
    contract_cutoff = config.end_of_season_plus(snapshot, config.CONTRACT_MAX_YEARS_AHEAD)
    leverage_cutoff = config.end_of_season_plus(snapshot, config.CONTRACT_LEVERAGED_YEARS)

    # Leagues we can serve from dcaribou (everything except external scraped tiers).
    dcaribou_ids = [l["id"] for l in config.LEAGUES if not l.get("external")]
    relaxed_ids = config.RELAXED_MINUTES_IDS
    ids_sql = _sql_set(dcaribou_ids)
    relaxed_sql = _sql_set(relaxed_ids) if relaxed_ids else "(NULL)"

    print("Build configuration")
    print(f"  Snapshot date:           {snapshot}")
    print(f"  Lookback start:          {lookback_start}")
    print(f"  Contract cutoff:         {contract_cutoff}")
    print(f"  Leverage cutoff (flag):  {leverage_cutoff}")
    print(f"  Age band:                {config.AGE_MIN}-{config.AGE_MAX}")
    print(f"  Value band:              €{config.TM_VALUE_MIN_EUR:,} – €{config.TM_VALUE_MAX_EUR:,}")
    print(f"  Min minutes share:       {config.MIN_MINUTES_SHARE:.0%}")
    print(f"  Leagues (dcaribou):      {dcaribou_ids}")
    print(f"  Relaxed-minutes leagues: {sorted(relaxed_ids) or 'none'}")
    print()

    src = duckdb.connect(config.DUCKDB_FILE, read_only=True)

    # 1. Available minutes per club in the window.
    src.execute(f"""
        CREATE OR REPLACE TEMP TABLE club_available AS
        SELECT
            CAST(a.player_club_id AS VARCHAR) AS club_id,
            COUNT(DISTINCT a.game_id) AS games_in_window,
            COUNT(DISTINCT a.game_id) * 90 AS available_minutes
        FROM appearances a
        JOIN competitions c ON c.competition_id = a.competition_id
        WHERE a.date >= DATE '{lookback_start}'
          AND a.date <= DATE '{snapshot}'
          AND c.type != 'national_team_competition'
        GROUP BY a.player_club_id
    """)

    # 2. Player minutes in the window.
    src.execute(f"""
        CREATE OR REPLACE TEMP TABLE player_minutes AS
        SELECT
            a.player_id,
            SUM(a.minutes_played) AS minutes_in_window,
            COUNT(DISTINCT a.game_id) AS apps_in_window
        FROM appearances a
        JOIN competitions c ON c.competition_id = a.competition_id
        WHERE a.date >= DATE '{lookback_start}'
          AND a.date <= DATE '{snapshot}'
          AND c.type != 'national_team_competition'
        GROUP BY a.player_id
    """)

    # 3. Most recent transfer with a positive fee per player.
    src.execute("""
        CREATE OR REPLACE TEMP TABLE last_fee AS
        WITH paid AS (
            SELECT player_id, transfer_date, transfer_fee,
                   ROW_NUMBER() OVER (PARTITION BY player_id ORDER BY transfer_date DESC) AS rn
            FROM transfers
            WHERE transfer_fee IS NOT NULL AND transfer_fee > 0
        )
        SELECT player_id,
               transfer_date AS last_fee_date,
               CAST(transfer_fee AS BIGINT) AS last_fee_eur
        FROM paid
        WHERE rn = 1
    """)

    # 4. Funnel — same filter steps, broken down for visibility.
    funnel = src.execute(f"""
        WITH eligible AS (
            SELECT
                p.player_id,
                p.current_club_domestic_competition_id AS league_id,
                p.market_value_in_eur AS mv,
                CAST(EXTRACT(YEAR FROM AGE(DATE '{snapshot}', CAST(p.date_of_birth AS DATE))) AS INTEGER) AS age,
                CAST(p.contract_expiration_date AS DATE) AS contract_end,
                lf.last_fee_eur,
                COALESCE(pm.minutes_in_window, 0) AS minutes_in_window,
                ca.available_minutes
            FROM players p
            LEFT JOIN player_minutes pm ON pm.player_id = p.player_id
            LEFT JOIN last_fee lf ON lf.player_id = p.player_id
            LEFT JOIN club_available ca ON ca.club_id = p.current_club_id
            WHERE p.current_club_domestic_competition_id IN {ids_sql}
        )
        SELECT
            COUNT(*) AS in_leagues,
            COUNT(*) FILTER (WHERE age BETWEEN {config.AGE_MIN} AND {config.AGE_MAX}) AS after_age,
            COUNT(*) FILTER (WHERE age BETWEEN {config.AGE_MIN} AND {config.AGE_MAX}
                               AND mv BETWEEN {config.TM_VALUE_MIN_EUR} AND {config.TM_VALUE_MAX_EUR}) AS after_value,
            COUNT(*) FILTER (WHERE age BETWEEN {config.AGE_MIN} AND {config.AGE_MAX}
                               AND mv BETWEEN {config.TM_VALUE_MIN_EUR} AND {config.TM_VALUE_MAX_EUR}
                               AND contract_end IS NOT NULL AND contract_end <= DATE '{contract_cutoff}') AS after_contract,
            COUNT(*) FILTER (WHERE age BETWEEN {config.AGE_MIN} AND {config.AGE_MAX}
                               AND mv BETWEEN {config.TM_VALUE_MIN_EUR} AND {config.TM_VALUE_MAX_EUR}
                               AND contract_end IS NOT NULL AND contract_end <= DATE '{contract_cutoff}'
                               AND (
                                   league_id IN {relaxed_sql}
                                   OR (available_minutes IS NOT NULL AND available_minutes > 0
                                       AND (minutes_in_window * 1.0 / available_minutes) >= {config.MIN_MINUTES_SHARE})
                               )) AS after_minutes,
            COUNT(*) FILTER (WHERE age BETWEEN {config.AGE_MIN} AND {config.AGE_MAX}
                               AND mv BETWEEN {config.TM_VALUE_MIN_EUR} AND {config.TM_VALUE_MAX_EUR}
                               AND contract_end IS NOT NULL AND contract_end <= DATE '{contract_cutoff}'
                               AND (
                                   league_id IN {relaxed_sql}
                                   OR (available_minutes IS NOT NULL AND available_minutes > 0
                                       AND (minutes_in_window * 1.0 / available_minutes) >= {config.MIN_MINUTES_SHARE})
                               )
                               AND (last_fee_eur IS NULL OR mv >= last_fee_eur)) AS after_fee
        FROM eligible
    """).fetchone()
    (in_leagues, after_age, after_value, after_contract, after_minutes, after_fee) = funnel
    print("Filter funnel")
    print(f"  In the league set:                                {in_leagues:>6,}")
    print(f"  After age {config.AGE_MIN}-{config.AGE_MAX}:                                 {after_age:>6,}")
    print(f"  After value €{config.TM_VALUE_MIN_EUR/1e6:.0f}m–€{config.TM_VALUE_MAX_EUR/1e6:.0f}m:                          {after_value:>6,}")
    print(f"  After contract ends ≤ {contract_cutoff}:                  {after_contract:>6,}")
    print(f"  After minutes ≥ {config.MIN_MINUTES_SHARE:.0%} (relaxed for SA1/MLS1):       {after_minutes:>6,}")
    print(f"  After current value ≥ last fee:                   {after_fee:>6,}")
    print()

    # 5. Pull the final universe with flag columns computed.
    rows = src.execute(f"""
        SELECT
            p.player_id,
            p.name,
            p.current_club_name,
            p.current_club_id,
            p.current_club_domestic_competition_id AS league_id,
            p.position,
            p.sub_position,
            CAST(EXTRACT(YEAR FROM AGE(DATE '{snapshot}', CAST(p.date_of_birth AS DATE))) AS INTEGER) AS age,
            CAST(p.date_of_birth AS DATE) AS dob,
            p.market_value_in_eur,
            CAST(p.contract_expiration_date AS DATE) AS contract_end,
            lf.last_fee_eur,
            lf.last_fee_date,
            COALESCE(pm.minutes_in_window, 0) AS minutes_in_window,
            COALESCE(pm.apps_in_window, 0) AS apps_in_window,
            ca.available_minutes,
            p.agent_name,
            p.foot,
            p.height_in_cm,
            p.country_of_citizenship,
            -- Flag columns:
            CASE WHEN lf.last_fee_eur IS NULL OR p.market_value_in_eur >= lf.last_fee_eur
                 THEN 1 ELSE 0 END AS right_priced,
            CASE WHEN p.current_club_domestic_competition_id IN {relaxed_sql} THEN 0
                 WHEN ca.available_minutes IS NULL OR ca.available_minutes = 0 THEN 0
                 WHEN (COALESCE(pm.minutes_in_window,0) * 1.0 / ca.available_minutes) >= {config.MIN_MINUTES_SHARE}
                 THEN 1 ELSE 0 END AS finished_product,
            CASE WHEN CAST(p.contract_expiration_date AS DATE) <= DATE '{leverage_cutoff}'
                 THEN 1 ELSE 0 END AS contract_leveraged
        FROM players p
        LEFT JOIN player_minutes pm ON pm.player_id = p.player_id
        LEFT JOIN last_fee lf ON lf.player_id = p.player_id
        LEFT JOIN club_available ca ON ca.club_id = p.current_club_id
        WHERE p.current_club_domestic_competition_id IN {ids_sql}
          AND CAST(EXTRACT(YEAR FROM AGE(DATE '{snapshot}', CAST(p.date_of_birth AS DATE))) AS INTEGER) BETWEEN {config.AGE_MIN} AND {config.AGE_MAX}
          AND p.market_value_in_eur BETWEEN {config.TM_VALUE_MIN_EUR} AND {config.TM_VALUE_MAX_EUR}
          AND p.contract_expiration_date IS NOT NULL
          AND CAST(p.contract_expiration_date AS DATE) <= DATE '{contract_cutoff}'
          AND (
              p.current_club_domestic_competition_id IN {relaxed_sql}
              OR (ca.available_minutes IS NOT NULL AND ca.available_minutes > 0
                  AND (COALESCE(pm.minutes_in_window, 0) * 1.0 / ca.available_minutes) >= {config.MIN_MINUTES_SHARE})
          )
          AND (lf.last_fee_eur IS NULL OR p.market_value_in_eur >= lf.last_fee_eur)
        ORDER BY p.current_club_domestic_competition_id, p.market_value_in_eur DESC
    """).fetchall()

    # 6. Clubs in our league set for the club_pressure stub.
    club_rows = src.execute(f"""
        SELECT c.club_id, c.name, c.domestic_competition_id
        FROM clubs c
        WHERE c.domestic_competition_id IN {ids_sql}
        ORDER BY c.domestic_competition_id, c.name
    """).fetchall()

    # 7. Write to SQLite.
    sqlite_path = Path(config.SQLITE_FILE)
    with sqlite3.connect(sqlite_path) as dst:
        dst.execute("DELETE FROM player_universe WHERE data_source = 'dcaribou'")
        dst.execute("DELETE FROM club_pressure")
        for r in rows:
            (player_id, name, current_club, current_club_id, league_id, position, sub_position,
             age, dob, mv, contract_end, last_fee_eur, last_fee_date,
             minutes_in_window, apps_in_window, available_minutes,
             agent_name, foot, height_cm, nationality,
             right_priced, finished_product, contract_leveraged) = r
            share = round(100 * minutes_in_window / available_minutes, 1) if available_minutes else None
            current_club_id_str = str(current_club_id) if current_club_id is not None else None
            dst.execute(
                "INSERT INTO player_universe VALUES (" + ",".join(["?"] * 32) + ")",
                (
                    int(player_id),
                    name,
                    current_club,
                    current_club_id_str,
                    config.LEAGUE_DISPLAY.get(league_id, league_id),
                    league_id,
                    position,
                    sub_position,
                    int(age) if age is not None else None,
                    str(dob) if dob else None,
                    int(mv) if mv is not None else None,
                    str(contract_end) if contract_end else None,
                    int(last_fee_eur) if last_fee_eur is not None else None,
                    str(last_fee_date) if last_fee_date else None,
                    int(minutes_in_window),
                    int(apps_in_window),
                    int(available_minutes) if available_minutes is not None else None,
                    share,
                    agent_name,
                    foot,
                    int(height_cm) if height_cm is not None else None,
                    nationality,
                    int(right_priced),
                    int(finished_product),
                    int(contract_leveraged),
                    None,  # position_bucket — set by 09_compute_sellability.py
                    None,  # sellability_score — set by 09_compute_sellability.py
                    current_club,        # parent_club — default = current_club; overwritten by 11
                    current_club_id_str, # parent_club_id — default = current_club_id
                    0,                   # on_loan — default 0; flipped by 11
                    "dcaribou",
                    str(snapshot),
                ),
            )
        for cid, cname, comp_id in club_rows:
            dst.execute(
                """INSERT INTO club_pressure
                   (club_id, name, league, league_id,
                    manager_change_flag, public_must_sell_flag, snapshot_date)
                   VALUES (?, ?, ?, ?, 0, 0, ?)""",
                (
                    str(cid),
                    cname,
                    config.LEAGUE_DISPLAY.get(comp_id, comp_id),
                    comp_id,
                    str(snapshot),
                ),
            )
        dst.commit()
        pu_n = dst.execute("SELECT COUNT(*) FROM player_universe").fetchone()[0]
        cp_n = dst.execute("SELECT COUNT(*) FROM club_pressure").fetchone()[0]

    print(f"Wrote {pu_n:,} rows to player_universe")
    print(f"Wrote {cp_n:,} rows to club_pressure (scores all NULL until Day 3)")


if __name__ == "__main__":
    main()
