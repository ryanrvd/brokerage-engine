"""SQLite connection + cached query helpers for the Streamlit app.

One function per common query. All marked `@st.cache_data` so repeated
reads inside a session are free. Cache TTL = 60 seconds — when pipeline
scripts re-run, the app picks up changes within a minute (or the user
can force-reload via Streamlit's "Rerun" menu).
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
import config       # noqa: E402
import kill_list    # noqa: E402  — single source of truth, shared with scripts/23

CACHE_TTL = 60  # seconds


# ─── Connection ───────────────────────────────────────────────────────────────

@st.cache_resource
def get_connection() -> sqlite3.Connection:
    """Cached read-only connection to db/yatin.db."""
    db_path = PROJECT_ROOT / config.SQLITE_FILE
    if not db_path.exists():
        st.error(f"Database not found: {db_path}. Run the pipeline first.")
        st.stop()
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def _read_sql(sql: str, params: tuple = ()) -> pd.DataFrame:
    return pd.read_sql_query(sql, get_connection(), params=params)


# ─── Kill List (single source of truth, shared with scripts/23) ─────────────

@st.cache_data(ttl=CACHE_TTL)
def get_kill_list_state() -> dict:
    """Live read of the Kill List + agency rules. Used by all exclusion code paths.
    Mirrors what scripts/23 computes on its run; both share kill_list.py."""
    con = get_connection()
    return kill_list.compute_kill_list_state(con)


@st.cache_data(ttl=CACHE_TTL)
def get_excluded_ids() -> set[int]:
    """player_ids excluded across both manual entries + agency rules."""
    return get_kill_list_state()["excluded_ids"]


@st.cache_data(ttl=CACHE_TTL)
def get_player_search_options() -> list[dict]:
    """Players in the global-search dropdown. Includes sellable_now (Brokerage
    substrate) plus the wider mandate-relevant cohort (Market View) so search
    works in both engines. Phase A.8.7: brokerage_eligible gates Brokerage rows."""
    rows = _read_sql("""
        SELECT pu.player_id, pu.name AS official_name, pu.current_club, pu.league_id
        FROM player_universe pu
        WHERE COALESCE(pu.is_imminent_free_agent, 0) = 0
          AND (
            (pu.sellability_status = 'sellable_now' AND pu.brokerage_eligible = 1)
            OR (pu.sellability_score >= 35
                AND EXISTS (SELECT 1 FROM player_ratings pr2
                            WHERE pr2.tm_player_id = pu.player_id
                              AND pr2.current_ability IS NOT NULL))
          )
    """)
    import player_display as _pd
    pmap = _pd.load_display_map()
    out = []
    for _, r in rows.iterrows():
        pid = int(r["player_id"])
        out.append({
            "player_id":     pid,
            "official_name": r["official_name"],
            "display_name":  pmap.get(pid, r["official_name"]),
            "current_club":  r["current_club"],
            "league_id":     r["league_id"],
        })
    return out


@st.cache_data(ttl=CACHE_TTL)
def get_club_search_options() -> list[dict]:
    """All clubs from club_pressure with both display and official names."""
    rows = _read_sql("SELECT club_id, name AS official_name, league_id FROM club_pressure")
    import club_display as _cd
    cmap = _cd.load_display_map()
    out = []
    for _, r in rows.iterrows():
        cid = int(r["club_id"])
        out.append({
            "club_id":       cid,
            "official_name": r["official_name"],
            "display_name":  cmap.get(cid, r["official_name"]),
            "league_id":     r["league_id"],
        })
    return out


@st.cache_data(ttl=CACHE_TTL)
def get_excluded() -> pd.DataFrame:
    """Rich table for the Excluded Players page: one row per excluded player
    with their source (manual entry text or agency-rule label) joined to
    current player context (club, league, TM, sellability)."""
    state = get_kill_list_state()
    by_pid = {p["player_id"]: p for p in state["player_rows"]}
    rows: list[dict] = []

    # Manual entries first — match each entry back to the player_id it hit.
    manual_excluded_ids = state["manual_excluded_ids"]
    manual_lookup = {pid: None for pid in manual_excluded_ids}  # fill below

    # Re-match to capture which entry → which player_id (single-hit cases only)
    matched_to_entry: dict[int, tuple[str, str]] = {}
    for entry_name, reason in state["manual_entries"]:
        e_norm = kill_list.normalise_name(entry_name)
        if not e_norm:
            continue
        e_tokens = set(e_norm.split())
        for p in state["player_rows"]:
            p_norm = kill_list.normalise_name(p["player_name"])
            p_tokens = set(p_norm.split())
            if p_norm == e_norm or (e_tokens and e_tokens.issubset(p_tokens)):
                if p["player_id"] in manual_excluded_ids:
                    matched_to_entry[p["player_id"]] = (entry_name, reason)
                    break

    for pid in manual_excluded_ids:
        p = by_pid.get(pid, {})
        entry, reason = matched_to_entry.get(pid, ("(matched)", ""))
        rows.append({
            "player_id":       pid,
            "name":            p.get("player_name", entry),
            "position":        p.get("position_bucket", ""),
            "current_club":    p.get("current_club", ""),
            "parent_club":     p.get("parent_club", ""),
            "league":          p.get("league_id", ""),
            "agency":          p.get("agency", ""),
            "TM_value_eur":    p.get("current_tm_value_eur"),
            "sellability":     p.get("sellability_score"),
            "reason":          reason or "(manual entry)",
            "source":          "manual",
        })

    # Agency-rule auto rows
    for r in state["auto_rows"]:
        p = by_pid.get(r["player_id"], {})
        rows.append({
            "player_id":       r["player_id"],
            "name":            r["player_name"],
            "position":        p.get("position_bucket", ""),
            "current_club":    p.get("current_club", ""),
            "parent_club":     p.get("parent_club", ""),
            "league":          p.get("league_id", ""),
            "agency":          p.get("agency", ""),
            "TM_value_eur":    p.get("current_tm_value_eur"),
            "sellability":     p.get("sellability_score"),
            "reason":          r["reason"],
            "source":          r["source"],
        })

    return pd.DataFrame(rows)


# ─── Snapshot date ────────────────────────────────────────────────────────────

@st.cache_data(ttl=CACHE_TTL)
def get_snapshot_date() -> date:
    con = get_connection()
    row = con.execute("SELECT MAX(snapshot_date) FROM player_universe").fetchone()
    if row and row[0]:
        try:
            return datetime.fromisoformat(row[0]).date()
        except (TypeError, ValueError):
            pass
    return date.today()


# ─── Matches queries ──────────────────────────────────────────────────────────

# Adds display-name columns to every row using the Club Display Names tab.
# Pure pandas join: keep SQL canonical (official names), do the display swap
# in Python where the live xlsx is the source.

def _attach_display_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Add display-name columns for current_club, parent_club, buyer_club,
    and player_name. Falls back to the official name when no mapping exists."""
    import club_display as _cd
    import player_display as _pd
    club_map = _cd.load_display_map()
    player_map = _pd.load_display_map()
    if "current_club_id" in df.columns:
        df["current_club_display"] = df.apply(
            lambda r: club_map.get(int(r["current_club_id"]), r.get("current_club", ""))
            if pd.notna(r.get("current_club_id")) else r.get("current_club", ""),
            axis=1,
        )
    if "parent_club_id" in df.columns:
        df["parent_club_display"] = df.apply(
            lambda r: club_map.get(int(r["parent_club_id"]), r.get("parent_club", ""))
            if pd.notna(r.get("parent_club_id")) else r.get("parent_club", ""),
            axis=1,
        )
    if "buyer_club_id" in df.columns:
        df["buyer_club_display"] = df.apply(
            lambda r: club_map.get(int(r["buyer_club_id"]), r.get("buyer_club_name", ""))
            if pd.notna(r.get("buyer_club_id")) else r.get("buyer_club_name", ""),
            axis=1,
        )
    if "player_id" in df.columns:
        df["player_name_display"] = df.apply(
            lambda r: player_map.get(int(r["player_id"]), r.get("player_name", ""))
            if pd.notna(r.get("player_id")) else r.get("player_name", ""),
            axis=1,
        )
    return df


# ─── Engine selection (fork-at-login architecture) ───────────────────────────

ENGINE_BROKERAGE = "brokerage"
ENGINE_MARKET    = "market_view"

ENGINE_LABEL = {
    ENGINE_BROKERAGE: "Brokerage Engine",
    ENGINE_MARKET:    "Market View",
}


def active_engine() -> str | None:
    """Return the engine key in session state, or None if user hasn't picked."""
    return st.session_state.get("engine_selected")


def active_engine_label() -> str:
    """Human label for the active engine. Defaults to Market View if unset."""
    return ENGINE_LABEL.get(active_engine() or ENGINE_MARKET, "Market View")


# Engine-chrome colours — Phase B (2026-06-08). Source of truth for every
# branded surface (sidebar header banner, page accent bar, active-nav highlight).
# Hexes mirror the two card borders/CTAs on `app/fork.py`.
_BROKERAGE_PRIMARY = "#b91c1c"  # brick-red — fork.py BROKERAGE_RED
_BROKERAGE_DARK    = "#7f1d1d"  # 700 — for hover / depressed states
_MARKET_PRIMARY    = "#1d4ed8"  # brand-blue — fork.py MARKET_BLUE
_MARKET_DARK       = "#1e3a8a"  # 800 — for hover / depressed states


def active_engine_colour() -> dict:
    """Return the colour palette for the active engine.

    Keys:
      primary         — the canonical engine colour (banner bg, accent bar,
                        active-nav underline)
      primary_dark    — darker variant for hover / pressed states
      text_on_primary — text colour over a primary-coloured background
    """
    if active_engine() == ENGINE_BROKERAGE:
        return {"primary": _BROKERAGE_PRIMARY,
                "primary_dark": _BROKERAGE_DARK,
                "text_on_primary": "#ffffff"}
    return {"primary": _MARKET_PRIMARY,
            "primary_dark": _MARKET_DARK,
            "text_on_primary": "#ffffff"}


def active_match_score_col() -> str:
    """Return the match-score column name to sort/rank by for the active engine.

    Brokerage Engine → `match_score`        (sellable_now cohort)
    Market View      → `market_match_score` (mandate-relevant cohort)
    """
    return "match_score" if active_engine() == ENGINE_BROKERAGE else "market_match_score"


def active_cohort_filter() -> str:
    """SQL WHERE-fragment for the active engine's player-cohort filter.

    Brokerage Engine → strict sellable_now cohort.
    Market View      → wider mandate-relevant cohort: sellable_now ∪
                       (sellability_score ≥ MANDATE_COHORT_SELLABILITY_FLOOR
                        AND player has CA),
                       AND NOT imminent_fa.
    Floor lowered 50 → 35 in Phase A.8.7.
    """
    if active_engine() == ENGINE_BROKERAGE:
        return ("pu.sellability_status = 'sellable_now' "
                "AND pu.brokerage_eligible = 1 "
                "AND COALESCE(pu.is_imminent_free_agent, 0) = 0")
    return (
        "COALESCE(pu.is_imminent_free_agent, 0) = 0 "
        "AND (pu.sellability_status = 'sellable_now' "
        "     OR (pu.sellability_score >= 35 "
        "         AND EXISTS (SELECT 1 FROM player_ratings pr2 "
        "                     WHERE pr2.tm_player_id = pu.player_id "
        "                       AND pr2.current_ability IS NOT NULL)))"
    )


@st.cache_data(ttl=CACHE_TTL)
def cohort_size_brokerage() -> int:
    """Live count: strict sellable_now + brokerage_eligible cohort.
    Phase A.8.7: universe expanded; Brokerage substrate now enforced via flag."""
    df = _read_sql("""
        SELECT COUNT(*) AS n FROM player_universe pu
        WHERE pu.sellability_status = 'sellable_now'
          AND pu.brokerage_eligible = 1
          AND COALESCE(pu.is_imminent_free_agent, 0) = 0
    """)
    return int(df["n"].iloc[0]) if not df.empty else 0


@st.cache_data(ttl=CACHE_TTL)
def cohort_size_market_view() -> int:
    """Live count: mandate-relevant cohort = sellable_now ∪ (sellability ≥ 35
    AND has CA), excluding IFAs. Floor lowered 50 → 35 in Phase A.8.7."""
    df = _read_sql("""
        SELECT COUNT(*) AS n FROM player_universe pu
        WHERE COALESCE(pu.is_imminent_free_agent, 0) = 0
          AND (
            pu.sellability_status = 'sellable_now'
            OR (pu.sellability_score >= 35
                AND EXISTS (SELECT 1 FROM player_ratings pr2
                             WHERE pr2.tm_player_id = pu.player_id
                               AND pr2.current_ability IS NOT NULL))
          )
    """)
    return int(df["n"].iloc[0]) if not df.empty else 0


# ─── Backwards-compat aliases — keep until all call sites migrate ────────────
# Used by pages that still reference the old toggle terminology.
VIEW_MARKET = "Market View"
VIEW_BROKERAGE = "Brokerage Engine"


def active_view_label() -> str:
    return active_engine_label()


_MATCHES_SQL = """
    SELECT
        m.match_id,
        m.player_id,
        m.buyer_club_id,
        m.match_score,
        m.market_match_score,
        m.player_name,
        pu.age,
        m.position_bucket,
        pu.current_club,
        pu.current_club_id,
        pu.parent_club,
        pu.parent_club_id,
        pu.on_loan,
        pu.league_id AS player_league,
        cp.league_id AS parent_league,
        pu.current_tm_value_eur,
        pu.contract_end_date,
        pu.last_fee_paid_eur,
        pu.agency,
        m.buyer_club_name,
        m.buyer_league_id,
        m.max_transfer_fee_eur,
        m.max_wage_pw_eur,
        m.sellability_score,
        m.budget_fit,
        m.demand_intensity,
        m.wage_feasibility,
        m.wage_feasibility_label,
        m.request_source,
        m.request_validated,
        m.tier_move,
        m.player_wage_pw_eur,
        m.player_ca,
        m.player_pa,
        m.club_threshold_for_request,
        m.level_fit,
        m.level_fit_multiplier,
        m.age_mult,
        m.demand_term_mult,
        m.demand_tier_label,
        m.level_market_mult,
        m.pathway_mult,
        m.pathway_label,
        m.scarcity_mult,
        m.valuation_mult,
        m.tension_mult,
        m.tension_ratio,
        m.financial_fit_mult,
        m.financial_fit_label,
        m.level_fit_gap_ca,
        m.level_fit_gap_pa,
        cp.club_median_ca AS parent_club_median_ca,
        cp.total_pressure_score AS parent_pressure_score,
        pu.contract_leveraged,
        pu.right_priced,
        pu.finished_product,
        cp.manager_change_flag,
        cp.public_must_sell_flag,
        cp.contract_leverage_score,
        cp.squad_oversupply_score,
        cp.net_spend_score
    FROM matches m
    JOIN player_universe pu ON pu.player_id = m.player_id
    LEFT JOIN club_pressure cp ON cp.club_id = pu.parent_club_id
"""


@st.cache_data(ttl=CACHE_TTL)
def get_all_matches(sort_col: str = "match_score") -> pd.DataFrame:
    """Every row in the matches table — no exclusions applied.

    `sort_col` is the column to ORDER BY (DESC NULLS LAST) — pass either
    "match_score" (Brokerage Engine, default) or "market_match_score"
    (Market View). Callers typically pass `active_match_score_col()`.
    """
    df = _read_sql(
        _MATCHES_SQL + f" ORDER BY m.{sort_col} DESC NULLS LAST, m.player_name"
    )
    return _attach_display_columns(df)


@st.cache_data(ttl=CACHE_TTL)
def get_targets(sort_col: str = "match_score") -> pd.DataFrame:
    """Targets view = all matches MINUS players Ryan flagged exclude=1.

    Sort column toggles between match_score (Brokerage) and
    market_match_score (Market View) — see `active_match_score_col()`.
    """
    excluded = get_excluded_ids()
    df = get_all_matches(sort_col=sort_col)
    if not excluded:
        return df
    return df[~df["player_id"].isin(excluded)].reset_index(drop=True)


# ─── Per-entity queries ───────────────────────────────────────────────────────

@st.cache_data(ttl=CACHE_TTL)
def get_player(player_id: int, sort_col: str = "match_score") -> dict:
    """Full player profile + all their matches, ranked by `sort_col`."""
    profile_df = _read_sql("""
        SELECT pu.*, cp.total_pressure_score, cp.league_id AS parent_league_id,
               cp.manager_change_flag, cp.public_must_sell_flag,
               cp.contract_leverage_score, cp.squad_oversupply_score, cp.net_spend_score,
               cp.top_3_likely_to_move,
               pr.current_ability   AS player_ca,
               pr.potential_ability AS player_pa,
               pr.status            AS rating_status
        FROM player_universe pu
        LEFT JOIN club_pressure  cp ON cp.club_id = pu.parent_club_id
        LEFT JOIN player_ratings pr ON pr.tm_player_id = pu.player_id
        WHERE pu.player_id = ?
    """, (player_id,))
    matches_df = _read_sql(
        _MATCHES_SQL + f" WHERE m.player_id = ? ORDER BY m.{sort_col} DESC NULLS LAST",
        (player_id,),
    )
    return {
        "profile":  profile_df.iloc[0] if len(profile_df) else None,
        "matches":  matches_df,
        "excluded": player_id in get_excluded_ids(),
    }


@st.cache_data(ttl=CACHE_TTL)
def get_club(club_id: int, sort_col: str = "match_score") -> dict:
    """Club profile (pressure components, overview), sellable assets parented there,
    and the club's buyer requests if mapped.

    Returns:
      { 'pressure':       Series (club_pressure row),
        'overview':       Series or None (map_club_overview row),
        'sellable_here':  DataFrame (sellable players whose parent_club_id == club_id),
        'requests':       DataFrame (map_club_requests + inferred_club_requests for this club),
        'matches_as_buyer': DataFrame (rows in matches where buyer_club_id == club_id) }
    """
    pressure_df = _read_sql("SELECT * FROM club_pressure WHERE club_id = ?", (club_id,))
    overview_df = _read_sql("SELECT * FROM map_club_overview WHERE club_id = ?", (club_id,))
    sellable_df = _read_sql("""
        SELECT pu.*, COALESCE((SELECT MAX(match_score) FROM matches WHERE player_id=pu.player_id), 0) best_match
        FROM player_universe pu
        WHERE pu.parent_club_id = ?
        ORDER BY pu.sellability_score DESC
    """, (club_id,))
    requests_df = _read_sql("""
        SELECT 'explicit' AS demand_layer, club_name, league, position_bucket,
               preferred_side, max_transfer_fee_eur, max_wage_pw_eur, source, validated
        FROM map_club_requests WHERE club_id = ?
        UNION ALL
        SELECT 'inferred' AS demand_layer, club_name, league, position_bucket,
               preferred_side, max_transfer_fee_eur, max_wage_pw_eur, source, validated
        FROM inferred_club_requests WHERE club_id = ?
    """, (club_id, club_id))
    buyer_matches_df = _read_sql(
        _MATCHES_SQL + f" WHERE m.buyer_club_id = ? ORDER BY m.{sort_col} DESC NULLS LAST",
        (club_id,),
    )
    return {
        "pressure":         pressure_df.iloc[0] if len(pressure_df) else None,
        "overview":         overview_df.iloc[0] if len(overview_df) else None,
        "sellable_here":    sellable_df,
        "requests":         requests_df,
        "matches_as_buyer": buyer_matches_df,
    }


@st.cache_data(ttl=CACHE_TTL)
def get_position(bucket: str, sort_col: str = "match_score",
                  engine_key: str | None = None) -> dict:
    """Position-bucket view: every cohort-active player at this bucket + every buyer request.

    `engine_key` (2026-06-09) drives the cohort filter:
      • "brokerage" → sellable_now ∩ brokerage_eligible (~5–20 per bucket)
      • "market_view" / None → wider mandate cohort (sellability ≥ 35 + has CA,
                                NOT IFA — matches `scripts/22_match_engine.py`)
    Passed as a parameter (rather than read from session state inside) so the
    `@st.cache_data` cache keys correctly disambiguate the two engines.
    """
    if engine_key == ENGINE_BROKERAGE:
        cohort_sql = ("pu.sellability_status = 'sellable_now' "
                      "AND pu.brokerage_eligible = 1 "
                      "AND COALESCE(pu.is_imminent_free_agent, 0) = 0")
    else:
        cohort_sql = (
            "COALESCE(pu.is_imminent_free_agent, 0) = 0 "
            "AND (pu.sellability_status = 'sellable_now' "
            "     OR (pu.sellability_score >= 35 "
            "         AND EXISTS (SELECT 1 FROM player_ratings pr2 "
            "                     WHERE pr2.tm_player_id = pu.player_id "
            "                       AND pr2.current_ability IS NOT NULL)))"
        )
    players_df = _read_sql(f"""
        SELECT pu.*, cp.total_pressure_score AS parent_pressure_score,
               COALESCE((SELECT MAX(match_score) FROM matches WHERE player_id=pu.player_id), 0) best_match
        FROM player_universe pu
        LEFT JOIN club_pressure cp ON cp.club_id = pu.parent_club_id
        WHERE pu.position_bucket = ?
          AND {cohort_sql}
        ORDER BY pu.sellability_score DESC
    """, (bucket,))
    requests_df = _read_sql("""
        SELECT 'explicit' AS demand_layer, club_name, league, preferred_side,
               max_transfer_fee_eur, max_wage_pw_eur, source, validated
        FROM map_club_requests WHERE position_bucket = ?
        UNION ALL
        SELECT 'inferred' AS demand_layer, club_name, league, preferred_side,
               max_transfer_fee_eur, max_wage_pw_eur, source, validated
        FROM inferred_club_requests WHERE position_bucket = ?
        ORDER BY max_transfer_fee_eur DESC
    """, (bucket, bucket))
    matches_df = _read_sql(
        _MATCHES_SQL + f" WHERE m.position_bucket = ? ORDER BY m.{sort_col} DESC NULLS LAST",
        (bucket,),
    )
    return {
        "players":  players_df,
        "requests": requests_df,
        "matches":  matches_df,
    }


@st.cache_data(ttl=CACHE_TTL)
def get_league(league_code: str, sort_col: str = "match_score",
                engine_key: str | None = None) -> dict:
    """League view: sellers in this league + buyers in this league + matches involving it.

    `engine_key` (2026-06-09): drives the cohort filter for the sellers slice:
      • "brokerage"  → sellable_now ∩ brokerage_eligible (small per-league count)
      • "market_view" → wider Market View cohort (sellability ≥ 35 ∩ has CA ∩ NOT IFA)
    Matches the same engine-aware pattern in `get_position` and the page-level
    cohort filter helpers. Passed as a parameter so `@st.cache_data` keys
    don't share state between engines.
    """
    if engine_key == ENGINE_BROKERAGE:
        cohort_sql = ("pu.sellability_status = 'sellable_now' "
                      "AND pu.brokerage_eligible = 1 "
                      "AND COALESCE(pu.is_imminent_free_agent, 0) = 0")
    else:
        cohort_sql = (
            "COALESCE(pu.is_imminent_free_agent, 0) = 0 "
            "AND EXISTS (SELECT 1 FROM player_ratings pr2 "
            "            WHERE pr2.tm_player_id = pu.player_id "
            "              AND pr2.current_ability IS NOT NULL) "
            "AND (pu.sellability_score >= 35 OR pu.sellability_status = 'sellable_now')"
        )
    sellers_df = _read_sql(f"""
        SELECT pu.*, cp.total_pressure_score,
               COALESCE((SELECT MAX(match_score) FROM matches WHERE player_id=pu.player_id), 0) best_match
        FROM player_universe pu
        LEFT JOIN club_pressure cp ON cp.club_id = pu.parent_club_id
        WHERE cp.league_id = ?
          AND {cohort_sql}
        ORDER BY pu.sellability_score DESC
    """, (league_code,))
    requests_df = _read_sql("""
        SELECT 'explicit' AS demand_layer, club_name, position_bucket, preferred_side,
               max_transfer_fee_eur, max_wage_pw_eur, source, validated
        FROM map_club_requests WHERE league = ?
        UNION ALL
        SELECT 'inferred' AS demand_layer, club_name, position_bucket, preferred_side,
               max_transfer_fee_eur, max_wage_pw_eur, source, validated
        FROM inferred_club_requests WHERE league = ?
        ORDER BY max_transfer_fee_eur DESC
    """, (league_code, league_code))
    matches_df = _read_sql(
        _MATCHES_SQL + f"""
        WHERE m.buyer_league_id = ? OR pu.league_id = ?
        ORDER BY m.{sort_col} DESC NULLS LAST
    """,
        (league_code, league_code),
    )
    return {
        "sellers":  sellers_df,
        "requests": requests_df,
        "matches":  matches_df,
    }
