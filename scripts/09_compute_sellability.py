"""
Step 09 — Compute sellability_score per Player Universe player.

Formula (Day 5 — additive rebalance after named-list diagnostic):
    finished_product_value = 1.0 (true) | 0.5 (NULL/unknown) | 0.0 (false)
    player_quality = (right_priced + contract_leveraged + finished_product_value) / 3

    additive   = player_quality * 50 + (parent_club.total_pressure_score / 100) * 50
    floor      = contract_leveraged * finished_product_value * 50    # Bosman safety net
    loan_bonus = LOAN_BONUS  if (on_loan == 1 AND finished_product == 1) else 0
    sellability = min(100, max(additive, floor) + loan_bonus)

Why additive (replaces multiplicative branch, Day 3.6 → Day 5):
The earlier formula was `quality × (pressure/100) × 100`, making pressure a
multiplier on quality. That collapsed sellability for high-quality players at
moderate-pressure clubs — Tzolis at Brugge (quality 0.67, pressure 32) scored
only 21, far below the brokerage threshold, despite being an obvious sale
candidate. Football's transfer market doesn't gate on contract-leveraged
status; a good bid is enough to move a player whose owner needs cash or wants
a sale. The additive form gives quality and pressure each up to 50 points,
honestly reflecting that both axes contribute independently.

Three player-side flags still weighted equally inside `quality` — maps cleanly
to the brief's three player criteria (right price, not over-locked-in,
finished product).

Loan handling (Day 3.5): join uses parent_club_id (legal owner). For loaned
players pressure is re-attributed to the parent. Parents outside our 19-league
coverage (Shakhtar Donetsk, Bayern Munich II) have NULL pressure → additive
branch returns just `quality × 50`; floor + loan_bonus still apply.

Floor logic: a contract-leveraged player is sellable independent of parent
pressure (Bosman risk). Floor modulates by finished_product_value so a
reserve-team player in their final year doesn't over-score. right_priced does
NOT feed the floor — the Bosman scenario is about contract running out, not
about loss-aversion on past fees. Raised from 20 → 50 to scale with the new
additive shape.

Loan bonus: +15 if on_loan=1 AND finished_product=1, capturing the
"loaned-and-showcasing" structural sell pathway.

Also: assigns position_bucket to player_universe and computes
top_3_likely_to_move per PARENT club.
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import config
from _position_buckets import bucket_for

LOAN_BONUS = 15.0
SCORE_CAP = 100.0


def finished_product_value(flag: int | None) -> float:
    if flag is None:
        return 0.5
    return 1.0 if flag else 0.0


def main() -> None:
    with sqlite3.connect(config.SQLITE_FILE) as con:
        # 1. position_bucket on player_universe.
        rows = con.execute("SELECT player_id, sub_position FROM player_universe").fetchall()
        for pid, sp in rows:
            con.execute(
                "UPDATE player_universe SET position_bucket = ? WHERE player_id = ?",
                (bucket_for(sp), pid),
            )

        # 2. Compute sellability per player — join on parent_club_id (Day 3.5).
        players = con.execute("""
            SELECT pu.player_id, pu.right_priced, pu.contract_leveraged, pu.finished_product,
                   pu.on_loan, cp.total_pressure_score
            FROM player_universe pu
            LEFT JOIN club_pressure cp ON cp.club_id = pu.parent_club_id
        """).fetchall()

        updates = []
        for pid, right_priced, contract_leveraged, finished_product, on_loan, parent_pressure in players:
            rp = int(right_priced or 0)
            cl = int(contract_leveraged or 0)
            fpv = finished_product_value(finished_product)
            quality = (rp + cl + fpv) / 3.0
            parent_p = float(parent_pressure or 0.0)
            # Additive: quality and pressure each contribute up to 50.
            additive = quality * 50.0 + (parent_p / 100.0) * 50.0
            # Bosman safety net — raised from 20 to 50 to scale with the new shape.
            floor = cl * fpv * 50.0  # right_priced intentionally excluded — see module docstring
            bonus = LOAN_BONUS if (on_loan == 1 and finished_product == 1) else 0.0
            score = min(SCORE_CAP, max(additive, floor) + bonus)
            updates.append((round(score, 2), pid))
        con.executemany(
            "UPDATE player_universe SET sellability_score = ? WHERE player_id = ?",
            updates,
        )

        # 3. Compute top_3_likely_to_move per PARENT club, write to club_pressure.
        top_rows = con.execute("""
            SELECT parent_club_id, name, sellability_score
            FROM player_universe
            WHERE parent_club_id IS NOT NULL AND sellability_score IS NOT NULL
            ORDER BY parent_club_id, sellability_score DESC, name
        """).fetchall()
        from collections import defaultdict
        per_club: dict[str, list[str]] = defaultdict(list)
        for cid, name, _ in top_rows:
            if len(per_club[cid]) < 3:
                per_club[cid].append(name)
        # Reset the column before re-populating so stale top-3 lists from Day 3 vanish.
        con.execute("UPDATE club_pressure SET top_3_likely_to_move = NULL")
        for cid, names in per_club.items():
            con.execute(
                "UPDATE club_pressure SET top_3_likely_to_move = ? WHERE club_id = ?",
                ("; ".join(names), cid),
            )
        con.commit()

        # 4. Print top 20.
        print("Top 20 players by sellability_score:")
        print(f"  {'player':26s} {'current/parent club':36s} {'lg':5s} "
              f"{'rp':>3s} {'lev':>3s} {'fin':>4s} {'loan':>4s} {'pr':>5s} {'sell':>6s}")
        rows = con.execute("""
            SELECT pu.name, pu.current_club, pu.parent_club, pu.on_loan, pu.league_id,
                   pu.right_priced, pu.contract_leveraged, pu.finished_product,
                   cp.total_pressure_score, pu.sellability_score
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
                  f"{r[8] or 0:>5.1f} {r[9] or 0:>6.2f}")

        n = con.execute(
            "SELECT COUNT(*) FROM player_universe WHERE sellability_score IS NOT NULL"
        ).fetchone()[0]
        n_loan = con.execute(
            "SELECT COUNT(*) FROM player_universe WHERE on_loan = 1"
        ).fetchone()[0]
        print(f"\n{n} players scored. {n_loan} on loan.")


if __name__ == "__main__":
    main()
