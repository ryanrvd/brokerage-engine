"""
Step 21 — Build inferred_club_requests for the 9 leagues with no explicit
buyer-side mapping (TR1, DK1, SC1, GR1, SA1, MLS1, ES2, IT2, L2) PLUS unmapped
clubs in the 10 demand-mapped leagues.

The matcher previously found buyer candidates only in `map_club_requests` (the
8 manual Google Sheets workbooks), so 80 of 354 clubs surfaced as buyers and
9 of 19 leagues were buyer-invisible. This script generates synthetic demand
from each club's `senior_roster` thinness — the inverse of the squad-oversupply
logic used in Seller Pressure.

Inferred-demand semantics:
  • source     = "Inferred"
  • validated  = "AUTO"
  • These get demand_intensity = 0.40 in script 22 (below explicit-source values).

Idempotent. Drops and rebuilds `inferred_club_requests` on every run.
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

# "Thin" thresholds — a club is thin in bucket B if its active headcount is
# ≤ THIN_THRESHOLDS[B]. Inverse of the oversupply rules in script 08, set
# conservatively from the empirical headcount distribution across 354 clubs.
# DM and AM skipped because their headcount varies by formation (a 4-4-2 has 0
# AMs by design); using a threshold here would over-fire.
THIN_THRESHOLDS: dict[str, int] = {
    "GK":    1,
    "CB":    2,
    "LB":    1,
    "RB":    1,
    "CM":    2,
    "LW":    1,
    "RW":    1,
    "ST_CF": 2,
}

# League-tier default max_transfer_fee_eur for UNMAPPED clubs (no
# map_club_overview row). Mapped clubs use their actual
# highest_transfer_fee_2526_eur — so PSV/Ajax/Benfica/Sporting/Porto/Braga get
# their real spending power, while the bulk of NL/POR/BEL register at €0.5-3m
# and self-filter via budget_fit=0 + score-floor=10.
#
# Unmapped tier defaults are intentionally low: it's better to under-state
# spending power than to flood the matches table with affordability noise. The
# score floor catches over-defaults regardless.
TIER_DEFAULT_MAX_FEE_UNMAPPED: dict[int, int] = {
    1: 25_000_000,   # Tier A: GB1/ES1/IT1/L1/FR1 (all mapped — fallback unlikely)
    2:  5_000_000,   # Tier B unmapped: TR1, ES2, IT2, L2 — conservative
    3:  3_000_000,   # Tier C: DK1/SC1/GR1
    4: 15_000_000,   # Tier D: SA1/MLS1
}


def init_inferred_table(con: sqlite3.Connection) -> None:
    con.execute("DROP TABLE IF EXISTS inferred_club_requests")
    con.execute("""
        CREATE TABLE inferred_club_requests (
            request_id            INTEGER PRIMARY KEY AUTOINCREMENT,
            club_id               INTEGER NOT NULL,
            club_name             TEXT,
            league                TEXT,
            position_bucket       TEXT NOT NULL,
            preferred_side        TEXT,
            max_transfer_fee_eur  INTEGER,
            max_wage_pw_eur       INTEGER,
            source                TEXT,
            validated             TEXT,
            headcount_observed    INTEGER,
            thin_threshold        INTEGER,
            tier                  INTEGER,
            snapshot_date         TEXT
        )
    """)
    con.execute("CREATE INDEX idx_inferred_bucket ON inferred_club_requests(position_bucket)")


def build_inferred_demand(con: sqlite3.Connection) -> tuple[int, dict[str, int], dict[str, int]]:
    """Returns (rows_inserted, per_bucket_counts, budget_source_counts).
    budget_source_counts tracks how many rows used overview vs tier-default."""
    externs = ",".join(f"'{x}'" for x in config.EXTERNAL_LEAGUE_IDS)
    rows = con.execute(f"""
        SELECT cp.club_id, cp.name AS club_name, cp.league_id,
               sr.position_bucket, COUNT(*) AS headcount
        FROM senior_roster sr
        JOIN club_pressure cp ON cp.club_id = sr.club_id
        WHERE sr.position_bucket IS NOT NULL
          AND (sr.minutes_last_18m > 0 OR cp.league_id IN ({externs}))
        GROUP BY cp.club_id, sr.position_bucket
    """).fetchall()
    headcount_idx: dict[tuple[int, str], int] = {(r[0], r[3]): r[4] for r in rows}

    # Per-club budget signal from map_club_overview where available.
    overview_budget: dict[int, int] = {
        cid: mfee for cid, mfee in con.execute(
            "SELECT club_id, highest_transfer_fee_2526_eur FROM map_club_overview "
            "WHERE highest_transfer_fee_2526_eur IS NOT NULL"
        ).fetchall()
    }

    # Skip clubs/buckets that already have an explicit map_club_requests row —
    # don't generate redundant inferred demand on top of validated/intel signal.
    explicit_pairs: set[tuple[int, str]] = {
        (cid, b) for cid, b in con.execute(
            "SELECT DISTINCT club_id, position_bucket FROM map_club_requests "
            "WHERE club_id IS NOT NULL AND position_bucket IS NOT NULL"
        ).fetchall()
    }

    clubs = con.execute("SELECT club_id, name, league_id FROM club_pressure").fetchall()

    per_bucket_counts: dict[str, int] = {b: 0 for b in THIN_THRESHOLDS}
    budget_source_counts = {"overview": 0, "tier_default": 0}
    to_insert: list[tuple] = []
    snapshot = str(config.SNAPSHOT_DATE)

    # Phase A.8.7: every club must have at least minimal buyer presence in
    # either map_club_requests or inferred_club_requests. Track which clubs
    # have explicit demand at any bucket so that any clubs with zero presence
    # afterwards get filled with their two weakest-headcount buckets.
    clubs_with_explicit: set[str] = {cid for (cid, _b) in explicit_pairs}
    clubs_filled_via_thin: set[str] = set()

    for club_id, club_name, league_id in clubs:
        tier = config.LEAGUE_TIERS.get(league_id)
        if tier is None:
            continue
        max_fee = overview_budget.get(club_id)
        budget_source = "overview"
        if max_fee is None or max_fee == 0:
            max_fee = TIER_DEFAULT_MAX_FEE_UNMAPPED.get(tier, 5_000_000)
            budget_source = "tier_default"

        for bucket, threshold in THIN_THRESHOLDS.items():
            if (club_id, bucket) in explicit_pairs:
                continue
            hc = headcount_idx.get((club_id, bucket), 0)
            if hc <= threshold:
                to_insert.append((
                    club_id, club_name, league_id, bucket,
                    "Either", max_fee, None,
                    "Inferred", "AUTO",
                    hc, threshold, tier, snapshot,
                ))
                per_bucket_counts[bucket] += 1
                budget_source_counts[budget_source] += 1
                clubs_filled_via_thin.add(str(club_id))

    # Phase A.8.7 floor: every club must have at least minimal buyer presence.
    # For clubs with zero explicit map_club_requests AND zero thin-bucket
    # inferred requests, generate inferred requests for the two weakest
    # buckets (lowest headcount). This guarantees `neither` column = 0 in
    # the per-league coverage audit.
    n_zero_filled = 0
    for club_id, club_name, league_id in clubs:
        tier = config.LEAGUE_TIERS.get(league_id)
        if tier is None:
            continue
        cid_s = str(club_id)
        if cid_s in clubs_with_explicit or cid_s in clubs_filled_via_thin:
            continue
        max_fee = overview_budget.get(club_id)
        budget_source = "overview"
        if max_fee is None or max_fee == 0:
            max_fee = TIER_DEFAULT_MAX_FEE_UNMAPPED.get(tier, 5_000_000)
            budget_source = "tier_default"
        # Pick the two buckets with lowest headcount at this club.
        per_bucket = sorted(
            ((b, headcount_idx.get((club_id, b), 0)) for b in THIN_THRESHOLDS),
            key=lambda x: (x[1], x[0]),
        )
        for bucket, hc in per_bucket[:2]:
            to_insert.append((
                club_id, club_name, league_id, bucket,
                "Either", max_fee, None,
                "Inferred", "AUTO_MIN",
                hc, THIN_THRESHOLDS.get(bucket, 0), tier, snapshot,
            ))
            per_bucket_counts[bucket] += 1
            budget_source_counts[budget_source] += 1
        n_zero_filled += 1
    if n_zero_filled:
        print(f"[A.8.7 zero-presence floor] generated min-2-buckets for "
              f"{n_zero_filled} clubs that had no explicit + no thin demand")

    init_inferred_table(con)
    con.executemany("""
        INSERT INTO inferred_club_requests (
            club_id, club_name, league, position_bucket,
            preferred_side, max_transfer_fee_eur, max_wage_pw_eur,
            source, validated,
            headcount_observed, thin_threshold, tier, snapshot_date
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, to_insert)
    con.commit()
    return len(to_insert), per_bucket_counts, budget_source_counts


def main() -> None:
    if not Path(config.SQLITE_FILE).exists():
        sys.exit(f"Missing {config.SQLITE_FILE} — run 02→08 first.")

    with sqlite3.connect(config.SQLITE_FILE) as con:
        n, per_bucket, budget_src = build_inferred_demand(con)
        # Coverage stats
        n_explicit = con.execute("SELECT COUNT(*) FROM map_club_requests").fetchone()[0]
        n_clubs_inferred = con.execute("SELECT COUNT(DISTINCT club_id) FROM inferred_club_requests").fetchone()[0]
        n_leagues_inferred = con.execute("SELECT COUNT(DISTINCT league) FROM inferred_club_requests").fetchone()[0]
        n_clubs_total_buyer = con.execute("""
            SELECT COUNT(DISTINCT club_id) FROM (
                SELECT club_id FROM map_club_requests
                UNION
                SELECT club_id FROM inferred_club_requests
            )
        """).fetchone()[0]
        n_leagues_total_buyer = con.execute("""
            SELECT COUNT(DISTINCT lg) FROM (
                SELECT league lg FROM map_club_requests
                UNION
                SELECT league lg FROM inferred_club_requests
            )
        """).fetchone()[0]

    print(f"Wrote {n} inferred_club_requests rows across {n_clubs_inferred} clubs / {n_leagues_inferred} leagues.")
    print()
    print("By position bucket (firings):")
    for b in ("GK","CB","LB","RB","CM","LW","RW","ST_CF"):
        print(f"  {b:6s}  {per_bucket.get(b, 0):>4}")
    print()
    print("Budget proxy source:")
    print(f"  per-club (map_club_overview.highest_transfer_fee_2526_eur): {budget_src['overview']:>5}")
    print(f"  tier default (unmapped clubs):                              {budget_src['tier_default']:>5}")
    print()
    print("Demand-side coverage now:")
    print(f"  explicit map_club_requests:           {n_explicit:>5} rows")
    print(f"  inferred_club_requests:               {n:>5} rows")
    print(f"  total distinct buyer clubs:           {n_clubs_total_buyer:>5} (was 196)")
    print(f"  total distinct buyer leagues:         {n_leagues_total_buyer:>5} (was 10)")


if __name__ == "__main__":
    main()
