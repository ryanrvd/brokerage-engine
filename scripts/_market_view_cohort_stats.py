"""Compute cohort statistics for Market View match scoring.

Two outputs:
  cohort_stats_position           — per position: median CA + std CA + n
  cohort_stats_valuation_benchmark — per (position × CA band): median predicted_fee + n

Cohort definition (per docs/market_view_match_formula.md §Cohort definition):
    cohort_member = (player.sellability_score > 50)
                  OR (player flagged "available" in market_movement_maps)

TODO(maps Stage 2): the "available" flag in market maps doesn't exist yet — the
spec defers it to maps Stage 2. Today the cohort is implemented as
sellability_score > 50 only. When the availability flag lands in maps, extend
the cohort to UNION players flagged available there.

Position buckets (10): GK, CB, RB, LB, DM, CM, AM, LW, RW, ST_CF.
CA bands: 60-70 / 70-80 / 80-90 / 90-100 / 100-110 / 110-120 / 120-130 / 130+.
Cells with <5 players → store NULL benchmark (Market View falls back to 1.0).

Pre-match-engine invocation: 22_match_engine.py reads these tables to compute
the scarcity_term and valuation_term components.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import statistics
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
import config

POSITION_BUCKETS = ["GK", "CB", "RB", "LB", "DM", "CM", "AM", "LW", "RW", "ST_CF"]
CA_BANDS = [
    ("60-70",   60.0,   70.0),
    ("70-80",   70.0,   80.0),
    ("80-90",   80.0,   90.0),
    ("90-100",  90.0,  100.0),
    ("100-110",100.0,  110.0),
    ("110-120",110.0,  120.0),
    ("120-130",120.0,  130.0),
    ("130+",   130.0,  9999.0),
]
MIN_CELL_N = 5  # min players per (position, ca_band) to compute a benchmark


def _ensure_tables(con: sqlite3.Connection) -> None:
    con.execute("""
        CREATE TABLE IF NOT EXISTS cohort_stats_position (
            position_bucket TEXT PRIMARY KEY,
            n_players       INTEGER,
            median_ca       REAL,
            std_ca          REAL,
            computed_at     TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS cohort_stats_valuation_benchmark (
            position_bucket    TEXT,
            ca_band            TEXT,
            n_players          INTEGER,
            median_predicted_fee REAL,
            computed_at        TEXT,
            PRIMARY KEY (position_bucket, ca_band)
        )
    """)


def _load_cohort(con: sqlite3.Connection) -> list[dict]:
    """Return cohort members: every player with sellability_score > 50 and
    a SciSports CA from player_ratings."""
    rows = con.execute("""
        SELECT pu.player_id, pu.position_bucket, pu.current_tm_value_eur,
               pu.sellability_score, pr.current_ability
        FROM player_universe pu
        JOIN player_ratings pr ON pr.tm_player_id = pu.player_id
        WHERE pu.sellability_score > 50
          AND pr.current_ability IS NOT NULL
          AND pu.position_bucket IS NOT NULL
    """).fetchall()
    return [
        {"pid": r[0], "pos": r[1], "tm_value": r[2],
         "sellability": r[3], "ca": float(r[4])}
        for r in rows
    ]


def _ca_band_for(ca: float) -> str | None:
    for label, lo, hi in CA_BANDS:
        if lo <= ca < hi:
            return label
    return None


def main() -> None:
    today_iso = dt.date.today().isoformat()
    con = sqlite3.connect(config.SQLITE_FILE)
    _ensure_tables(con)

    cohort = _load_cohort(con)
    print(f"Cohort (sellability > 50, CA not null): {len(cohort)} players")
    print()

    # ── 1. Position stats ────────────────────────────────────────────────
    by_pos: dict[str, list[float]] = {p: [] for p in POSITION_BUCKETS}
    for c in cohort:
        if c["pos"] in by_pos:
            by_pos[c["pos"]].append(c["ca"])

    print("Per-position cohort stats:")
    print(f"  {'position':10s} {'n':>5s} {'median':>8s} {'std':>7s}")
    print(f"  {'-'*10} {'-'*5} {'-'*8} {'-'*7}")
    con.execute("DELETE FROM cohort_stats_position")
    pos_rows = []
    for pos in POSITION_BUCKETS:
        cas = by_pos[pos]
        if len(cas) < 2:
            print(f"  {pos:10s} {len(cas):>5d}    (insufficient)")
            con.execute("""
                INSERT INTO cohort_stats_position
                  (position_bucket, n_players, median_ca, std_ca, computed_at)
                VALUES (?, ?, NULL, NULL, ?)
            """, (pos, len(cas), today_iso))
            pos_rows.append((pos, len(cas), None, None))
            continue
        median = statistics.median(cas)
        std = statistics.stdev(cas) if len(cas) >= 2 else 0.0
        con.execute("""
            INSERT INTO cohort_stats_position
              (position_bucket, n_players, median_ca, std_ca, computed_at)
            VALUES (?, ?, ?, ?, ?)
        """, (pos, len(cas), median, std, today_iso))
        pos_rows.append((pos, len(cas), median, std))
        print(f"  {pos:10s} {len(cas):>5d} {median:>8.1f} {std:>7.2f}")

    # ── 2. Valuation benchmarks per (position × CA band) ──────────────────
    print()
    print("Per (position × CA band) valuation benchmarks:")
    con.execute("DELETE FROM cohort_stats_valuation_benchmark")
    # Build cell map
    cells: dict[tuple[str, str], list[float]] = {}
    for c in cohort:
        if c["pos"] not in POSITION_BUCKETS:
            continue
        band = _ca_band_for(c["ca"])
        if band is None:
            continue
        if not c["tm_value"]:
            continue
        # predicted_fee
        pred = c["tm_value"] * config.tm_to_fee_multiplier(c["tm_value"])
        cells.setdefault((c["pos"], band), []).append(pred)

    n_with_data = 0
    n_null = 0
    print(f"  {'position':6s} {'band':9s} {'n':>4s} {'median_fee':>12s}")
    print(f"  {'-'*6} {'-'*9} {'-'*4} {'-'*12}")
    for pos in POSITION_BUCKETS:
        for band, _, _ in CA_BANDS:
            fees = cells.get((pos, band), [])
            if len(fees) >= MIN_CELL_N:
                median_fee = statistics.median(fees)
                con.execute("""
                    INSERT INTO cohort_stats_valuation_benchmark
                      (position_bucket, ca_band, n_players, median_predicted_fee, computed_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (pos, band, len(fees), median_fee, today_iso))
                n_with_data += 1
                if len(fees) >= 5:
                    print(f"  {pos:6s} {band:9s} {len(fees):>4d} €{median_fee/1e6:>10.1f}m")
            else:
                con.execute("""
                    INSERT INTO cohort_stats_valuation_benchmark
                      (position_bucket, ca_band, n_players, median_predicted_fee, computed_at)
                    VALUES (?, ?, ?, NULL, ?)
                """, (pos, band, len(fees), today_iso))
                n_null += 1

    con.commit()
    con.close()
    print()
    print(f"Cells with benchmark (n ≥ {MIN_CELL_N}): {n_with_data}")
    print(f"Cells with NULL benchmark:        {n_null}")


if __name__ == "__main__":
    main()
