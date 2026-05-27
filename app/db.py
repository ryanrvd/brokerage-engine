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
    """All sellable players with both display and official names — used by the
    sidebar global-search dropdown to find by either form."""
    rows = _read_sql("""
        SELECT pu.player_id, pu.name AS official_name, pu.current_club, pu.league_id
        FROM player_universe pu
        WHERE pu.sellability_status = 'sellable_now'
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


_MATCHES_SQL = """
    SELECT
        m.match_id,
        m.player_id,
        m.buyer_club_id,
        m.match_score,
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
def get_all_matches() -> pd.DataFrame:
    """Every row in the matches table — no exclusions applied. Used by Sheet 1 mirror /
    All Matches page (Day 7). Includes *_display columns from Club Display Names."""
    df = _read_sql(_MATCHES_SQL + " ORDER BY m.match_score DESC, m.player_name")
    return _attach_display_columns(df)


@st.cache_data(ttl=CACHE_TTL)
def get_targets() -> pd.DataFrame:
    """Targets view = all matches MINUS players Ryan flagged exclude=1 in
    data/player_overrides.xlsx. This is the headline view on the Home page."""
    excluded = get_excluded_ids()
    df = get_all_matches()
    if not excluded:
        return df
    return df[~df["player_id"].isin(excluded)].reset_index(drop=True)


# ─── Per-entity queries ───────────────────────────────────────────────────────

@st.cache_data(ttl=CACHE_TTL)
def get_player(player_id: int) -> dict:
    """Full player profile + all their matches.

    Returns a dict:
      { 'profile':  Series (single row from player_universe + parent pressure),
        'matches':  DataFrame (this player's matches, sorted by score DESC),
        'excluded': bool (whether the player is in the overrides exclude list) }
    """
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
    matches_df = _read_sql(_MATCHES_SQL + " WHERE m.player_id = ? ORDER BY m.match_score DESC",
                            (player_id,))
    return {
        "profile":  profile_df.iloc[0] if len(profile_df) else None,
        "matches":  matches_df,
        "excluded": player_id in get_excluded_ids(),
    }


@st.cache_data(ttl=CACHE_TTL)
def get_club(club_id: int) -> dict:
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
    buyer_matches_df = _read_sql(_MATCHES_SQL + " WHERE m.buyer_club_id = ? ORDER BY m.match_score DESC",
                                  (club_id,))
    return {
        "pressure":         pressure_df.iloc[0] if len(pressure_df) else None,
        "overview":         overview_df.iloc[0] if len(overview_df) else None,
        "sellable_here":    sellable_df,
        "requests":         requests_df,
        "matches_as_buyer": buyer_matches_df,
    }


@st.cache_data(ttl=CACHE_TTL)
def get_position(bucket: str) -> dict:
    """Position-bucket view: every sellable player at this bucket + every buyer request."""
    players_df = _read_sql("""
        SELECT pu.*, cp.total_pressure_score AS parent_pressure_score,
               COALESCE((SELECT MAX(match_score) FROM matches WHERE player_id=pu.player_id), 0) best_match
        FROM player_universe pu
        LEFT JOIN club_pressure cp ON cp.club_id = pu.parent_club_id
        WHERE pu.position_bucket = ?
          AND pu.sellability_status = 'sellable_now'
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
    matches_df = _read_sql(_MATCHES_SQL + " WHERE m.position_bucket = ? ORDER BY m.match_score DESC",
                            (bucket,))
    return {
        "players":  players_df,
        "requests": requests_df,
        "matches":  matches_df,
    }


@st.cache_data(ttl=CACHE_TTL)
def get_league(league_code: str) -> dict:
    """League view: sellers in this league + buyers in this league + matches involving it."""
    sellers_df = _read_sql("""
        SELECT pu.*, cp.total_pressure_score,
               COALESCE((SELECT MAX(match_score) FROM matches WHERE player_id=pu.player_id), 0) best_match
        FROM player_universe pu
        LEFT JOIN club_pressure cp ON cp.club_id = pu.parent_club_id
        WHERE cp.league_id = ?
          AND pu.sellability_status = 'sellable_now'
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
    matches_df = _read_sql(_MATCHES_SQL + """
        WHERE m.buyer_league_id = ? OR pu.league_id = ?
        ORDER BY m.match_score DESC
    """, (league_code, league_code))
    return {
        "sellers":  sellers_df,
        "requests": requests_df,
        "matches":  matches_df,
    }
