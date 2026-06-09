"""
Step 09 — Compute sellability_score + sellability_status for every player.

sellability_score (0-100) — numeric score, computed for EVERY player in
player_universe regardless of whether they pass the original inclusion filters.

sellability_status — categorical tag:
  sellable_now:          passes all current rules (preserves prior cohort)
  sellable_with_caveat:  passes core profile but fails 1-2 rules
  not_sellable:          fails 3+ core rules
  out_of_scope:          Kill List / agency-blocked / retired / deceased
  imminent_fa:           registered contract ends ≤ 180 days from snapshot
                         (Bosman pre-contract window). Sellability forced to 0;
                         excluded from matches; surfaced in Club View's
                         "Imminent Free Agents" panel.

The numeric score uses the additive formula (Day 5). The status tag is new
(Day 8) — it replaces the implicit filter with explicit classification so
Market View can surface all players while the matcher defaults to sellable_now.

Formula (Day 5 additive + 2026-06-04 age multiplier):
    finished_product_value = 1.0 (true) | 0.5 (NULL/unknown) | 0.0 (false)
    player_quality = (right_priced + contract_leveraged + finished_product_value) / 3
    additive    = player_quality * 50 + (parent_club.total_pressure_score / 100) * 50
    floor       = contract_leveraged * finished_product_value * 50
    loan_bonus  = 15 if (on_loan == 1 AND finished_product == 1) else 0
    raw         = max(additive, floor) + loan_bonus
    sellability = min(100, raw * age_multiplier(age))

age_multiplier bands (≤25 → 1.00, 26-29 → 0.85, 30-32 → 0.60, 33+ → 0.35).
NULL age → 1.0 (neutral). Prevents imminent-free-agent senior players
(33+ with expiring contracts) from dominating sellability rankings purely
on the strength of contract_leveraged + parent club pressure. Bands match
market_match_score's age_multiplier in docs/market_view_match_formula.md
for consistency across both scoring layers.

Also: assigns position_bucket and computes top_3_likely_to_move per PARENT club.
"""

import sqlite3
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import config
import kill_list
from _position_buckets import bucket_for

LOAN_BONUS = 15.0
SCORE_CAP = 100.0

# Bosman pre-contract window. A player whose registered contract ends within
# this many days of the snapshot can sign a pre-contract with any club and
# leave on a free — they are NOT a fee-bearing brokerage opportunity and are
# removed from both the sellability ranking (score forced to 0, status
# 'imminent_fa') and the matches table (excluded by scripts/22_match_engine.py).
# Surfaced separately in Club View's "Imminent Free Agents" panel.
IMMINENT_FA_WINDOW_DAYS = 180


def _parse_iso(d) -> "date | None":
    if d is None:
        return None
    if isinstance(d, date):
        return d
    s = str(d)
    if not s or s.lower() == "none":
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def is_imminent_free_agent(contract_end, snapshot_date) -> bool:
    """True iff the player's registered contract ends within IMMINENT_FA_WINDOW_DAYS
    of the snapshot AND has not already expired."""
    end = _parse_iso(contract_end)
    snap = _parse_iso(snapshot_date)
    if end is None or snap is None:
        return False
    days_remaining = (end - snap).days
    return 0 < days_remaining <= IMMINENT_FA_WINDOW_DAYS

# Age multiplier — mirrors market_match_score's bands (docs/market_view_match_formula.md).
# Dampens senior-cohort sellability so 33+ Bosman edge cases don't dominate the
# top of the rankings on the strength of contract_leveraged + parent pressure alone.
AGE_MULTIPLIER_BANDS = {
    "≤25":    1.00,
    "26-29":  0.85,
    "30-32":  0.60,
    "33+":    0.35,
}


def age_multiplier(age: int | None) -> float:
    if age is None:
        return 1.0
    if age <= 25:
        return 1.0
    if age <= 29:
        return 0.85
    if age <= 32:
        return 0.60
    return 0.35


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
    parent_pressure: float | None,
    is_killed: bool,
    contract_cutoff_str: str,
    brokerage_eligible: int = 1,
) -> str:
    if is_killed:
        return "out_of_scope"

    # Phase A.8.7: sellable_now requires brokerage_eligible = 1. The universe
    # now includes every senior player at every covered club; the brokerage
    # cohort substrate (age/value/minutes/contract gates) is enforced here.
    if brokerage_eligible != 1:
        # Fall through to the rule-evaluator below; everyone outside the
        # brokerage substrate ends up sellable_with_caveat / not_sellable.
        pass
    # Players from the original filtered pipeline (dcaribou/tm_scrape) — when
    # brokerage_eligible — preserve sellable_now as before.
    elif data_source in ("dcaribou", "tm_scrape"):
        rp = bool(right_priced) if right_priced is not None else False
        fp = (finished_product == 1 or finished_product is None)
        cl = bool(contract_leveraged) if contract_leveraged is not None else False
        if rp or fp or cl:
            return "sellable_now"
        return "sellable_with_caveat"

    # For PL squad expansion players (tm_squad_scrape): evaluate each rule.
    rules: dict[str, bool | None] = {}

    if age is not None:
        rules["age"] = config.AGE_MIN <= age <= config.AGE_MAX
    else:
        rules["age"] = None

    if tm_value is not None and tm_value > 0:
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


def compute_brokerage_eligible(age, tm_value, contract_end, minutes_share_pct,
                                league_id, contract_cutoff_str) -> int:
    """Phase A.8.7: brokerage cohort substrate. The universe now includes every
    senior player; brokerage_eligible flags the subset that still meets the
    original Day-2 brokerage filters (age 17-24, TM €8-45m, minutes ≥ 50%,
    contract ≤ 2029-06-30 EOS). Brokerage Engine surfaces filter on this.

    Returns 1 if the player meets all four criteria, else 0.
    NULL on any check → fails (be strict — don't accidentally include
    insufficiently-characterised rows).
    """
    if age is None or not (config.AGE_MIN <= age <= config.AGE_MAX):
        return 0
    if tm_value is None or not (config.TM_VALUE_MIN_EUR <= tm_value <= config.TM_VALUE_MAX_EUR):
        return 0
    if contract_end is None or str(contract_end) > contract_cutoff_str:
        return 0
    if league_id in config.RELAXED_MINUTES_IDS:
        pass  # minutes filter relaxed
    else:
        if minutes_share_pct is None or minutes_share_pct < (config.MIN_MINUTES_SHARE * 100):
            return 0
    return 1


def main() -> None:
    snapshot = config.SNAPSHOT_DATE
    contract_cutoff = config.end_of_season_plus(snapshot, config.CONTRACT_MAX_YEARS_AHEAD)
    contract_cutoff_str = str(contract_cutoff)

    with sqlite3.connect(config.SQLITE_FILE) as con:
        try:
            kl_state = kill_list.compute_kill_list_state(con)
            killed_ids = kl_state["excluded_ids"]
        except Exception:
            killed_ids = set()

        # 1. position_bucket on all players.
        rows = con.execute("SELECT player_id, sub_position FROM player_universe").fetchall()
        for pid, sp in rows:
            con.execute(
                "UPDATE player_universe SET position_bucket = ? WHERE player_id = ?",
                (bucket_for(sp), pid),
            )

        # 2. Score + tag every player.
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
        ifa_updates = []  # (is_imminent_free_agent 0/1, player_id)
        brokerage_updates = []  # (brokerage_eligible 0/1, player_id)
        for (pid, right_priced, contract_leveraged, finished_product, on_loan,
             parent_pressure, age, tm_value, contract_end, minutes_share,
             league_id, data_source) in players:

            # Brokerage cohort substrate flag (Phase A.8.7).
            brokerage_eligible = compute_brokerage_eligible(
                age, tm_value, contract_end, minutes_share,
                league_id, contract_cutoff_str,
            )
            brokerage_updates.append((brokerage_eligible, pid))

            # Imminent Free Agent check — overrides sellability + status.
            ifa = is_imminent_free_agent(contract_end, snapshot)
            ifa_updates.append((1 if ifa else 0, pid))

            if ifa:
                score_updates.append((0.0, pid))
                status_updates.append(("imminent_fa", pid))
                continue

            rp = int(right_priced or 0)
            cl = int(contract_leveraged or 0)
            fpv = finished_product_value(finished_product)
            quality = (rp + cl + fpv) / 3.0
            parent_p = float(parent_pressure or 0.0)
            additive = quality * 50.0 + (parent_p / 100.0) * 50.0
            floor = cl * fpv * 50.0
            bonus = LOAN_BONUS if (on_loan == 1 and finished_product == 1) else 0.0
            raw = max(additive, floor) + bonus
            score = min(SCORE_CAP, raw * age_multiplier(age))
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
                parent_pressure=parent_pressure,
                is_killed=(pid in killed_ids),
                contract_cutoff_str=contract_cutoff_str,
                brokerage_eligible=brokerage_eligible,
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
        con.executemany(
            "UPDATE player_universe SET is_imminent_free_agent = ? WHERE player_id = ?",
            ifa_updates,
        )
        con.executemany(
            "UPDATE player_universe SET brokerage_eligible = ? WHERE player_id = ?",
            brokerage_updates,
        )
        n_ifa = sum(v for v, _ in ifa_updates)
        n_brok = sum(v for v, _ in brokerage_updates)
        print(f"Imminent free agents flagged: {n_ifa} (contract end ≤ {IMMINENT_FA_WINDOW_DAYS} days from snapshot)")
        print(f"Brokerage-eligible flagged:   {n_brok} (passes age/value/minutes/contract filters)")

        # 3. top_3_likely_to_move per PARENT club — sellable_now only.
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

        # 4. Distribution summary.
        status_counts = dict(con.execute(
            "SELECT sellability_status, COUNT(*) FROM player_universe GROUP BY sellability_status"
        ).fetchall())
        n_total = con.execute("SELECT COUNT(*) FROM player_universe").fetchone()[0]
        n_loan = con.execute("SELECT COUNT(*) FROM player_universe WHERE on_loan = 1").fetchone()[0]

        print(f"Sellability tagging complete for {n_total} players. {n_loan} on loan.")
        print()
        print("sellability_status distribution:")
        for status in ("sellable_now", "sellable_with_caveat", "not_sellable", "out_of_scope"):
            label = ""
            if status == "sellable_now":
                label = " (should ≈ 124 — preserves prior cohort)"
            print(f"  {status:25s} {status_counts.get(status, 0):>5}{label}")
        if None in status_counts:
            print(f"  {'(NULL)':25s} {status_counts[None]:>5}")


if __name__ == "__main__":
    main()
