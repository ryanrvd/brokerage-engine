"""
Step 28 — Ingest full Premier League squads into player_universe.

Expands the player_universe from "filtered sellable cohort" to "every squad
player at every PL club". Uses dcaribou data (no TM scraping required) to
pull ALL players at PL clubs without applying the Day 1 age/value/contract/
minutes filters.

Players already in player_universe (from 03/06) are preserved via INSERT OR
IGNORE — they have richer data (e.g. minutes, fee history) from the filtered
pipeline.

Pipeline position: runs after 07_extend_roster + 19_apply_league_overrides
(so league corrections are in place), before 08_compute_pressure.

Known limitation: loaned-OUT PL players (physically at non-PL clubs but owned
by PL clubs) are NOT captured here. dcaribou shows them under their loan
destination. Script 11 patches parent_club for the filtered cohort; for the
full PL expansion, loaned-out players outside the filtered cohort require TM
profile scraping (deferred to when TM access is restored). Loaned-out players
already in the filtered cohort (from script 03) DO get parent_club patched by
script 11.
"""

import sqlite3
import sys
from datetime import date
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import config
from _position_buckets import bucket_for

PL_LEAGUE_ID = "GB1"


def months_before(d: date, months: int) -> date:
    y, m = d.year, d.month - months
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, d.day)


def get_active_pl_clubs(con: sqlite3.Connection) -> set[str]:
    """Return club_ids of real PL clubs (those with players in senior_roster)."""
    rows = con.execute("""
        SELECT DISTINCT club_id FROM senior_roster
        WHERE league_id = ? AND club_id IS NOT NULL
    """, (PL_LEAGUE_ID,)).fetchall()
    return {r[0] for r in rows}


def main() -> None:
    snapshot = config.SNAPSHOT_DATE
    lookback_start = months_before(snapshot, config.MINUTES_LOOKBACK_MONTHS)
    contract_cutoff = config.end_of_season_plus(snapshot, config.CONTRACT_MAX_YEARS_AHEAD)
    leverage_cutoff = config.end_of_season_plus(snapshot, config.CONTRACT_LEVERAGED_YEARS)

    print(f"Snapshot: {snapshot}   Lookback: {lookback_start}")
    print(f"Contract cutoff: {contract_cutoff}   Leverage cutoff: {leverage_cutoff}")
    print()

    # Get active PL club IDs from senior_roster (post-override).
    with sqlite3.connect(config.SQLITE_FILE) as con:
        pl_club_ids = get_active_pl_clubs(con)
        existing_pids = set(
            r[0] for r in con.execute("SELECT player_id FROM player_universe").fetchall()
        )

    print(f"Active PL clubs: {len(pl_club_ids)}")
    print(f"Existing player_universe: {len(existing_pids)} rows")
    print()

    # Pull ALL players at PL clubs from dcaribou — no age/value/contract/minutes filters.
    src = duckdb.connect(config.DUCKDB_FILE, read_only=True)
    pl_ids_sql = "(" + ",".join(f"'{i}'" for i in pl_club_ids) + ")"

    # Available minutes per club in the window (same as script 03).
    src.execute(f"""
        CREATE OR REPLACE TEMP TABLE club_available AS
        SELECT
            CAST(a.player_club_id AS VARCHAR) AS club_id,
            COUNT(DISTINCT a.game_id) * 90 AS available_minutes
        FROM appearances a
        JOIN competitions c ON c.competition_id = a.competition_id
        WHERE a.date >= DATE '{lookback_start}'
          AND a.date <= DATE '{snapshot}'
          AND c.type != 'national_team_competition'
        GROUP BY a.player_club_id
    """)

    # Player minutes in the window.
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

    # Most recent transfer with a positive fee per player.
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

    # Pull every player at a PL club — no filters.
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
            CASE WHEN lf.last_fee_eur IS NULL OR p.market_value_in_eur >= lf.last_fee_eur
                 THEN 1 ELSE 0 END AS right_priced,
            CASE WHEN ca.available_minutes IS NULL OR ca.available_minutes = 0 THEN NULL
                 WHEN (COALESCE(pm.minutes_in_window,0) * 1.0 / ca.available_minutes) >= {config.MIN_MINUTES_SHARE}
                 THEN 1 ELSE 0 END AS finished_product,
            CASE WHEN CAST(p.contract_expiration_date AS DATE) <= DATE '{leverage_cutoff}'
                 THEN 1 ELSE 0 END AS contract_leveraged
        FROM players p
        LEFT JOIN player_minutes pm ON pm.player_id = p.player_id
        LEFT JOIN last_fee lf ON lf.player_id = p.player_id
        LEFT JOIN club_available ca ON ca.club_id = p.current_club_id
        WHERE CAST(p.current_club_id AS VARCHAR) IN {pl_ids_sql}
          AND p.date_of_birth IS NOT NULL
          AND p.current_club_id IS NOT NULL
        ORDER BY p.current_club_name, p.market_value_in_eur DESC NULLS LAST
    """).fetchall()
    src.close()

    print(f"dcaribou PL players (unfiltered): {len(rows)}")

    # Insert into player_universe — INSERT OR IGNORE preserves existing rows.
    inserted = 0
    skipped_existing = 0

    with sqlite3.connect(config.SQLITE_FILE) as dst:
        for r in rows:
            (player_id, name, current_club, current_club_id, league_id,
             position, sub_position, age, dob, mv, contract_end,
             last_fee_eur, last_fee_date, minutes_in_window, apps_in_window,
             available_minutes, agent_name, foot, height_cm, nationality,
             right_priced, finished_product, contract_leveraged) = r

            pid = int(player_id)
            if pid in existing_pids:
                skipped_existing += 1
                continue

            share = (
                round(100 * minutes_in_window / available_minutes, 1)
                if available_minutes else None
            )
            current_club_id_str = str(current_club_id) if current_club_id else None
            league_display = config.LEAGUE_DISPLAY.get(PL_LEAGUE_ID, PL_LEAGUE_ID)

            try:
                dst.execute(
                    "INSERT OR IGNORE INTO player_universe VALUES ("
                    + ",".join(["?"] * 34) + ")",
                    (
                        pid,
                        name,
                        current_club,
                        current_club_id_str,
                        league_display,
                        PL_LEAGUE_ID,
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
                        int(right_priced) if right_priced is not None else None,
                        int(finished_product) if finished_product is not None else None,
                        int(contract_leveraged) if contract_leveraged is not None else None,
                        bucket_for(sub_position),
                        None,                   # sellability_score — set by 09
                        current_club,            # parent_club — default = current
                        current_club_id_str,     # parent_club_id — overwritten by 11
                        0,                       # on_loan — overwritten by 11
                        "pl_squad_full",
                        str(snapshot),
                        None,                   # sellability_status — set by 09
                        None,                   # loan_end_date
                    ),
                )
                inserted += 1
                existing_pids.add(pid)
            except sqlite3.IntegrityError:
                skipped_existing += 1

        dst.commit()

        # Summary stats.
        n_total = dst.execute("SELECT COUNT(*) FROM player_universe").fetchone()[0]
        n_pl_full = dst.execute(
            "SELECT COUNT(*) FROM player_universe WHERE data_source = 'pl_squad_full'"
        ).fetchone()[0]
        n_dca_pl = dst.execute(
            "SELECT COUNT(*) FROM player_universe WHERE data_source = 'dcaribou' AND league_id = ?",
            (PL_LEAGUE_ID,),
        ).fetchone()[0]

        # Per-club breakdown.
        club_counts = dst.execute("""
            SELECT pu.parent_club, COUNT(*) AS n,
                   SUM(CASE WHEN pu.data_source = 'pl_squad_full' THEN 1 ELSE 0 END) AS n_new
            FROM player_universe pu
            WHERE pu.league_id = ?
            GROUP BY pu.parent_club
            ORDER BY n DESC
        """, (PL_LEAGUE_ID,)).fetchall()

    bar = "─" * 80
    print()
    print(bar)
    print("PL Full Squad Ingestion — Summary")
    print(bar)
    print(f"  dcaribou PL players (raw):    {len(rows)}")
    print(f"  Skipped (already in universe): {skipped_existing}")
    print(f"  New players inserted:          {inserted}")
    print()
    print(f"  player_universe total:         {n_total}")
    print(f"    PL from dcaribou (filtered): {n_dca_pl}")
    print(f"    PL from full expansion:      {n_pl_full}")
    print()
    print("Per-club breakdown:")
    print(f"  {'club':40s} {'total':>6s} {'new':>5s}")
    for club_name, n, n_new in club_counts:
        print(f"  {(club_name or '?')[:40]:40s} {n:>6d} {n_new:>5d}")


if __name__ == "__main__":
    main()
