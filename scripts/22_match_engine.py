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

# Market View score floor — lower than Brokerage because Market View can lift
# matches above 100 via position tension + scarcity, so a score of 5 in
# Market View represents the same "barely-worth-surfacing" frontier that 10
# does in Brokerage. A match is kept if EITHER floor is satisfied.
MARKET_SCORE_FLOOR = 5.0

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
            level_fit_multiplier  REAL,            -- 1.20 / 1.05 / 0.85 / 1.00 — Brokerage Engine multiplier
            -- Market View score (docs/market_view_match_formula.md). Computed
            -- side-by-side with match_score; UI sidebar toggles which one ranks.
            market_match_score    REAL,
            UNIQUE(player_id, buyer_request_id)
        )
    """)
    con.execute("CREATE INDEX idx_matches_player ON matches(player_id)")
    con.execute("CREATE INDEX idx_matches_score ON matches(match_score DESC)")
    con.execute("CREATE INDEX idx_matches_market ON matches(market_match_score DESC)")


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


# ─── Market View components (docs/market_view_match_formula.md) ─────────────
# Multipliers per the spec; UNRATED hard rule means UNRATED matches do not
# enter the matches table at all (skipped in build_matches, tracked separately
# for the review worklist).
MARKET_LEVEL_FIT = {
    "ON_LEVEL": 1.00,
    "UPSIDE":   0.70,
    "BELOW":    0.35,
}

# Age multiplier — first-order viability dampener
def market_age_multiplier(age: int | None) -> float:
    if age is None:
        return 1.0
    if age <= 25:
        return 1.0
    if age <= 29:
        return 0.85
    if age <= 32:
        return 0.6
    return 0.35


# Demand term — positional+level signal only; named-player interest dropped
def market_demand_term(source: str | None, validated: str | None) -> float:
    src = (source or "").strip()
    val = (validated or "").strip().upper()
    if src == "Agent" and val == "YES":
        return 1.0
    if src == "Agent":
        return 1.0  # Agent without YES still treated as agent-validated positional
    if src == "Intel":
        return 0.75
    if src == "Inferred":
        return 0.5
    return 0.5  # NULL/NULL fallback


# Pathway plausibility — S/A/B/C/D/X tier transition matrix
LEAGUE_TIER_LABEL = {
    "GB1": "S",
    "ES1": "A", "IT1": "A", "L1": "A", "FR1": "A",
    "NL1": "B", "PO1": "B", "BE1": "B", "TR1": "B",
    "GB2": "C", "L2": "C", "FR2": "C", "IT2": "C", "ES2": "C",
    "GR1": "D", "DK1": "D", "SC1": "D",
    "MLS1": "X", "SA1": "X",
}
_TIER_RANK = {"S": 5, "A": 4, "B": 3, "C": 2, "D": 1, "X": 0}


def market_pathway_score(from_league: str | None, to_league: str | None) -> float:
    f = LEAGUE_TIER_LABEL.get(from_league)
    t = LEAGUE_TIER_LABEL.get(to_league)
    if f is None or t is None:
        return 0.5
    # X tier special-cases
    if t == "X" and f != "X":
        return 0.5   # into MLS/Saudi
    if f == "X" and t != "X":
        return 0.4   # out of MLS/Saudi
    # Same-tier
    if f == t:
        return 0.95 if f == "S" else 0.85
    fr, tr = _TIER_RANK[f], _TIER_RANK[t]
    if tr > fr:  # upward in pyramid
        return 1.0
    # downward
    return 0.6 if (fr - tr) == 1 else 0.45  # mild vs steep


# Scarcity term — exp((CA - median) / std), clipped [0.5, 1.5]
def market_scarcity_term(player_ca: float | None,
                         median_ca: float | None,
                         std_ca: float | None) -> float:
    if player_ca is None or median_ca is None or std_ca is None or std_ca <= 0:
        return 1.0
    import math
    z = (player_ca - median_ca) / std_ca
    return max(0.5, min(1.5, math.exp(z)))


# Valuation term — exp(-(pred - benchmark) / benchmark), clipped [0.6, 1.4]
def market_valuation_term(predicted_fee: float | None,
                          benchmark_fee: float | None) -> float:
    if not predicted_fee or not benchmark_fee or benchmark_fee <= 0:
        return 1.0
    import math
    delta_frac = (predicted_fee - benchmark_fee) / benchmark_fee
    return max(0.6, min(1.4, math.exp(-delta_frac)))


# CA band lookup (same bands as cohort_stats_valuation_benchmark)
_CA_BANDS = [
    ("60-70",   60.0,   70.0),
    ("70-80",   70.0,   80.0),
    ("80-90",   80.0,   90.0),
    ("90-100",  90.0,  100.0),
    ("100-110",100.0,  110.0),
    ("110-120",110.0,  120.0),
    ("120-130",120.0,  130.0),
    ("130+",   130.0,  9999.0),
]


def _ca_band_for(ca: float) -> str | None:
    for label, lo, hi in _CA_BANDS:
        if lo <= ca < hi:
            return label
    return None


# Position tension multiplier — NOW ENABLED per spec (squad ingestion across
# 10 demand-mapped leagues + ES2/IT2/L2 supply complete, sellability tagged).
def market_tension_multiplier(ratio: float) -> float:
    if ratio > 1.3:
        return 1.4
    if ratio >= 0.7:
        return 1.0
    return 0.7


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


def build_cohort_unrated(con: sqlite3.Connection) -> int:
    """Rebuild the UNRATED worklist: players with sellability_score > 50 but
    no current_ability available (or status departed). Surfaces operationally
    via Streamlit; rebuild every match engine run so it stays fresh.
    """
    import datetime as _dt
    con.execute("DROP TABLE IF EXISTS cohort_unrated")
    con.execute("""
        CREATE TABLE cohort_unrated (
            player_id          INTEGER PRIMARY KEY,
            player_name        TEXT,
            parent_club_name   TEXT,
            position_bucket    TEXT,
            age                INTEGER,
            sellability_score  REAL,
            league_id          TEXT,
            reason             TEXT,
            snapshot_date      TEXT
        )
    """)
    today = _dt.date.today().isoformat()
    # no_ca: sellability > 50 but no CA in player_ratings (or row missing)
    con.execute("""
        INSERT INTO cohort_unrated
        SELECT pu.player_id, pu.name, pu.parent_club, pu.position_bucket,
               pu.age, pu.sellability_score, pu.league_id,
               CASE
                 WHEN pr.status = 'departed' THEN 'departed'
                 ELSE 'no_ca'
               END AS reason,
               ?
        FROM player_universe pu
        LEFT JOIN player_ratings pr ON pr.tm_player_id = pu.player_id
        WHERE pu.sellability_score > 50
          AND (pr.current_ability IS NULL OR pr.current_ability = 0)
    """, (today,))
    return con.execute("SELECT COUNT(*) FROM cohort_unrated").fetchone()[0]


def build_matches(con: sqlite3.Connection) -> tuple[int, int, dict]:
    """Returns (total_scored_pairs, total_retained_after_top3, stats).

    Cohort scope (Market View, per docs/market_view_match_formula.md §Cohort):
        sellability_score > 50  AND  player_ratings.current_ability IS NOT NULL

    Both scores computed per match row:
      - match_score (Brokerage): NULL if player not in sellable_now status
      - market_match_score: always computed per the Market View formula

    Same matches table; two columns; sellable_now slice is the Brokerage
    Engine view, full cohort is the Market View.
    """
    # Pull the wider Market View cohort. sellability_status pulled so we can
    # tell which subset is also in the Brokerage Engine's sellable_now slice.
    #
    # Cohort = (Market View cohort) UNION (sellable_now cohort).
    #   Market View: sellability_score > 50 AND has CA
    #   Brokerage scope: every sellable_now player, regardless of sellability
    #     score — these are the original-spec targeted cohort. Some have
    #     scores below 50 (e.g. Kubo at 42.6 — sellable_now from the
    #     classifier's rule path but didn't clear the 50 watermark). We must
    #     keep them in the match engine to preserve the Brokerage view; their
    #     market_match_score is computed alongside.
    # UNRATED hard rule still applies in the per-pair loop.
    players = con.execute("""
        SELECT pu.player_id, pu.name, pu.age, pu.position_bucket,
               pu.current_tm_value_eur, pu.sellability_score, pu.league_id,
               pu.current_club, pu.parent_club, pu.parent_club_id, pu.on_loan,
               pu.sellability_status
        FROM player_universe pu
        WHERE (
            (pu.sellability_score > 50
             AND EXISTS (
                 SELECT 1 FROM player_ratings
                 WHERE tm_player_id = pu.player_id
                   AND current_ability IS NOT NULL
             ))
            OR pu.sellability_status = 'sellable_now'
        )
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

    # ─── Market View pre-loads ─────────────────────────────────────────────
    # Cohort stats per position (scarcity_term)
    cohort_stats: dict[str, tuple[float, float]] = {}
    for pos, med, std in con.execute(
        "SELECT position_bucket, median_ca, std_ca FROM cohort_stats_position "
        "WHERE median_ca IS NOT NULL"
    ).fetchall():
        cohort_stats[pos] = (float(med), float(std))

    # Valuation benchmarks per (position × CA band)
    valuation_benchmark: dict[tuple[str, str], float] = {}
    for pos, band, fee in con.execute(
        "SELECT position_bucket, ca_band, median_predicted_fee FROM cohort_stats_valuation_benchmark "
        "WHERE median_predicted_fee IS NOT NULL"
    ).fetchall():
        valuation_benchmark[(pos, band)] = float(fee)

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

    # ─── Position tension multiplier ───────────────────────────────────────
    # ratio = demand_count / sellability_weighted_supply_count, per position.
    # Demand = number of buyer requests across all leagues at that position.
    # Supply = sum of (sellability_score/100) over Market-View cohort
    #           (sellability > 50 with CA available) at that position.
    demand_by_pos: dict[str, int] = {}
    for req in requests:
        demand_by_pos[req[4]] = demand_by_pos.get(req[4], 0) + 1

    supply_weighted_by_pos: dict[str, float] = {}
    for pos, w in con.execute("""
        SELECT pu.position_bucket, SUM(pu.sellability_score / 100.0)
        FROM player_universe pu
        JOIN player_ratings pr ON pr.tm_player_id = pu.player_id
        WHERE pu.sellability_score > 50
          AND pr.current_ability IS NOT NULL
          AND pu.position_bucket IS NOT NULL
        GROUP BY pu.position_bucket
    """).fetchall():
        supply_weighted_by_pos[pos] = float(w or 0)

    tension_mult_by_pos: dict[str, float] = {}
    for pos in set(demand_by_pos) | set(supply_weighted_by_pos):
        d = demand_by_pos.get(pos, 0)
        s = supply_weighted_by_pos.get(pos, 0.0)
        ratio = (d / s) if s > 0 else (10.0 if d > 0 else 1.0)  # avoid divide-by-zero
        tension_mult_by_pos[pos] = market_tension_multiplier(ratio)

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
        "market_unrated_skipped":   0,   # UNRATED hard-rule
        "market_unrated_players":   set(),
    }

    rows_to_insert: list[tuple] = []
    for p in players:
        (pid, pname, page, pbucket, ptm, psell, pleague, pclub, pparent,
         pparent_id, ponloan, psell_status) = p
        is_sellable_now = (psell_status == "sellable_now")
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

            # ── Market View score (UNRATED hard rule) ──────────────────────
            # Per docs/market_view_match_formula.md: UNRATED matches do NOT
            # enter the matches table. Skip and track for the review worklist.
            if s["level_fit"] == "UNRATED":
                stats["market_unrated_skipped"] += 1
                stats["market_unrated_players"].add(pid)
                continue

            sellability_pillar = (psell or 0.0) / 100.0
            age_mult = market_age_multiplier(page)
            demand_t = market_demand_term(source, validated)
            level_t = MARKET_LEVEL_FIT.get(s["level_fit"], 0.35)
            financial_t = float(s["budget_fit"]) * float(s["wage_feasibility"])
            pathway_t = market_pathway_score(pleague, bleague)
            tension_m = tension_mult_by_pos.get(pbucket, 1.0)
            # Scarcity
            med_std = cohort_stats.get(pbucket)
            if med_std and p_ca is not None:
                scarcity_t = market_scarcity_term(p_ca, med_std[0], med_std[1])
            else:
                scarcity_t = 1.0
            # Valuation
            pred_fee = (ptm or 0) * config.tm_to_fee_multiplier(ptm or 0) if ptm else None
            band = _ca_band_for(p_ca) if p_ca is not None else None
            bench = valuation_benchmark.get((pbucket, band)) if band else None
            valuation_t = market_valuation_term(pred_fee, bench)

            market_score_raw = (
                sellability_pillar
                * age_mult
                * demand_t
                * level_t
                * financial_t
                * pathway_t
                * tension_m
                * scarcity_t
                * valuation_t
            )
            # Scale to 0-100 to mirror match_score's range for UI consistency
            market_score = round(market_score_raw * 100.0, 1)

            # Brokerage Engine match_score is meaningful only for sellable_now
            # players (the targeted cohort). For wider Market View cohort
            # members, match_score is NULL — the row exists for Market View
            # only and shouldn't surface in Brokerage-default views.
            brokerage_score = s["match_score"] if is_sellable_now else None

            row = {
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
                "market_match_score": market_score,
                **s,
            }
            row["match_score"] = brokerage_score
            scored.append(row)
        # Apply EITHER floor — keep if Brokerage match_score ≥ MATCH_SCORE_FLOOR
        # (only meaningful for sellable_now players, NULL otherwise) OR if
        # Market View market_match_score ≥ MARKET_SCORE_FLOOR. This preserves
        # both lenses' coverage; rows that meet only the market floor will
        # have NULL match_score.
        def _keep(s):
            ms = s.get("match_score")
            mks = s.get("market_match_score")
            brok_ok = (ms is not None and ms >= MATCH_SCORE_FLOOR)
            mkt_ok = (mks is not None and mks >= MARKET_SCORE_FLOOR)
            return brok_ok or mkt_ok
        scored = [s for s in scored if _keep(s)]
        stats["pairs_after_score_floor"] += len(scored)
        # Sort by market_match_score (always non-null when row is kept) so
        # the per-player loop ranking is consistent even for non-sellable_now.
        scored.sort(key=lambda d: -(d.get("market_match_score") or 0))
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
                m["market_match_score"],
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
            level_fit, level_fit_multiplier,
            market_match_score
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, rows_to_insert)
    con.commit()
    # Convert the unrated_players set length for the stats return
    stats["market_unrated_players_count"] = len(stats.get("market_unrated_players", set()))
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
        n_unrated = build_cohort_unrated(con)
        con.commit()
        print(f"UNRATED worklist (cohort_unrated): {n_unrated} players")
        print()
        n_total, n_retained, stats = build_matches(con)

        # Position breakdown.
        bucket_counts = dict(con.execute(
            "SELECT position_bucket, COUNT(*) FROM matches GROUP BY position_bucket"
        ).fetchall())

        # Top 20 by market_match_score (Market View — every match has one).
        top20 = con.execute("""
            SELECT m.*, pu.age, pu.parent_club, pu.parent_club_id, pu.on_loan,
                   pu.current_club, pu.league_id AS player_league_id
            FROM matches m
            JOIN player_universe pu ON pu.player_id = m.player_id
            ORDER BY m.market_match_score DESC NULLS LAST, m.player_name
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
    print(f"   ↳ after floor (brok≥{int(MATCH_SCORE_FLOOR)} OR mkt≥{MARKET_SCORE_FLOOR}): {stats['pairs_after_score_floor']:>6}")
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
    print("Top 20 by market_match_score (Market View — comprehensive cohort)")
    print(bar)
    print(f"  {'#':>2}  {'mkt':>6s}  {'brok':>6s}  {'player':28s} {'age':>3s} {'pos':5s}  "
          f"{'p_lg':4s} {'b_lg':4s}  {'TM':>6s}  {'max_fee':>8s}  buyer")
    for i, r in enumerate(top20, start=1):
        row = dict(zip(col_names, r))
        mkt = row.get("market_match_score")
        brok = row.get("match_score")
        mkt_str = f"{mkt:>6.1f}" if mkt is not None else "    —"
        brok_str = f"{brok:>6.1f}" if brok is not None else "    —"
        print(f"  {i:>2}. {mkt_str}  {brok_str}  "
              f"{(row['player_name'] or '')[:28]:28s} "
              f"{row['age']:>3}  {row['position_bucket']:5s}  "
              f"{row['player_league_id']:4s} {row['buyer_league_id']:4s}  "
              f"€{(row['player_tm_value_eur'] or 0)/1e6:>4.0f}m  "
              f"€{(row['max_transfer_fee_eur'] or 0)/1e6:>5.0f}m  "
              f"{(row['buyer_club_name'] or '')[:30]}")

    print()
    print(bar)
    print("Top 3 rationales (by market_match_score)")
    print(bar)
    with sqlite3.connect(config.SQLITE_FILE) as con2:
        for i, r in enumerate(top20[:3], start=1):
            row = dict(zip(col_names, r))
            rationale = build_rationale(con2, row)
            mkt = row.get("market_match_score") or 0
            brok = row.get("match_score")
            brok_str = f"{brok:.1f}" if brok is not None else "—"
            print(f"\n  {i}. {row['player_name']} ({row['age']}, {row['position_bucket']}) → "
                  f"{row['buyer_club_name']}  [market={mkt:.1f}  brokerage={brok_str}]")
            print(f"     {rationale}")


if __name__ == "__main__":
    main()
