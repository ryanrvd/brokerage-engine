"""Needs Rating — SciSports CA/PA Worklist.

Surfaces the cohort_unrated table (populated each match engine run by
scripts/22_match_engine.py:build_cohort_unrated). These are players who
would be in the mandate-relevant cohort but lack a SciSports CA/PA — they
do NOT enter the matches table until rated.

Operational signal: worklist size = data debt. Closing the worklist closes
the debt. Today: 657 players, dominated by recently-relegated European
clubs whose squads weren't yet in SciSports' pre-relegation league index.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

import db
import labels
import components as ui

# config — for league display + DEMAND_MAPPED_LEAGUES list.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent.parent))
import config


st.set_page_config(
    page_title="Brokerage Engine · Needs Rating",
    page_icon="⏳",
    layout="wide",
    initial_sidebar_state="expanded",
)
ui.inject_css()

# ─── Sidebar ─────────────────────────────────────────────────────────────────
ui.render_page_accent()
ui.render_sidebar_engine_header()
ui.render_global_search(db.get_player_search_options(), db.get_club_search_options())

# ─── Load cohort_unrated ─────────────────────────────────────────────────────
con = db.get_connection()
df = pd.read_sql_query("""
    SELECT player_id, player_name, parent_club_name, position_bucket,
           age, sellability_score, league_id, reason, snapshot_date
    FROM cohort_unrated
""", con)
n_total = len(df)

# Header
st.title("Needs Rating — SciSports CA/PA Worklist")
st.caption(
    f"{n_total:,} mandate-relevant players awaiting SciSports rating. "
    "These don't enter the matches table until rated. Rate them via "
    "`data/scisports_ratings.xlsx` or via a manual SciSports API fetch — "
    "see the operational instructions at the bottom of this page."
)

if n_total == 0:
    st.success("Worklist empty — every mandate-cohort player has a CA/PA. Great state.")
    st.stop()

# ─── KPI strip ───────────────────────────────────────────────────────────────
top_sellability_row = df.sort_values("sellability_score", ascending=False).iloc[0]
top_sell = float(top_sellability_row["sellability_score"])
top_name = top_sellability_row["player_name"]

# League with biggest gap
league_counts = df.groupby("league_id").size().sort_values(ascending=False)
biggest_lg_code = league_counts.index[0]
biggest_lg_name = labels.league_name(biggest_lg_code)
biggest_lg_n = int(league_counts.iloc[0])


def _tile(label: str, value: str, *, subline: str = "", title: str = "") -> str:
    sub_html = f'<div class="rvc-tile-sub">{subline}</div>' if subline else ""
    return (
        f'<div class="rvc-tile" title="{title}">'
        f'<div class="rvc-tile-label">{label}</div>'
        f'<div class="rvc-tile-value">{value}</div>'
        f'{sub_html}'
        f'</div>'
    )


tiles = [
    _tile("Total worklist", f"{n_total:,}",
          subline="players awaiting CA/PA",
          title="Distinct mandate-cohort players missing SciSports CA/PA in player_ratings."),
    _tile("Top sellability missing", f"{top_sell:.1f}",
          subline=labels.player_display_name(int(top_sellability_row["player_id"]), top_name),
          title="Highest-sellability player still unrated. Rate them first."),
    _tile("League with biggest gap", biggest_lg_name,
          subline=f"{biggest_lg_n} unrated players",
          title="League contributing the most unrated players. Recently-relegated European clubs dominate today."),
]
st.markdown(f'<div class="rvc-tile-row">{"".join(tiles)}</div>', unsafe_allow_html=True)
st.markdown("")  # spacing


# ─── Per-league bar chart ────────────────────────────────────────────────────
st.markdown("### Per-league breakdown")
chart_df = (
    league_counts.reset_index().rename(columns={"index": "league_id", 0: "n_players"})
)
# Use display names + sort by count desc
chart_df["league_display"] = chart_df["league_id"].apply(labels.league_name)
chart_df = chart_df.rename(columns={chart_df.columns[1]: "n_players"})
st.bar_chart(
    chart_df.set_index("league_display")["n_players"],
    use_container_width=True,
    color="#A85432",
)


# ─── Sidebar filters ─────────────────────────────────────────────────────────
all_leagues = sorted(df["league_id"].dropna().unique().tolist())
all_positions = sorted(df["position_bucket"].dropna().unique().tolist())

with st.sidebar:
    st.markdown("---")
    st.markdown("**Filters**")
    f_lg = st.multiselect("League", options=all_leagues, default=all_leagues,
                          format_func=labels.league_name, key="nr_f_lg")
    f_pos = st.multiselect("Position", options=all_positions, default=all_positions,
                           format_func=labels.display_bucket, key="nr_f_pos")
    s_min_raw = float(df["sellability_score"].min())
    s_max_raw = float(df["sellability_score"].max())
    s_lo = float(int(s_min_raw))
    s_hi = float(int(s_max_raw) + 1)
    f_sell = st.slider("Min sellability",
                       min_value=s_lo, max_value=s_hi,
                       value=s_lo, step=1.0, key="nr_f_sell")

# Apply filters
filtered = df.copy()
if f_lg:
    filtered = filtered[filtered["league_id"].isin(f_lg)]
if f_pos:
    filtered = filtered[filtered["position_bucket"].isin(f_pos)]
filtered = filtered[filtered["sellability_score"] >= f_sell]

filtered = filtered.sort_values("sellability_score", ascending=False).reset_index(drop=True)
n_filtered = len(filtered)


# ─── Worklist table ──────────────────────────────────────────────────────────
st.markdown(f"### Worklist — {n_filtered:,} players (sorted by sellability desc)")


def _days_since(ts: str | None) -> str:
    if not ts:
        return "—"
    try:
        from datetime import date as _date, datetime as _datetime
        d0 = _datetime.fromisoformat(ts).date()
        return f"{(_date.today() - d0).days}d"
    except (TypeError, ValueError):
        return "—"


def _reason_pill(reason: str | None) -> str:
    if reason == "no_ca":
        return '<span style="background:#FEF3C7; color:#92400E; padding:1px 7px; border-radius:6px; font-size:0.72rem; font-weight:700;">no_ca</span>'
    if reason == "departed":
        return '<span style="background:#FEE2E2; color:#991B1B; padding:1px 7px; border-radius:6px; font-size:0.72rem; font-weight:700;">departed</span>'
    return f'<span style="background:#F3F4F6; color:#374151; padding:1px 7px; border-radius:6px; font-size:0.72rem;">{reason or "—"}</span>'


# Build HTML table — paginated client-side at 100 rows/page
PAGE_SIZE = 100
if "nr_page" not in st.session_state:
    st.session_state["nr_page"] = 0
total_pages = max(1, (n_filtered + PAGE_SIZE - 1) // PAGE_SIZE)
page = min(st.session_state["nr_page"], total_pages - 1)

col_p, col_info = st.columns([0.4, 0.6])
with col_p:
    cols = st.columns([0.2, 0.3, 0.2, 0.3])
    if cols[0].button("◀", key="nr_prev", disabled=(page == 0)):
        st.session_state["nr_page"] = max(0, page - 1)
        st.rerun()
    cols[1].markdown(
        f'<div style="text-align:center; padding-top:6px; font-weight:600;">'
        f'Page {page + 1} of {total_pages}</div>',
        unsafe_allow_html=True,
    )
    if cols[2].button("▶", key="nr_next", disabled=(page >= total_pages - 1)):
        st.session_state["nr_page"] = min(total_pages - 1, page + 1)
        st.rerun()
with col_info:
    st.caption(f"{PAGE_SIZE} rows per page")

slice_df = filtered.iloc[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
rows_html: list[str] = []
for _, r in slice_df.iterrows():
    pid = int(r["player_id"])
    p_disp = labels.player_display_name(pid, r["player_name"])
    p_href = ui.with_auth(f"/player_view?player_id={pid}")
    pc_disp = r.get("parent_club_name") or "—"
    pos = r.get("position_bucket") or "—"
    pos_disp = labels.display_bucket(pos)
    age = int(r["age"]) if pd.notna(r.get("age")) else "—"
    sell = float(r["sellability_score"])
    lg = r.get("league_id") or "—"
    lg_disp = labels.league_name(lg)
    reason_pill = _reason_pill(r.get("reason"))
    days = _days_since(r.get("snapshot_date"))

    rows_html.append(
        f'<tr>'
        f'<td><a href="{p_href}" target="_self" style="color:#1F3864; font-weight:700; text-decoration:none;">{p_disp}</a></td>'
        f'<td style="text-align:center;">{age}</td>'
        f'<td style="text-align:center;">{pos_disp}</td>'
        f'<td>{pc_disp}</td>'
        f'<td style="text-align:center;">{lg_disp}</td>'
        f'<td style="text-align:right; font-weight:600; color:#7C2D12;">{sell:.1f}</td>'
        f'<td style="text-align:center;">{reason_pill}</td>'
        f'<td style="text-align:right; color:#6B7280; font-size:0.78rem;">{days}</td>'
        f'</tr>'
    )

table_html = (
    '<table class="rvc-table" style="width:100%; border-collapse:collapse; font-size:0.86rem;">'
    '<thead style="background:#F9FAFB; border-bottom:2px solid #E5E7EB;">'
    '<tr>'
    '<th style="text-align:left; padding:8px;">Player</th>'
    '<th>Age</th><th>Position</th>'
    '<th style="text-align:left;">Parent Club</th>'
    '<th>League</th>'
    '<th>Sellability</th>'
    '<th>Reason</th>'
    '<th>Days on worklist</th>'
    '</tr></thead>'
    f'<tbody>{"".join(rows_html)}</tbody>'
    '</table>'
)
st.markdown(table_html, unsafe_allow_html=True)


# ─── Operational instructions ────────────────────────────────────────────────
st.markdown("---")
st.markdown("### How to clear the worklist")
st.markdown(
    """
1. **Open** `data/scisports_ratings.xlsx` from the repo.
2. **Filter** the `status` column to `pending` — those rows are this worklist.
3. **Fill in** `current_ability` and `potential_ability` from the SciSports
   dashboard for the player.
4. **Save** the workbook.
5. **Run** the SciSports refresh pipeline so the API-derived values reload
   into `player_ratings`, and re-run the match engine:

       .venv/bin/python scripts/load_scisports_ratings.py
       .venv/bin/python scripts/22_match_engine.py

   Players just rated will enter the matches table on the next run.

Alternative (bulk): if many worklist entries are in a single SciSports league
that wasn't yet covered, extend `scripts/_scisports_phase4_refresh.py` and
re-run for that league. See `CLAUDE.md` for the Phase 4 pattern.
"""
)
