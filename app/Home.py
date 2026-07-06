"""Single entry point for the whole multi-page Streamlit app.

Consolidates the 5 previously-standalone pages (each ran as its own
`streamlit run app/N_Something.py` process on its own port -- see the old
per-page `.claude/launch.json` entries) into ONE real multi-page app via
`st.navigation`/`st.Page`.

`st.set_page_config()` can only be called once per app run, and must be the
first Streamlit command -- it now lives here ONLY; it has been removed from
every individual page file (each of which still runs fine standalone via
`streamlit run app/1_Campus_Roadmap.py` etc., just with Streamlit's default
page chrome instead of its own title/layout, since there's no other
`set_page_config` call to conflict with).

`st.session_state["selected_campus"]` -- already used identically by all 4
dashboard pages in anticipation of exactly this consolidation -- is now
genuinely shared across page navigations for free, with no extra plumbing,
since they're one Streamlit session instead of 5 separate ones.

Run with `streamlit run app/Home.py` (see .claude/launch.json's single
"dashboard" entry). This is also the file Render's start command should
point at once this is deployed (see README.md's deployment note).
"""

import os
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Entity Match Review mutates the shared canonical database and has no
# access control -- shown by default for local use, but Render's env sets
# SHOW_ADMIN_PAGE=false so it's never reachable on the public deployment
# until real auth is added in front of it.
_SHOW_ADMIN_PAGE = os.environ.get("SHOW_ADMIN_PAGE", "true").strip().lower() not in ("false", "0", "")

st.set_page_config(page_title="UC Dining Sustainability Dashboard", page_icon="🌱", layout="wide")

_APP_DIR = Path(__file__).resolve().parent


def _render_home() -> None:
    st.title("UC Dining Sustainability Dashboard")
    st.markdown(
        "A single platform consolidating four tools for UC dining sustainability procurement, built on one "
        "shared canonical dataset (SQLite, one row per product/purchase across all 7 reporting campuses) and "
        "two shared engines (entity matching, optimization) rather than four separately-cleaned pipelines. "
        "Pick a tool from the sidebar, or jump in below."
    )

    st.divider()

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("🌱 Campus Roadmap")
        st.markdown(
            "Model three ways a campus could shift its purchasing mix toward more certified-sustainable food "
            "-- minimize spend while holding today's sustainable share, maximize sustainable share while "
            "holding today's spend, or a conservative threshold-then-maximize blend. Generates a downloadable "
            "PDF report."
        )
        st.page_link("1_Campus_Roadmap.py", label="Open Campus Roadmap", icon="🌱")

        st.subheader("📋 Auto-Classifier")
        st.markdown(
            "Upload any purchasing sheet and get it back with sustainability certifications, SIMAP category, "
            "and validated-sustainable status filled in wherever a confident match exists against every "
            "product any campus has already classified. Read-only -- never writes to the shared database."
        )
        st.page_link("3_Auto_Classifier.py", label="Open Auto-Classifier", icon="📋")

    with col2:
        st.subheader("🍽️ Dining Dashboard")
        st.markdown(
            "Search validated-sustainable products purchased across every campus, organized by SIMAP-57 "
            "category -- so chefs can find sustainable items to onboard, ideally through a distributor they "
            "already use. Price-free by design: it's about *what* other campuses buy, not what they pay."
        )
        st.page_link("2_Dining_Dashboard.py", label="Open Dining Dashboard", icon="🍽️")

        st.subheader("💲 Competitive Price Checker")
        st.markdown(
            "Test whether a hypothetical new item would actually get chosen by the optimizer, given everything "
            "else it has to balance -- a real re-optimization with the item injected as a new, capped-supply "
            "sourcing option, not a simple price comparison."
        )
        st.page_link("4_Competitive_Price_Checker.py", label="Open Price Checker", icon="💲")

    st.divider()
    st.caption(
        "\"Sustainable\" always means `products.validated_sustainable_yn` (AASHE STARS for academic campuses, "
        "Practice Greenhealth for health systems) -- never SIMAP category membership, which is used only to "
        "group similar foods and estimate greenhouse-gas impact."
    )


home_page = st.Page(_render_home, title="Home", icon="🏠", url_path="home", default=True)
roadmap_page = st.Page(_APP_DIR / "1_Campus_Roadmap.py", title="Campus Roadmap", icon="🌱", url_path="roadmap")
dining_page = st.Page(_APP_DIR / "2_Dining_Dashboard.py", title="Dining Dashboard", icon="🍽️", url_path="dining")
classifier_page = st.Page(_APP_DIR / "3_Auto_Classifier.py", title="Auto-Classifier", icon="📋", url_path="classifier")
price_checker_page = st.Page(
    _APP_DIR / "4_Competitive_Price_Checker.py", title="Price Checker", icon="💲", url_path="price-checker"
)
nav_sections = {
    "": [home_page],
    "Dashboard": [roadmap_page, dining_page, classifier_page, price_checker_page],
}
if _SHOW_ADMIN_PAGE:
    entity_review_page = st.Page(
        _APP_DIR / "Entity_Match_Review.py", title="Entity Match Review", icon="🔍", url_path="entity-review"
    )
    nav_sections["Admin"] = [entity_review_page]

nav = st.navigation(nav_sections)
nav.run()
