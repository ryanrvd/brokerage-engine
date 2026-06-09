"""Home page — Targets view.

Active brokerage matches across (player, buyer) pairs, minus everything on the
Kill List. Click a player → Player View. Click a club → Club View.

Day 7 polish: sidebar reduced to wordmark + global search; filter widgets
moved into a row beneath the table column headers; KPI tiles refined.
"""

from __future__ import annotations

import math
import pandas as pd
import streamlit as st

import db
import components as ui
import labels

# config.DEMAND_MAPPED_LEAGUES — buyer-side restriction applied below.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent.parent))
import config

st.set_page_config(
    page_title="Brokerage Engine",
    page_icon="⚽",
    layout="wide",
    initial_sidebar_state="expanded",
)
ui.inject_css()

# ─── Sidebar — wordmark first, then global search ────────────────────────────
ui.render_page_accent()
ui.render_sidebar_engine_header()
# render_global_search prepends "---" inside the sidebar, giving a clean
# divider between the wordmark and the search.
ui.render_global_search(db.get_player_search_options(), db.get_club_search_options())

# ─── Load & shape ─────────────────────────────────────────────────────────────

df = db.get_targets(sort_col=db.active_match_score_col())
# Sidebar toggle picks the active scoring column for sort/filter logic.
# Both columns remain in the DataFrame; the table still renders both side-by-side.
_SCORE_COL = db.active_match_score_col()

# Demand-side restriction: only show matches whose buyer is in one of the
# 10 leagues we trust for demand signal. Non-mapped leagues (MLS, Saudi,
# Turkish, Greek, Scottish, Danish, non-FR second tiers) are dropped here.
df = df[df["buyer_league_id"].isin(config.DEMAND_MAPPED_LEAGUES)].reset_index(drop=True)
snapshot = db.get_snapshot_date()

df["contract_years_remaining"] = df["contract_end_date"].apply(
    lambda ce: ui.contract_years_remaining(ce, snapshot)
)
df["current_club_display"]  = df["current_club_display"].fillna(df["current_club"])
df["parent_club_display"]   = df["parent_club_display"].fillna(df["parent_club"])
df["buyer_club_display"]    = df["buyer_club_display"].fillna(df["buyer_club_name"])
df["player_name_display"]   = df["player_name_display"].fillna(df["player_name"])
df["super_bucket"] = df["position_bucket"].apply(labels.super_bucket)
df["parent_club_with_league"] = df.apply(
    lambda r: f"{r['parent_club_display']} · {labels.league_name(r['parent_league'])}"
    if pd.notna(r["parent_club"]) and r["parent_league"]
    else (r["parent_club_display"] or ""),
    axis=1,
)

# ─── Filter-state defaults (derived from df) ─────────────────────────────────
_all_buckets   = labels.TIER_ORDER
_all_positions = sorted(df["position_bucket"].dropna().unique().tolist())
_all_leagues   = sorted(df["parent_league"].dropna().unique().tolist())
_mv_lo = int(df["current_tm_value_eur"].min() / 1e6)
_mv_hi = int(df["current_tm_value_eur"].max() / 1e6) + 1
_ms_lo = int(df[_SCORE_COL].min())
_ms_hi = int(df[_SCORE_COL].max()) + 1
_sl_lo = int(df["sellability_score"].min())
_sl_hi = int(df["sellability_score"].max()) + 1

# Seed session_state defaults (only on first render — Streamlit keeps the
# user's choices across reruns). Doing it here lets the widgets below set
# `key=` only — no `value=` / `default=` collision with session_state.
# Multi-select / text-input keys use setdefault (Streamlit treats unset
# multi-selects as empty, which would mean "show nothing" — wrong default).
# Range-slider keys hold tuples; Streamlit's slider needs `value=(lo, hi)`
# to infer range mode, so those are read via .get() with a tuple fallback
# instead.
_DEFAULTS = {
    "filter_player":        "",
    "filter_current_club":  "",
    "filter_buyer":         "",
    "filter_bucket":        _all_buckets,
    "filter_position":      _all_positions,
    "filter_parent_league": _all_leagues,
}
for _k, _v in _DEFAULTS.items():
    st.session_state.setdefault(_k, _v)

_RANGE_DEFAULTS = {
    "filter_mv_range":   (_mv_lo, _mv_hi),
    "filter_ms_range":   (_ms_lo, _ms_hi),
    "filter_sell_range": (_sl_lo, _sl_hi),
}

FILTER_KEYS = list(_DEFAULTS.keys()) + list(_RANGE_DEFAULTS.keys())

# Read current filter state — widgets below mutate the same keys
f_player        = st.session_state["filter_player"]
f_current_club  = st.session_state["filter_current_club"]
f_buyer         = st.session_state["filter_buyer"]
f_bucket        = st.session_state["filter_bucket"]
f_position      = st.session_state["filter_position"]
f_parent_league = st.session_state["filter_parent_league"]
f_mv_range      = st.session_state.get("filter_mv_range",   _RANGE_DEFAULTS["filter_mv_range"])
f_ms_range      = st.session_state.get("filter_ms_range",   _RANGE_DEFAULTS["filter_ms_range"])
f_sell_range    = st.session_state.get("filter_sell_range", _RANGE_DEFAULTS["filter_sell_range"])

# Detect any non-default filter (drives the "Clear all" link visibility)
_any_filter_active = (
    bool(f_player) or bool(f_current_club) or bool(f_buyer)
    or set(f_bucket)        != set(_all_buckets)
    or set(f_position)      != set(_all_positions)
    or set(f_parent_league) != set(_all_leagues)
    or tuple(f_mv_range)   != _RANGE_DEFAULTS["filter_mv_range"]
    or tuple(f_ms_range)   != _RANGE_DEFAULTS["filter_ms_range"]
    or tuple(f_sell_range) != _RANGE_DEFAULTS["filter_sell_range"]
)

# ─── Apply filters ────────────────────────────────────────────────────────────

filtered = df.copy()
if f_player:
    q = f_player.lower()
    filtered = filtered[
        filtered["player_name"].str.lower().str.contains(q, na=False)
        | filtered["player_name_display"].str.lower().str.contains(q, na=False)
    ]
if f_current_club:
    q = f_current_club.lower()
    filtered = filtered[
        filtered["current_club"].str.lower().str.contains(q, na=False)
        | filtered["current_club_display"].str.lower().str.contains(q, na=False)
    ]
if f_buyer:
    q = f_buyer.lower()
    filtered = filtered[
        filtered["buyer_club_name"].str.lower().str.contains(q, na=False)
        | filtered["buyer_club_display"].str.lower().str.contains(q, na=False)
    ]
if f_bucket:
    filtered = filtered[filtered["super_bucket"].isin(f_bucket)]
else:
    filtered = filtered.iloc[0:0]
if f_position:
    filtered = filtered[filtered["position_bucket"].isin(f_position)]
else:
    filtered = filtered.iloc[0:0]
if f_parent_league:
    filtered = filtered[filtered["parent_league"].fillna("").isin(list(f_parent_league) + [""])]
filtered = filtered[
    (filtered["current_tm_value_eur"] / 1e6).between(f_mv_range[0], f_mv_range[1])
    & filtered[_SCORE_COL].between(f_ms_range[0], f_ms_range[1])
    & filtered["sellability_score"].between(f_sell_range[0], f_sell_range[1])
].reset_index(drop=True)

# Collapse to one row per player (top match)
total_matches_before_collapse = len(filtered)
if len(filtered):
    filtered = filtered.sort_values(_SCORE_COL, ascending=False, kind="stable")
    filtered["other_buyers_count"] = (
        filtered.groupby("player_id")["match_id"].transform("count") - 1
    )
    filtered = filtered.drop_duplicates(subset=["player_id"], keep="first").reset_index(drop=True)
else:
    filtered["other_buyers_count"] = 0
filtered.insert(0, "rank", range(1, len(filtered) + 1))

# Top-5 identity by match_score — computed once, BEFORE the user's sort runs.
# Drives the ⭐ prefix and cream row tint so those flags always mark the five
# best targets overall, not whichever rows happen to land in positions 1-5
# under the current sort.
if len(filtered):
    filtered["is_top5_overall"] = (
        filtered[_SCORE_COL].rank(method="first", ascending=False) <= 5
    )
else:
    filtered["is_top5_overall"] = False

# ─── Page header ─────────────────────────────────────────────────────────────

st.markdown(
    """
    <div class="rvc-page-title">
        <h1>Targets</h1>
        <div class="rvc-page-subtitle">
            Active brokerage matches — one row per player, showing the highest-scoring buyer.
            Click any player or club to drill in.
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

# ─── Sidebar — filter widgets (after wordmark + global search) ───────────────
with st.sidebar:
    st.markdown("---")
    if _any_filter_active:
        if st.button("✕ Clear all filters", key="clear_all_filters",
                     help="Reset every filter back to its default state.",
                     use_container_width=True):
            for k in FILTER_KEYS:
                if k in st.session_state:
                    del st.session_state[k]
            st.rerun()

    # POSITION & LEAGUE
    st.markdown(
        '<div class="rvc-filter-section-label">Position &amp; League</div>',
        unsafe_allow_html=True,
    )
    st.multiselect("Bucket", options=_all_buckets, key="filter_bucket",
                   placeholder="All buckets")
    st.multiselect("Position", options=_all_positions, key="filter_position",
                   format_func=labels.display_bucket,
                   placeholder="All positions")
    st.multiselect("Parent league", options=_all_leagues,
                   key="filter_parent_league",
                   format_func=labels.league_name,
                   placeholder="All parent leagues")

    # NAME SEARCH
    st.markdown(
        '<div class="rvc-filter-section-label">Name Search</div>',
        unsafe_allow_html=True,
    )
    st.text_input("Player", key="filter_player", placeholder="Player name…")
    st.text_input("Current club", key="filter_current_club",
                  placeholder="Current club…")
    st.text_input("Buyer", key="filter_buyer", placeholder="Buyer club…")

    # SCORE & VALUE RANGES
    st.markdown(
        '<div class="rvc-filter-section-label">Score &amp; Value Ranges</div>',
        unsafe_allow_html=True,
    )
    st.slider(
        "Market value (€m)",
        min_value=_mv_lo, max_value=_mv_hi,
        value=_RANGE_DEFAULTS["filter_mv_range"], step=1,
        key="filter_mv_range",
    )
    st.slider(
        f"{db.active_view_label()} score",
        min_value=_ms_lo, max_value=_ms_hi,
        value=_RANGE_DEFAULTS["filter_ms_range"], step=1,
        key="filter_ms_range",
    )
    st.slider(
        "Sellability",
        min_value=_sl_lo, max_value=_sl_hi,
        value=_RANGE_DEFAULTS["filter_sell_range"], step=1,
        key="filter_sell_range",
    )

# ─── KPI tiles (uniform custom HTML) ─────────────────────────────────────────

top_score = None
top_player = top_buyer = ""
if len(filtered):
    top_row = filtered.iloc[filtered[_SCORE_COL].idxmax()]
    top_score = float(top_row[_SCORE_COL])
    top_player = str(top_row["player_name_display"])
    top_buyer  = str(top_row["buyer_club_display"])


def _tile(label: str, value: str, *, subline: str = "", title: str = "") -> str:
    sub_html = (
        f'<div class="rvc-tile-sub">{subline}</div>' if subline else ""
    )
    return (
        f'<div class="rvc-tile" title="{title}">'
        f'<div class="rvc-tile-label">{label}</div>'
        f'<div class="rvc-tile-value">{value}</div>'
        f'{sub_html}'
        f'</div>'
    )


tiles_html = "".join([
    _tile("Players",
          str(filtered["player_id"].nunique() if len(filtered) else 0),
          title="Unique players in the universe with at least one buyer match (excluding Kill List)."),
    _tile("Total buyer matches",
          f"{total_matches_before_collapse:,}",
          title="Total player-buyer pairs in the matcher's output before collapsing to top buyer per player."),
    _tile("Active top buyers",
          str(filtered["buyer_club_id"].nunique() if len(filtered) else 0),
          title="Number of distinct clubs that are the highest-scoring buyer for at least one player."),
    _tile("Top match",
          f"{top_score:.1f}" if top_score is not None else "—",
          subline=f"{top_player} → {top_buyer}" if top_score is not None else "",
          title="Highest match score across all player-buyer pairs."),
])
st.markdown(f'<div class="rvc-tile-row">{tiles_html}</div>', unsafe_allow_html=True)

# ─── Sort state from query params (drives clickable column headers) ─────────
# Every data column is sortable. Each sortable <th> becomes an anchor that
# updates `?sort=KEY&dir=desc|asc`. Click to sort, click again to flip
# direction. Per-column defaults: alpha columns open ASC on first click,
# numeric columns open DESC.
_TGT_SORTABLE = {
    "Player":          ("name",            "asc"),
    "Age":             ("age",             "desc"),
    "Bucket":          ("bucket",          "asc"),
    "Position":        ("position",        "asc"),
    "Current Club":    ("current_club",    "asc"),
    "Parent Club":     ("parent_club",     "asc"),
    "Years Remaining": ("years_remaining", "desc"),
    "Market Value":    ("tm_value",        "desc"),
    "Buyer":           ("buyer",           "asc"),
    "Other Buyers":    ("other_buyers",    "desc"),
    "Match Score":     ("match_score",     "desc"),
    "Sellability":     ("sellability",     "desc"),
}
_TGT_KEY_TO_COL = {
    "name":            "player_name_display",
    "age":             "age",
    "bucket":          "super_bucket",
    "position":        "position_bucket",
    "current_club":    "current_club_display",
    "parent_club":     "parent_club_display",
    "years_remaining": "contract_years_remaining",
    "tm_value":        "current_tm_value_eur",
    "buyer":           "buyer_club_display",
    "other_buyers":    "other_buyers_count",
    "match_score":     "match_score",
    "sellability":     "sellability_score",
}
sort_key = st.query_params.get("sort", "match_score")
sort_dir = st.query_params.get("dir", "desc")
if sort_key not in _TGT_KEY_TO_COL:
    sort_key, sort_dir = "match_score", "desc"
ascending = (sort_dir == "asc")
_sort_col = _TGT_KEY_TO_COL[sort_key]
if len(filtered):
    filtered = filtered.sort_values(
        _sort_col, ascending=ascending, kind="stable", na_position="last"
    ).reset_index(drop=True)
    filtered["rank"] = range(1, len(filtered) + 1)

# ─── Build display DataFrame (post-filter, post-sort) ────────────────────────

# Sci Sports CA · PA strip rendered beneath the player name. Level fit is
# omitted here — Targets is a launchpad, not a per-match decision view.
def _player_cell_with_ratings(row: pd.Series) -> str:
    pid = int(row["player_id"])
    name = str(row["player_name_display"])
    anchor = ui.player_link(pid, name)
    ca = row.get("player_ca")
    pa = row.get("player_pa")
    if pd.notna(ca) or pd.notna(pa):
        ca_str = f"{float(ca):.0f}" if pd.notna(ca) else "—"
        pa_str = f"{float(pa):.0f}" if pd.notna(pa) else "—"
        sub = (f'<div style="font-size:0.7rem; color:#6b7280; line-height:1.3; '
               f'margin-top:2px;">CA {ca_str} · PA {pa_str}</div>')
        return anchor + sub
    return anchor


filtered["Player_html"] = filtered.apply(_player_cell_with_ratings, axis=1)
filtered["ParentClub_html"] = filtered.apply(
    lambda r: ui.club_link(int(r["parent_club_id"]), str(r["parent_club_with_league"]))
    if pd.notna(r["parent_club_id"]) else (r.get("parent_club_display") or ""),
    axis=1,
)
filtered["Buyer_html"] = filtered.apply(
    lambda r: ui.club_link(int(r["buyer_club_id"]), str(r["buyer_club_display"])),
    axis=1,
)
filtered["other_buyers_pill"] = filtered["other_buyers_count"].apply(
    lambda n: (f'<span class="rvc-pill-otherbuyers">+{int(n)}</span>'
               if n > 0 else "")
)

display = filtered[[
    "rank",
    "Player_html",
    "age",
    "super_bucket",
    "position_bucket",
    "current_club_display",
    "ParentClub_html",
    "contract_years_remaining",
    "current_tm_value_eur",
    "Buyer_html",
    "other_buyers_pill",
    "match_score",
    "market_match_score",
    "sellability_score",
]].rename(columns={
    "rank":                     "#",
    "Player_html":              "Player",
    "age":                      "Age",
    "super_bucket":             "Bucket",
    "position_bucket":          "Position",
    "current_club_display":     "Current Club",
    "ParentClub_html":          "Parent Club",
    "contract_years_remaining": "Years Remaining",
    "current_tm_value_eur":     "Market Value",
    "Buyer_html":               "Buyer",
    "other_buyers_pill":        "Other Buyers",
    "match_score":              "Brokerage Score",
    "market_match_score":       "Market Score",
    "sellability_score":        "Sellability",
})

# Star prefix on the five best targets overall (by match_score). ⭐ follows
# player identity, not row position — so a top-5 target keeps its star when
# the user re-sorts by Age, Player name, or any other column.
_top5_mask = filtered["is_top5_overall"].reset_index(drop=True).tolist()
display["#"] = [
    (f"⭐ {i + 1}" if _top5_mask[i] else str(i + 1))
    for i in range(len(display))
]


def _fmt_money_eur(x):
    if pd.isna(x):
        return "—"
    x = float(x)
    if abs(x) >= 1_000_000:
        return f"€{x/1_000_000:.1f}m"
    if abs(x) >= 1_000:
        return f"€{x/1_000:.0f}k"
    return f"€{int(x)}"


display["Market Value"] = display["Market Value"].apply(_fmt_money_eur)
display["Position"]     = display["Position"].apply(labels.display_bucket)
display["Brokerage Score"] = display["Brokerage Score"].apply(ui.fmt_score_capped)
display["Market Score"]    = display["Market Score"].apply(ui.fmt_score_capped)
display["Sellability"]   = display["Sellability"].apply(ui.fmt_score_capped)
display["Years Remaining"] = display["Years Remaining"].apply(
    lambda v: f"{v:.1f}" if pd.notna(v) else "—"
)

# ─── Styler chain (presentation only) ────────────────────────────────────────

def _bucket_bg(val):
    colour = labels.TIER_COLOURS.get(val, "")
    return f"background-color: {colour}; font-weight: 600;" if colour else ""


def _highlight_top_rows(row):
    """Cream tint on the five best targets overall (identity-based, not
    position-based — so re-sorting carries the highlight with the player)."""
    idx = int(row.name)
    is_top = _top5_mask[idx] if 0 <= idx < len(_top5_mask) else False
    return ['background-color: #fffbf0;' if is_top else '' for _ in row]


def _bg_match(v):
    try: return ui.heatmap_gradient(v, 0, 100)
    except (TypeError, ValueError): return ""
def _bg_sell(v):
    try: return ui.heatmap_gradient(v, 0, 100)
    except (TypeError, ValueError): return ""

styled_html = (
    display.style
    .apply(_highlight_top_rows, axis=1)
    .map(_bucket_bg, subset=["Bucket"])
    .map(_bg_match,  subset=["Brokerage Score", "Market Score"])
    .map(_bg_sell,   subset=["Sellability"])
)
ui.render_match_score_glossary()
ui.render_html_table(
    styled_html, max_height_px=720,
    sortable=_TGT_SORTABLE,
    current_sort=(sort_key, sort_dir),
    sort_path="/",
)
