"""League View — market-per-league drill-in.

Sibling to Position View. Same visual rhythm (header → KPI tiles →
commentary → supply/demand bar charts → tables), but the lens is league
not position. Built around two structural facts of our data:

  1. Supply universe spans 19 leagues (all of dcaribou + second-tier scrape).
  2. Demand coverage stops at the 10 mapped leagues (config.DEMAND_MAPPED_LEAGUES).

The page surfaces that asymmetry explicitly — non-mapped leagues get a
"supply only" badge in the header, an em-dash placeholder on the demand KPI
tile, and a stub message in place of the demand-side panels/tables.
"""

from __future__ import annotations

from datetime import datetime
import pandas as pd
import streamlit as st

import db
import labels
import components as ui

# config.DEMAND_MAPPED_LEAGUES — same filter applied across the app.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent.parent))
import config

st.set_page_config(
    page_title="Brokerage Engine",
    page_icon="🌍",
    layout="wide",
    initial_sidebar_state="expanded",
)
ui.inject_css()

# ─── Sidebar wordmark + global search (matches other pages) ──────────────────
ui.render_page_accent()
ui.render_sidebar_engine_header()
ui.render_global_search(db.get_player_search_options(), db.get_club_search_options())

# All 19 leagues, ordered top-tier-then-second-tier-then-rest-of-world for
# the selector — top picks first since those are the leagues with the most
# data depth.
ALL_LEAGUES = [
    "GB1", "ES1", "IT1", "L1", "FR1",                  # top 5 mapped
    "PO1", "NL1", "BE1", "TR1",                        # rest of European top tier
    "GB2", "FR2", "ES2", "IT2", "L2",                  # second tiers
    "DK1", "SC1", "GR1",                               # smaller European top tier
    "SA1", "MLS1",                                     # rest of world
]

# Default & resolve
default_lg = st.query_params.get("league", "GB1")
if default_lg not in ALL_LEAGUES:
    default_lg = "GB1"

# ─── League selector (compact, top-right of header area) ─────────────────────
# Selector renders inline above the title so changing league re-flows the
# whole page in place. Display names only; underlying value stays as the
# code (GB1 etc.) for URL persistence and downstream filtering.
sel_col, _spacer = st.columns([3, 5])
with sel_col:
    lg = st.selectbox(
        "Pick a league",
        ALL_LEAGUES,
        index=ALL_LEAGUES.index(default_lg),
        format_func=labels.league_name,
    )
st.query_params["league"] = lg  # shareable link

league_display = labels.league_name(lg)
country_name, country_flag = labels.country_for_league(lg)
is_demand_mapped = lg in config.DEMAND_MAPPED_LEAGUES

# ─── Header — league title + demand-mapped badge ─────────────────────────────
st.markdown(f"## {league_display} market")

# Subline — country + demand-mapped status badge
if is_demand_mapped:
    badge_html = (
        '<span style="display:inline-block; padding:3px 10px; border-radius:10px; '
        'background:#dcfce7; color:#14532d; font-weight:600; font-size:0.8rem; '
        'margin-left:4px;">demand-mapped</span>'
    )
else:
    badge_html = (
        '<span style="display:inline-block; padding:3px 10px; border-radius:10px; '
        'background:#f3f4f6; color:#374151; font-weight:600; font-size:0.8rem; '
        'margin-left:4px;">supply-only</span>'
    )
flag_str = f"{country_flag} " if country_flag else ""
st.markdown(
    f'<div style="color:#374151; font-size:0.95rem; margin-bottom:6px;">'
    f'{flag_str}{country_name} · {league_display}{badge_html}'
    f'</div>',
    unsafe_allow_html=True,
)

# ─── Pull data ───────────────────────────────────────────────────────────────
data = db.get_league(lg, sort_col=db.active_match_score_col(), engine_key=db.active_engine())
sellers_all = data["sellers"].copy()        # players whose parent_club is in this league
requests_all = data["requests"].copy()      # buyer requests from clubs in this league
matches_all = data["matches"].copy()        # any match where seller OR buyer is in this league

excluded_ids = db.get_excluded_ids()
con = db.get_connection()

# Drop matches whose buyer is in a non-demand-mapped league (consistent with
# the filter applied on Targets / All Matches / Player View / Club View /
# Position View). Sell-side rows from non-mapped leagues stay — the
# selected league is what we're scoping by.
matches_all = matches_all[
    matches_all["buyer_league_id"].isin(config.DEMAND_MAPPED_LEAGUES)
].reset_index(drop=True)


def _money(x) -> str:
    if x is None or pd.isna(x): return "—"
    x = float(x)
    if abs(x) >= 1_000_000: return f"€{x/1_000_000:.1f}m"
    if abs(x) >= 1_000:     return f"€{x/1_000:.0f}k"
    return f"€{int(x)}"


# ─── Sidebar filters ─────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("---")
    st.markdown('<div class="rvc-filter-section-label">Position</div>', unsafe_allow_html=True)
    all_positions = sorted(sellers_all["position_bucket"].dropna().unique().tolist())
    sel_positions = st.multiselect(
        "Position", options=all_positions, key=f"lv_filter_pos_{lg}",
        format_func=labels.display_bucket,
        placeholder="All positions",
        label_visibility="collapsed",
    )

    sl_lo = int(sellers_all["sellability_score"].min()) if len(sellers_all) else 0
    sl_hi = int(sellers_all["sellability_score"].max()) + 1 if len(sellers_all) else 100
    st.markdown('<div class="rvc-filter-section-label">Sellability</div>', unsafe_allow_html=True)
    sel_sell = st.slider(
        "Sellability", min_value=sl_lo, max_value=sl_hi,
        value=(sl_lo, sl_hi), step=1,
        key=f"lv_filter_sell_{lg}",
        label_visibility="collapsed",
    )

    st.markdown('<div class="rvc-filter-section-label">Kill List</div>', unsafe_allow_html=True)
    show_kill_list = st.toggle(
        "Show Kill List players",
        value=True,
        key=f"lv_show_kill_{lg}",
        help="ON: Kill List players visible in tables with the ⊘ Excluded badge. "
             "KPI tiles and commentary always exclude them from named picks.",
    )


# ─── Apply filters to working copies ─────────────────────────────────────────
sellers_view = sellers_all.copy()
if sel_positions:
    sellers_view = sellers_view[sellers_view["position_bucket"].isin(sel_positions)]
sellers_view = sellers_view[sellers_view["sellability_score"].between(sel_sell[0], sel_sell[1])]

sellers_actionable = sellers_view[~sellers_view["player_id"].isin(excluded_ids)].copy()
matches_actionable = matches_all[~matches_all["player_id"].isin(excluded_ids)].copy()

# Buyer-side requests count (only meaningful for demand-mapped leagues)
n_buyer_requests = len(requests_all) if is_demand_mapped else 0

# ─── KPI tiles ───────────────────────────────────────────────────────────────
n_sellable_actionable = len(sellers_actionable)
n_sellable_universe = len(sellers_view)


# NET ROLE tile removed post-A.8.7. The supply ÷ demand label was computed
# against the brokerage cohort but compared to the full demand UNION, which
# gave wrong reads after the cohort expansion. Re-introduce only if both
# sides of the ratio can be put on the same cohort footing.

# Top Match — highest score where a player FROM THIS LEAGUE is being sold
# (sell-side perspective). Reads from the engine-active score column so
# Market View doesn't NaN out on Market-only rows where match_score is NULL.
# Kill-List-excluded. Restricted to mapped-buyer matches (already applied).
top_match_row = None
_score_col = db.active_match_score_col()
seller_side_matches = matches_actionable[
    (matches_actionable["parent_league"] == lg)
    & matches_actionable[_score_col].notna()
]
if len(seller_side_matches):
    top_match_row = seller_side_matches.loc[seller_side_matches[_score_col].idxmax()]

if top_match_row is not None:
    top_player_name = labels.player_display_name(
        int(top_match_row["player_id"]), top_match_row["player_name"])
    top_buyer_name = labels.club_display_name(
        int(top_match_row["buyer_club_id"]) if pd.notna(top_match_row["buyer_club_id"]) else None,
        top_match_row["buyer_club_name"])
    top_match_value = ui.fmt_score_capped(top_match_row[_score_col])
    top_match_subline = f"{top_player_name} → {top_buyer_name}"
else:
    top_match_value = "—"
    top_match_subline = "No outbound matches in this window."


def _tile(label: str, value: str, *, subline: str = "", title: str = "") -> str:
    sub_html = f'<div class="rvc-tile-sub">{subline}</div>' if subline else ""
    return (
        f'<div class="rvc-tile" title="{title}">'
        f'<div class="rvc-tile-label">{label}</div>'
        f'<div class="rvc-tile-value">{value}</div>'
        f'{sub_html}'
        f'</div>'
    )


sellable_subline = (
    f"({n_sellable_universe} before Kill List)"
    if n_sellable_universe != n_sellable_actionable else ""
)
buyer_requests_value = str(n_buyer_requests) if is_demand_mapped else "—"
buyer_requests_subline = "" if is_demand_mapped else (
    '<span style="color:#6b7280;">Demand not mapped</span>'
)

tiles = [
    _tile("Sellable players", str(n_sellable_actionable),
          subline=sellable_subline,
          title="Players whose parent club is in this league and who pass the "
                "sellable filters. Excludes Kill List; subline shows the "
                "unfiltered count when Kill List removes anyone."),
    _tile("Buyer requests", buyer_requests_value,
          subline=buyer_requests_subline,
          title="Buyer requests originating from clubs in this league. "
                "Coverage limited to the 10 demand-mapped leagues; non-mapped "
                "leagues read as '—'."),
    _tile("Top match", top_match_value,
          subline=top_match_subline,
          title="Highest match score where a player from this league is being sold. "
                "Kill List excluded."),
]
st.markdown(f'<div class="rvc-tile-row">{"".join(tiles)}</div>', unsafe_allow_html=True)


# ─── Commentary panel ───────────────────────────────────────────────────────
def _build_commentary() -> str:
    """One-paragraph plain-English market summary for the league. Names
    real players and clubs (Kill List excluded). For non-mapped leagues,
    skips buyer-side content and surfaces the Stage 2 backlog note."""

    # Supply-side position concentration (top 2 buckets from sellable cohort)
    if len(sellers_actionable):
        sup_groups = (sellers_actionable.groupby("position_bucket").size()
                      .sort_values(ascending=False))
        supply_top = [(labels.display_bucket(b), int(n))
                      for b, n in sup_groups.head(2).items() if b]
    else:
        supply_top = []

    # Demand-side position concentration (only meaningful if mapped)
    demand_top: list[tuple[str, int]] = []
    if is_demand_mapped and len(requests_all):
        dem_groups = (requests_all.groupby("position_bucket").size()
                      .sort_values(ascending=False))
        demand_top = [(labels.display_bucket(b), int(n))
                      for b, n in dem_groups.head(2).items() if b]

    # Most pressured clubs in this league (top 3 by total_pressure_score)
    pressured = con.execute("""
        SELECT club_id, name, total_pressure_score
        FROM club_pressure
        WHERE league_id = ? AND total_pressure_score IS NOT NULL
        ORDER BY total_pressure_score DESC
        LIMIT 3
    """, (lg,)).fetchall()
    pressured_names = [
        labels.club_display_name(int(cid), name) for cid, name, _ in pressured
    ]

    # Top 2 named available players, Kill-List-excluded
    strongest = []
    if len(sellers_actionable):
        top2 = sellers_actionable.nlargest(2, "sellability_score")
        for _, r in top2.iterrows():
            pname = labels.player_display_name(int(r["player_id"]), r["name"])
            parent_disp = labels.club_display_name(
                int(r["parent_club_id"]) if pd.notna(r.get("parent_club_id")) else None,
                r.get("parent_club") or r.get("current_club") or "")
            sell = r.get("sellability_score") or 0
            strongest.append(f"<strong>{pname}</strong> ({parent_disp}, sellability {sell:.1f})")

    # ── Compose ────────────────────────────────────────────────────────
    if not is_demand_mapped:
        # Supply-only narrative
        parts = []
        parts.append(
            f"{league_display} is supply-only in our current coverage — "
            f"{n_sellable_actionable} sellable players surface from this league."
        )
        if supply_top:
            supply_str = ", ".join([f"{name} ({n})" for name, n in supply_top])
            parts.append(f"Supply concentrates at {supply_str}.")
        if pressured_names:
            parts.append(
                f"Selling pressure sits heaviest at {', '.join(pressured_names)}."
            )
        if strongest:
            parts.append("Strongest available: " + " and ".join(strongest) + ".")
        parts.append(
            "Sell-side coverage only — demand mapping for this league is on the "
            "Stage 2 backlog."
        )
        return " ".join(parts)

    # Mapped-league narrative — full structure
    parts = []
    parts.append(
        f"{league_display}: {n_sellable_actionable} sellable players against "
        f"{n_buyer_requests} buyer requests from {league_display} clubs."
    )

    # Sentence 2 — supply + demand position concentration
    s2_bits = []
    if supply_top:
        sup_str = ", ".join([f"{name} ({n})" for name, n in supply_top])
        s2_bits.append(f"Supply concentrates at {sup_str}")
    if demand_top:
        dem_str = ", ".join([f"{name} ({n})" for name, n in demand_top])
        s2_bits.append(f"buyer requests skew toward {dem_str}")
    if s2_bits:
        parts.append("; ".join(s2_bits) + ".")

    # Sentence 3 — most pressured selling clubs
    if pressured_names:
        parts.append(
            f"Selling pressure sits heaviest at {', '.join(pressured_names)}."
        )

    # Sentence 4 — strongest available
    if strongest:
        parts.append("Strongest available: " + " and ".join(strongest) + ".")

    return " ".join(parts)


commentary_html = _build_commentary()
st.markdown(
    f'<div style="background:#f9fafb; border:1px solid #e5e7eb; border-radius:8px; '
    f'padding:16px 18px; margin:18px 0 6px 0; line-height:1.6; color:#374151; '
    f'font-size:0.95rem;">{commentary_html}</div>',
    unsafe_allow_html=True,
)


# ─── Supply / Demand by position bar charts (mirror Position View) ──────────
def _render_bar_chart(title: str, pairs: list[tuple[str, int]], unit_label: str,
                      empty_message: str | None = None) -> str:
    if not pairs:
        msg = empty_message or "No data."
        return (
            f'<div class="rvc-mini-chart">'
            f'<div class="rvc-mini-chart-title">{title}</div>'
            f'<div style="color:#6b7280; font-size:0.85rem; padding:8px 0; '
            f'line-height:1.45;">{msg}</div>'
            f'</div>'
        )
    max_n = max(n for _, n in pairs) or 1
    rows = []
    for name, n in pairs:
        width_pct = (n / max_n) * 100
        rows.append(
            f'<div class="rvc-bar-row">'
            f'  <div class="rvc-bar-label">{name}</div>'
            f'  <div class="rvc-bar-track">'
            f'    <div class="rvc-bar-fill" style="width:{width_pct:.1f}%;"></div>'
            f'  </div>'
            f'  <div class="rvc-bar-count">{n}</div>'
            f'</div>'
        )
    return (
        f'<div class="rvc-mini-chart">'
        f'<div class="rvc-mini-chart-title">{title}</div>'
        + "".join(rows) +
        f'<div class="rvc-bar-foot">{unit_label}</div>'
        f'</div>'
    )


# Reuse the same CSS class names defined on Position View — both pages
# inject inline so it's safe to declare again here.
st.markdown(
    """
    <style>
    .rvc-charts-row { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin: 6px 0 18px 0; }
    .rvc-mini-chart { background: #ffffff; border: 1px solid #e5e7eb; border-radius: 8px; padding: 14px 18px; }
    .rvc-mini-chart-title { font-size: 0.78rem; font-weight: 700; color: #6B7280;
        text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 10px; }
    .rvc-bar-row { display: grid; grid-template-columns: 130px 1fr 36px;
        gap: 8px; align-items: center; padding: 3px 0; font-size: 0.85rem; color: #374151; }
    .rvc-bar-label { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .rvc-bar-track { background: #f3f4f6; border-radius: 4px; height: 16px; overflow: hidden; }
    .rvc-bar-fill { background: #1F3864; height: 100%; border-radius: 4px; min-width: 2px; }
    .rvc-bar-count { font-weight: 600; color: #111827; text-align: right; font-size: 0.85rem; }
    .rvc-bar-foot { color: #9ca3af; font-size: 0.72rem; margin-top: 8px; }
    </style>
    """,
    unsafe_allow_html=True,
)

# Supply by position (top 6 buckets, Kill-List-excluded cohort)
if len(sellers_actionable):
    sup_groups = (sellers_actionable.groupby("position_bucket").size()
                  .sort_values(ascending=False))
    _supply_top6 = [(labels.display_bucket(b), int(n))
                    for b, n in sup_groups.head(6).items() if b]
    _supply_extra = len(sup_groups) - len(_supply_top6)
    if _supply_extra > 0:
        _supply_top6.append((f"+ {_supply_extra} more positions", 0))
else:
    _supply_top6 = []

# Demand by position (top 6 buckets, mapped-only)
_demand_top6: list[tuple[str, int]] = []
demand_empty_msg = None
if is_demand_mapped and len(requests_all):
    dem_groups = (requests_all.groupby("position_bucket").size()
                  .sort_values(ascending=False))
    _demand_top6 = [(labels.display_bucket(b), int(n))
                    for b, n in dem_groups.head(6).items() if b]
    _demand_extra = len(dem_groups) - len(_demand_top6)
    if _demand_extra > 0:
        _demand_top6.append((f"+ {_demand_extra} more positions", 0))
elif not is_demand_mapped:
    demand_empty_msg = (
        "Demand for this league not mapped in current build. Stage 2 will extend "
        "market maps to second-tier and rest-of-world leagues."
    )

st.markdown(
    '<div class="rvc-charts-row">'
    + _render_bar_chart(
        "Where the supply sits", _supply_top6,
        "players available per position",
    )
    + _render_bar_chart(
        "Where the demand sits", _demand_top6,
        "buyer requests per position",
        empty_message=demand_empty_msg,
    )
    + '</div>',
    unsafe_allow_html=True,
)

# ─── Helpers for tables ──────────────────────────────────────────────────────
def _player_cell(row: pd.Series) -> str:
    """Player name as same-tab anchor + ⊘ Excluded badge on Kill List rows."""
    pid = int(row["player_id"])
    display_name = labels.player_display_name(
        pid, row.get("name") or row.get("player_name") or "")
    anchor = ui.player_link(pid, display_name)
    if pid in excluded_ids:
        anchor += ' <span class="rvc-excluded-badge" title="Excluded from Targets">⊘ Excluded</span>'
    return anchor


# ─── Sellable players in this league (table) ─────────────────────────────────
sellers_for_table = sellers_view.copy()
if not show_kill_list:
    sellers_for_table = sellers_for_table[~sellers_for_table["player_id"].isin(excluded_ids)]

st.markdown(
    f"### Sellable players from {league_display} "
    f"({len(sellers_for_table)}) {ui.level_fit_info_icon()}",
    unsafe_allow_html=True,
)
if not len(sellers_for_table):
    st.info("No sellable players from this league match the current filters.")
else:
    sdf = sellers_for_table.copy()
    sdf["parent_club_display"] = sdf.apply(
        lambda r: labels.club_display_name(
            int(r["parent_club_id"]) if pd.notna(r.get("parent_club_id")) else None,
            r.get("parent_club")),
        axis=1,
    )
    sdf["mv_str"] = sdf["current_tm_value_eur"].apply(_money)
    sdf["Player_html"] = sdf.apply(_player_cell, axis=1)
    sdf["ParentClub_html"] = sdf.apply(
        lambda r: ui.club_link(
            int(r["parent_club_id"]) if pd.notna(r.get("parent_club_id")) else None,
            str(r["parent_club_display"])),
        axis=1,
    )
    sdf = sdf.sort_values("sellability_score", ascending=False).reset_index(drop=True)

    SHOW_LIMIT_S = 15
    show_all_s_key = f"lv_show_all_sellers_{lg}"
    show_all_s = st.session_state.get(show_all_s_key, False)
    table_s = sdf if (show_all_s or len(sdf) <= SHOW_LIMIT_S) else sdf.head(SHOW_LIMIT_S)
    table_s = table_s.reset_index(drop=True)

    _excluded_mask_s = table_s["player_id"].isin(excluded_ids).tolist()

    # Sci Sports CA / PA + Level fit (vs top buyer) — same lookup pattern as
    # Position View. One DB hit per visible player; visible list capped at 15
    # by default so cost is bounded.
    def _ca_str(pid: int) -> str:
        r = con.execute(
            "SELECT current_ability FROM player_ratings WHERE tm_player_id=? "
            "AND current_ability IS NOT NULL", (int(pid),)).fetchone()
        return f"{r[0]:.0f}" if r else "—"

    def _pa_str(pid: int) -> str:
        r = con.execute(
            "SELECT potential_ability FROM player_ratings WHERE tm_player_id=? "
            "AND potential_ability IS NOT NULL", (int(pid),)).fetchone()
        return f"{r[0]:.0f}" if r else "—"

    def _top_lf(pid: int) -> str | None:
        r = con.execute(
            f"SELECT level_fit FROM matches WHERE player_id=? "
            f"AND {_score_col} IS NOT NULL "
            f"ORDER BY {_score_col} DESC LIMIT 1", (int(pid),)).fetchone()
        return r[0] if r else None

    table_s = table_s.assign(
        ca_str=table_s["player_id"].apply(_ca_str),
        pa_str=table_s["player_id"].apply(_pa_str),
        level_fit_pill=table_s["player_id"].apply(lambda pid: ui.level_fit_pill(_top_lf(pid))),
    )

    show_df = table_s[[
        "Player_html", "position_bucket", "age", "mv_str",
        "ca_str", "pa_str", "level_fit_pill",
        "sellability_score", "best_match", "ParentClub_html",
    ]].rename(columns={
        "Player_html":       "Player",
        "position_bucket":   "Position",
        "age":               "Age",
        "mv_str":            "TM value",
        "ca_str":            "CA",
        "pa_str":            "PA",
        "level_fit_pill":    "Level fit (top buyer)",
        "sellability_score": "Sellability",
        "best_match":        "Best match",
        "ParentClub_html":   "Parent club",
    })
    show_df["Position"]    = show_df["Position"].apply(labels.display_bucket)
    show_df["Sellability"] = show_df["Sellability"].apply(ui.fmt_score_capped)
    show_df["Best match"]  = show_df["Best match"].apply(ui.fmt_score_capped)

    def _highlight_excluded(row):
        idx = int(row.name)
        return ['background-color: #FEF2F2;' if (_excluded_mask_s[idx] if 0 <= idx < len(_excluded_mask_s) else False) else '' for _ in row]

    def _bg_sell(v):
        try: return ui.heatmap_gradient(v, 0, 100)
        except (TypeError, ValueError): return ""
    def _bg_match(v):
        try: return ui.heatmap_gradient(v, 0, 100)
        except (TypeError, ValueError): return ""

    styled = (
        show_df.style
        .apply(_highlight_excluded, axis=1)
        .map(_bg_sell,  subset=["Sellability"])
        .map(_bg_match, subset=["Best match"])
    )
    ui.render_html_table(styled, max_height_px=min(620, 60 + 38 * len(show_df)))

    if len(sdf) > SHOW_LIMIT_S and not show_all_s:
        if st.button(f"Show all {len(sdf)} sellable players", key=f"lv_toggle_s_{lg}"):
            st.session_state[show_all_s_key] = True
            st.rerun()
    elif show_all_s and len(sdf) > SHOW_LIMIT_S:
        if st.button("Collapse to top 15", key=f"lv_toggle_s_collapse_{lg}"):
            st.session_state[show_all_s_key] = False
            st.rerun()


# ─── Most pressured selling clubs in this league (table) ─────────────────────
# Every club in the league, top 5 visible + expander for the rest. Even clubs
# with zero sellable players in our cohort are shown — they still have a
# pressure score and that signal matters for triangulation.
pressured_rows = con.execute("""
    SELECT cp.club_id, cp.name AS official_name, cp.total_pressure_score,
           (SELECT COUNT(*) FROM player_universe pu
            WHERE pu.parent_club_id = cp.club_id
              AND pu.sellability_status = 'sellable_now'
           ) AS n_sellable
    FROM club_pressure cp
    WHERE cp.league_id = ?
    ORDER BY cp.total_pressure_score DESC NULLS LAST
""", (lg,)).fetchall()

st.markdown("### Most pressured selling clubs")
if not pressured_rows:
    st.info(f"No clubs with pressure scores in {league_display}.")
else:
    p_df = pd.DataFrame(pressured_rows, columns=[
        "club_id", "official_name", "total_pressure_score", "n_sellable",
    ])
    p_df["club_display"] = p_df.apply(
        lambda r: labels.club_display_name(
            int(r["club_id"]) if pd.notna(r["club_id"]) else None, r["official_name"]),
        axis=1,
    )
    p_df["Club_html"] = p_df.apply(
        lambda r: ui.club_link(
            int(r["club_id"]) if pd.notna(r["club_id"]) else None,
            str(r["club_display"])),
        axis=1,
    )

    # Top sellable player per club (Kill-List-included so the table reflects
    # the underlying universe; clickable anchor goes to Player View where the
    # Kill List banner shows the status)
    top_player_per_club: dict[str, str] = {}
    if len(sellers_all):
        for cid in p_df["club_id"]:
            club_players = sellers_all[sellers_all["parent_club_id"] == cid]
            if not len(club_players):
                continue
            best = club_players.nlargest(1, "sellability_score").iloc[0]
            pid = int(best["player_id"])
            pname = labels.player_display_name(pid, best["name"])
            top_player_per_club[cid] = ui.player_link(pid, pname)
    p_df["TopPlayer_html"] = p_df["club_id"].apply(
        lambda cid: top_player_per_club.get(cid, "—")
    )

    pressured_show = p_df[[
        "Club_html", "total_pressure_score", "n_sellable", "TopPlayer_html",
    ]].rename(columns={
        "Club_html":             "Club",
        "total_pressure_score":  "Selling pressure",
        "n_sellable":            "Sellable players",
        "TopPlayer_html":        "Top sellable player",
    })
    pressured_show["Selling pressure"] = pressured_show["Selling pressure"].apply(
        lambda v: f"{v:.1f}" if pd.notna(v) else "—"
    )

    # Stretched gradient — vmin/vmax computed from actual league spread so
    # the heat-map reads at a glance instead of all rows looking the same
    # shade. Use the league's pressure range with a small floor cushion so
    # the lowest value still gets some colour, not pure white.
    _pressure_vals = [
        float(r[2]) for r in pressured_rows if r[2] is not None
    ]
    if _pressure_vals:
        _p_min = max(0.0, min(_pressure_vals) - 2)
        _p_max = max(_pressure_vals) + 2
    else:
        _p_min, _p_max = 0.0, 100.0

    def _bg_pressure(v):
        try: return ui.heatmap_gradient(v, 0, 100)
        except (TypeError, ValueError): return ""

    # Split into top-5 (rendered immediately) + remainder (inside expander)
    n_total = len(pressured_show)
    TOP_N = 5
    top_view = pressured_show.head(TOP_N).reset_index(drop=True)
    rest_view = pressured_show.iloc[TOP_N:].reset_index(drop=True)

    styled_top = top_view.style.map(_bg_pressure, subset=["Selling pressure"])
    ui.render_html_table(styled_top, max_height_px=60 + 38 * len(top_view))

    if len(rest_view):
        with st.expander(f"Show all {n_total} clubs in {league_display}"):
            styled_rest = rest_view.style.map(_bg_pressure, subset=["Selling pressure"])
            ui.render_html_table(styled_rest, max_height_px=min(640, 60 + 38 * len(rest_view)))


# ─── Buyer requests from this league — redesigned ───────────────────────────
# Replaces the old flat 123-row table with a grouped, collapsible view per
# buying club. Adds match-highlighting on interest names that appear in our
# sellable cohort (the high-signal brokerage opportunities) and a
# top-of-section callout summarising matched count.
st.markdown(
    f"### Buyer requests from clubs in {league_display} {ui.level_fit_info_icon()}",
    unsafe_allow_html=True,
)
if not is_demand_mapped:
    st.info(
        "Demand for this league not mapped in current build. Stage 2 will extend "
        "market maps to second-tier and rest-of-world leagues."
    )
elif not len(requests_all):
    st.info(f"No buyer requests recorded from {league_display} clubs.")
else:
    # Pull club_id + linked_shortlisted_player for explicit rows.
    raw_rows = con.execute("""
        SELECT club_id, club_name, position_bucket, preferred_side,
               max_transfer_fee_eur, linked_shortlisted_player
        FROM map_club_requests WHERE league = ?
    """, (lg,)).fetchall()
    if not raw_rows:
        st.info(f"No explicit buyer requests recorded from {league_display} clubs.")
    else:
        # Build the sellable-cohort match index (cached lookup of normalised
        # player names + surnames). Used for highlighting which interest
        # names intersect with our sellable players.
        _full_lookup, _surname_lookup = ui.build_player_match_index(con)

        # (player_id, buyer_club_id) → best-level-fit details. Used to colour
        # the interest dots in buyer cards + annotate the matched-pairs
        # expander with CA + threshold + ✓/↗. If a (player, club) pair has
        # multiple matches (different positions), take the strongest level_fit.
        _LF_RANK = {"ON_LEVEL": 0, "UPSIDE": 1, "BELOW": 2, "UNRATED": 3}
        _level_fit_lookup: dict[tuple[int, str], dict] = {}
        for r in con.execute(
            "SELECT player_id, buyer_club_id, level_fit, player_ca, "
            "club_threshold_for_request FROM matches"
        ).fetchall():
            pid_i, bcid_s, lf, p_ca, thr = int(r[0]), str(r[1]), r[2], r[3], r[4]
            key = (pid_i, bcid_s)
            existing = _level_fit_lookup.get(key)
            if existing is None or _LF_RANK.get(lf, 9) < _LF_RANK.get(existing["level_fit"], 9):
                _level_fit_lookup[key] = {
                    "level_fit":      lf,
                    "player_ca":      p_ca,
                    "club_threshold": thr,
                }

        # Aggregate raw rows into one entry per buying club.
        clubs_agg: dict[int, dict] = {}
        all_matched_pairs: list[dict] = []

        for cid, cname, pos, side, budget, linked in raw_rows:
            try:
                cid_int = int(cid)
            except (TypeError, ValueError):
                continue
            interests_raw = (linked or "").strip()
            interest_names = [i.strip() for i in interests_raw.split("/") if i.strip()]

            interests = []
            for name in interest_names:
                m = ui.match_interest_name(name, _full_lookup, _surname_lookup)
                interests.append({"name": name, "match": m})
                if m:
                    all_matched_pairs.append({
                        "player_id":         m[0],
                        "player_display":    m[1],
                        "buying_club_id":    cid_int,
                        "buying_club_name":  cname,
                        "position_bucket":   pos,
                    })

            entry = clubs_agg.setdefault(cid_int, {
                "club_id":           cid_int,
                "club_name":         cname,
                "max_budget":        0.0,
                "positions":         [],
                "total_interests":   0,
                "n_open_positions":  0,
            })
            entry["max_budget"]      = max(entry["max_budget"], float(budget) if budget else 0.0)
            entry["total_interests"] += len(interests)
            entry["positions"].append({
                "position":  pos,
                "side":      side,
                "interests": interests,
            })
        # Distinct positions per club (one club can request the same position
        # twice with different sides — count distinct buckets, not rows).
        for entry in clubs_agg.values():
            entry["n_open_positions"] = len({p["position"] for p in entry["positions"]})

        # ── Top-of-section callout: matched-opportunities count ────────────
        total_interests = sum(c["total_interests"] for c in clubs_agg.values())
        n_matched = len(all_matched_pairs)
        st.markdown(
            f'<div style="background:#eff6ff; border:1px solid #bfdbfe; '
            f'border-radius:8px; padding:12px 16px; margin:8px 0 14px 0; '
            f'color:#1e3a8a; font-size:0.95rem;">'
            f'Of <strong>{total_interests}</strong> player interests across '
            f'{league_display} buyer requests, '
            f'<strong>{n_matched}</strong> match players in our sellable cohort.'
            f'</div>',
            unsafe_allow_html=True,
        )

        # Expander revealing the matched pairs only — high-signal shortlist.
        # Grouped by buying club alphabetically (cleaned display names);
        # players sorted alphabetically within each club group. Easier to
        # navigate to "what does Arsenal want?" than scanning a flat list.
        if n_matched:
            # st.expander labels are plain text — can't embed the info-icon
            # in the title itself, so we render the icon as a small adjacent
            # caption above. Clicking it expands the level-fit explanation
            # without disturbing the matched-pairs expander state.
            st.markdown(
                f'<div style="margin:2px 0 6px 0; font-size:0.85rem; '
                f'color:#6b7280;">Coloured dots on each line below indicate '
                f'level fit — {ui.level_fit_info_icon()}</div>',
                unsafe_allow_html=True,
            )
            with st.expander(f"Show {n_matched} matched opportunities"):
                # Bucket matched pairs by buying club. Dedup (player_id,
                # club_id, position) tuples — same player can appear in
                # multiple position requests at the same club.
                by_club: dict[int, dict] = {}
                seen_pairs: set[tuple[int, int, str]] = set()
                for m in all_matched_pairs:
                    key = (m["player_id"], m["buying_club_id"], m["position_bucket"])
                    if key in seen_pairs:
                        continue
                    seen_pairs.add(key)
                    cid = m["buying_club_id"]
                    cdisp = labels.club_display_name(cid, m["buying_club_name"])
                    bucket = by_club.setdefault(cid, {
                        "club_display": cdisp,
                        "club_name":    m["buying_club_name"],
                        "pairs":        [],
                    })
                    bucket["pairs"].append({
                        "player_id":      m["player_id"],
                        "player_display": m["player_display"],
                        "position":       m["position_bucket"],
                    })

                # Sort clubs alphabetically by cleaned display name (case-insensitive)
                for cid in sorted(by_club, key=lambda c: by_club[c]["club_display"].lower()):
                    bucket = by_club[cid]
                    cdisp = bucket["club_display"]
                    buyer_html = ui.club_link(cid, cdisp)
                    st.markdown(
                        f'<div style="font-weight:700; color:#111827; '
                        f'margin:10px 0 4px 0; font-size:0.95rem;">{buyer_html}</div>',
                        unsafe_allow_html=True,
                    )
                    # Sort players within the club alphabetically by display name
                    for pair in sorted(bucket["pairs"],
                                       key=lambda p: p["player_display"].lower()):
                        player_html = ui.player_link(pair["player_id"], pair["player_display"])
                        pos_disp = labels.display_bucket(pair["position"])
                        lf_info = _level_fit_lookup.get((pair["player_id"], str(cid)), {})
                        lf = lf_info.get("level_fit")
                        p_ca = lf_info.get("player_ca")
                        thr = lf_info.get("club_threshold")
                        ca_str = f"CA {float(p_ca):.0f}" if p_ca is not None else ""
                        thr_str = f"threshold {float(thr):.0f}" if thr is not None else ""
                        context = " · ".join(s for s in (ca_str, thr_str) if s)
                        context_html = (f' <span style="color:#6b7280;">'
                                        f'· {context}</span>') if context else ""
                        st.markdown(
                            f'<div style="padding:2px 0 2px 16px; font-size:0.9rem; '
                            f'color:#374151;">'
                            f'{ui.level_fit_dot(lf)} {player_html} '
                            f'<span style="color:#6b7280;">({pos_disp})</span>'
                            f'{context_html}'
                            f'</div>',
                            unsafe_allow_html=True,
                        )

        # ── Per-club collapsible cards, sorted ALPHABETICALLY by club ─────
        # display name (cleaned). Total-interests counts still appear in the
        # card header so the user can spot Newcastle 49 vs Bournemouth 4
        # without needing a sorted-by-interest order. Alphabetic lets users
        # jump straight to a specific club.
        sorted_clubs = sorted(
            clubs_agg.values(),
            key=lambda c: labels.club_display_name(c["club_id"], c["club_name"]).lower(),
        )
        for club in sorted_clubs:
            club_disp = labels.club_display_name(club["club_id"], club["club_name"])
            budget_str = _money(club["max_budget"]) if club["max_budget"] else "—"
            title = (
                f"{club_disp}  ·  {budget_str} budget  ·  "
                f"{club['n_open_positions']} open position"
                f"{'s' if club['n_open_positions'] != 1 else ''}  ·  "
                f"{club['total_interests']} player interest"
                f"{'s' if club['total_interests'] != 1 else ''}"
            )
            with st.expander(title):
                # Group by position bucket — collapse multiple side-requests
                # for the same position into one block (typical pattern: a
                # club lists LB and RB as separate requests but with the same
                # interest list, so consolidate).
                by_pos: dict[str, dict] = {}
                for p in club["positions"]:
                    pos_key = p["position"]
                    entry = by_pos.setdefault(pos_key, {"sides": [], "interests_seen": set(), "interests_ordered": []})
                    side_val = (p["side"] or "").strip()
                    if side_val and side_val not in entry["sides"]:
                        entry["sides"].append(side_val)
                    for interest in p["interests"]:
                        key = interest["name"].lower()
                        if key in entry["interests_seen"]:
                            continue
                        entry["interests_seen"].add(key)
                        entry["interests_ordered"].append(interest)

                for pos_key, body in by_pos.items():
                    pos_disp = labels.display_bucket(pos_key)
                    sides_str = "/".join(s.lower() for s in body["sides"]) if body["sides"] else "any"
                    n_targets = len(body["interests_ordered"])
                    st.markdown(
                        f'<div style="margin:8px 0 4px 0; font-size:0.95rem; '
                        f'color:#111827;"><strong>{pos_disp}</strong> '
                        f'<span style="color:#6b7280; font-weight:400;">'
                        f'({sides_str} footed, {n_targets} '
                        f'target{"s" if n_targets != 1 else ""})</span>:</div>',
                        unsafe_allow_html=True,
                    )
                    parts = []
                    for interest in body["interests_ordered"]:
                        if interest["match"]:
                            pid, pdisp = interest["match"]
                            # Coloured dot reflects level fit at THIS buying
                            # club. Tooltip shows CA + threshold for context.
                            lf_info = _level_fit_lookup.get((pid, str(club["club_id"])), {})
                            lf = lf_info.get("level_fit")
                            p_ca = lf_info.get("player_ca")
                            thr = lf_info.get("club_threshold")
                            tooltip = ""
                            if p_ca is not None and thr is not None:
                                tooltip = f' title="CA {float(p_ca):.0f} vs threshold {float(thr):.0f}"'
                            parts.append(
                                f'<span style="white-space:nowrap;"{tooltip}>'
                                f'{ui.level_fit_dot(lf)} '
                                f'<a href="{ui.with_auth(f"/player_view?player_id={pid}")}" target="_self" '
                                f'style="color:#1F3864; font-weight:700; '
                                f'text-decoration:none;">{pdisp}</a>'
                                f'</span>'
                            )
                        else:
                            parts.append(
                                f'<span style="color:#374151;">{interest["name"]}</span>'
                            )
                    st.markdown(
                        f'<div style="font-size:0.9rem; line-height:1.8; '
                        f'padding-left:8px;">' + ' · '.join(parts) + '</div>',
                        unsafe_allow_html=True,
                    )
