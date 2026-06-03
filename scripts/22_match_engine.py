"""
Step 22 — Match engine. Builds the (player, buyer_request) candidate set for
Sheet 1 (Brokerage Opportunities) by cross-referencing the Sellable Asset
Ledger (Sheet 4) against the Demand Map (map_club_requests).

Output: a `matches` table in db/yatin.db. One row per (player, buyer_request)
pair that survives all filters, scored 0..100. Every passing pair is retained
(no per-player cap) — Yatin sees the full candidate set per player and applies
his own judgement; the match_score provides the ranking.

Demand-side inputs (UNION):
  • map_club_requests       — explicit signal from 8 Google Sheets workbooks
  • inferred_club_requests  — synthetic signal from senior_roster thinness
                              (built by scripts/21_infer_demand.py)

Filters (in order — cheapest first):
  1. Position bucket match (player.position_bucket == request.position_bucket)
  2. Budget feasible (request.max_transfer_fee_eur >= player.current_tm_value_eur)
  3. League tier rule (lateral or upward only; Tier D self-contained)
  4. Side preference (only enforced for side-intrinsic buckets LB/RB/LW/RW)
  5. Score floor (match_score >= 10 — suppresses marginal long-tail matches)

Score (see CLAUDE.md → "Match scoring (Day 5)"):
  match_score = sellability/100 × demand_intensity × budget_fit × wage_feasibility

Where budget_fit uses a plateau curve: 0.80 once buyer can clearly afford the
indicative fee (2× the player's TM), linear ramp below. This removes PL
big-budget dominance — mid-budget buyers compete fairly on cheaper players.

Idempotent. Drops and recreates the `matches` table on every run.
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

# ─── Scoring constants ────────────────────────────────────────────────────────

# demand_intensity: (source, validated) → multiplier. See CLAUDE.md.
def demand_intensity(source: str | None, validated: str | None) -> float:
    src = (source or "").strip()
    val = (validated or "").strip().upper()
    if src == "Agent" and val == "YES":
        return 1.00
    if src == "Agent":  # Agent but other validated_by — e.g. a person-name leak
        return 0.85
    if src == "Intel" and val == "NO":
        return 0.60
    if src == "Inferred":  # synthetic demand from senior_roster thinness
        return 0.40
    return 0.50  # NULL / NULL fallback

# wage_feasibility per pair:
#   1.0   if player.wage ≤ buyer.max_wage_pw_eur  (confirmed feasible)
#   0.0   if player.wage > buyer.max_wage_pw_eur  (known infeasible)
#   0.7   otherwise (player or buyer wage missing — neutral fallback)
# Player wages come from data/manual_wages.xlsx (built by scripts/26_manual_wages_template.py).
WAGE_FEASIBILITY_UNKNOWN = 0.7
WAGE_FEASIBILITY_OK = 1.0
WAGE_FEASIBILITY_INFEASIBLE = 0.0

# Drop matches scoring below this threshold from the matches table. A pair can
# clear all four filters but still produce a marginal score (e.g. weak sellability,
# inferred demand, tight budget). The floor suppresses that long tail so Sheet 1
# isn't padded with noise. Set to 10/100 = 0.10 raw.
MATCH_SCORE_FLOOR = 10.0

# Side-intrinsic buckets — position carries side info. Others pass any preferred_side.
INTRINSIC_SIDE = {"LB": "Left", "RB": "Right", "LW": "Left", "RW": "Right"}

# No per-player cap. Every (player, buyer_request) pair that survives the four
# filters is retained — Yatin sees the full candidate set per player and applies
# his own judgement. The match_score gives the ranking; the workbook auto-filter
# lets him pick a player and see all their candidate buyers in score order.
TOP_N_PER_PLAYER: int | None = None


# ─── Schema ───────────────────────────────────────────────────────────────────

def init_matches_table(con: sqlite3.Connection) -> None:
    con.execute("DROP TABLE IF EXISTS matches")
    con.execute("""
        CREATE TABLE matches (
            match_id              INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id             INTEGER NOT NULL,
            buyer_request_id      INTEGER NOT NULL,
            player_name           TEXT,
            position_bucket       TEXT,
            player_tm_value_eur   INTEGER,
            sellability_score     REAL,
            buyer_club_id         INTEGER,
            buyer_club_name       TEXT,
            buyer_league_id       TEXT,
            max_transfer_fee_eur  INTEGER,
            max_wage_pw_eur       INTEGER,
            request_source        TEXT,
            request_validated     TEXT,
            preferred_side        TEXT,
            sellability_term      REAL,
            demand_intensity      REAL,
            budget_fit            REAL,
            wage_feasibility      REAL,
            match_score_raw       REAL,
            match_score           REAL,
            tier_move             TEXT,            -- 'upward' | 'lateral'
            wage_feasibility_label TEXT,           -- 'ok' | 'infeasible' | 'unknown'
            player_wage_pw_eur    INTEGER,         -- from manual_wages.xlsx if set
            -- Sci Sports CA/PA + level-fit (joined from player_ratings +
            -- map_club_overview at scoring time). Persisted so the UI can
            -- render without re-computing.
            player_ca             REAL,            -- player_ratings.current_ability
            player_pa             REAL,            -- player_ratings.potential_ability
            club_threshold_for_request REAL,       -- buyer's CA threshold for the requested level (today: first-team = sci_first_team_level)
            level_fit             TEXT,            -- 'ON_LEVEL' | 'UPSIDE' | 'BELOW' | 'UNRATED'
            level_fit_multiplier  REAL,            -- 1.20 / 1.05 / 0.85 / 1.00 — the multiplier applied to match_score
            UNIQUE(player_id, buyer_request_id)
        )
    """)
    con.execute("CREATE INDEX idx_matches_player ON matches(player_id)")
    con.execute("CREATE INDEX idx_matches_score ON matches(match_score DESC)")


# ─── Sci Sports level-fit ────────────────────────────────────────────────────
# Stage 1 derivation rule: every buyer request is assumed to be a FIRST-TEAM
# level requirement. `map_club_requests` doesn't carry a per-request level
# column today (only club-level thresholds exist on map_club_overview); the
# user's sellable cohort is the first-team prospect band by design, so this
# default is defensible and uniform. Stage 2 adds an explicit `level_required`
# column to the workbook — see BACKLOG.
LEVEL_FIT_MULTIPLIERS = {
    "ON_LEVEL": 1.20,
    "UPSIDE":   1.05,
    "BELOW":    0.85,
    "UNRATED":  1.00,
}


def compute_level_fit(player_ca: float | None,
                      player_pa: float | None,
                      threshold: float | None) -> tuple[str, float]:
    """Returns (level_fit_label, multiplier). Threshold = buyer club's CA
    threshold at the requested level (first-team default for now)."""
    if player_ca is None and player_pa is None:
        return "UNRATED", LEVEL_FIT_MULTIPLIERS["UNRATED"]
    if threshold is None:
        return "UNRATED", LEVEL_FIT_MULTIPLIERS["UNRATED"]
    if player_ca is not None and player_ca >= threshold:
        return "ON_LEVEL", LEVEL_FIT_MULTIPLIERS["ON_LEVEL"]
    if player_pa is not None and player_pa >= threshold:
        return "UPSIDE", LEVEL_FIT_MULTIPLIERS["UPSIDE"]
    return "BELOW", LEVEL_FIT_MULTIPLIERS["BELOW"]


# ─── Filters ──────────────────────────────────────────────────────────────────

def league_move_allowed(player_league: str, buyer_league: str) -> tuple[bool, str]:
    """Returns (allowed, label). Label is 'upward' | 'lateral' on success.

    Rule: player at tier N can move to tier <= N (lower number = higher tier).
    Tier D (SA1/MLS1) is self-contained: D players can only move within D.
    Non-D players cannot move down into D.
    """
    p_tier = config.LEAGUE_TIERS.get(player_league)
    b_tier = config.LEAGUE_TIERS.get(buyer_league)
    if p_tier is None or b_tier is None:
        return False, ""
    # Tier D self-containment rule
    if p_tier == 4 and b_tier != 4:
        return False, ""
    if b_tier == 4 and p_tier != 4:
        return False, ""
    # Lateral or upward only
    if b_tier > p_tier:
        return False, ""
    return True, ("upward" if b_tier < p_tier else "lateral")


def side_ok(player_bucket: str, preferred_side: str | None) -> bool:
    """preferred_side is enforced only when the player's bucket carries side info."""
    if not preferred_side or preferred_side.strip() == "" or preferred_side == "Either":
        return True
    player_side = INTRINSIC_SIDE.get(player_bucket)
    if player_side is None:
        # Non-side-intrinsic bucket (CB/CM/DM/AM/GK/ST_CF) — preference doesn't apply
        return True
    return player_side == preferred_side


# ─── Scoring ──────────────────────────────────────────────────────────────────

def budget_fit_curve(indicative_fee: float, buyer_max_fee: float) -> float:
    """Two-stage curve.

    Below MIN_BROKERAGE_FEE (€15m): 0 — club isn't a credible brokerage buyer.
    Between MIN_BROKERAGE_FEE and 2× indicative_fee: linear ramp from 0 to 0.80
      — captures clubs willing to stretch toward (but possibly below) the
      indicative fee. The €15m floor reflects "real spending intent on a single
      signing".
    At or above 2× indicative_fee: plateau at 0.80 — once a buyer can clearly
      afford the player, more headroom doesn't reflect real deal flexibility.

    Effect: clubs like Newcastle/Real Madrid surface on higher-TM players (their
    max_fee is below indicative but above €15m → they could stretch). Eredivisie
    bottom-half clubs below €15m max never surface.
    """
    if buyer_max_fee < config.MIN_BROKERAGE_FEE:
        return 0.0
    threshold = 2.0 * indicative_fee
    if buyer_max_fee >= threshold:
        return 0.80
    # Linear ramp from MIN_BROKERAGE_FEE (=0) to 2*indicative_fee (=0.80)
    span = threshold - config.MIN_BROKERAGE_FEE
    if span <= 0:
        return 0.80
    return 0.80 * (buyer_max_fee - config.MIN_BROKERAGE_FEE) / span


def wage_feasibility_for(player_wage_pw: float | None, buyer_max_wage_pw: float | None) -> tuple[float, str]:
    """Returns (multiplier, label). label is 'ok' / 'infeasible' / 'unknown'.
    Tracks whether the term is discriminative for this specific pair."""
    if player_wage_pw is None or buyer_max_wage_pw is None:
        return WAGE_FEASIBILITY_UNKNOWN, "unknown"
    if float(player_wage_pw) <= float(buyer_max_wage_pw):
        return WAGE_FEASIBILITY_OK, "ok"
    return WAGE_FEASIBILITY_INFEASIBLE, "infeasible"


def score_pair(player_tm: int, sellability: float, buyer_max_fee: int,
               source: str | None, validated: str | None,
               player_wage_pw: float | None = None,
               buyer_max_wage_pw: float | None = None,
               player_ca: float | None = None,
               player_pa: float | None = None,
               club_threshold: float | None = None) -> dict:
    sell_term = (sellability or 0.0) / 100.0
    di = demand_intensity(source, validated)
    # Tiered TM-to-fee multiplier — see config.TM_TO_FEE_BANDS. Honest about
    # the empirical lift varying by player band: low-TM prospects 2×, mid 1.5×,
    # established 1.2×.
    indicative_fee = player_tm * config.tm_to_fee_multiplier(player_tm)
    budget = budget_fit_curve(indicative_fee, float(buyer_max_fee))
    wage, wage_label = wage_feasibility_for(player_wage_pw, buyer_max_wage_pw)
    # Sci Sports level-fit multiplier — rewards ON_LEVEL, dampens BELOW,
    # neutral when player is unrated or club has no threshold data.
    level_fit, level_mult = compute_level_fit(player_ca, player_pa, club_threshold)
    raw = sell_term * di * budget * wage * level_mult
    return {
        "sellability_term":     round(sell_term, 4),
        "demand_intensity":     di,
        "budget_fit":           round(budget, 4),
        "wage_feasibility":     wage,
        "wage_label":           wage_label,
        "level_fit":            level_fit,
        "level_fit_multiplier": level_mult,
        "match_score_raw":      round(raw, 4),
        "match_score":          round(raw * 100.0, 1),
    }


# ─── Driver ───────────────────────────────────────────────────────────────────

def _load_manual_wages() -> dict[int, float]:
    """Load wage_pw_eur per player_id from data/manual_wages.xlsx if present.
    Returns {player_id: wage_pw_eur_int}. Players not in the file or with blank
    wages aren't included → they fall back to the 0.7 wage_feasibility default."""
    p = Path("data/manual_wages.xlsx")
    if not p.exists():
        return {}
    from openpyxl import load_workbook as _lw
    wb = _lw(p, data_only=True, read_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    if not rows:
        return {}
    headers = [str(h) if h is not None else "" for h in rows[0]]
    idx = {h: i for i, h in enumerate(headers)}
    if "player_id" not in idx or "wage_pw_eur" not in idx:
        return {}
    out: dict[int, float] = {}
    for r in rows[1:]:
        if not r or r[idx["player_id"]] is None:
            continue
        try:
            pid = int(r[idx["player_id"]])
            wage = r[idx["wage_pw_eur"]]
            if wage in (None, ""):
                continue
            out[pid] = float(wage)
        except (TypeError, ValueError):
            continue
    return out


def build_matches(con: sqlite3.Connection) -> tuple[int, int, dict]:
    """Returns (total_scored_pairs, total_retained_after_top3, stats)."""

    # Pull sellable assets — same filter as Sheet 4 (script 10).
    players = con.execute("""
        SELECT pu.player_id, pu.name, pu.age, pu.position_bucket,
               pu.current_tm_value_eur, pu.sellability_score, pu.league_id,
               pu.current_club, pu.parent_club, pu.parent_club_id, pu.on_loan
        FROM player_universe pu
        WHERE pu.sellability_status = 'sellable_now'
    """).fetchall()

    # Manual wage data (data/manual_wages.xlsx) — only populated for players
    # Ryan has filled in. Missing entries → wage_feasibility = 0.7 fallback.
    player_wages = _load_manual_wages()

    # Sci Sports ratings — CA + PA per player, loaded from player_ratings
    # (populated by scripts/load_scisports_ratings.py from
    # data/scisports_ratings.xlsx). Missing entries → level_fit=UNRATED, ×1.0.
    player_ratings: dict[int, tuple[float | None, float | None]] = {}
    has_ratings = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='player_ratings'"
    ).fetchone() is not None
    if has_ratings:
        for pid, ca, pa in con.execute(
            "SELECT tm_player_id, current_ability, potential_ability "
            "FROM player_ratings WHERE status != 'invalid'"
        ).fetchall():
            player_ratings[int(pid)] = (ca, pa)

    # Club-level first-team CA thresholds (Stage 1: every request defaults to
    # first-team level). Keyed by club_id as TEXT (matches map_club_overview).
    club_thresholds: dict[str, float] = {}
    for cid, threshold in con.execute(
        "SELECT club_id, sci_first_team_level FROM map_club_overview "
        "WHERE sci_first_team_level IS NOT NULL"
    ).fetchall():
        club_thresholds[str(cid)] = float(threshold)

    # Pull buyer requests from both explicit (map_club_requests, 8 manual workbooks)
    # and inferred (inferred_club_requests, senior_roster thinness for all 354 clubs).
    # Distinguished downstream via the `source` column ("Inferred" vs Agent/Intel/...).
    has_inferred = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='inferred_club_requests'"
    ).fetchone() is not None
    if has_inferred:
        # Namespace inferred IDs (+10M) to avoid collision with explicit IDs
        # on the matches.(player_id, buyer_request_id) UNIQUE constraint.
        requests = con.execute("""
            SELECT request_id, club_id, club_name, league AS buyer_league,
                   position_bucket, preferred_side,
                   max_transfer_fee_eur, max_wage_pw_eur,
                   source, validated
            FROM map_club_requests
            WHERE position_bucket IS NOT NULL AND max_transfer_fee_eur IS NOT NULL
            UNION ALL
            SELECT request_id + 10000000 AS request_id, club_id, club_name, league AS buyer_league,
                   position_bucket, preferred_side,
                   max_transfer_fee_eur, max_wage_pw_eur,
                   source, validated
            FROM inferred_club_requests
            WHERE position_bucket IS NOT NULL AND max_transfer_fee_eur IS NOT NULL
        """).fetchall()
    else:
        requests = con.execute("""
            SELECT request_id, club_id, club_name, league AS buyer_league,
                   position_bucket, preferred_side,
                   max_transfer_fee_eur, max_wage_pw_eur,
                   source, validated
            FROM map_club_requests
            WHERE position_bucket IS NOT NULL AND max_transfer_fee_eur IS NOT NULL
        """).fetchall()

    # Group requests by position_bucket for fast per-player lookup.
    reqs_by_bucket: dict[str, list[tuple]] = {}
    for r in requests:
        reqs_by_bucket.setdefault(r[4], []).append(r)

    stats = {
        "players_processed":        0,
        "players_with_any_match":   0,
        "players_with_zero_match":  0,
        "pairs_position_matched":   0,
        "pairs_after_budget":       0,
        "pairs_after_side":         0,
        "pairs_after_tier":         0,
        "pairs_after_score_floor":  0,
        "pairs_retained":           0,
    }

    rows_to_insert: list[tuple] = []
    for p in players:
        (pid, pname, page, pbucket, ptm, psell, pleague, pclub, pparent, pparent_id, ponloan) = p
        stats["players_processed"] += 1

        if pbucket is None or ptm is None or ptm == 0:
            stats["players_with_zero_match"] += 1
            continue

        candidates = reqs_by_bucket.get(pbucket, [])
        scored: list[dict] = []
        for req in candidates:
            (rid, bcid, bname, bleague, _bucket, side, max_fee, max_wage, source, validated) = req
            stats["pairs_position_matched"] += 1
            # Conservative filter: buyer must demonstrate ≥ MIN_BROKERAGE_FEE
            # spending intent. We allow stretching below player TM at the
            # scoring layer (budget_fit), but a club with €5m max_fee isn't
            # a credible buyer for any brokerage-band player regardless of
            # tactical fit.
            if max_fee < config.MIN_BROKERAGE_FEE:
                continue
            stats["pairs_after_budget"] += 1
            if not side_ok(pbucket, side):
                continue
            stats["pairs_after_side"] += 1
            ok, tier_label = league_move_allowed(pleague, bleague)
            if not ok:
                continue
            stats["pairs_after_tier"] += 1
            p_ca, p_pa = player_ratings.get(pid, (None, None))
            threshold = club_thresholds.get(str(bcid))
            s = score_pair(ptm, psell, max_fee, source, validated,
                           player_wage_pw=player_wages.get(pid),
                           buyer_max_wage_pw=max_wage,
                           player_ca=p_ca, player_pa=p_pa,
                           club_threshold=threshold)
            scored.append({
                "player_id":     pid,
                "buyer_request_id": rid,
                "buyer_club_id": bcid,
                "buyer_club_name": bname,
                "buyer_league_id": bleague,
                "max_transfer_fee_eur": max_fee,
                "max_wage_pw_eur":      max_wage,
                "source": source,
                "validated": validated,
                "preferred_side": side,
                "tier_move": tier_label,
                "player_ca": p_ca,
                "player_pa": p_pa,
                "club_threshold_for_request": threshold,
                **s,
            })
        # Apply the score floor — drop the long tail of marginal matches
        # (low sellability + inferred demand + tight budget = score < 10).
        scored = [s for s in scored if s["match_score"] >= MATCH_SCORE_FLOOR]
        stats["pairs_after_score_floor"] += len(scored)
        # Keep all surviving matches — no per-player cap.
        scored.sort(key=lambda d: -d["match_score"])
        kept = scored if TOP_N_PER_PLAYER is None else scored[:TOP_N_PER_PLAYER]
        if kept:
            stats["players_with_any_match"] += 1
            stats["pairs_retained"] += len(kept)
        else:
            stats["players_with_zero_match"] += 1
        for m in kept:
            rows_to_insert.append((
                pid, m["buyer_request_id"],
                pname, pbucket, ptm, psell,
                m["buyer_club_id"], m["buyer_club_name"], m["buyer_league_id"],
                m["max_transfer_fee_eur"], m["max_wage_pw_eur"],
                m["source"], m["validated"], m["preferred_side"],
                m["sellability_term"], m["demand_intensity"], m["budget_fit"],
                m["wage_feasibility"], m["match_score_raw"], m["match_score"],
                m["tier_move"], m["wage_label"],
                player_wages.get(pid),
                m["player_ca"], m["player_pa"],
                m["club_threshold_for_request"],
                m["level_fit"], m["level_fit_multiplier"],
            ))

    init_matches_table(con)
    con.executemany("""
        INSERT INTO matches (
            player_id, buyer_request_id, player_name, position_bucket,
            player_tm_value_eur, sellability_score, buyer_club_id, buyer_club_name,
            buyer_league_id, max_transfer_fee_eur, max_wage_pw_eur,
            request_source, request_validated, preferred_side,
            sellability_term, demand_intensity, budget_fit, wage_feasibility,
            match_score_raw, match_score, tier_move, wage_feasibility_label,
            player_wage_pw_eur,
            player_ca, player_pa, club_threshold_for_request,
            level_fit, level_fit_multiplier
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, rows_to_insert)
    con.commit()
    return len(rows_to_insert), len(rows_to_insert), stats


# ─── Rationale text (for printout + Sheet 1 column) ──────────────────────────

def build_rationale(con: sqlite3.Connection, match_row: dict) -> str:
    """Short trade-logic sentence combining seller-side reason, buyer-side reason,
    and the trade frame. Buyers are described by source/validated; sellers by their
    biggest-firing pressure component."""
    pid = match_row["player_id"]
    # Seller side: largest pressure component contributing to base_pressure of parent club.
    p = con.execute("""
        SELECT pu.parent_club, pu.parent_club_id, pu.on_loan, pu.contract_leveraged,
               pu.right_priced, pu.finished_product, pu.league_id,
               cp.contract_leverage_score, cp.squad_oversupply_score, cp.net_spend_score,
               cp.manager_change_flag, cp.public_must_sell_flag, cp.total_pressure_score
        FROM player_universe pu
        LEFT JOIN club_pressure cp ON cp.club_id = pu.parent_club_id
        WHERE pu.player_id = ?
    """, (pid,)).fetchone()
    if not p:
        return "no rationale (player not found)"
    (parent_club, parent_id, on_loan, c_lev, r_priced, fin_prod, p_league,
     cl_s, so_s, ns_s, mc_flag, ps_flag, tp_score) = p

    # Seller-side driver
    drivers = []
    if ps_flag == 1:
        drivers.append("public must-sell flag (parachute/FFP)")
    if mc_flag == 1:
        drivers.append("manager change")
    if c_lev == 1:
        drivers.append("contract leverage")
    if on_loan:
        drivers.append("loan with sell-readiness signal")
    # Largest structural component if no flag driver
    if not drivers:
        comps = [("contract leverage", cl_s or 0), ("squad oversupply", so_s or 0), ("net-spend headroom", ns_s or 0)]
        comps.sort(key=lambda x: -x[1])
        if comps[0][1] > 0:
            drivers.append(f"{comps[0][0]} ({comps[0][1]:.0f}/100)")
    if parent_id is None:
        seller_text = f"parent club outside coverage — {parent_club or '(unknown)'}"
    elif drivers:
        seller_text = f"{parent_club} ({p_league}) — " + ", ".join(drivers[:2])
    else:
        seller_text = f"{parent_club} ({p_league}) — low structural pressure"

    # Buyer-side driver
    src = (match_row.get("request_source") or "").strip()
    val = (match_row.get("request_validated") or "").strip()
    if src == "Agent" and val.upper() == "YES":
        buyer_text = f"{match_row['buyer_club_name']} ({match_row['buyer_league_id']}) — validated agent-confirmed need"
    elif src == "Intel":
        buyer_text = f"{match_row['buyer_club_name']} ({match_row['buyer_league_id']}) — intel-derived positional gap"
    else:
        buyer_text = f"{match_row['buyer_club_name']} ({match_row['buyer_league_id']}) — open need (unverified)"

    # Trade frame: budget headroom
    headroom = match_row.get("max_transfer_fee_eur", 0) - match_row.get("player_tm_value_eur", 0)
    headroom_text = f"€{headroom/1e6:.0f}m headroom above TM"

    tier = match_row.get("tier_move", "lateral")
    return f"Seller: {seller_text}. Buyer: {buyer_text}. Trade: {tier} move, {headroom_text}."


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    if not Path(config.SQLITE_FILE).exists():
        sys.exit(f"Missing {config.SQLITE_FILE} — run 02→09 first.")

    with sqlite3.connect(config.SQLITE_FILE) as con:
        n_total, n_retained, stats = build_matches(con)

        # Position breakdown.
        bucket_counts = dict(con.execute(
            "SELECT position_bucket, COUNT(*) FROM matches GROUP BY position_bucket"
        ).fetchall())

        # Top 20 by match_score.
        top20 = con.execute("""
            SELECT m.*, pu.age, pu.parent_club, pu.parent_club_id, pu.on_loan,
                   pu.current_club, pu.league_id AS player_league_id
            FROM matches m
            JOIN player_universe pu ON pu.player_id = m.player_id
            ORDER BY m.match_score DESC, m.player_name, m.match_score_raw DESC
            LIMIT 20
        """).fetchall()
        col_names = [d[0] for d in con.execute(
            "SELECT m.*, pu.age, pu.parent_club, pu.parent_club_id, pu.on_loan, "
            "pu.current_club, pu.league_id AS player_league_id FROM matches m "
            "JOIN player_universe pu ON pu.player_id = m.player_id LIMIT 0"
        ).description]

    bar = "─" * 100
    cap_label = "no per-player cap" if TOP_N_PER_PLAYER is None else f"top {TOP_N_PER_PLAYER} per player"
    print(bar)
    print(f"Match engine — {n_total} rows in `matches` table ({cap_label})")
    print(bar)
    print(f"  Players processed:       {stats['players_processed']:>6}")
    print(f"  Players with ≥1 match:   {stats['players_with_any_match']:>6}")
    print(f"  Players with 0 matches:  {stats['players_with_zero_match']:>6}")
    print(f"  Pairs position-matched:  {stats['pairs_position_matched']:>6}")
    print(f"   ↳ after budget:         {stats['pairs_after_budget']:>6}")
    print(f"   ↳ after side:           {stats['pairs_after_side']:>6}")
    print(f"   ↳ after league-tier:    {stats['pairs_after_tier']:>6}")
    print(f"   ↳ after score floor ≥{int(MATCH_SCORE_FLOOR)}: {stats['pairs_after_score_floor']:>6}")
    print(f"   ↳ retained:             {stats['pairs_retained']:>6}")

    print()
    print("Match rows by position bucket:")
    for b in ("GK","CB","LB","RB","DM","CM","AM","LW","RW","ST_CF"):
        print(f"  {b:6s} {bucket_counts.get(b, 0):>4}")

    # wage_feasibility coverage on the matches table
    with sqlite3.connect(config.SQLITE_FILE) as con3:
        wage_rows = list(con3.execute(
            "SELECT wage_feasibility_label, COUNT(*) FROM matches GROUP BY wage_feasibility_label"
        ))
    print()
    print("wage_feasibility distribution across match rows:")
    for label, count in wage_rows:
        print(f"  {(label or 'unknown'):14s} {count:>5}")

    print()
    print(bar)
    print("Top 20 brokerage opportunities (sanity check before Sheet 1 export)")
    print(bar)
    print(f"  {'#':>2}  {'score':>5s}  {'player':28s} {'age':>3s} {'pos':5s}  "
          f"{'p_lg':4s} {'b_lg':4s}  {'TM':>6s}  {'max_fee':>8s}  buyer")
    for i, r in enumerate(top20, start=1):
        row = dict(zip(col_names, r))
        print(f"  {i:>2}. {row['match_score']:>5.1f}  "
              f"{(row['player_name'] or '')[:28]:28s} "
              f"{row['age']:>3}  {row['position_bucket']:5s}  "
              f"{row['player_league_id']:4s} {row['buyer_league_id']:4s}  "
              f"€{(row['player_tm_value_eur'] or 0)/1e6:>4.0f}m  "
              f"€{(row['max_transfer_fee_eur'] or 0)/1e6:>5.0f}m  "
              f"{(row['buyer_club_name'] or '')[:30]}")

    print()
    print(bar)
    print("Top 3 brokerage rationales in full (by match_score)")
    print(bar)
    with sqlite3.connect(config.SQLITE_FILE) as con2:
        for i, r in enumerate(top20[:3], start=1):
            row = dict(zip(col_names, r))
            rationale = build_rationale(con2, row)
            print(f"\n  {i}. {row['player_name']} ({row['age']}, {row['position_bucket']}) → "
                  f"{row['buyer_club_name']}  [match_score = {row['match_score']:.1f}]")
            print(f"     {rationale}")


if __name__ == "__main__":
    main()
