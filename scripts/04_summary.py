"""
Step 04 — Day 1 summary.

Prints per-league counts and a sample of the highest-value rows so we can
sanity-check the names against what a brokerage scout would expect to see.
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config


def main() -> None:
    with sqlite3.connect(config.SQLITE_FILE) as con:
        snapshot = con.execute("SELECT MIN(snapshot_date) FROM player_universe").fetchone()[0]
        total = con.execute("SELECT COUNT(*) FROM player_universe").fetchone()[0]

        print(f"Player Universe — snapshot {snapshot}")
        print("=" * 78)
        rows = con.execute("""
            SELECT league,
                   COUNT(*) AS n,
                   ROUND(AVG(current_tm_value_eur) / 1e6, 1) AS avg_value_m,
                   ROUND(AVG(age), 1) AS avg_age
            FROM player_universe
            GROUP BY league_id, league
            ORDER BY n DESC
        """).fetchall()
        for league, n, avg_value_m, avg_age in rows:
            print(f"  {league:<24s} {n:>4d}    avg €{avg_value_m or 0:>4.1f}m    avg age {avg_age or 0:>4.1f}")
        print(f"  {'─' * 50}")
        print(f"  {'Total':<24s} {total:>4d}")
        print()

        print("Top 15 by current TM value")
        print("=" * 78)
        print(f"  {'€m':>4s}  {'Player':<24s} {'Age':>3s}  {'Position':<16s} {'Club':<26s} {'Ctr End':<11s} {'Mins':>5s}  Agent")
        print(f"  {'─' * 74}")
        sample = con.execute("""
            SELECT name, current_club, sub_position, age,
                   current_tm_value_eur / 1e6 AS mv_m,
                   contract_end_date,
                   ROUND(minutes_share_pct, 0) AS mins_pct,
                   agency
            FROM player_universe
            ORDER BY current_tm_value_eur DESC
            LIMIT 15
        """).fetchall()
        for r in sample:
            name, club, pos, age, mv, contract_end, mins, agent = r
            club_short = (club or "")[:26]
            name_short = (name or "")[:24]
            pos_short = (pos or "")[:16]
            agent_short = (agent or "—")[:25]
            print(f"  €{mv:>3.0f}  {name_short:<24s} {age:>3}  {pos_short:<16s} {club_short:<26s} {contract_end:<11s} {int(mins):>4}%  {agent_short}")
        print()

        print("Position breakdown")
        print("=" * 78)
        bp = con.execute("""
            SELECT COALESCE(primary_position, '(unknown)') AS pos, COUNT(*) AS n
            FROM player_universe
            GROUP BY pos ORDER BY n DESC
        """).fetchall()
        for pos, n in bp:
            print(f"  {pos:<20s} {n:>4d}")
        print()

        print("Club_pressure stub")
        print("=" * 78)
        cp_total = con.execute("SELECT COUNT(*) FROM club_pressure").fetchone()[0]
        cp_by_league = con.execute("""
            SELECT league, COUNT(*) AS n
            FROM club_pressure
            GROUP BY league_id, league
            ORDER BY n DESC
        """).fetchall()
        for league, n in cp_by_league:
            print(f"  {league:<24s} {n:>4d}")
        print(f"  {'─' * 50}")
        print(f"  {'Total clubs':<24s} {cp_total:>4d}    (pressure scores all NULL until Day 3)")


if __name__ == "__main__":
    main()
