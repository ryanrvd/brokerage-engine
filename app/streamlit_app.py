"""Entry point — fork-at-login architecture.

Flow:
  1. Password gate (URL-token auth, iOS-Safari-ITP safe).
  2. If no engine selected → render fork.py (the two-card chooser).
  3. Once an engine is selected, build the engine-specific st.navigation()
     and render the chosen page.

Sidebar surfaces a "Switch to <other engine>" link at the bottom of every
page so the user can pivot without going back to fork.
"""

import hashlib

import streamlit as st

import db
import fork


def _auth_hash() -> str:
    return hashlib.sha256(
        str(st.secrets.get("password", "")).encode("utf-8")
    ).hexdigest()[:32]


def _check_password() -> bool:
    expected = _auth_hash()
    token = st.query_params.get("_a")
    if token == expected:
        st.session_state["_password_correct"] = True
        return True
    if st.session_state.get("_password_correct"):
        st.query_params["_a"] = expected
        return True

    st.markdown("### RV Corp")
    st.caption("Private preview · enter access password")
    pw = st.text_input("Password", type="password", key="_password_input")
    if pw:
        if pw == st.secrets.get("password"):
            st.session_state["_password_correct"] = True
            st.query_params["_a"] = expected
            st.rerun()
        else:
            st.error("Incorrect password")
    st.stop()


_check_password()


# ─── Hydrate engine_selected from URL ────────────────────────────────────────
# Click-through navigation opens a fresh WebSocket — session state is empty on
# arrival. Every in-app anchor carries ?engine=<key> via components.with_auth(),
# and we re-seat the session here so the routing below sees it.
_engine_from_url = st.query_params.get("engine")
if _engine_from_url in (db.ENGINE_BROKERAGE, db.ENGINE_MARKET):
    st.session_state["engine_selected"] = _engine_from_url


# ─── Fork: render the chooser if no engine selected ──────────────────────────
if not st.session_state.get("engine_selected"):
    fork.render()
    st.stop()


# Engine is set — ensure the URL reflects it so subsequent links carry it.
if st.query_params.get("engine") != st.session_state["engine_selected"]:
    st.query_params["engine"] = st.session_state["engine_selected"]


# ─── Page lists per engine ──────────────────────────────────────────────────
def _brokerage_pages() -> list:
    return [
        st.Page("pages_brokerage/targets.py",     title="Targets",      icon="🎯",
                default=True, url_path="targets"),
        st.Page("pages_brokerage/all_matches.py", title="All Matches",  icon="📋",
                url_path="all_matches"),
        st.Page("pages_shared/player_view.py",    title="Player View",  icon="👤",
                url_path="player_view"),
        st.Page("pages_shared/club_view.py",      title="Club View",    icon="🏟️",
                url_path="club_view"),
        st.Page("pages_brokerage/position_view.py", title="Position View", icon="📍",
                url_path="position_view"),
        st.Page("pages_brokerage/league_view.py",   title="League View",   icon="🌍",
                url_path="league_view"),
        st.Page("pages_brokerage/kill_list.py",   title="Kill List",    icon="⛔",
                url_path="kill_list"),
    ]


def _market_view_pages() -> list:
    return [
        st.Page("pages_market/opportunities.py",  title="Market Opportunities", icon="🌐",
                default=True, url_path="market_opportunities"),
        st.Page("pages_market/overview.py",       title="Market Overview",      icon="📊",
                url_path="market_overview"),
        st.Page("pages_market/mandate_territory.py", title="Mandate Territory", icon="🚨",
                url_path="mandate_territory"),
        st.Page("pages_shared/player_view.py",    title="Player View",          icon="👤",
                url_path="player_view"),
        st.Page("pages_shared/club_view.py",      title="Club View",            icon="🏟️",
                url_path="club_view"),
        st.Page("pages_market/position_view.py",  title="Position View",        icon="📍",
                url_path="position_view"),
        st.Page("pages_market/league_view.py",    title="League View",          icon="🌍",
                url_path="league_view"),
        st.Page("pages_market/needs_rating.py",   title="Needs Rating",         icon="⏳",
                url_path="needs_rating"),
    ]


engine = db.active_engine()
pages = _brokerage_pages() if engine == db.ENGINE_BROKERAGE else _market_view_pages()

# Phase B engine chrome: inject CSS that paints the active top-nav tab in the
# engine's primary colour. Streamlit's st.navigation uses st.page_link under
# the hood; the active link carries `aria-current="page"`. We style that.
_palette = db.active_engine_colour()
st.markdown(
    f"""
    <style>
      /* Active top-nav tab — engine-coloured underline + colour text */
      a[data-testid="stPageLink"][aria-current="page"] {{
          color: {_palette["primary"]} !important;
          font-weight: 700 !important;
          border-bottom: 3px solid {_palette["primary"]} !important;
      }}
      a[data-testid="stPageLink"][aria-current="page"] span {{
          color: {_palette["primary"]} !important;
      }}
      /* Hover for inactive tabs adopts the engine colour subtly */
      a[data-testid="stPageLink"]:hover {{
          color: {_palette["primary"]} !important;
      }}
    </style>
    """,
    unsafe_allow_html=True,
)

pg = st.navigation(pages, position="top", expanded=True)
pg.run()
