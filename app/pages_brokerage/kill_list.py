"""Kill List — full transparency view of the exclusion layer.

Manually flagged players that are removed from matching, KPI counts, and
auto-generated commentary — but still surfaced in tables across the product
with a red Excluded badge. The page exists so the exclusion layer is
auditable rather than hidden.

Source of truth (NOT data/player_overrides.xlsx — that file is legacy):
  - Manual entries:  BrokerageWorkbook.xlsx → "Kill List" tab
  - Agency rules:    data/blocked_agencies.csv

Both are read live every page render with a 60s cache TTL.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

import db
import labels
import components as ui

st.set_page_config(
    page_title="Brokerage Engine",
    page_icon="🚫",
    layout="wide",
    initial_sidebar_state="expanded",
)
ui.inject_css()

# ─── Sidebar — wordmark + global search only ─────────────────────────────────
ui.render_page_accent()
ui.render_sidebar_engine_header()
ui.render_global_search(db.get_player_search_options(), db.get_club_search_options())


# ─── Pull data ───────────────────────────────────────────────────────────────
df = db.get_excluded()
state = db.get_kill_list_state()
con = db.get_connection()

# Enrich with age + cleaned league name (db.get_excluded only carries the
# league code). Doing a single per-page lookup keyed by player_id.
if len(df):
    pid_csv = ",".join(str(int(p)) for p in df["player_id"])
    age_lookup = dict(con.execute(
        f"SELECT player_id, age FROM player_universe WHERE player_id IN ({pid_csv})"
    ).fetchall())
    df["age"] = df["player_id"].apply(lambda pid: age_lookup.get(int(pid)))
else:
    df["age"] = []


# ─── Header — title + info icon + subline ────────────────────────────────────
_kill_list_info_icon = (
    '<details style="display:inline-block; margin-left:8px;">'
    '<summary style="display:inline; cursor:pointer; color:#1F3864; '
    'font-weight:600; font-size:0.85rem; list-style:none;">how this works ⓘ</summary>'
    '<div style="margin-top:10px; padding:14px 16px; background:#ffffff; '
    'border:1px solid #e5e7eb; border-radius:6px; font-size:0.85rem; '
    'color:#374151; line-height:1.55; max-width:580px;">'
    '<p style="margin:0 0 10px 0;">The <strong>Kill List</strong> captures '
    'players we\'ve manually flagged out of the active sellable cohort. '
    'Common reasons include behavioural concerns, injury status, agent '
    'conflicts, or strategic deprioritisation.</p>'
    '<p style="margin:0 0 6px 0; font-weight:600; color:#111827;">How exclusion works:</p>'
    '<ul style="margin:0 0 10px 0; padding-left:20px;">'
    '<li>Excluded players are removed from match commentary and KPI tile '
    'counts (e.g. <em>"10 players available (12 before Kill List)"</em> — '
    'the 2-player gap is the Kill List).</li>'
    '<li>Excluded players still appear in tables across the product with a '
    'red <strong>Excluded</strong> badge — transparency over filtering.</li>'
    '<li>Their sellability score is still computed; it just isn\'t actioned.</li>'
    '<li>Excluded players still receive a SciSports rating row in '
    '<code style="background:#f3f4f6; padding:1px 4px; border-radius:3px;">'
    'scisports_ratings.xlsx</code> (status = killed) for completeness when '
    'the kill is lifted.</li>'
    '</ul>'
    '<p style="margin:10px 0 0 0; padding-top:10px; border-top:1px solid #f3f4f6; '
    'color:#6b7280; font-size:0.8rem;">'
    'Managed via the <code style="background:#f3f4f6; padding:1px 4px; '
    'border-radius:3px;">Kill List</code> tab in '
    '<code style="background:#f3f4f6; padding:1px 4px; border-radius:3px;">'
    'BrokerageWorkbook.xlsx</code> (manual entries) and '
    '<code style="background:#f3f4f6; padding:1px 4px; border-radius:3px;">'
    'data/blocked_agencies.csv</code> (agency rules). Edits propagate on the '
    'next refresh.</p>'
    '</div></details>'
)

st.markdown(
    f'<div class="rvc-page-title">'
    f'<div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap;">'
    f'<h1 style="margin:0 !important;">Kill List</h1>'
    f'{_kill_list_info_icon}'
    f'</div>'
    f'<div class="rvc-page-subtitle" style="margin-top:6px;">'
    f'Players manually excluded from matching, KPIs, and commentary. '
    f'Excluded players remain visible in tables across the product (flagged) '
    f'so the exclusion layer is transparent rather than hidden.'
    f'</div></div>',
    unsafe_allow_html=True,
)


# ─── Warnings panel (manual entries that failed to match) ────────────────────
warnings = state.get("warnings") or []
if warnings:
    with st.expander(f"⚠ {len(warnings)} manual entries need attention "
                     f"(zero / ambiguous matches)", expanded=False):
        for w in warnings:
            if w["kind"] == "zero":
                st.warning(f"**ZERO MATCH**: `{w['entry']}` did not match any player. Entry NOT applied.")
            else:
                names = ", ".join(pn for _pid, pn in w["matches"])
                st.warning(
                    f"**AMBIGUOUS**: `{w['entry']}` matched {len(w['matches'])} players — {names}. "
                    f"Entry NOT applied (use a more specific name)."
                )


# ─── KPI tiles ───────────────────────────────────────────────────────────────
n_total = df["player_id"].nunique() if len(df) else 0


def _money(x) -> str:
    if x is None or pd.isna(x): return "—"
    x = float(x)
    if abs(x) >= 1_000_000:
        m = x / 1_000_000
        return f"€{m:.0f}m" if m == int(m) else f"€{m:.1f}m"
    if abs(x) >= 1_000:
        return f"€{x/1_000:.0f}k"
    return f"€{int(x)}"


def _tile(label: str, value: str, *, subline: str = "", title: str = "") -> str:
    sub_html = f'<div class="rvc-tile-sub">{subline}</div>' if subline else ""
    return (
        f'<div class="rvc-tile" title="{title}">'
        f'<div class="rvc-tile-label">{label}</div>'
        f'<div class="rvc-tile-value">{value}</div>'
        f'{sub_html}'
        f'</div>'
    )


if not len(df):
    st.markdown(
        f'<div class="rvc-tile-row">{_tile("Total excluded", "0")}</div>',
        unsafe_allow_html=True,
    )
    st.info("No players excluded. The Kill List is empty.")
    st.stop()


# Top 3 positions by exclusion count
pos_counts = (df["position"].fillna("—").value_counts().head(3).items())
pos_inline = " · ".join(
    f"{labels.display_bucket(p)} ×{n}" for p, n in pos_counts
) or "—"

# Top 3 leagues by exclusion count (use cleaned league names)
lg_counts = (df["league"].fillna("—").value_counts().head(3).items())
lg_inline = " · ".join(
    f"{labels.league_name(lg)} ×{n}" for lg, n in lg_counts
) or "—"

tiles = [
    _tile("Total excluded", str(n_total),
          title="Distinct players currently on the Kill List."),
    _tile("By position", "",
          subline=f'<span style="color:#374151; font-size:0.85rem; font-weight:600;">{pos_inline}</span>',
          title="Top 3 positions by exclusion count."),
    _tile("By league", "",
          subline=f'<span style="color:#374151; font-size:0.85rem; font-weight:600;">{lg_inline}</span>',
          title="Top 3 leagues by exclusion count (parent-club league)."),
]
st.markdown(f'<div class="rvc-tile-row">{"".join(tiles)}</div>', unsafe_allow_html=True)


# ─── Reasons bar chart — group by `source` ──────────────────────────────────
# Source values are "manual" (for hand-entered Kill List rows) and
# "agency:<rule_name>" (for blocked-agency rules). Useful breakdown — shows
# how much of the kill list is policy-driven (agency) vs case-by-case (manual).
source_counts = df["source"].value_counts()
if len(source_counts) > 1 or (len(source_counts) == 1 and source_counts.index[0] != "manual"):
    # Worth rendering only if there's a non-trivial breakdown
    st.markdown(
        '<div style="margin-top:18px; font-size:1rem; font-weight:700; color:#111827;">'
        'Exclusions by source</div>',
        unsafe_allow_html=True,
    )

    def _source_label(s: str) -> str:
        if s == "manual": return "Manual (case-by-case)"
        if s.startswith("agency:"): return f"Agency rule — {s.removeprefix('agency:')}"
        return s

    max_n = int(source_counts.max())
    bar_rows = []
    for src, n in source_counts.items():
        label = _source_label(src)
        width_pct = (n / max_n) * 100
        bar_rows.append(
            f'<div class="rvc-bar-row">'
            f'<div class="rvc-bar-label">{label}</div>'
            f'<div class="rvc-bar-track">'
            f'<div class="rvc-bar-fill" style="width:{width_pct:.1f}%;"></div>'
            f'</div>'
            f'<div class="rvc-bar-count">{int(n)}</div>'
            f'</div>'
        )
    # Reuse the same .rvc-bar-row / .rvc-bar-* CSS classes injected on Position
    # View & League View charts. Inject the styles here too so they're
    # available on a cold page load when those pages haven't been visited yet.
    st.markdown(
        """
        <style>
        .rvc-bar-row { display: grid; grid-template-columns: 220px 1fr 40px;
            gap: 8px; align-items: center; padding: 4px 0; font-size: 0.88rem; color: #374151; }
        .rvc-bar-label { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .rvc-bar-track { background: #f3f4f6; border-radius: 4px; height: 16px; overflow: hidden; }
        .rvc-bar-fill { background: #1F3864; height: 100%; border-radius: 4px; min-width: 2px; }
        .rvc-bar-count { font-weight: 600; color: #111827; text-align: right; font-size: 0.85rem; }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.markdown(
        '<div style="background:#ffffff; border:1px solid #e5e7eb; border-radius:8px; '
        'padding:14px 18px; max-width:760px; margin-bottom:8px;">'
        + "".join(bar_rows) +
        '</div>',
        unsafe_allow_html=True,
    )


# ─── Main table — excluded players ──────────────────────────────────────────
st.markdown(f"### Excluded players ({n_total})")


def _player_cell(row: pd.Series) -> str:
    """Player anchor + red Excluded badge."""
    pid = int(row["player_id"])
    raw_name = row.get("name") or ""
    display_name = labels.player_display_name(pid, raw_name)
    anchor = ui.player_link(pid, display_name)
    return (
        f'{anchor} <span class="rvc-excluded-badge" '
        f'title="Excluded from active matching">⊘ Excluded</span>'
    )


def _parent_club_cell(row: pd.Series) -> str:
    """Parent-club anchor via labels.club_display_name."""
    # The get_excluded DataFrame doesn't carry parent_club_id, so look up
    # club_id from player_universe via player_id → parent_club_id.
    pid = int(row["player_id"])
    cid_row = con.execute(
        "SELECT parent_club_id FROM player_universe WHERE player_id = ?",
        (pid,)).fetchone()
    cid = cid_row[0] if cid_row else None
    parent_raw = row.get("parent_club") or row.get("current_club") or ""
    display_name = labels.club_display_name(
        int(cid) if cid is not None else None, parent_raw
    )
    if cid is None:
        return display_name or "—"
    return ui.club_link(int(cid), display_name)


df_disp = df.copy()
df_disp["Player"]      = df_disp.apply(_player_cell, axis=1)
df_disp["Position"]    = df_disp["position"].fillna("—").apply(labels.display_bucket)
df_disp["Age"]         = df_disp["age"].apply(lambda v: int(v) if pd.notna(v) else "—")
df_disp["TM value"]    = df_disp["TM_value_eur"].apply(_money)
df_disp["Sellability"] = df_disp["sellability"].apply(
    lambda v: f"{float(v):.1f}" if pd.notna(v) else "—")
df_disp["Parent club"] = df_disp.apply(_parent_club_cell, axis=1)
df_disp["Reason"]      = df_disp["reason"].fillna("—")
df_disp["Source"]      = df_disp["source"].apply(
    lambda s: "Manual" if s == "manual" else ("Agency rule" if s.startswith("agency:") else s)
)

# Sort manual entries first, then by sellability DESC inside each band
df_disp = df_disp.assign(
    _src_rank=df_disp["source"].apply(lambda s: 0 if s == "manual" else 1)
).sort_values(
    ["_src_rank", "sellability"], ascending=[True, False], na_position="last"
).reset_index(drop=True)

show = df_disp[[
    "Player", "Position", "Age", "TM value",
    "Sellability", "Parent club", "Reason", "Source",
]]


def _bg_excluded(_row):
    """Apply the same pale-red row tint used on All Matches for Excluded rows."""
    return ['background-color: #FEF2F2;' for _ in _row]


def _bg_sell(v):
    try: return ui.heatmap_gradient(v, 0, 100)
    except (TypeError, ValueError): return ""


styled = (
    show.style
    .apply(_bg_excluded, axis=1)
    .map(_bg_sell, subset=["Sellability"])
)
ui.render_html_table(styled, max_height_px=min(720, 60 + 38 * len(show)))


# ─── Footer caption ──────────────────────────────────────────────────────────
st.markdown(
    '<div style="margin-top:32px; padding-top:16px; border-top:1px solid #e5e7eb; '
    'color:#9ca3af; font-size:0.8rem; line-height:1.5;">'
    'Kill List is managed manually via the <strong>Kill List</strong> tab in '
    '<code style="background:#f3f4f6; padding:1px 4px; border-radius:3px;">'
    'BrokerageWorkbook.xlsx</code> and the agency-rules CSV at '
    '<code style="background:#f3f4f6; padding:1px 4px; border-radius:3px;">'
    'data/blocked_agencies.csv</code>. New exclusions or removals are picked '
    'up on each refresh. Stage 2 will move this to a Notion operational layer '
    'with audit history.'
    '</div>',
    unsafe_allow_html=True,
)
