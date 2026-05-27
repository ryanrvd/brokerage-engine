"""
Step 09 — Compute sellability_score + sellability_status per player.

sellability_score (0-100) — numeric score, computed for EVERY player in
player_universe regardless of whether they pass the original inclusion filters.

sellability_status — categorical tag:
  sellable_now:          passes all original inclusion rules
  sellable_with_caveat:  passes most rules (1-2 soft failures or unknowns)
  not_sellable:          fails 3+ core rules
  out_of_scope:          Kill List / agency-blocked

The numeric score uses the same additive formula as before (Day 5). The status
tag is new (Day 8) — it replaces the old implicit filter with an explicit
classification so the Market View can surface all players while the matcher
defaults to sellable_now to preserve existing behaviour.

Formula (Day 5 — additive rebalance):
    finished_product_value = 1.0 (true) | 0.5 (NULL/unknown) | 0.0 (false)
    player_quality = (right_priced + contract_leveraged + finished_product_value) / 3
    additive   = player_quality * 50 + (parent_club.total_pressure_score / 100) * 50
    floor      = contract_leveraged * finished_product_value * 50
    loan_bonus = 15 if (on_loan == 1 AND finished_product == 1) else 0
    sellability = min(100, max(additive, floor) + loan_bonus)

Also: assigns position_bucket to player_universe and computes
top_3_likely_to_move per PARENT club.
"""

import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import config
import kill_list
from _position_buckets import bucket_for

LOAN_BONUS = 15.0
SCORE_CAP = 100.0


def finished_product_value(flag: int | None) -> float:
    if flag is None:
        return 0.5
    return 1.0 if flag else 0.0


def compute_sellability_status(
    age: int | None,
    tm_value: int | None,
    contract_end: str | None,
    minutes_share_pct: float | None,
    right_priced: int | None,
    finished_product: int | None,
    contract_leveraged: int | None,
    league_id: str | None,
    data_source: str | None,
    is_killed: bool,
    contract_cutoff_str: str,
) -> str:
    if is_killed:
        return "out_of_scope"

    # Players from the filtered pipeline (dcaribou/tm_scrape) already passed
    # all inclusion filters at ingestion time. Classify them as sellable_now
    # if they have at least one of the three sellability flags — this exactly
    # preserves the prior cohort.
    if data_source in ("dcaribou", "tm_scrape"):
        rp = bool(right_priced) if right_priced is not None else False
        fp = (finished_product == 1 or finished_product is None)
        cl = bool(contract_leveraged) if contract_leveraged is not None else False
        if rp or fp or cl:
            return "sellable_now"
        return "sellable_with_caveat"

    # For expansion players (pl_squad_full): evaluate each inclusion rule.
    rules: dict[str, bool | None] = {}

    if age is not None:
        rules["age"] = config.AGE_MIN <= age <= config.AGE_MAX
    else:
        rules["age"] = None

    if tm_value is not None:
        rules["value"] = config.TM_VALUE_MIN_EUR <= tm_value <= config.TM_VALUE_MAX_EUR
    else:
        rules["value"] = None

    if contract_end:
        rules["contract"] = contract_end <= contract_cutoff_str
    else:
        rules["contract"] = None

    if league_id in config.RELAXED_MINUTES_IDS:
        rules["minutes"] = True
    elif minutes_share_pct is not None:
        rules["minutes"] = minutes_share_pct >= (config.MIN_MINUTES_SHARE * 100)
    else:
        rules["minutes"] = None

    if right_priced is not None:
        rules["right_priced"] = bool(right_priced)
    else:
        rules["right_priced"] = None

    passes = sum(1 for v in rules.values() if v is True)
    fails = sum(1 for v in rules.values() if v is False)

    if fails == 0 and passes == len(rules):
        return "sellable_now"
    elif fails <= 2:
        return "sellable_with_caveat"
    else:
        return "not_sellable"


def main() -> None:
    snapshot = config.SNAPSHOT_DATE
    contract_cutoff = config.end_of_season_plus(snapshot, config.CONTRACT_MAX_YEARS_AHEAD)
    contract_cutoff_str = str(contract_cutoff)

    with sqlite3.connect(config.SQLITE_FILE) as con:
        # 0. Get Kill List IDs for status classification.
        try:
            kl_state = kill_list.compute_kill_list_state(con)
            killed_ids = kl_state["excluded_ids"]
        except Exception:
            killed_ids = set()

        # 1. position_bucket on player_universe (all players).
        rows = con.execute("SELECT player_id, sub_position FROM player_universe").fetchall()
        for pid, sp in rows:
            con.execute(
                "UPDATE player_universe SET position_bucket = ? WHERE player_id = ?",
                (bucket_for(sp), pid),
            )

        # 2. Compute sellability_score + sellability_status per player.
        players = con.execute("""
            SELECT pu.player_id, pu.right_priced, pu.contract_leveraged, pu.finished_product,
                   pu.on_loan, cp.total_pressure_score,
                   pu.age, pu.current_tm_value_eur, pu.contract_end_date,
                   pu.minutes_share_pct, pu.league_id, pu.data_source
            FROM player_universe pu
            LEFT JOIN club_pressure cp ON cp.club_id = pu.parent_club_id
        """).fetchall()

        score_updates = []
        status_updates = []
        for (pid, right_priced, contract_leveraged, finished_product, on_loan,
             parent_pressure, age, tm_value, contract_end, minutes_share,
             league_id, data_source) in players:
            rp = int(right_priced or 0)
            cl = int(contract_leveraged or 0)
            fpv = finished_product_value(finished_product)
            quality = (rp + cl + fpv) / 3.0
            parent_p = float(parent_pressure or 0.0)
            additive = quality * 50.0 + (parent_p / 100.0) * 50.0
            floor = cl * fpv * 50.0
            bonus = LOAN_BONUS if (on_loan == 1 and finished_product == 1) else 0.0
            score = min(SCORE_CAP, max(additive, floor) + bonus)
            score_updates.append((round(score, 2), pid))

            status = compute_sellability_status(
                age=age,
                tm_value=tm_value,
                contract_end=contract_end,
                minutes_share_pct=minutes_share,
                right_priced=right_priced,
                finished_product=finished_product,
                contract_leveraged=contract_leveraged,
                league_id=league_id,
                data_source=data_source,
                is_killed=(pid in killed_ids),
                contract_cutoff_str=contract_cutoff_str,
            )
            status_updates.append((status, pid))

        con.executemany(
            "UPDATE player_universe SET sellability_score = ? WHERE player_id = ?",
            score_updates,
        )
        con.executemany(
            "UPDATE player_universe SET sellability_status = ? WHERE player_id = ?",
            status_updates,
        )

        # 3. Compute top_3_likely_to_move per PARENT club.
        #    Only consider sellable_now players for the top-3 list.
        top_rows = con.execute("""
            SELECT parent_club_id, name, sellability_score
            FROM player_universe
            WHERE parent_club_id IS NOT NULL AND sellability_score IS NOT NULL
              AND sellability_status = 'sellable_now'
            ORDER BY parent_club_id, sellability_score DESC, name
        """).fetchall()
        per_club: dict[str, list[str]] = defaultdict(list)
        for cid, name, _ in top_rows:
            if len(per_club[cid]) < 3:
                per_club[cid].append(name)
        con.execute("UPDATE club_pressure SET top_3_likely_to_move = NULL")
        for cid, names in per_club.items():
            con.execute(
                "UPDATE club_pressure SET top_3_likely_to_move = ? WHERE club_id = ?",
                ("; ".join(names), cid),
            )
        con.commit()

        # 4. Print summary.
        status_counts = dict(con.execute(
            "SELECT sellability_status, COUNT(*) FROM player_universe GROUP BY sellability_status"
        ).fetchall())
        n_total = con.execute("SELECT COUNT(*) FROM player_universe").fetchone()[0]
        n_loan = con.execute("SELECT COUNT(*) FROM player_universe WHERE on_loan = 1").fetchone()[0]

        print(f"Sellability computed for {n_total} players. {n_loan} on loan.")
        print()
        print("sellability_status distribution:")
        for status in ("sellable_now", "sellable_with_caveat", "not_sellable", "out_of_scope"):
            print(f"  {status:25s} {status_counts.get(status, 0):>5}")
        if None in status_counts:
            print(f"  {'(NULL)':25s} {status_counts[None]:>5}")

        print()
        print("Top 20 players by sellability_score:")
        print(f"  {'player':26s} {'current/parent club':36s} {'lg':5s} "
              f"{'rp':>3s} {'lev':>3s} {'fin':>4s} {'loan':>4s} {'pr':>5s} {'sell':>6s} {'status':>20s}")
        rows = con.execute("""
            SELECT pu.name, pu.current_club, pu.parent_club, pu.on_loan, pu.league_id,
                   pu.right_priced, pu.contract_leveraged, pu.finished_product,
                   cp.total_pressure_score, pu.sellability_score, pu.sellability_status
            FROM player_universe pu
            LEFT JOIN club_pressure cp ON cp.club_id = pu.parent_club_id
            ORDER BY pu.sellability_score DESC NULLS LAST, pu.name
            LIMIT 20
        """).fetchall()
        for r in rows:
            fp = "T" if r[7] == 1 else ("F" if r[7] == 0 else "?")
            on_loan = r[3]
            club_display = (
                f"{(r[1] or '')[:16]} ← {(r[2] or '')[:14]}"
                if on_loan else (r[1] or "")
            )
            print(f"  {(r[0] or '')[:26]:26s} {club_display[:36]:36s} {r[4] or '':5s} "
                  f"{r[5] or 0:>3d} {r[6] or 0:>3d} {fp:>4s} {('Y' if on_loan else ''):>4s} "
                  f"{r[8] or 0:>5.1f} {r[9] or 0:>6.2f} {(r[10] or ''):>20s}")


if __name__ == "__main__":
    main()
