"""Market Overview — single-screen synthesis dashboard.

The user-facing landing surface. Synthesises everything the other pages
break down: KPI tiles → 10×10 position×league heat map → top opportunities
+ pressured clubs → position state cards + league net-role cards.

Heat map cell colours come from the same `_tension_label` palette used on
Position View (Sellers in control = red, Buyers in control = blue,
Balanced = green, No signal = grey) so the visual language is consistent
across the app.

Sidebar is minimal — wordmark + global search only. This is a synthesis
view, not a drill-down; filters live on the deeper pages.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

import db
import labels
import components as ui

# config — for SNAPSHOT_DATE + DEMAND_MAPPED_LEAGUES.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent.parent))
import config

st.set_page_config(
    page_title="Brokerage Engine",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)
ui.inject_css()

# ─── Sidebar — wordmark + global search only ─────────────────────────────────
ui.render_page_accent()
ui.render_sidebar_engine_header()
ui.render_global_search(db.get_player_search_options(), db.get_club_search_options())

# ─── Header ──────────────────────────────────────────────────────────────────
con = db.get_connection()
snapshot = db.get_snapshot_date()
n_leagues_total = con.execute("SELECT COUNT(DISTINCT league_id) FROM club_pressure").fetchone()[0]
n_demand_mapped = len(config.DEMAND_MAPPED_LEAGUES)

st.markdown(
    f'<div class="rvc-page-title"><h1>Market overview</h1>'
    f'<div class="rvc-page-subtitle">Snapshot: <strong>{snapshot.isoformat()}</strong> '
    f' · {n_leagues_total} leagues covered '
    f' · {n_demand_mapped} demand-mapped'
    f'</div></div>',
    unsafe_allow_html=True,
)

# ─── KPI tiles ───────────────────────────────────────────────────────────────
excluded_ids = db.get_excluded_ids()

# Sellable players — total
_dml = config.DEMAND_MAPPED_LEAGUES
_dml_ph = ",".join("?" * len(_dml))

n_sellable_total = con.execute("""
    SELECT COUNT(*) FROM player_universe
    WHERE sellability_status = 'sellable_now'
""").fetchone()[0]
n_sellable_excluded = 0
if excluded_ids:
    ids_sql = "(" + ",".join(str(int(i)) for i in excluded_ids) + ")"
    n_sellable_excluded = con.execute(f"""
        SELECT COUNT(*) FROM player_universe
        WHERE player_id IN {ids_sql}
          AND sellability_status = 'sellable_now'
    """).fetchone()[0]
n_sellable_actionable = n_sellable_total - n_sellable_excluded

# SciSports coverage
n_rated = con.execute("""
    SELECT COUNT(*) FROM player_ratings
    WHERE current_ability IS NOT NULL OR potential_ability IS NOT NULL
""").fetchone()[0]
n_in_universe = con.execute("""
    SELECT COUNT(*) FROM player_universe pu
    WHERE sellability_status = 'sellable_now'
""").fetchone()[0]
n_unrated = max(0, n_in_universe - n_rated)

# Buyer requests — total across mapped leagues
n_buyer_requests = con.execute(f"""
    SELECT COUNT(*) FROM map_club_requests WHERE league IN ({_dml_ph})
""", _dml).fetchone()[0]
n_buying_clubs = con.execute(f"""
    SELECT COUNT(DISTINCT club_id) FROM map_club_requests WHERE league IN ({_dml_ph})
""", _dml).fetchone()[0]

# Total matches + level-fit distribution
n_total_matches = con.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
lf_counts = dict(con.execute("SELECT level_fit, COUNT(*) FROM matches GROUP BY level_fit").fetchall())
n_on_level = lf_counts.get("ON_LEVEL", 0)
n_upside   = lf_counts.get("UPSIDE", 0)
n_below    = lf_counts.get("BELOW", 0)

# Top match — picks the active scoring column (Market View or Brokerage) per
# the sidebar toggle so the tile reflects whichever lens the user is in.
_score_col = db.active_match_score_col()
top_match_row = con.execute(f"""
    SELECT player_id, player_name, buyer_club_id, buyer_club_name, {_score_col}
    FROM matches
    WHERE player_id NOT IN ({','.join(str(int(i)) for i in excluded_ids) or '-1'})
      AND {_score_col} IS NOT NULL
    ORDER BY {_score_col} DESC LIMIT 1
""").fetchone() if True else None

if top_match_row:
    tp_pid, tp_pname, tp_bcid, tp_bname, tp_score = top_match_row
    tp_player_disp = labels.player_display_name(int(tp_pid), tp_pname)
    tp_buyer_disp = labels.club_display_name(int(tp_bcid) if tp_bcid else None, tp_bname)
    top_match_value = f"{float(tp_score):.1f}"
    top_match_subline = f"{tp_player_disp} → {tp_buyer_disp}"
else:
    top_match_value = "—"
    top_match_subline = ""


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
    f"({n_sellable_total} before Kill List) · {n_rated} rated · {n_unrated} pending"
    if n_sellable_excluded > 0 else f"{n_rated} rated · {n_unrated} pending"
)

tiles = [
    _tile("Sellable players", str(n_sellable_actionable),
          subline=sellable_subline,
          title="Players passing the sellable filters across all 19 leagues. "
                "Excludes Kill List. SciSports coverage shown on the subline."),
    _tile("Buyer requests", f"{n_buyer_requests:,}",
          subline=f"from {n_buying_clubs} buying clubs",
          title="Buyer requests across the 10 demand-mapped leagues."),
    _tile("Total matches", f"{n_total_matches:,}",
          subline=(f'<span style="color:#15803d; font-weight:600;">{n_on_level}</span> ON LEVEL · '
                   f'<span style="color:#b45309; font-weight:600;">{n_upside}</span> UPSIDE · '
                   f'<span style="color:#6b7280; font-weight:600;">{n_below}</span> BELOW'),
          title="Match rows after the SciSports level-fit weighting."),
    _tile("Top match", top_match_value,
          subline=top_match_subline,
          title="Highest match score across the whole product (Kill List excluded)."),
]
st.markdown(f'<div class="rvc-tile-row">{"".join(tiles)}</div>', unsafe_allow_html=True)


# ─── Heat map — Position × Demand-mapped League ──────────────────────────────
HEAT_POSITIONS = ["GK", "CB", "RB", "LB", "DM", "CM", "AM", "LW", "RW", "ST_CF"]
HEAT_LEAGUES   = list(config.DEMAND_MAPPED_LEAGUES)

# Phase C (2026-06-09): supply uses Market View cohort definition consistent
# with scripts/22_match_engine.py tension calc:
#   sellability_score >= 35
#   AND player_ratings.current_ability IS NOT NULL
#   AND COALESCE(is_imminent_free_agent, 0) = 0
# Aggregated per (position bucket × parent-league).
_supply = {(p, l): 0 for p in HEAT_POSITIONS for l in HEAT_LEAGUES}
for pos, lg, n in con.execute("""
    SELECT pu.position_bucket, cp.league_id, COUNT(*)
    FROM player_universe pu
    JOIN club_pressure cp ON cp.club_id = pu.parent_club_id
    JOIN player_ratings pr ON pr.tm_player_id = pu.player_id
    WHERE pu.sellability_score >= 35
      AND pr.current_ability IS NOT NULL
      AND COALESCE(pu.is_imminent_free_agent, 0) = 0
    GROUP BY pu.position_bucket, cp.league_id
""").fetchall():
    if (pos, lg) in _supply:
        _supply[(pos, lg)] = int(n)

# Demand: raw count from UNION of map_club_requests + inferred_club_requests
# per the Phase A.8.7 tension recompute. No 0.4 intensity weighting at this
# aggregate level — per-pair scoring keeps it, but the macro market state
# uses raw counts so Market Overview matches the engine's tension table.
_demand = {(p, l): 0 for p in HEAT_POSITIONS for l in HEAT_LEAGUES}
for pos, lg, n in con.execute(f"""
    SELECT position_bucket, league, COUNT(*)
    FROM (
        SELECT position_bucket, league FROM map_club_requests
        WHERE league IN ({_dml_ph})
        UNION ALL
        SELECT position_bucket, league FROM inferred_club_requests
        WHERE league IN ({_dml_ph})
    )
    GROUP BY position_bucket, league
""", (*_dml, *_dml)).fetchall():
    if (pos, lg) in _demand:
        _demand[(pos, lg)] = int(n)


# Cell colour: unified white→green via heatmap_gradient. Ratio scaled so 1.3
# (the engine's "tight market" anchor) maps to the dark-green stop; anything
# above caps to dark green (cap-aware via heatmap_gradient's >=100 clamp).
# 0.0 ratio → white. 0.7 ≈ light green. 1.3+ → dark green.
def _cell_state(supply: int, demand: int) -> tuple[str, float | None]:
    """Return (state_label, scaled_value) where scaled_value drives the
    heatmap_gradient colour. None = no signal (cell renders white)."""
    if supply <= 0 or demand <= 0:
        return ("No signal", None)
    ratio = demand / supply
    if ratio >= 1.3:
        label = "Tight (sellers in control)"
    elif ratio <= 0.7:
        label = "Loose (buyers in control)"
    else:
        label = "Balanced"
    # Scale: ratio of 1.3 → 100 (dark-green anchor); higher caps via the
    # gradient's own >=100 clamp.
    return label, min(120.0, (ratio / 1.3) * 100.0)


# Info-icon tooltip for the heat map. Phase C: rewritten for the unified
# white→green palette (same as every other score column in the app).
_heat_icon_block = (
    '<details style="display:inline-block; margin-left:8px;">'
    '<summary style="display:inline; cursor:pointer; color:#1F3864; '
    'font-weight:600; font-size:0.8rem; list-style:none;">heat map ⓘ</summary>'
    '<div style="margin-top:10px; padding:14px 16px; background:#ffffff; '
    'border:1px solid #e5e7eb; border-radius:6px; font-size:0.85rem; '
    'color:#374151; line-height:1.55; max-width:520px;">'
    '<p style="margin:0 0 10px 0;">Each cell sits at the intersection of '
    'a <strong>position bucket</strong> (rows) and a '
    '<strong>demand-mapped league</strong> (columns). Colour reflects the '
    'demand÷supply ratio scaled against the engine\'s tight-market threshold '
    '(1.3). Same white→green palette as Sellability / Brokerage Score / '
    'Market Score columns elsewhere in the app.</p>'
    '<div style="margin:6px 0;"><span style="display:inline-block; width:14px; '
    'height:14px; background:#16a34a; border-radius:3px; vertical-align:middle; '
    'margin-right:6px;"></span><strong>Dark green</strong> — ratio ≥ 1.3 (tight market, sellers in control)</div>'
    '<div style="margin:6px 0;"><span style="display:inline-block; width:14px; '
    'height:14px; background:#8ed0a3; border-radius:3px; vertical-align:middle; '
    'margin-right:6px;"></span><strong>Medium green</strong> — ratio ≈ 0.9–1.2 (balanced to slightly tight)</div>'
    '<div style="margin:6px 0;"><span style="display:inline-block; width:14px; '
    'height:14px; background:#e8f7ed; border-radius:3px; vertical-align:middle; '
    'margin-right:6px;"></span><strong>Pale</strong> — ratio ≈ 0.3–0.6 (oversupplied, buyers in control)</div>'
    '<div style="margin:6px 0;"><span style="display:inline-block; width:14px; '
    'height:14px; background:#f3f4f6; border:1px solid #e5e7eb; border-radius:3px; '
    'vertical-align:middle; margin-right:6px;"></span><strong>No signal</strong> — supply or demand is zero</div>'
    '<p style="margin:10px 0 0 0; color:#6b7280; font-size:0.8rem;">'
    'Supply = Market View cohort (sellability ≥ 35, has CA, not IFA). '
    'Demand = raw count from UNION of explicit + inferred buyer requests. '
    'Hover any cell for the underlying counts and ratio.</p>'
    '</div></details>'
)

st.markdown(
    f'<div style="margin-top:24px; display:flex; align-items:baseline; gap:6px;">'
    f'<div style="font-size:1.1rem; font-weight:700; color:#111827;">'
    f'Position × League heat map</div>{_heat_icon_block}</div>',
    unsafe_allow_html=True,
)

# Build the grid HTML — 12 columns (1 row-header + 10 league cols + 1 global),
# N+1 rows. The rightmost "All" column shows the engine's global per-position
# tension ratio across all 19 leagues — same numbers the scripts/22_match_engine.py
# tension table prints. Lets the user see both the per-league breakdown AND
# the global rollup on the same page.
LEAGUE_SHORT = {
    "GB1":"PL","GB2":"Champ","ES1":"LaLiga","IT1":"Serie A",
    "FR1":"L1","FR2":"L2","L1":"Bund","PO1":"Primeira",
    "NL1":"Eredivisie","BE1":"Pro Lge",
}
POS_SHORT = {p: labels.display_bucket(p) for p in HEAT_POSITIONS}

# Phase C: global per-position tension — supply across the full Market View
# cohort, demand across the full UNION of explicit + inferred requests. Same
# definition as scripts/22_match_engine.py.
_global_supply = dict(con.execute("""
    SELECT pu.position_bucket, COUNT(*)
    FROM player_universe pu
    JOIN player_ratings pr ON pr.tm_player_id = pu.player_id
    WHERE pu.sellability_score >= 35
      AND pr.current_ability IS NOT NULL
      AND COALESCE(pu.is_imminent_free_agent, 0) = 0
    GROUP BY pu.position_bucket
""").fetchall())
_global_demand = dict(con.execute("""
    SELECT position_bucket, COUNT(*) FROM (
        SELECT position_bucket FROM map_club_requests WHERE position_bucket IS NOT NULL
        UNION ALL
        SELECT position_bucket FROM inferred_club_requests WHERE position_bucket IS NOT NULL
    ) GROUP BY position_bucket
""").fetchall())

grid_cells = []
# Header row — top-left empty corner + league columns + "All" global column
grid_cells.append(
    '<div style="background:transparent;"></div>'
)
for lg in HEAT_LEAGUES:
    short = LEAGUE_SHORT.get(lg, lg)
    full = labels.league_name(lg)
    grid_cells.append(
        f'<div style="font-size:0.72rem; font-weight:600; color:#6b7280; '
        f'text-align:center; padding:4px 2px; letter-spacing:0.02em;" '
        f'title="{full}">{short}</div>'
    )
# Global-rollup column header — matches the engine's position tension table
grid_cells.append(
    '<div style="font-size:0.72rem; font-weight:700; color:#111827; '
    'text-align:center; padding:4px 2px; letter-spacing:0.02em; '
    'border-left:2px solid #d1d5db;" '
    'title="Engine global tension — supply and demand across all 19 leagues. '
    'Same numbers as scripts/22_match_engine.py prints.">All</div>'
)

# Body — one row per position
for pos in HEAT_POSITIONS:
    pos_full = {"GK": "Goalkeeper", "CB": "Centre-back", "LB": "Left-back",
                "RB": "Right-back", "DM": "Defensive midfielder",
                "CM": "Central midfielder", "AM": "Attacking midfielder",
                "LW": "Left winger", "RW": "Right winger",
                "ST_CF": "Striker"}.get(pos, pos)
    grid_cells.append(
        f'<div style="font-size:0.78rem; font-weight:600; color:#374151; '
        f'padding:4px 8px; text-align:right;" title="{pos_full}">'
        f'{POS_SHORT[pos]}</div>'
    )
    for lg in HEAT_LEAGUES:
        supply = _supply.get((pos, lg), 0)
        demand = _demand.get((pos, lg), 0)
        state, scaled = _cell_state(supply, demand)
        lg_short = LEAGUE_SHORT.get(lg, lg)
        ratio = (demand / supply) if supply > 0 else None
        ratio_str = f"{ratio:.2f}" if ratio is not None else "—"
        tooltip = (f"{POS_SHORT[pos]} × {lg_short}\n"
                   f"Supply: {supply} player{'s' if supply != 1 else ''}\n"
                   f"Demand: {demand} buyer request{'s' if demand != 1 else ''}\n"
                   f"Ratio: {ratio_str}\n"
                   f"State: {state}")
        if scaled is None:
            # No signal — neutral grey fill
            style = "background:#f3f4f6; color:#9ca3af;"
            inner = "&nbsp;"
        else:
            # Unified white→green via heatmap_gradient (cap-aware ≥100).
            style = ui.heatmap_gradient(scaled, 0, 100) or "background:#ffffff;"
            inner = (f'<span style="font-weight:700;">{demand}</span>'
                     f'<span style="opacity:0.7; margin:0 2px;">/</span>'
                     f'<span style="font-weight:500;">{supply}</span>')
        grid_cells.append(
            f'<div title="{tooltip}" style="{style} '
            f'border-radius:4px; padding:8px 6px; text-align:center; '
            f'font-size:0.78rem; line-height:1.2; min-height:38px; '
            f'display:flex; align-items:center; justify-content:center;">'
            f'{inner}</div>'
        )
    # Global rollup cell for this position (rightmost column).
    g_supply = int(_global_supply.get(pos, 0))
    g_demand = int(_global_demand.get(pos, 0))
    g_state, g_scaled = _cell_state(g_supply, g_demand)
    g_ratio = (g_demand / g_supply) if g_supply > 0 else None
    g_ratio_str = f"{g_ratio:.2f}" if g_ratio is not None else "—"
    g_tooltip = (f"{POS_SHORT[pos]} — All 19 leagues (engine global)\n"
                 f"Supply: {g_supply} player{'s' if g_supply != 1 else ''}\n"
                 f"Demand: {g_demand} buyer request{'s' if g_demand != 1 else ''}\n"
                 f"Ratio: {g_ratio_str}\n"
                 f"State: {g_state}")
    if g_scaled is None:
        g_style = "background:#f3f4f6; color:#9ca3af;"
        g_inner = "&nbsp;"
    else:
        g_style = ui.heatmap_gradient(g_scaled, 0, 100) or "background:#ffffff;"
        # Show the ratio itself in this rollup cell — it's the engine number.
        g_inner = f'<span style="font-weight:800;">{g_ratio:.2f}</span>'
    grid_cells.append(
        f'<div title="{g_tooltip}" style="{g_style} '
        f'border-radius:4px; padding:8px 6px; text-align:center; '
        f'font-size:0.82rem; line-height:1.2; min-height:38px; '
        f'border-left:2px solid #d1d5db; '
        f'display:flex; align-items:center; justify-content:center;">'
        f'{g_inner}</div>'
    )

st.markdown(
    '<div style="display:grid; grid-template-columns: 110px repeat(10, 1fr) 80px; '
    'gap:4px; margin:8px 0 6px 0; max-width:1180px;">'
    + "".join(grid_cells) +
    '</div>'
    '<div style="font-size:0.75rem; color:#9ca3af; margin-bottom:24px;">'
    'Each per-league cell shows <strong>demand / supply</strong> counts. The rightmost '
    '<strong>All</strong> column shows the engine\'s global per-position tension ratio across '
    'all 19 leagues — same numbers <code>scripts/22_match_engine.py</code> prints. '
    'Hover for the full breakdown.</div>',
    unsafe_allow_html=True,
)


# ─── Two-column row: Top 5 opportunities + Top 5 pressured clubs ─────────────
st.markdown("---")
col_opps, col_pressed = st.columns(2)


def _money(x) -> str:
    if x is None or pd.isna(x): return "—"
    x = float(x)
    if abs(x) >= 1_000_000: return f"€{x/1_000_000:.1f}m"
    if abs(x) >= 1_000:     return f"€{x/1_000:.0f}k"
    return f"€{int(x)}"


with col_opps:
    # Header reflects the active view; the score column follows the toggle.
    _view_label = db.active_view_label()
    st.markdown(f"### Top 5 opportunities — {_view_label}")
    _score_col_top = db.active_match_score_col()
    excluded_csv = ",".join(str(int(i)) for i in excluded_ids) or "-1"
    top_opps = con.execute(f"""
        SELECT player_id, player_name, buyer_club_id, buyer_club_name,
               {_score_col_top}, level_fit
        FROM matches
        WHERE player_id NOT IN ({excluded_csv})
          AND {_score_col_top} IS NOT NULL
        ORDER BY {_score_col_top} DESC LIMIT 5
    """).fetchall()
    if not top_opps:
        st.info("No matches yet — run the matcher.")
    else:
        rows_html = []
        for pid, pname, bcid, bname, score, lf in top_opps:
            pname_disp = labels.player_display_name(int(pid), pname)
            bname_disp = labels.club_display_name(int(bcid) if bcid else None, bname)
            player_html = ui.player_link(int(pid), pname_disp)
            buyer_html  = ui.club_link(int(bcid) if bcid else None, bname_disp)
            pill = ui.level_fit_pill(lf)
            rows_html.append(
                f'<tr><td style="padding:8px 10px; border-bottom:1px solid #f3f4f6;">{player_html}</td>'
                f'<td style="padding:8px 10px; border-bottom:1px solid #f3f4f6;">{buyer_html}</td>'
                f'<td style="padding:8px 10px; border-bottom:1px solid #f3f4f6; white-space:nowrap;">'
                f'<strong>{float(score):.1f}</strong> &nbsp; {pill}</td></tr>'
            )
        st.markdown(
            '<table style="width:100%; border-collapse:collapse; font-size:0.875rem;">'
            '<thead><tr style="background:#1F3864; color:white;">'
            '<th style="padding:10px; text-align:left;">Player</th>'
            '<th style="padding:10px; text-align:left;">Best buyer</th>'
            '<th style="padding:10px; text-align:left;">Match score</th>'
            '</tr></thead><tbody>' + "".join(rows_html) + '</tbody></table>'
            '<div style="font-size:0.75rem; color:#6b7280; margin-top:8px;">'
            'Match score includes SciSports level-fit weighting. Hover the pill for details.</div>',
            unsafe_allow_html=True,
        )

with col_pressed:
    st.markdown("### Top 5 most pressured selling clubs")
    pressed = con.execute("""
        SELECT cp.club_id, cp.name, cp.total_pressure_score, cp.league_id,
               (SELECT COUNT(*) FROM player_universe pu
                WHERE pu.parent_club_id = cp.club_id
                  AND pu.sellability_status = 'sellable_now'
               ) AS n_sellable
        FROM club_pressure cp
        WHERE cp.total_pressure_score IS NOT NULL
        ORDER BY cp.total_pressure_score DESC LIMIT 5
    """).fetchall()
    # Stretched gradient — vmin/vmax from the displayed values
    _press_vals = [float(r[2]) for r in pressed if r[2] is not None]
    _pmin = max(0.0, min(_press_vals) - 2) if _press_vals else 0.0
    _pmax = max(_press_vals) + 2 if _press_vals else 100.0

    if not pressed:
        st.info("No pressured clubs yet.")
    else:
        rows_html = []
        for cid, name, score, lg, n_sell in pressed:
            cdisp = labels.club_display_name(int(cid) if cid else None, name)
            club_html = ui.club_link(int(cid) if cid else None, cdisp)
            # Top sellable player at this club
            top_player_row = con.execute("""
                SELECT player_id, name FROM player_universe
                WHERE parent_club_id = ?
                  AND sellability_status = 'sellable_now'
                ORDER BY sellability_score DESC LIMIT 1
            """, (cid,)).fetchone()
            if top_player_row:
                tp_pid, tp_pname = top_player_row
                tp_disp = labels.player_display_name(int(tp_pid), tp_pname)
                top_player_html = ui.player_link(int(tp_pid), tp_disp)
            else:
                top_player_html = "—"
            # Phase C — unified white→green palette via heatmap_gradient.
            grad = ui.heatmap_gradient(float(score), 0, 100)
            rows_html.append(
                f'<tr>'
                f'<td style="padding:8px 10px; border-bottom:1px solid #f3f4f6;">{club_html}</td>'
                f'<td style="padding:8px 10px; border-bottom:1px solid #f3f4f6; '
                f'font-weight:700; {grad}">{float(score):.1f}</td>'
                f'<td style="padding:8px 10px; border-bottom:1px solid #f3f4f6;">{n_sell}</td>'
                f'<td style="padding:8px 10px; border-bottom:1px solid #f3f4f6;">{top_player_html}</td>'
                f'</tr>'
            )
        st.markdown(
            '<table style="width:100%; border-collapse:collapse; font-size:0.875rem;">'
            '<thead><tr style="background:#1F3864; color:white;">'
            '<th style="padding:10px; text-align:left;">Club</th>'
            '<th style="padding:10px; text-align:left;">Selling pressure</th>'
            '<th style="padding:10px; text-align:left;">Sellable</th>'
            '<th style="padding:10px; text-align:left;">Top sellable player</th>'
            '</tr></thead><tbody>' + "".join(rows_html) + '</tbody></table>',
            unsafe_allow_html=True,
        )

# ─── Two-column row: Position state cards + League net-role cards ────────────
st.markdown("")
col_pos, col_lg = st.columns(2)

with col_pos:
    st.markdown("### Position market states")
    # Tension per position — uses Market View cohort definition for supply
    # (matches scripts/22_match_engine.py tension calc + the heat map above).
    # Demand = raw UNION of explicit + inferred buyer requests across all
    # leagues (the engine's macro market state).
    _pos_supply_all = dict(con.execute("""
        SELECT pu.position_bucket, COUNT(*)
        FROM player_universe pu
        JOIN player_ratings pr ON pr.tm_player_id = pu.player_id
        WHERE pu.sellability_score >= 35
          AND pr.current_ability IS NOT NULL
          AND COALESCE(pu.is_imminent_free_agent, 0) = 0
        GROUP BY pu.position_bucket
    """).fetchall())
    _pos_demand_all = dict(con.execute("""
        SELECT position_bucket, COUNT(*) FROM (
            SELECT position_bucket FROM map_club_requests
              WHERE position_bucket IS NOT NULL
            UNION ALL
            SELECT position_bucket FROM inferred_club_requests
              WHERE position_bucket IS NOT NULL
        ) GROUP BY position_bucket
    """).fetchall())
    pos_rows = []
    for pos in HEAT_POSITIONS:
        supply = int(_pos_supply_all.get(pos, 0))
        demand = int(_pos_demand_all.get(pos, 0))
        state, _scaled = _cell_state(supply, demand)
        # Card border colour reflects categorical state. Cards stay
        # red/blue/green-banded because they're a directional categorical
        # signal — distinct from the heat map's continuous-intensity palette.
        if state.startswith("Tight"):
            border, accent = "#fecaca", "#b91c1c"
        elif state.startswith("Loose"):
            border, accent = "#bfdbfe", "#1d4ed8"
        elif state == "Balanced":
            border, accent = "#bbf7d0", "#15803d"
        else:
            border, accent = "#e5e7eb", "#9ca3af"
        pos_full = {"GK": "Goalkeeper", "CB": "Centre-back", "LB": "Left-back",
                    "RB": "Right-back", "DM": "Defensive midfielder",
                    "CM": "Central midfielder", "AM": "Attacking midfielder",
                    "LW": "Left winger", "RW": "Right winger",
                    "ST_CF": "Striker"}.get(pos, pos)
        pos_rows.append(
            f'<a href="{ui.with_auth(f"/position_view?bucket={pos}")}" target="_self" '
            f'style="text-decoration:none; color:inherit;">'
            f'<div style="background:#ffffff; border:1px solid {border}; '
            f'border-radius:6px; padding:10px 12px; cursor:pointer; '
            f'transition:border-color 0.15s, box-shadow 0.15s;" '
            f'onmouseover="this.style.borderColor=\'{accent}\'; this.style.boxShadow=\'0 2px 6px rgba(15,23,42,0.06)\';" '
            f'onmouseout="this.style.borderColor=\'{border}\'; this.style.boxShadow=\'none\';">'
            f'<div style="font-weight:700; color:#111827; font-size:0.95rem;">{pos_full}</div>'
            f'<div style="color:{accent}; font-weight:600; font-size:0.82rem; margin-top:2px;">{state}</div>'
            f'<div style="color:#6b7280; font-size:0.75rem; margin-top:4px;">'
            f'{supply} sellers · {demand} buyers</div>'
            f'</div></a>'
        )
    st.markdown(
        '<div style="display:grid; grid-template-columns:1fr 1fr; gap:8px;">'
        + "".join(pos_rows) + '</div>',
        unsafe_allow_html=True,
    )

with col_lg:
    st.markdown("### League net roles")
    lg_rows = []
    for lg in HEAT_LEAGUES:
        # Sellers from this league
        n_sellers = con.execute("""
            SELECT COUNT(*) FROM player_universe pu
            JOIN club_pressure cp ON cp.club_id = pu.parent_club_id
            WHERE cp.league_id = ?
              AND pu.player_id NOT IN ({excl})
              AND pu.sellability_status = 'sellable_now'
        """.format(excl=excluded_csv), (lg,)).fetchone()[0]
        # Requests from this league
        n_req = con.execute("""
            SELECT COUNT(*) FROM map_club_requests WHERE league = ?
        """, (lg,)).fetchone()[0]
        # Net role (same logic as League View)
        if n_req <= 0 and n_sellers <= 0:
            net, accent, border = "No clear signal", "#6b7280", "#e5e7eb"
        elif n_req <= 0:
            net, accent, border = "Supply only", "#6b7280", "#e5e7eb"
        elif n_sellers <= 0:
            net, accent, border = "Net buyer", "#1d4ed8", "#bfdbfe"
        else:
            ratio = n_sellers / n_req
            if ratio >= 1.3:
                net, accent, border = "Net supplier", "#b91c1c", "#fecaca"
            elif ratio <= 0.7:
                net, accent, border = "Net buyer", "#1d4ed8", "#bfdbfe"
            else:
                net, accent, border = "Balanced", "#15803d", "#bbf7d0"
        lg_display = labels.league_name(lg)
        lg_rows.append(
            f'<a href="{ui.with_auth(f"/league_view?league={lg}")}" target="_self" '
            f'style="text-decoration:none; color:inherit;">'
            f'<div style="background:#ffffff; border:1px solid {border}; '
            f'border-radius:6px; padding:10px 12px; cursor:pointer; '
            f'transition:border-color 0.15s, box-shadow 0.15s;" '
            f'onmouseover="this.style.borderColor=\'{accent}\'; this.style.boxShadow=\'0 2px 6px rgba(15,23,42,0.06)\';" '
            f'onmouseout="this.style.borderColor=\'{border}\'; this.style.boxShadow=\'none\';">'
            f'<div style="font-weight:700; color:#111827; font-size:0.95rem;">{lg_display}</div>'
            f'<div style="color:{accent}; font-weight:600; font-size:0.82rem; margin-top:2px;">{net}</div>'
            f'<div style="color:#6b7280; font-size:0.75rem; margin-top:4px;">'
            f'{n_sellers} sellable · {n_req} requests</div>'
            f'</div></a>'
        )
    st.markdown(
        '<div style="display:grid; grid-template-columns:1fr 1fr; gap:8px;">'
        + "".join(lg_rows) + '</div>',
        unsafe_allow_html=True,
    )


# ─── Footer caption ──────────────────────────────────────────────────────────
st.markdown(
    f'<div style="margin-top:32px; padding-top:16px; border-top:1px solid #e5e7eb; '
    f'color:#9ca3af; font-size:0.8rem; line-height:1.5;">'
    f'Snapshot frozen <strong>{snapshot.isoformat()}</strong>. '
    f'SciSports talent layer covers <strong>{n_rated}/{n_in_universe}</strong> '
    f'sellable cohort players. Demand mapped across <strong>{n_demand_mapped}</strong> '
    f'leagues. Stage 2 will extend demand coverage to second-tier and '
    f'rest-of-world leagues.'
    f'</div>',
    unsafe_allow_html=True,
)

