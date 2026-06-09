"""Market View — comprehensive mandate territory surface.

Scores every (player × buyer) match in the ~3,800-player mandate-relevant
cohort (sellability_score > 50 with SciSports CA, UNION'd with the
sellable_now classifier). Brokerage Engine view available via the sidebar
toggle; this page is intentionally Market-View-first.

Layout (per the spec):
  1. KPI strip: total matches / players with matches / top match / UNRATED count
  2. Recently-relegated callout — top 5 by market_match_score from the
     14 recently_relegated parent clubs
  3. Top opportunities table — sorted by market_match_score, paginated,
     with sidebar filters
  4. Position tension panel — current per-position multipliers
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

import db
import labels
import components as ui

# config — for league display names + DEMAND_MAPPED_LEAGUES.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent.parent))
import config


st.set_page_config(
    page_title="Brokerage Engine · Market View",
    page_icon="🌐",
    layout="wide",
    initial_sidebar_state="expanded",
)
ui.inject_css()

# ─── Sidebar — wordmark + global search + view toggle ───────────────────────
ui.render_page_accent()
ui.render_sidebar_engine_header()
ui.render_global_search(db.get_player_search_options(), db.get_club_search_options())

# ─── Header ──────────────────────────────────────────────────────────────────
con = db.get_connection()
snapshot = db.get_snapshot_date()

# Cohort counts — same definition as scripts/22_match_engine.py
n_cohort = con.execute("""
    SELECT COUNT(*) FROM player_universe pu
    WHERE (pu.sellability_score > 50
           AND EXISTS (SELECT 1 FROM player_ratings WHERE tm_player_id=pu.player_id AND current_ability IS NOT NULL))
       OR pu.sellability_status = 'sellable_now'
""").fetchone()[0]

st.title("Market View — Comprehensive mandate territory")
ui.render_match_score_glossary()
st.caption(
    f"Scoring every (player × buyer) match in the {n_cohort:,}-player "
    f"mandate-relevant cohort. Brokerage Engine view available via the "
    f"sidebar toggle. Snapshot: {snapshot}."
)

# ─── KPI strip ───────────────────────────────────────────────────────────────
n_market_matches = con.execute(
    "SELECT COUNT(*) FROM matches WHERE market_match_score IS NOT NULL"
).fetchone()[0]
n_players_w_match = con.execute(
    "SELECT COUNT(DISTINCT player_id) FROM matches WHERE market_match_score IS NOT NULL"
).fetchone()[0]
n_unrated = con.execute("SELECT COUNT(*) FROM cohort_unrated").fetchone()[0]

top_match = con.execute("""
    SELECT player_id, player_name, buyer_club_id, buyer_club_name, market_match_score
    FROM matches
    WHERE market_match_score IS NOT NULL
    ORDER BY market_match_score DESC LIMIT 1
""").fetchone()
if top_match:
    tp_pid, tp_pname, tp_bcid, tp_bname, tp_score = top_match
    top_value = f"{float(tp_score):.1f}"
    top_subline = (
        f"{labels.player_display_name(int(tp_pid), tp_pname)} → "
        f"{labels.club_display_name(int(tp_bcid) if tp_bcid else None, tp_bname)}"
    )
else:
    top_value = "—"
    top_subline = ""


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
    _tile("Total matches", f"{n_market_matches:,}",
          subline="every player × buyer in the cohort",
          title="Rows where market_match_score is populated. Includes all pairs that survived the score floor of 5."),
    _tile("Players with matches", f"{n_players_w_match:,}",
          subline=f"of {n_cohort:,} in cohort",
          title="Distinct players appearing in at least one Market View match row."),
    _tile("Top match", top_value,
          subline=top_subline,
          title="Highest market_match_score across the entire cohort."),
    _tile("UNRATED worklist", f"{n_unrated:,}",
          subline=f'<a href="{ui.with_auth("/needs_rating")}" target="_self" style="color:#A85432; font-weight:600;">→ go to worklist</a>',
          title="Players in the mandate cohort missing SciSports CA/PA. They do not enter the matches table until rated."),
]
st.markdown(f'<div class="rvc-tile-row">{"".join(tiles)}</div>', unsafe_allow_html=True)

st.markdown("")  # vertical spacing


# ─── Recently-relegated callout ──────────────────────────────────────────────
n_rel_clubs = con.execute("SELECT COUNT(*) FROM club_pressure WHERE recently_relegated = 1").fetchone()[0]
rel_top5 = con.execute("""
    SELECT m.player_id, m.player_name, pu.parent_club_id, pu.parent_club,
           m.buyer_club_id, m.buyer_club_name, m.market_match_score,
           m.position_bucket, pu.age
    FROM matches m
    JOIN player_universe pu ON pu.player_id = m.player_id
    JOIN club_pressure cp ON cp.club_id = pu.parent_club_id
    WHERE cp.recently_relegated = 1
      AND m.market_match_score IS NOT NULL
    ORDER BY m.market_match_score DESC LIMIT 5
""").fetchall()

with st.container():
    st.markdown(
        f'<div style="background:#FEF3C7; border-left:4px solid #F59E0B; '
        f'padding:12px 16px; border-radius:6px; margin-bottom:8px;">'
        f'<div style="font-weight:700; font-size:0.95rem; color:#92400E;">'
        f'🚨 Recently-relegated cohort highlights — {n_rel_clubs} clubs in mandate territory'
        f'</div>'
        f'<div style="font-size:0.82rem; color:#78350F;">'
        f'Top 5 mandate moves out of recently-relegated clubs (Wolves / Burnley / West Ham + 11 European)'
        f'</div></div>',
        unsafe_allow_html=True,
    )

if rel_top5:
    rel_rows_html = []
    for pid, pname, pcid, pclub, bcid, bname, score, pos, age in rel_top5:
        p_disp = labels.player_display_name(int(pid), pname)
        b_disp = labels.club_display_name(int(bcid) if bcid else None, bname)
        pc_disp = labels.club_display_name(int(pcid) if pcid else None, pclub)
        p_href = ui.with_auth(f"/player_view?player_id={int(pid)}")
        b_href = ui.with_auth(f"/club_view?club_id={int(bcid)}") if bcid else "#"
        rel_rows_html.append(
            f'<div style="display:grid; grid-template-columns: 60px 1fr 1fr 60px 80px; '
            f'gap:12px; padding:8px 12px; border-bottom:1px solid #F3F4F6;">'
            f'<div style="font-weight:700; color:#7C2D12;">{float(score):.1f}</div>'
            f'<div><a href="{p_href}" target="_self" style="color:#1F3864; font-weight:700; text-decoration:none;">{p_disp}</a> '
            f'<span style="color:#6B7280; font-size:0.78rem;">· {pos}, {age}</span></div>'
            f'<div style="color:#374151;">{pc_disp} <span style="color:#9CA3AF;">→</span> '
            f'<a href="{b_href}" target="_self" style="color:#1F3864; font-weight:700; text-decoration:none;">{b_disp}</a></div>'
            f'<div style="text-align:right; color:#6B7280; font-size:0.78rem;">{pos}</div>'
            f'<div style="text-align:right; color:#6B7280; font-size:0.78rem;">age {age}</div>'
            f'</div>'
        )
    st.markdown(
        f'<div style="background:#FFFBEB; border:1px solid #FBBF24; border-radius:6px; margin-bottom:18px;">'
        + "".join(rel_rows_html) + '</div>',
        unsafe_allow_html=True,
    )

st.markdown("")

# ─── Sidebar filters ─────────────────────────────────────────────────────────
df = db.get_all_matches(sort_col="market_match_score")
df = df[df["market_match_score"].notna()].reset_index(drop=True)

with st.sidebar:
    st.markdown("---")
    st.markdown("**Filters**")
    parent_leagues = sorted(df["parent_league"].dropna().unique().tolist())
    buyer_leagues = sorted(df["buyer_league_id"].dropna().unique().tolist())
    positions = sorted(df["position_bucket"].dropna().unique().tolist())
    f_parent = st.multiselect("Parent league", options=parent_leagues,
                              default=parent_leagues, format_func=labels.league_name,
                              key="mv_f_parent")
    f_buyer = st.multiselect("Buyer league", options=buyer_leagues,
                             default=buyer_leagues, format_func=labels.league_name,
                             key="mv_f_buyer")
    f_pos = st.multiselect("Position", options=positions,
                           default=positions, format_func=labels.display_bucket,
                           key="mv_f_pos")
    mkt_min = float(df["market_match_score"].min())
    mkt_max = float(df["market_match_score"].max())
    f_score = st.slider("Min Market score",
                        min_value=float(int(mkt_min)),
                        max_value=float(int(mkt_max) + 1),
                        value=float(10),  # default surfaces meaningful matches
                        step=0.5,
                        key="mv_f_score")
    f_brok_only = st.checkbox("Brokerage-scored only", value=False,
                              key="mv_f_brok_only",
                              help="Filter to rows where Brokerage match_score is also populated.")


# Apply filters
filtered = df.copy()
if f_parent:
    filtered = filtered[filtered["parent_league"].isin(f_parent)]
if f_buyer:
    filtered = filtered[filtered["buyer_league_id"].isin(f_buyer)]
if f_pos:
    filtered = filtered[filtered["position_bucket"].isin(f_pos)]
filtered = filtered[filtered["market_match_score"] >= f_score]
if f_brok_only:
    filtered = filtered[filtered["match_score"].notna()]

n_filtered = len(filtered)

# Distinct-player collapse so a single mega-buyer doesn't dominate the table.
# Take the best (player, buyer) per player; show count of additional buyers.
if len(filtered):
    filtered = filtered.sort_values("market_match_score", ascending=False, kind="stable")
    other_buyers = filtered.groupby("player_id")["match_id"].transform("count") - 1
    filtered["other_buyers"] = other_buyers
    filtered = filtered.drop_duplicates(subset=["player_id"], keep="first").reset_index(drop=True)

n_after_collapse = len(filtered)


# ─── Top opportunities table ─────────────────────────────────────────────────
st.markdown(f"### Top opportunities — {n_after_collapse:,} unique players (from {n_filtered:,} pairs)")

PAGE_SIZE = 50
if "mv_page" not in st.session_state:
    st.session_state["mv_page"] = 0
total_pages = max(1, (n_after_collapse + PAGE_SIZE - 1) // PAGE_SIZE)
page = min(st.session_state["mv_page"], total_pages - 1)

# Pagination control
col_p, col_info = st.columns([0.4, 0.6])
with col_p:
    cols = st.columns([0.2, 0.3, 0.2, 0.3])
    if cols[0].button("◀", key="mv_prev", disabled=(page == 0)):
        st.session_state["mv_page"] = max(0, page - 1)
        st.rerun()
    cols[1].markdown(
        f'<div style="text-align:center; padding-top:6px; font-weight:600;">'
        f'Page {page + 1} of {total_pages}</div>',
        unsafe_allow_html=True,
    )
    if cols[2].button("▶", key="mv_next", disabled=(page >= total_pages - 1)):
        st.session_state["mv_page"] = min(total_pages - 1, page + 1)
        st.rerun()
with col_info:
    st.caption(f"Showing {min(PAGE_SIZE, max(0, n_after_collapse - page * PAGE_SIZE))} rows · {PAGE_SIZE}/page")


def _level_pill(lf: str | None) -> str:
    colors = {
        "ON_LEVEL": ("#15803d", "#dcfce7"),
        "UPSIDE":   ("#b45309", "#fef3c7"),
        "BELOW":    ("#6b7280", "#f3f4f6"),
    }
    fg, bg = colors.get(lf or "", ("#6b7280", "#f3f4f6"))
    return f'<span style="display:inline-block; padding:1px 7px; border-radius:6px; background:{bg}; color:{fg}; font-weight:600; font-size:0.78rem;">{lf or "—"}</span>'


def _pathway_short(pleague: str | None, bleague: str | None) -> str:
    if not pleague or not bleague:
        return "—"
    return f"{labels.league_name(pleague)[:18]} → {labels.league_name(bleague)[:18]}"


# Build the table HTML
slice_df = filtered.iloc[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
rows_html: list[str] = []
for _, r in slice_df.iterrows():
    pid = int(r["player_id"]) if pd.notna(r.get("player_id")) else None
    bcid = int(r["buyer_club_id"]) if pd.notna(r.get("buyer_club_id")) else None
    p_disp = labels.player_display_name(pid, r.get("player_name_display") or r.get("player_name"))
    b_disp = labels.club_display_name(bcid, r.get("buyer_club_display") or r.get("buyer_club_name"))
    pc_disp = labels.club_display_name(
        int(r["parent_club_id"]) if pd.notna(r.get("parent_club_id")) else None,
        r.get("parent_club_display") or r.get("parent_club"),
    )
    p_href = ui.with_auth(f"/player_view?player_id={pid}") if pid else "#"
    b_href = ui.with_auth(f"/club_view?club_id={bcid}") if bcid else "#"
    age = int(r["age"]) if pd.notna(r.get("age")) else "—"
    pos = r.get("position_bucket") or "—"
    pos_disp = labels.display_bucket(pos)
    ca = f"{float(r['player_ca']):.1f}" if pd.notna(r.get("player_ca")) else "—"
    pa = f"{float(r['player_pa']):.1f}" if pd.notna(r.get("player_pa")) else "—"
    _sell_v = float(r["sellability_score"]) if pd.notna(r.get("sellability_score")) else None
    _brok_v = float(r["match_score"]) if pd.notna(r.get("match_score")) else None
    _mkt_v  = float(r["market_match_score"]) if pd.notna(r.get("market_match_score")) else None
    # All three score cells share the same white→green scale, capped at 100.
    # Raw market_match_score can exceed 100 (scarcity × tension lift) — clamp
    # the displayed value to 100+ so the user sees a consistent ceiling
    # without losing the "headroom" signal.
    sell_style = ui.heatmap_gradient(_sell_v, 0, 100) if _sell_v is not None else ""
    brok_style = ui.heatmap_gradient(_brok_v, 0, 100) if _brok_v is not None else ""
    mkt_style  = ui.heatmap_gradient(_mkt_v,  0, 100) if _mkt_v is not None else ""
    sell = ui.fmt_score_capped(_sell_v) if _sell_v is not None else "—"
    brok = ui.fmt_score_capped(_brok_v) if _brok_v is not None else '<span style="color:#9CA3AF;">—</span>'
    mkt  = f"<b>{ui.fmt_score_capped(_mkt_v)}</b>" if _mkt_v is not None else "—"
    pathway = _pathway_short(r.get("parent_league"), r.get("buyer_league_id"))
    lf_pill = _level_pill(r.get("level_fit"))
    other_buyers = int(r.get("other_buyers", 0))
    other_pill = (
        f'<span style="background:#EEF2FF; color:#3730A3; padding:1px 6px; '
        f'border-radius:6px; font-size:0.72rem; font-weight:600;">+{other_buyers}</span>'
        if other_buyers > 0 else ""
    )

    rows_html.append(
        f'<tr>'
        f'<td><a href="{p_href}" target="_self" style="color:#1F3864; font-weight:700; text-decoration:none;">{p_disp}</a> {other_pill}</td>'
        f'<td style="text-align:center;">{age}</td>'
        f'<td style="text-align:center;">{pos_disp}</td>'
        f'<td>{pc_disp}</td>'
        f'<td><a href="{b_href}" target="_self" style="color:#1F3864; font-weight:700; text-decoration:none;">{b_disp}</a></td>'
        f'<td style="text-align:center;">{labels.league_name(r.get("buyer_league_id") or "")[:18]}</td>'
        f'<td style="text-align:center;">{lf_pill}</td>'
        f'<td style="text-align:right;">{ca}</td>'
        f'<td style="text-align:right;">{pa}</td>'
        f'<td style="text-align:right; {sell_style}">{sell}</td>'
        f'<td style="text-align:right; {brok_style}">{brok}</td>'
        f'<td style="text-align:right; {mkt_style}">{mkt}</td>'
        f'<td style="font-size:0.78rem; color:#374151;">{pathway}</td>'
        f'</tr>'
    )

table_html = (
    '<table class="rvc-table" style="width:100%; border-collapse:collapse; font-size:0.86rem;">'
    '<thead style="background:#F9FAFB; border-bottom:2px solid #E5E7EB;">'
    '<tr>'
    '<th style="text-align:left; padding:8px;">Player</th>'
    '<th>Age</th><th>Pos</th>'
    '<th style="text-align:left;">Parent Club</th>'
    '<th style="text-align:left;">Buyer</th>'
    '<th>Buyer League</th><th>Level Fit</th>'
    '<th>CA</th><th>PA</th><th>Sell</th>'
    '<th style="color:#6B7280;">Brokerage</th>'
    '<th style="color:#7C2D12;">Market</th>'
    '<th style="text-align:left;">Pathway</th>'
    '</tr></thead>'
    f'<tbody>{"".join(rows_html)}</tbody>'
    '</table>'
)
st.markdown(table_html, unsafe_allow_html=True)


# ─── Position tension panel ──────────────────────────────────────────────────
st.markdown("---")
st.markdown("### Position tension (live)")
st.caption(
    "Ratio = demand_count / sellability-weighted supply_count, per position. "
    "Multiplier applied inside market_match_score: 1.4 if ratio > 1.3 (tight), "
    "1.0 if balanced, 0.7 if ratio < 0.7 (loose). Click a position to drill into Position View."
)

POSITIONS = ["GK", "CB", "LB", "RB", "DM", "CM", "AM", "LW", "RW", "ST_CF"]
demand_by_pos = {}
for r in con.execute("SELECT position_bucket, COUNT(*) FROM map_club_requests WHERE position_bucket IS NOT NULL GROUP BY 1"):
    demand_by_pos[r[0]] = r[1]
for r in con.execute("SELECT position_bucket, COUNT(*) FROM inferred_club_requests WHERE position_bucket IS NOT NULL GROUP BY 1"):
    demand_by_pos[r[0]] = demand_by_pos.get(r[0], 0) + r[1]

supply_w = {}
for r in con.execute("""
    SELECT pu.position_bucket, SUM(pu.sellability_score/100.0)
    FROM player_universe pu JOIN player_ratings pr ON pr.tm_player_id=pu.player_id
    WHERE pu.sellability_score > 50 AND pr.current_ability IS NOT NULL
      AND pu.position_bucket IS NOT NULL
    GROUP BY pu.position_bucket
"""):
    supply_w[r[0]] = r[1]


def _tension_pill(ratio: float) -> tuple[str, float, str]:
    if ratio > 1.3:
        return "tight", 1.4, "#EF4444"
    if ratio >= 0.7:
        return "balanced", 1.0, "#10B981"
    return "loose", 0.7, "#3B82F6"


tension_cards: list[str] = []
for pos in POSITIONS:
    d = demand_by_pos.get(pos, 0)
    s = supply_w.get(pos, 0)
    ratio = (d / s) if s > 0 else 0.0
    label, mult, color = _tension_pill(ratio)
    pos_disp = labels.display_bucket(pos)
    href = ui.with_auth(f"/position_view?bucket={pos}")
    tension_cards.append(
        f'<a href="{href}" target="_self" style="text-decoration:none;">'
        f'<div style="background:white; border:1px solid #E5E7EB; border-left:4px solid {color}; '
        f'border-radius:6px; padding:10px; min-width:100px;">'
        f'<div style="font-weight:700; font-size:1rem; color:#111827;">{pos_disp}</div>'
        f'<div style="font-size:0.72rem; color:#6B7280; margin-top:2px;">'
        f'd={d} / s={s:.0f} → <b>{ratio:.2f}</b></div>'
        f'<div style="margin-top:6px; display:flex; gap:6px; align-items:center;">'
        f'<span style="background:{color}; color:white; padding:2px 7px; border-radius:6px; '
        f'font-size:0.72rem; font-weight:700;">{label}</span>'
        f'<span style="color:#374151; font-size:0.78rem;">×{mult:.1f}</span>'
        f'</div></div></a>'
    )

st.markdown(
    f'<div style="display:flex; flex-wrap:wrap; gap:8px;">{"".join(tension_cards)}</div>',
    unsafe_allow_html=True,
)
