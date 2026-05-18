"""Entry point — explicit st.navigation() shell.

Streamlit 1.36+ recommends building multipage apps via st.navigation() rather
than the legacy pages/ auto-discovery. Nav widgets are guaranteed regardless
of custom CSS or theme.

Nav is placed at the TOP of the page (horizontal tab bar) for maximum
visibility — see position="top" below.
"""

import streamlit as st

# Pages — each is a Streamlit script file relative to this entry script.
# Explicit url_path ensures stable routing so inline anchor hrefs always resolve.
market_overview = st.Page("pages/0_Market_Overview.py", title="Market Overview", icon="📊", default=True, url_path="market_overview")
home          = st.Page("home.py",                   title="Targets",            icon="🎯", url_path="targets")
all_matches   = st.Page("pages/1_All_Matches.py",    title="All Matches",        icon="📋", url_path="all_matches")
player_view   = st.Page("pages/2_Player_View.py",    title="Player View",        icon="👤", url_path="player_view")
club_view     = st.Page("pages/3_Club_View.py",      title="Club View",          icon="🏟️", url_path="club_view")
position_view = st.Page("pages/4_Position_View.py",  title="Position View",      icon="📍", url_path="position_view")
league_view   = st.Page("pages/5_League_View.py",    title="League View",        icon="🌍", url_path="league_view")
excluded      = st.Page("pages/6_Excluded_Players.py", title="Kill List", icon="🚫", url_path="kill_list")

pages = [market_overview, home, all_matches, player_view, club_view, position_view, league_view, excluded]

# Place the nav at the TOP of the page (horizontal tabs).
# expanded=True forces the nav to be fully visible rather than collapsed
# behind a popover button on smaller screens.
pg = st.navigation(pages, position="top", expanded=True)
pg.run()
