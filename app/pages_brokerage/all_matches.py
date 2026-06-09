"""All Matches — unfiltered Sheet 1.

Every (player, buyer) pair the matcher predicts — Kill List players included.
Cloned from home.py (Targets) with four meaningful differences:

  1. Data source: db.get_all_matches(sort_col=db.active_match_score_col()) — no Kill List filter applied.
  2. KPI tiles reflect the unfiltered cohort.
  3. Excluded rows visually flagged (pale red tint + ⊘ Excluded badge,
     exclusion reason on hover).
  4. "Show excluded" toggle in the sidebar — default ON; flipping it OFF makes
     this page mirror Targets behaviour.

Everything else (filters, sort, click-through, heat-map, top-5 stars + tint,
custom HTML table) stays identical to Targets so users learn one mental model.
"""

from __future__ import annotations

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
    page_icon="📋",
    layout="wide",
    initial_sidebar_state="expanded",
)
ui.inject_css()

# ─── Sidebar — wordmark first, then global search ────────────────────────────
ui.render_page_accent()
ui.render_sidebar_engine_header()
ui.render_global_search(db.get_player_search_options(), db.get_club_search_options())

# ─── Load & shape — UNFILTERED matches ───────────────────────────────────────

df = db.get_all_matches(sort_col=db.active_match_score_col())
# Sidebar toggle picks the active scoring column for sort/filter logic.
# Both columns remain in the DataFrame; the table still renders both side-by-side.
_SCORE_COL = db.active_match_score_col()

# Demand-side restriction: only show matches whose buyer is in one of the
# 10 demand-mapped leagues (England 1-2, Spain 1, Italy 1, France 1-2,
# Germany 1, Portugal 1, Netherlands 1, Belgium 1).
df = df[df["buyer_league_id"].isin(config.DEMAND_MAPPED_LEAGUES)].reset_index(drop=True)
snapshot = db.get_snapshot_date()
excluded_ids = db.get_excluded_ids()

# Reason lookup — one entry per excluded player_id → "reason" string.
_excluded_df = db.get_excluded()
_reason_lookup: dict[int, str] = {}
if len(_excluded_df):
    for _r in _excluded_df.itertuples(index=False):
        pid = getattr(_r, "player_id", None)
        if pid is not None and not pd.isna(pid):
            _reason_lookup[int(pid)] = str(getattr(_r, "reason", "") or "")

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
# Excluded flag + reason carried as columns so they survive collapse/sort
df["excluded"] = df["player_id"].apply(lambda pid: int(pid) in excluded_ids)
df["exclusion_reason"] = df["player_id"].apply(
    lambda pid: _reason_lookup.get(int(pid), "")
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

# Filter session-state defaults. Note `am_show_excluded` is page-local
# (the `am_` prefix prevents collision with Targets' session-state keys).
_DEFAULTS = {
    "am_show_excluded":      True,   # show Kill List rows by default
    "am_filter_player":      "",
    "am_filter_current_club":"",
    "am_filter_buyer":       "",
    "am_filter_bucket":      _all_buckets,
    "am_filter_position":    _all_positions,
    "am_filter_parent_league": _all_leagues,
}
for _k, _v in _DEFAULTS.items():
    st.session_state.setdefault(_k, _v)

_RANGE_DEFAULTS = {
    "am_filter_mv_range":   (_mv_lo, _mv_hi),
    "am_filter_ms_range":   (_ms_lo, _ms_hi),
    "am_filter_sell_range": (_sl_lo, _sl_hi),
}

FILTER_KEYS = list(_DEFAULTS.keys()) + list(_RANGE_DEFAULTS.keys())

# Read current filter state
f_show_excluded = st.session_state["am_show_excluded"]
f_player        = st.session_state["am_filter_player"]
f_current_club  = st.session_state["am_filter_current_club"]
f_buyer         = st.session_state["am_filter_buyer"]
f_bucket        = st.session_state["am_filter_bucket"]
f_position      = st.session_state["am_filter_position"]
f_parent_league = st.session_state["am_filter_parent_league"]
f_mv_range      = st.session_state.get("am_filter_mv_range",   _RANGE_DEFAULTS["am_filter_mv_range"])
f_ms_range      = st.session_state.get("am_filter_ms_range",   _RANGE_DEFAULTS["am_filter_ms_range"])
f_sell_range    = st.session_state.get("am_filter_sell_range", _RANGE_DEFAULTS["am_filter_sell_range"])

# Detect any non-default filter (drives Clear-all visibility). Note:
# show_excluded=True is the page default — toggling it OFF counts as "active".
_any_filter_active = (
    (not f_show_excluded)
    or bool(f_player) or bool(f_current_club) or bool(f_buyer)
    or set(f_bucket)        != set(_all_buckets)
    or set(f_position)      != set(_all_positions)
    or set(f_parent_league) != set(_all_leagues)
    or tuple(f_mv_range)   != _RANGE_DEFAULTS["am_filter_mv_range"]
    or tuple(f_ms_range)   != _RANGE_DEFAULTS["am_filter_ms_range"]
    or tuple(f_sell_range) != _RANGE_DEFAULTS["am_filter_sell_range"]
)

# ─── Apply filters ────────────────────────────────────────────────────────────

filtered = df.copy()
if not f_show_excluded:
    filtered = filtered[~filtered["excluded"]]
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

# Top-5 identity by match_score (pre-user-sort) — drives ⭐ and cream tint.
if len(filtered):
    filtered["is_top5_overall"] = (
        filtered[_SCORE_COL].rank(method="first", ascending=False) <= 5
    )
else:
    filtered["is_top5_overall"] = False

# ─── Page header ─────────────────────────────────────────────────────────────

st.markdown(
    f"""
    <div class="rvc-page-title">
        <h1>All Matches</h1>
        <div class="rvc-page-subtitle">
            All matches the engine predicts — including excluded players.
            Targets is the filtered operational view.
        </div>
        <div style="margin-top:6px; font-size:0.85rem; color:#6b7280;">
            Level fit column on the right — {ui.level_fit_info_icon()}
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

# ─── Sidebar — Show-excluded toggle + filter widgets ─────────────────────────
with st.sidebar:
    st.markdown("---")
    if _any_filter_active:
        if st.button("✕ Clear all filters", key="am_clear_all_filters",
                     help="Reset every filter back to its default state.",
                     use_container_width=True):
            for k in FILTER_KEYS:
                if k in st.session_state:
                    del st.session_state[k]
            st.rerun()

    # SHOW / HIDE excluded — first filter on the page since it's the
    # central knob that distinguishes All Matches from Targets.
    st.markdown(
        '<div class="rvc-filter-section-label">Kill List</div>',
        unsafe_allow_html=True,
    )
    st.toggle(
        "Show excluded players",
        key="am_show_excluded",
        help="ON (default): show every match including Kill List players. "
             "OFF: hide Kill List rows — view becomes equivalent to Targets.",
    )

    # POSITION & LEAGUE
    st.markdown(
        '<div class="rvc-filter-section-label">Position &amp; League</div>',
        unsafe_allow_html=True,
    )
    st.multiselect("Bucket", options=_all_buckets, key="am_filter_bucket",
                   placeholder="All buckets")
    st.multiselect("Position", options=_all_positions, key="am_filter_position",
                   format_func=labels.display_bucket,
                   placeholder="All positions")
    st.multiselect("Parent league", options=_all_leagues,
                   key="am_filter_parent_league",
                   format_func=labels.league_name,
                   placeholder="All parent leagues")

    # NAME SEARCH
    st.markdown(
        '<div class="rvc-filter-section-label">Name Search</div>',
        unsafe_allow_html=True,
    )
    st.text_input("Player", key="am_filter_player", placeholder="Player name…")
    st.text_input("Current club", key="am_filter_current_club",
                  placeholder="Current club…")
    st.text_input("Buyer", key="am_filter_buyer", placeholder="Buyer club…")

    # SCORE & VALUE RANGES
    st.markdown(
        '<div class="rvc-filter-section-label">Score &amp; Value Ranges</div>',
        unsafe_allow_html=True,
    )
    st.slider(
        "Market value (€m)",
        min_value=_mv_lo, max_value=_mv_hi,
        value=_RANGE_DEFAULTS["am_filter_mv_range"], step=1,
        key="am_filter_mv_range",
    )
    st.slider(
        "Match score",
        min_value=_ms_lo, max_value=_ms_hi,
        value=_RANGE_DEFAULTS["am_filter_ms_range"], step=1,
        key="am_filter_ms_range",
    )
    st.slider(
        "Sellability",
        min_value=_sl_lo, max_value=_sl_hi,
        value=_RANGE_DEFAULTS["am_filter_sell_range"], step=1,
        key="am_filter_sell_range",
    )

# ─── KPI tiles — reflect the UNFILTERED All Matches cohort ───────────────────
# Compute from `df` (raw all-matches), not `filtered`, so the tiles stay
# steady when the user toggles filters or hides excluded. The active filter
# state changes the table; the headline tiles tell you what the engine
# produced this run.

top_score = None
top_player = top_buyer = ""
if len(df):
    top_row = df.iloc[df[_SCORE_COL].idxmax()]
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
          str(df["player_id"].nunique()),
          title="Distinct players across all matches (no Kill List filter)."),
    _tile("Total buyer matches",
          f"{len(df):,}",
          title="Count of every (player, buyer) row the matcher predicted this run."),
    _tile("Active top buyers",
          str(df["buyer_club_id"].nunique()),
          title="Distinct buyer clubs across all matches."),
    _tile("Top match",
          f"{top_score:.1f}" if top_score is not None else "—",
          subline=f"{top_player} → {top_buyer}" if top_score is not None else "",
          title="Highest match score across all (player, buyer) pairs."),
])
st.markdown(f'<div class="rvc-tile-row">{tiles_html}</div>', unsafe_allow_html=True)

# ─── Sort state from query params (clickable column headers) ─────────────────
_AM_SORTABLE = {
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
_AM_KEY_TO_COL = {
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
if sort_key not in _AM_KEY_TO_COL:
    sort_key, sort_dir = "match_score", "desc"
ascending = (sort_dir == "asc")
_sort_col = _AM_KEY_TO_COL[sort_key]
if len(filtered):
    filtered = filtered.sort_values(
        _sort_col, ascending=ascending, kind="stable", na_position="last"
    ).reset_index(drop=True)
    filtered["rank"] = range(1, len(filtered) + 1)

# ─── Build display DataFrame (post-filter, post-sort) ────────────────────────

def _player_cell(row: pd.Series) -> str:
    """Player name as a same-tab anchor; excluded rows append a ⊘ badge with
    the exclusion reason on hover."""
    anchor = ui.player_link(int(row["player_id"]), str(row["player_name_display"]))
    if row.get("excluded"):
        reason = str(row.get("exclusion_reason") or "Excluded from Targets").replace('"', '&quot;')
        anchor += (
            f' <span class="rvc-excluded-badge" '
            f'title="{reason}">⊘ Excluded</span>'
        )
    return anchor


filtered["Player_html"] = filtered.apply(_player_cell, axis=1)
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
# Sci Sports columns — CA, PA, Level fit pill.
filtered["ca_str"]         = filtered["player_ca"].apply(lambda v: f"{float(v):.1f}" if pd.notna(v) else "—")
filtered["pa_str"]         = filtered["player_pa"].apply(lambda v: f"{float(v):.1f}" if pd.notna(v) else "—")
filtered["level_fit_pill"] = filtered["level_fit"].apply(ui.level_fit_pill)

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
    "ca_str",
    "pa_str",
    "Buyer_html",
    "other_buyers_pill",
    "match_score",
    "market_match_score",
    "level_fit_pill",
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
    "ca_str":                   "CA",
    "pa_str":                   "PA",
    "Buyer_html":               "Buyer",
    "other_buyers_pill":        "Other Buyers",
    "match_score":              "Brokerage Score",
    "market_match_score":       "Market Score",
    "level_fit_pill":           "Level fit",
    "sellability_score":        "Sellability",
})

# ⭐ on the five best targets overall (identity, not position).
_top5_mask = filtered["is_top5_overall"].reset_index(drop=True).tolist()
_excluded_mask = filtered["excluded"].reset_index(drop=True).tolist()
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

# ─── Styler chain ─────────────────────────────────────────────────────────────

def _bucket_bg(val):
    colour = labels.TIER_COLOURS.get(val, "")
    return f"background-color: {colour}; font-weight: 600;" if colour else ""


def _highlight_top_rows(row):
    """Cream tint on the five best targets overall (identity-based)."""
    idx = int(row.name)
    is_top = _top5_mask[idx] if 0 <= idx < len(_top5_mask) else False
    return ['background-color: #fffbf0;' if is_top else '' for _ in row]


def _highlight_excluded_rows(row):
    """Pale red tint on Kill List rows. Applied AFTER _highlight_top_rows so
    excluded styling wins on conflict (excluded is the dominant signal)."""
    idx = int(row.name)
    is_excluded = _excluded_mask[idx] if 0 <= idx < len(_excluded_mask) else False
    return ['background-color: #FEF2F2;' if is_excluded else '' for _ in row]


def _bg_match(v):
    try: return ui.heatmap_gradient(v, 0, 100)
    except (TypeError, ValueError): return ""
def _bg_sell(v):
    try: return ui.heatmap_gradient(v, 0, 100)
    except (TypeError, ValueError): return ""

styled_html = (
    display.style
    .apply(_highlight_top_rows,      axis=1)
    .apply(_highlight_excluded_rows, axis=1)  # wins over cream when both fire
    .map(_bucket_bg, subset=["Bucket"])
    .map(_bg_match,  subset=["Brokerage Score", "Market Score"])
    .map(_bg_sell,   subset=["Sellability"])
)
ui.render_match_score_glossary()
ui.render_html_table(
    styled_html, max_height_px=720,
    sortable=_AM_SORTABLE,
    current_sort=(sort_key, sort_dir),
    sort_path="/all_matches",
)
