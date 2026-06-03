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
with st.sidebar:
    st.markdown(
        """
        <div class="rvc-wordmark">
            <div class="rvc-wordmark-title">Brokerage Engine</div>
            <div class="rvc-wordmark-subtitle">
                <span class="rvc-wordmark-attribution">RMC</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
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

# Top match (Kill-List-excluded, ordered after level-fit multiplier)
top_match_row = con.execute(f"""
    SELECT player_id, player_name, buyer_club_id, buyer_club_name, match_score
    FROM matches
    WHERE player_id NOT IN ({','.join(str(int(i)) for i in excluded_ids) or '-1'})
    ORDER BY match_score DESC LIMIT 1
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

# Supply: sellable players in position P whose parent_club is in league L.
# Build via two passes — counts dict[(pos, league)] -> n
_supply = {(p, l): 0 for p in HEAT_POSITIONS for l in HEAT_LEAGUES}
for pos, lg, n in con.execute("""
    SELECT pu.position_bucket, cp.league_id, COUNT(*)
    FROM player_universe pu
    JOIN club_pressure cp ON cp.club_id = pu.parent_club_id
    WHERE pu.sellability_status = 'sellable_now'
    GROUP BY pu.position_bucket, cp.league_id
""").fetchall():
    if (pos, lg) in _supply:
        _supply[(pos, lg)] = int(n)

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


# Same colour convention as Position View's _tension_label.
def _cell_state(supply: int, demand: int) -> tuple[str, str, str]:
    """Return (state_label, css-colour, css-text-colour)."""
    if supply <= 0 or demand <= 0:
        return ("No signal", "#f3f4f6", "#9ca3af")
    ratio = demand / supply
    if ratio >= 1.3:
        return ("Sellers in control", "#b91c1c", "#ffffff")
    if ratio <= 0.7:
        return ("Buyers in control",  "#1d4ed8", "#ffffff")
    return ("Balanced", "#15803d", "#ffffff")


# Info-icon tooltip for the heat map — same <details>/<summary> pattern as
# the existing TM valuation methodology + Level fit icons.
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
    'demand-to-supply ratio at that intersection:</p>'
    '<div style="margin:6px 0;"><span style="display:inline-block; width:14px; '
    'height:14px; background:#b91c1c; border-radius:3px; vertical-align:middle; '
    'margin-right:6px;"></span><strong>Sellers in control</strong> — demand÷supply ≥ 1.3</div>'
    '<div style="margin:6px 0;"><span style="display:inline-block; width:14px; '
    'height:14px; background:#15803d; border-radius:3px; vertical-align:middle; '
    'margin-right:6px;"></span><strong>Balanced</strong> — ratio between 0.7 and 1.3</div>'
    '<div style="margin:6px 0;"><span style="display:inline-block; width:14px; '
    'height:14px; background:#1d4ed8; border-radius:3px; vertical-align:middle; '
    'margin-right:6px;"></span><strong>Buyers in control</strong> — ratio ≤ 0.7</div>'
    '<div style="margin:6px 0;"><span style="display:inline-block; width:14px; '
    'height:14px; background:#f3f4f6; border:1px solid #e5e7eb; border-radius:3px; '
    'vertical-align:middle; margin-right:6px;"></span><strong>No signal</strong> — supply or demand is zero</div>'
    '<p style="margin:10px 0 0 0; color:#6b7280; font-size:0.8rem;">'
    'Hover any cell for the underlying supply / demand counts.</p>'
    '</div></details>'
)

st.markdown(
    f'<div style="margin-top:24px; display:flex; align-items:baseline; gap:6px;">'
    f'<div style="font-size:1.1rem; font-weight:700; color:#111827;">'
    f'Position × League heat map</div>{_heat_icon_block}</div>',
    unsafe_allow_html=True,
)

# Build the grid HTML — 11 columns (1 row-header + 10 league cols), N+1 rows.
# Short league abbreviations fit in the column headers; cell tooltips
# carry the full breakdown.
LEAGUE_SHORT = {
    "GB1":"PL","GB2":"Champ","ES1":"LaLiga","IT1":"Serie A",
    "FR1":"L1","FR2":"L2","L1":"Bund","PO1":"Primeira",
    "NL1":"Eredivisie","BE1":"Pro Lge",
}
POS_SHORT = {p: labels.display_bucket(p) for p in HEAT_POSITIONS}

grid_cells = []
# Header row — top-left empty corner + league columns
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
        state, bg, fg = _cell_state(supply, demand)
        lg_short = LEAGUE_SHORT.get(lg, lg)
        tooltip = (f"{POS_SHORT[pos]} × {lg_short}\n"
                   f"Supply: {supply} player{'s' if supply != 1 else ''}\n"
                   f"Demand: {demand} buyer request{'s' if demand != 1 else ''}\n"
                   f"State: {state}")
        # Cell content — supply/demand counts inside the tile when both > 0,
        # otherwise blank (state colour alone communicates "no signal").
        if supply > 0 and demand > 0:
            inner = (f'<span style="font-weight:700;">{demand}</span>'
                     f'<span style="opacity:0.7; margin:0 2px;">/</span>'
                     f'<span style="font-weight:500;">{supply}</span>')
        else:
            inner = "&nbsp;"
        grid_cells.append(
            f'<div title="{tooltip}" style="background:{bg}; color:{fg}; '
            f'border-radius:4px; padding:8px 6px; text-align:center; '
            f'font-size:0.78rem; line-height:1.2; min-height:38px; '
            f'display:flex; align-items:center; justify-content:center;">'
            f'{inner}</div>'
        )

st.markdown(
    '<div style="display:grid; grid-template-columns: 110px repeat(10, 1fr); '
    'gap:4px; margin:8px 0 6px 0; max-width:1100px;">'
    + "".join(grid_cells) +
    '</div>'
    '<div style="font-size:0.75rem; color:#9ca3af; margin-bottom:24px;">'
    'Each cell shows <strong>demand / supply</strong> counts. Hover for the full breakdown.</div>',
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
    st.markdown("### Top 5 brokerage opportunities")
    excluded_csv = ",".join(str(int(i)) for i in excluded_ids) or "-1"
    top_opps = con.execute(f"""
        SELECT player_id, player_name, buyer_club_id, buyer_club_name,
               match_score, level_fit
        FROM matches
        WHERE player_id NOT IN ({excluded_csv})
        ORDER BY match_score DESC LIMIT 5
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
            grad = ui.green_gradient(float(score), _pmin, _pmax)
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
    # Tension per position. Supply = sellable players in this position across
    # ALL 19 leagues (matches Position View's overview cards). Demand = buyer
    # requests across the 10 demand-mapped leagues only (we don't trust demand
    # signal from non-mapped leagues).
    _pos_supply_all = dict(con.execute("""
        SELECT pu.position_bucket, COUNT(*)
        FROM player_universe pu
        WHERE pu.sellability_status = 'sellable_now'
        GROUP BY pu.position_bucket
    """).fetchall())
    _pos_demand_mapped = dict(con.execute(f"""
        SELECT position_bucket, COUNT(*)
        FROM map_club_requests WHERE league IN ({_dml_ph})
        GROUP BY position_bucket
    """, _dml).fetchall())
    pos_rows = []
    for pos in HEAT_POSITIONS:
        supply = int(_pos_supply_all.get(pos, 0))
        demand = int(_pos_demand_mapped.get(pos, 0))
        state, bg, fg = _cell_state(supply, demand)
        # Card colour intensity reflects state
        if state == "Sellers in control":
            border, accent = "#fecaca", "#b91c1c"
        elif state == "Buyers in control":
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

