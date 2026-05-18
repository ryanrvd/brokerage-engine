"""Shared rendering utilities for the Streamlit app.

Format helpers + reusable filter widgets + table builders. Each page imports
from here so the look-and-feel stays consistent.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pandas as pd
import streamlit as st

import labels  # for club_display_name + league_name in rationale builders

# kill_list lives at the project root — sibling of app/. Imported lazily
# (and via the sys.path hop) so components.py stays usable in any context.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
from kill_list import normalise_name as _normalise_name  # noqa: E402


# ─── Format helpers ───────────────────────────────────────────────────────────

def fmt_money(eur: float | int | None) -> str:
    if eur is None or pd.isna(eur):
        return "—"
    eur = float(eur)
    if abs(eur) >= 1_000_000:
        return f"€{eur/1_000_000:.1f}m"
    if abs(eur) >= 1_000:
        return f"€{eur/1_000:.0f}k"
    return f"€{eur:.0f}"


def fmt_pct(x: float | None, decimals: int = 1) -> str:
    if x is None or pd.isna(x):
        return "—"
    return f"{float(x) * 100:.{decimals}f}%"


def fmt_score(x: float | None, decimals: int = 1) -> str:
    if x is None or pd.isna(x):
        return "—"
    return f"{float(x):.{decimals}f}"


def contract_years_remaining(ce: str | None, snapshot: date) -> float | None:
    if not ce or pd.isna(ce):
        return None
    try:
        return round((datetime.fromisoformat(str(ce)).date() - snapshot).days / 365.25, 2)
    except (TypeError, ValueError):
        return None


def demand_intensity_label(src: str | None, val: str | None) -> str:
    src = (src or "").strip()
    val = (val or "").strip().upper()
    if src == "Agent" and val == "YES":
        return "Validated agent"
    if src == "Agent":
        return "Agent (partial)"
    if src == "Intel":
        return "Intel-derived"
    if src == "Inferred":
        return "Inferred (squad gap)"
    return "Unverified"


def current_club_display(row: pd.Series) -> str:
    if row.get("on_loan") and pd.notna(row.get("parent_club")) and row.get("parent_club") != row.get("current_club"):
        return f"{row['current_club']} (on loan from {row['parent_club']})"
    return row.get("current_club", "")


# ─── Rationale builder ────────────────────────────────────────────────────────

def _seller_drivers(row: pd.Series) -> list[str]:
    drivers: list[str] = []
    if row.get("public_must_sell_flag") == 1:
        drivers.append("public must-sell flag (parachute/FFP)")
    if row.get("manager_change_flag") == 1:
        drivers.append("manager change")
    if row.get("contract_leveraged") == 1:
        drivers.append("contract leverage")
    if row.get("on_loan"):
        drivers.append("loan with sell-readiness signal")
    if not drivers:
        comps = [
            ("contract leverage", row.get("contract_leverage_score") or 0),
            ("squad oversupply",  row.get("squad_oversupply_score") or 0),
            ("net-spend headroom", row.get("net_spend_score") or 0),
        ]
        comps.sort(key=lambda x: -x[1])
        if comps[0][1] > 0:
            drivers.append(f"{comps[0][0]} ({comps[0][1]:.0f}/100)")
    return drivers


def _buyer_text(row: pd.Series) -> str:
    """Buyer-side fragment using club display name. League is omitted from
    the rationale string — surface it in dedicated League columns instead."""
    buyer_cid  = row.get("buyer_club_id")
    buyer_name = row.get("buyer_club_name") or "(unknown)"
    buyer = labels.club_display_name(
        int(buyer_cid) if pd.notna(buyer_cid) else None,
        buyer_name,
    )

    src = (row.get("request_source") or "").strip()
    val = (row.get("request_validated") or "").strip().upper()
    if src == "Agent" and val == "YES":
        return f"{buyer} — validated agent-confirmed need"
    if src == "Intel":
        return f"{buyer} — intel-derived positional gap"
    if src == "Inferred":
        return f"{buyer} — inferred positional gap (squad-thinness)"
    return f"{buyer} — open need (unverified)"


def _trade_text(row: pd.Series) -> str:
    headroom = (row.get("max_transfer_fee_eur") or 0) - (row.get("current_tm_value_eur") or 0)
    tier = row.get("tier_move") or "lateral"
    return f"{tier} move, €{headroom/1e6:.0f}m headroom above TM"


def build_rationale(row: pd.Series) -> str:
    """Full rationale — Seller + Buyer + Trade. Uses club display names and
    league display names throughout. Used by Targets / All Matches / Club View
    (matches as buyer) where the seller varies per row."""
    drivers = _seller_drivers(row)

    parent_cid = row.get("parent_club_id")
    parent_name = row.get("parent_club") or "(unknown)"
    parent_disp = labels.club_display_name(
        int(parent_cid) if pd.notna(parent_cid) else None,
        parent_name,
    )
    if pd.isna(row.get("parent_pressure_score")):
        seller_text = f"parent club outside coverage — {parent_disp}"
    elif drivers:
        seller_text = f"{parent_disp} — " + ", ".join(drivers[:2])
    else:
        seller_text = f"{parent_disp} — low structural pressure"

    return (
        f"Seller: {seller_text}. "
        f"Buyer: {_buyer_text(row)}. "
        f"Trade: {_trade_text(row)}."
    )


def build_buyer_rationale(row: pd.Series) -> str:
    """Buyer-side + trade only — drops the Seller line. Used on Player View's
    matches table where every row shares the same seller (the focused player)
    and repeating "Seller: …" 29 times is wall-of-text noise."""
    trade = _trade_text(row)
    return f"{_buyer_text(row)}. {trade[0].upper()}{trade[1:]}."


def build_seller_context_line(profile: pd.Series, parent_pressure_score: float | None) -> str:
    """One-liner summarising the seller side of every match for a focused
    player — rendered once above the matches table on Player View."""
    drivers = _seller_drivers(profile)
    parent_cid = profile.get("parent_club_id")
    parent_name = profile.get("parent_club") or "(unknown)"
    parent_disp = labels.club_display_name(
        int(parent_cid) if pd.notna(parent_cid) else None,
        parent_name,
    )

    if parent_pressure_score is None or pd.isna(parent_pressure_score):
        pressure_str = "parent club outside coverage"
    else:
        pressure_str = f"{float(parent_pressure_score):.0f}/100 pressure"

    if drivers:
        return f"{parent_disp} — {', '.join(drivers[:3])} ({pressure_str})"
    return f"{parent_disp} — low structural pressure ({pressure_str})"


# ─── Sci Sports level-fit pill ──────────────────────────────────────────────
# Coloured inline pill for All Matches, Position View, Club View matches-
# as-buyer tables. Persisted level_fit values are 'ON_LEVEL', 'UPSIDE',
# 'BELOW', 'UNRATED' from scripts/22_match_engine.compute_level_fit.

def level_fit_pill(level_fit: str | None) -> str:
    """Returns inline HTML for the level-fit pill."""
    if level_fit == "ON_LEVEL":
        bg, fg, text = "#dcfce7", "#14532d", "✓ ON LEVEL"
    elif level_fit == "UPSIDE":
        bg, fg, text = "#fef3c7", "#78350f", "↗ UPSIDE"
    elif level_fit == "BELOW":
        bg, fg, text = "#f3f4f6", "#374151", "BELOW"
    else:
        return '<span style="color:#9ca3af;">—</span>'
    return (
        f'<span style="display:inline-block; padding:1px 8px; border-radius:8px; '
        f'background:{bg}; color:{fg}; font-weight:600; font-size:0.72rem; '
        f'letter-spacing:0.02em;">{text}</span>'
    )


def level_fit_info_icon() -> str:
    """Inline info-icon + expandable tooltip for Level Fit column headers /
    section titles. Same `<details>` / `<summary>` pattern as the TM
    valuation methodology icon on Player View, so the interaction model and
    visual rhythm are consistent across the app.

    Embeds the actual `level_fit_pill()` output inline in each definition so
    the tooltip's indicators match exactly what the user sees in the tables —
    no symbol drift between the legend and the data."""
    on_pill = level_fit_pill("ON_LEVEL")
    up_pill = level_fit_pill("UPSIDE")
    bl_pill = level_fit_pill("BELOW")
    un_pill = level_fit_pill("UNRATED")
    return (
        '<details style="display:inline-block; margin-left:8px;">'
        '<summary style="display:inline; cursor:pointer; color:#1F3864; '
        'font-weight:600; font-size:0.8rem; list-style:none;">level fit ⓘ</summary>'
        '<div style="margin-top:10px; padding:14px 16px; background:#ffffff; '
        'border:1px solid #e5e7eb; border-radius:6px; '
        'font-size:0.85rem; color:#374151; line-height:1.55; '
        'max-width:580px;">'
        '<p style="margin:0 0 10px 0;"><strong>Level fit</strong> — how well '
        'the player matches the buying club\'s required level.</p>'
        f'<div style="margin:8px 0;">{on_pill} '
        '<span style="margin-left:6px;"><strong>Current ability</strong> meets '
        'or exceeds the club\'s threshold for the requested level (squad / '
        'first team / key player). Plug-and-play fit.</span></div>'
        f'<div style="margin:8px 0;">{up_pill} '
        '<span style="margin-left:6px;"><strong>Current ability</strong> is '
        'below threshold but <strong>potential ability</strong> is at or above. '
        'Development play — the club would be buying for the ceiling, not '
        'the floor.</span></div>'
        f'<div style="margin:8px 0;">{bl_pill} '
        '<span style="margin-left:6px;">Both <strong>current</strong> and '
        '<strong>potential</strong> ability sit below the club\'s threshold. '
        'Positional match only; level gap is too wide to be actionable.</span></div>'
        f'<div style="margin:8px 0;">{un_pill} '
        '<span style="margin-left:6px;">Player has no SciSports CA/PA yet '
        '(typically a newly-added player from the most recent TM scrape, '
        'awaiting manual rating).</span></div>'
        '<p style="margin:12px 0 0 0; padding-top:10px; '
        'border-top:1px solid #f3f4f6; color:#6b7280; font-size:0.8rem;">'
        'Match scores are weighted: '
        '<strong>ON LEVEL ×1.20</strong> · '
        '<strong>UPSIDE ×1.05</strong> · '
        '<strong>BELOW ×0.85</strong> · '
        '<strong>UNRATED ×1.00</strong>.</p>'
        '</div></details>'
    )


def level_fit_dot(level_fit: str | None) -> str:
    """Smaller inline indicator for League View matched-pair lists.
    Coloured dot + (for ON_LEVEL/UPSIDE only) a small ✓ or ↗ glyph."""
    if level_fit == "ON_LEVEL":
        return ('<span style="color:#15803d; font-weight:700;">●</span>'
                '<span style="color:#15803d; font-size:0.78rem; font-weight:700; '
                'margin-left:2px;">✓</span>')
    if level_fit == "UPSIDE":
        return ('<span style="color:#d97706; font-weight:700;">●</span>'
                '<span style="color:#d97706; font-size:0.78rem; font-weight:700; '
                'margin-left:2px;">↗</span>')
    if level_fit == "BELOW":
        return '<span style="color:#9ca3af; font-weight:700;">●</span>'
    # UNRATED / no match
    return '<span style="color:#15803d; font-weight:700;">●</span>'


# ─── Buyer-request interest matching ────────────────────────────────────────
# Map_club_requests carries a free-text `linked_shortlisted_player` field
# (typed by hand from the Google Sheets workflow). Names may be surname-only,
# diacritic-stripped, or use slightly different spellings from what dcaribou
# stores. These helpers build a lookup keyed by normalised player name AND
# surname so we can highlight which of those typed interests intersect with
# our sellable cohort — the high-signal brokerage opportunities.


def build_player_match_index(con) -> tuple[dict, dict]:
    """Build (full_name_lookup, surname_lookup) from the sellable cohort in
    player_universe. Each lookup maps normalised name → list of
    (player_id, display_name) tuples. Used by `match_interest_name()`."""
    rows = con.execute("""
        SELECT player_id, name FROM player_universe
        WHERE (right_priced=1 OR finished_product=1
               OR finished_product IS NULL OR contract_leveraged=1)
    """).fetchall()

    full_lookup: dict[str, list[tuple[int, str]]] = {}
    surname_lookup: dict[str, list[tuple[int, str]]] = {}
    for pid, name in rows:
        pid_int = int(pid)
        display_name = labels.player_display_name(pid_int, name)

        for candidate in {name, display_name}:
            norm = _normalise_name(candidate)
            if not norm:
                continue
            full_lookup.setdefault(norm, []).append((pid_int, display_name))
            tokens = norm.split()
            if tokens:
                surname_lookup.setdefault(tokens[-1], []).append((pid_int, display_name))

    # Dedupe within each bucket — same player can be added via official + display
    def _dedupe(d: dict) -> dict:
        out = {}
        for k, v in d.items():
            seen = set()
            uniq = []
            for pid, dname in v:
                if pid not in seen:
                    seen.add(pid)
                    uniq.append((pid, dname))
            out[k] = uniq
        return out

    return _dedupe(full_lookup), _dedupe(surname_lookup)


def match_interest_name(
    interest: str,
    full_lookup: dict,
    surname_lookup: dict,
) -> tuple[int, str] | None:
    """Resolve a buyer-typed interest string against the sellable cohort.

    Match order:
      1. Exact normalised full-name match (handles "Sandro Tonali" / "Tonalí")
      2. Last-token (surname) match — only when unique. Multi-hit surnames
         can't be safely disambiguated so they're returned as no-match.

    Returns (player_id, display_name) on success, None otherwise."""
    n = _normalise_name(interest)
    if not n:
        return None
    if n in full_lookup:
        hits = full_lookup[n]
        if len(hits) == 1:
            return hits[0]
        return None  # ambiguous full-name
    tokens = n.split()
    if tokens:
        candidates = surname_lookup.get(tokens[-1], [])
        if len(candidates) == 1:
            return candidates[0]
    return None
    return f"{parent_disp} — low structural pressure ({pressure_str})"


# ─── Click-through URL builders ───────────────────────────────────────────────

def _auth_param() -> str:
    """Return '_a=<token>' for appending to in-app URLs, or '' if no auth.

    Reads the auth token from the current request's query string. Every
    in-app link must carry this through or the user lands on the password
    gate after navigation (a fresh WebSocket session has no session_state).
    """
    token = st.query_params.get("_a")
    return f"_a={token}" if token else ""


def with_auth(href: str) -> str:
    """Append the auth token to an in-app href. Idempotent — already-tagged
    URLs are returned unchanged."""
    auth = _auth_param()
    if not auth or "_a=" in href:
        return href
    sep = "&" if "?" in href else "?"
    return f"{href}{sep}{auth}"


def player_url(player_id: int, name: str) -> str:
    """URL for navigating to the Player View. Display name appears after #
    so st.column_config.LinkColumn(display_text=r'#(.+)') can show it."""
    return with_auth(f"/player_view?player_id={player_id}") + f"#{name}"


def club_url(club_id: int, name: str) -> str:
    return with_auth(f"/club_view?club_id={club_id}") + f"#{name}"


# ─── Anchor-tag builders for per-cell hyperlinks (same-tab) ──────────────────

_LINK_STYLE = "text-decoration:none; color:#1F3864; font-weight:700;"


def player_link(player_id: int | None, label: str) -> str:
    """HTML anchor for a Player View drill-through. target=_self → same tab."""
    if player_id is None or pd.isna(player_id) or not label:
        return label or ""
    href = with_auth(f"/player_view?player_id={int(player_id)}")
    return f'<a href="{href}" target="_self" style="{_LINK_STYLE}">{label}</a>'


def club_link(club_id: int | None, label: str) -> str:
    """HTML anchor for a Club View drill-through. target=_self → same tab."""
    if club_id is None or pd.isna(club_id) or not label:
        return label or ""
    href = with_auth(f"/club_view?club_id={int(club_id)}")
    return f'<a href="{href}" target="_self" style="{_LINK_STYLE}">{label}</a>'


def render_html_table(
    styler,
    *,
    max_height_px: int = 640,
    sortable: dict[str, str] | None = None,
    current_sort: tuple[str, str] | None = None,
    extra_qs: str = "",
    sort_path: str = "",
) -> None:
    """Render a pandas Styler as a scrollable HTML table via st.markdown.

    `styler` should have its index hidden via .hide(axis='index') and any
    link cells should already contain raw <a> HTML; we pass escape=False so
    the anchors are preserved with target='_self'.

    Clickable headers
    -----------------
    Pass ``sortable={display_header_text: sort_key}`` to wrap matching column
    headers in anchors that toggle the ``?sort=KEY&dir=desc|asc`` query params.
    To override the default direction on first click (e.g. alpha columns should
    open ascending, not descending), pass the value as a tuple instead of a
    string: ``{"Player": ("name", "asc"), "Match Score": ("match_score", "desc")}``.
    String values default to ``"desc"``.

    Pass ``current_sort=(sort_key, "desc"|"asc")`` so the active column shows
    an arrow indicator and clicking it again flips direction.
    ``extra_qs`` is verbatim query-string content (no leading ``?``, no trailing
    ``&``) that should persist across the sort click — used by detail pages
    that already carry e.g. ``player_id=42`` in the URL.
    """
    # Hide index defensively
    try:
        styler = styler.hide(axis="index")
    except Exception:
        pass
    html = styler.to_html(escape=False)

    # ── Column-width hooks for matches-style tables ────────────────────────
    # Pandas Styler emits `colN` classes on both <th> headers and <td> cells.
    # We sniff specific header names, pick up their column index, then emit
    # per-table scoped CSS so the Rationale column gets a wide min-width
    # (single-/two-line cells inside a wide column, not narrow-and-tall)
    # and small numeric / pill columns stay tight.
    import re as _re_cols
    _COL_WIDTH_RULES: dict[str, str] = {
        # header text → CSS declarations
        "Rationale":          "min-width:380px; max-width:560px; white-space:normal; line-height:1.45;",
        "Level fit":          "min-width:96px; white-space:nowrap; text-align:left;",
        "CA":                 "min-width:50px; white-space:nowrap; text-align:right;",
        "PA":                 "min-width:50px; white-space:nowrap; text-align:right;",
        "Buyer CA threshold": "min-width:80px; white-space:nowrap; text-align:right;",
    }
    _tbl_id_m = _re_cols.search(r'<table\s+id="([^"]+)"', html)
    _tbl_id = _tbl_id_m.group(1) if _tbl_id_m else None
    _per_table_css = ""
    if _tbl_id:
        _column_rules: list[str] = []
        for _header, _decls in _COL_WIDTH_RULES.items():
            _esc = _re_cols.escape(_header)
            _m = _re_cols.search(
                r'<th\b[^>]*class="[^"]*col_heading[^"]*\bcol(\d+)\b[^"]*"[^>]*>\s*'
                + _esc + r'\s*</th>',
                html,
            )
            if _m:
                _idx = _m.group(1)
                _column_rules.append(
                    f'#{_tbl_id} th.col{_idx}, #{_tbl_id} td.col{_idx} '
                    f'{{ {_decls} }}'
                )
        if _column_rules:
            # Matches-style table: pin all cells to nowrap so non-Rationale
            # columns stay tight; Rationale overrides via its own rule above.
            # Total table width then exceeds viewport → wrapper scrolls
            # horizontally instead of rows growing tall.
            _base_rule = (
                f'#{_tbl_id} thead th, #{_tbl_id} tbody td '
                f'{{ white-space:nowrap; }}'
            )
            _per_table_css = (
                "<style>" + _base_rule + " " + " ".join(_column_rules) + "</style>"
            )

    if sortable:
        import re
        cur_key, cur_dir = (current_sort or (None, None))

        # Normalise sortable values: str → (key, "desc"); tuple stays as-is.
        _sortable_norm: dict[str, tuple[str, str]] = {}
        for _header, _val in sortable.items():
            if isinstance(_val, tuple):
                _sortable_norm[_header] = _val
            else:
                _sortable_norm[_header] = (_val, "desc")

        def _replace_th(m: re.Match) -> str:
            open_tag, text, close_tag = m.group(1), m.group(2).strip(), m.group(3)
            entry = _sortable_norm.get(text)
            if not entry:
                return m.group(0)
            target_key, default_dir = entry
            # Toggle on the active column; otherwise use this column's default.
            if target_key == cur_key:
                new_dir = "asc" if cur_dir == "desc" else "desc"
                arrow = " ▼" if cur_dir == "desc" else " ▲"
            else:
                new_dir = default_dir
                arrow = ""
            qs_parts = []
            if extra_qs:
                qs_parts.append(extra_qs)
            qs_parts.append(f"sort={target_key}")
            qs_parts.append(f"dir={new_dir}")
            href = with_auth((sort_path or "") + "?" + "&".join(qs_parts))
            anchor = (
                f'<a href="{href}" target="_self" '
                f'style="color:white;text-decoration:none;display:block;'
                f'cursor:pointer;">{text}{arrow}</a>'
            )
            return f"{open_tag}{anchor}{close_tag}"

        html = re.sub(
            r'(<th\b[^>]*class="[^"]*col_heading[^"]*"[^>]*>)([^<]+)(</th>)',
            _replace_th,
            html,
        )

    # Table styling — match the Day 6 polish (subtle border, row padding, sticky header)
    # overflow-x:auto on the wrapper lets the table grow naturally to the
    # widest column total (driven by the Rationale min-width) and scroll
    # horizontally rather than wrapping cells vertically into tall rows.
    wrapper_style = (
        f"max-height:{max_height_px}px; overflow:auto; overflow-x:auto; "
        "border:1px solid #e5e7eb; border-radius:6px; background:white;"
    )
    table_css = (
        "<style>"
        ".rvc-table table { width:max-content; min-width:100%; "
        "  border-collapse:collapse; font-size:0.875rem; table-layout:auto; }"
        ".rvc-table thead th { position:sticky; top:0; background:#1F3864; color:white; "
        "  text-align:left; padding:11px 12px; font-weight:600; z-index:2; "
        "  letter-spacing:0.01em; }"
        ".rvc-table thead th a:hover { text-decoration:underline; opacity:0.92; }"
        ".rvc-table tbody td { padding:11px 12px; border-bottom:1px solid #f3f4f6; "
        "  vertical-align:middle; }"
        ".rvc-table tbody tr:hover { background:#f9fafb; }"
        ".rvc-table a:hover { text-decoration:underline; }"
        "</style>"
    )
    st.markdown(
        f'{table_css}{_per_table_css}<div class="rvc-table" style="{wrapper_style}">{html}</div>',
        unsafe_allow_html=True,
    )


# ─── CSS injection ────────────────────────────────────────────────────────────

def inject_css() -> None:
    css_path = Path(__file__).parent / "style.css"
    if css_path.exists():
        st.markdown(f"<style>{css_path.read_text()}</style>", unsafe_allow_html=True)


# ─── Manual nav bar (guaranteed visible — fallback for any Streamlit nav weirdness) ──

NAV_ITEMS = [
    ("🎯", "Targets",          "/targets"),
    ("📋", "All Matches",      "/all_matches"),
    ("👤", "Player View",      "/player_view"),
    ("🏟️", "Club View",        "/club_view"),
    ("📍", "Position View",    "/position_view"),
    ("🌍", "League View",      "/league_view"),
    ("🚫", "Kill List",        "/kill_list"),
]


# ─── Global "jump to" search dropdown (sidebar; every page) ─────────────────

def render_global_search(players: list[dict], clubs: list[dict]) -> None:
    """Render the cross-app search box + result dropdown in the sidebar.

    Type → up to 5 player matches + 5 club matches appear below the input
    as clickable links. Match on both official and display names.

    `players` / `clubs` are lists of dicts with keys: player_id/club_id,
    display_name, official_name (typically from db.get_player_search_options /
    db.get_club_search_options).
    """
    with st.sidebar:
        st.markdown("---")
        q = st.text_input(
            "🔍 Jump to player or club",
            placeholder="Type a name…",
            key="global_search_input",
            help="Searches both official and display names. Click any result to navigate.",
        )
        if not q:
            return
        ql = q.lower()
        # Player hits — limit 5
        player_hits = []
        for p in players:
            if ql in (p.get("display_name") or "").lower() or ql in (p.get("official_name") or "").lower():
                player_hits.append(p)
                if len(player_hits) >= 5:
                    break
        # Club hits — limit 5
        club_hits = []
        for c in clubs:
            if ql in (c.get("display_name") or "").lower() or ql in (c.get("official_name") or "").lower():
                club_hits.append(c)
                if len(club_hits) >= 5:
                    break

        if not player_hits and not club_hits:
            st.caption("_No matches._")
            return

        # Raw HTML anchors with target="_self" so clicks stay in the same tab.
        link_style = "text-decoration:none; color:#1F3864; font-weight:600;"
        if player_hits:
            st.markdown("**Players**")
            for p in player_hits:
                url = with_auth(f"/player_view?player_id={p['player_id']}")
                st.markdown(
                    f'• <a href="{url}" target="_self" style="{link_style}">{p["display_name"]}</a>',
                    unsafe_allow_html=True,
                )
        if club_hits:
            st.markdown("**Clubs**")
            for c in club_hits:
                url = with_auth(f"/club_view?club_id={c['club_id']}")
                st.markdown(
                    f'• <a href="{url}" target="_self" style="{link_style}">{c["display_name"]}</a>',
                    unsafe_allow_html=True,
                )


# ─── Bucket pill (inline HTML — for headers, identity strips, etc.) ─────────

def bucket_pill(super_bucket: str) -> str:
    """Returns an inline-HTML pill for the bucket tier, coloured per labels.TIER_COLOURS."""
    import labels
    colour = labels.TIER_COLOURS.get(super_bucket, "#e5e7eb")
    return (
        f'<span style="display:inline-block; padding:2px 10px; border-radius:10px; '
        f'background:{colour}; font-weight:600; font-size:0.85rem; color:#1f2937;">'
        f'{super_bucket}</span>'
    )


# ─── Green gradient — matches Targets heat-map ──────────────────────────────

def green_gradient(v: float | None, vmin: float, vmax: float) -> str:
    """Manual lerp from pale green → saturated brand green. No matplotlib dep.
    Returns a CSS background-color rule for a Styler cell."""
    if v is None or pd.isna(v):
        return ""
    span = (vmax - vmin) or 1.0
    pct = max(0.0, min(1.0, (float(v) - vmin) / span))
    r1, g1, b1 = 240, 253, 244
    r2, g2, b2 =  22, 163,  74
    r = int(r1 + (r2 - r1) * pct)
    g = int(g1 + (g2 - g1) * pct)
    b = int(b1 + (b2 - b1) * pct)
    text = "color: white;" if pct > 0.6 else ""
    return f"background-color: rgb({r},{g},{b}); {text}"


def render_nav_bar(active: str = "") -> None:
    """Render an HTML nav bar at the top of the page. Always visible regardless
    of Streamlit's framework nav state. `active` is the title of the current page
    so it can be styled differently."""
    items_html = []
    for icon, label, url in NAV_ITEMS:
        is_active = label == active
        style = (
            "color:#1F3864; font-weight:600; border-bottom:2px solid #1F3864;"
            if is_active
            else "color:#374151; font-weight:500;"
        )
        items_html.append(
            f'<a href="{with_auth(url)}" target="_self" style="text-decoration:none; padding:0.5rem 0.75rem; {style}">'
            f'{icon} {label}</a>'
        )
    html = (
        '<div style="display:flex; gap:0.5rem; padding:0.5rem 0; '
        'border-bottom:1px solid #e5e7eb; margin-bottom:1rem; flex-wrap:wrap; '
        'background:#fafafa; border-radius:6px 6px 0 0; padding-left:1rem; padding-right:1rem;">'
        + "".join(items_html)
        + "</div>"
    )
    st.markdown(html, unsafe_allow_html=True)


# ─── Shared filter widgets ────────────────────────────────────────────────────

def render_targets_filters(df: pd.DataFrame, key_prefix: str = "tgt") -> dict:
    """Renders the standard set of Targets-page filters in the sidebar.
    Returns a dict the caller uses to filter the DataFrame."""
    with st.sidebar:
        # Global search at top
        search = st.text_input(
            "🔍 Search player or club",
            key=f"{key_prefix}_search",
            placeholder="Type a name…",
        )

        st.markdown("### Filters")

        parent_leagues = sorted(df["parent_league"].dropna().unique().tolist())
        sel_leagues = st.multiselect(
            "League of parent club",
            options=parent_leagues,
            default=parent_leagues,
            key=f"{key_prefix}_leagues",
        )

        positions = sorted(df["position_bucket"].dropna().unique().tolist())
        bucket_order = ["GK", "CB", "LB", "RB", "DM", "CM", "AM", "LW", "RW", "ST_CF"]
        positions_sorted = [p for p in bucket_order if p in positions] + \
                           [p for p in positions if p not in bucket_order]
        sel_positions = st.multiselect(
            "Position bucket",
            options=positions_sorted,
            default=positions_sorted,
            key=f"{key_prefix}_positions",
        )

        tm_min = int(df["current_tm_value_eur"].min() / 1e6)
        tm_max = int(df["current_tm_value_eur"].max() / 1e6) + 1
        sel_tm = st.slider("TM value (€m)", tm_min, tm_max, (tm_min, tm_max), 1,
                           key=f"{key_prefix}_tm")

        sell_min = int(df["sellability_score"].min())
        sell_max = int(df["sellability_score"].max()) + 1
        sel_sell = st.slider("Sellability score", sell_min, sell_max,
                             (sell_min, sell_max), 1, key=f"{key_prefix}_sell")

        match_min = int(df["match_score"].min())
        match_max = int(df["match_score"].max()) + 1
        sel_match = st.slider("Match score", match_min, match_max,
                              (match_min, match_max), 1, key=f"{key_prefix}_match")

    return {
        "search":     search,
        "leagues":    sel_leagues,
        "positions":  sel_positions,
        "tm_range":   sel_tm,
        "sell_range": sel_sell,
        "match_range": sel_match,
    }


def apply_filters(df: pd.DataFrame, filters: dict) -> pd.DataFrame:
    """Apply filter dict (from render_targets_filters) to a matches DataFrame."""
    out = df.copy()
    if filters.get("search"):
        q = filters["search"].lower()
        mask = (
            out["player_name"].str.lower().str.contains(q, na=False)
            | out["buyer_club_name"].str.lower().str.contains(q, na=False)
            | out["current_club"].str.lower().str.contains(q, na=False)
            | out["parent_club"].fillna("").str.lower().str.contains(q, na=False)
        )
        out = out[mask]
    if filters.get("leagues"):
        out = out[out["parent_league"].fillna("").isin(filters["leagues"] + [""])]
    if filters.get("positions"):
        out = out[out["position_bucket"].isin(filters["positions"])]
    if filters.get("tm_range"):
        lo, hi = filters["tm_range"]
        out = out[(out["current_tm_value_eur"] / 1e6).between(lo, hi)]
    if filters.get("sell_range"):
        lo, hi = filters["sell_range"]
        out = out[out["sellability_score"].between(lo, hi)]
    if filters.get("match_range"):
        lo, hi = filters["match_range"]
        out = out[out["match_score"].between(lo, hi)]
    return out.reset_index(drop=True)
