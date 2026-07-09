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
# access control -- hidden by default everywhere (local and deployed).
# Set SHOW_ADMIN_PAGE=true to opt back in for a local review session.
_SHOW_ADMIN_PAGE = os.environ.get("SHOW_ADMIN_PAGE", "false").strip().lower() in ("true", "1")

st.set_page_config(page_title="UC Dining Sustainability Dashboard", page_icon="🌱", layout="wide")

_APP_DIR = Path(__file__).resolve().parent


def _render_home() -> None:
    st.title("UC Dining Sustainability Dashboard")
    st.markdown(
        "This is the <u>beta</u> version of a combined UC Sustainability Dining dashboard. It incorporates "
        "<a href='/data-sources' target='_self'>systemwide data</a> to help UC plan for the future and meet "
        "its sustainability goals, while improving the ease of reporting and procurement. This tool is meant "
        "for internal use by a variety of stakeholders — chefs, procurement, sustainability, business, "
        "students, and others who care about improving dining. For questions or suggestions please send "
        "feedback to Celeste Basken at "
        "<a href='mailto:cbasken@ucdavis.edu'>cbasken@ucdavis.edu</a>. Thank you.",
        unsafe_allow_html=True,
    )

    st.divider()

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("🌱 Campus Roadmap")
        st.markdown(
            "This tool is designed to assist stakeholders in envisioning strategies to increase sustainable "
            "procurement to meet internal UC goals. The tool allows campuses to understand the current "
            "sustainability makeup of their dining purchasing, and understand the price premiums they pay for "
            "sustainable items. The 'optimization' tool demonstrates strategies to adjust spend across "
            "categories to minimize cost while maximizing sustainable spend."
        )
        st.page_link("1_Campus_Roadmap.py", label="Open Campus Roadmap", icon="🌱")

        st.subheader("📋 Sustainability Auto-Reporting")
        st.markdown(
            "This tool is designed to automatically classify a campus' purchase orders as sustainable and "
            "determine the appropriate SIMAP category, based on previously uploaded sustainability reporting "
            "included in our data set. The tool compares uploaded products to a product list from prior years "
            "and automatically adds Sustainability Yes/No and applicable AASHE/PGH Standards, if applicable."
        )
        st.page_link("3_Auto_Classifier.py", label="Open Sustainability Auto-Reporting", icon="📋")

    with col2:
        st.subheader("🍽️ Dining Dashboard")
        st.markdown(
            "This tool is designed to help procurement specialists identify available sustainable products "
            "that other UCs are already purchasing. The tool identifies products by food category, "
            "distributor, supplier, and/or region. It highlights products that are available from a "
            "distributor already onboarded by the campus."
        )
        st.page_link("2_Dining_Dashboard.py", label="Open Dining Dashboard", icon="🍽️")

        st.subheader("💲 Competitive Price Checker")
        st.markdown(
            "This tool is designed to incorporate a hypothetical new item into the Campus Roadmap "
            "optimization, in order to determine if it is competitive in terms of price and sustainability "
            "status. Rather than merely comparing the sticker price to that of products in the same category, "
            "this tool checks if the new item can compete successfully in the basket of goods purchased by a "
            "given campus."
        )
        st.page_link("4_Competitive_Price_Checker.py", label="Open Price Checker", icon="💲")

    st.divider()
    st.caption(
        "\"Sustainable\" always means `products.validated_sustainable_yn` (AASHE STARS for academic campuses, "
        "Practice Greenhealth for health systems) -- never SIMAP category membership, which is used only to "
        "group similar foods and estimate greenhouse-gas impact. See \"Our Definition of Sustainable\" in the "
        "sidebar for the full policy."
    )


home_page = st.Page(_render_home, title="Home", icon="🏠", url_path="home", default=True)
roadmap_page = st.Page(_APP_DIR / "1_Campus_Roadmap.py", title="Campus Roadmap", icon="🌱", url_path="roadmap")
dining_page = st.Page(_APP_DIR / "2_Dining_Dashboard.py", title="Dining Dashboard", icon="🍽️", url_path="dining")
classifier_page = st.Page(
    _APP_DIR / "3_Auto_Classifier.py", title="Sustainability Auto-Reporting", icon="📋", url_path="classifier"
)
price_checker_page = st.Page(
    _APP_DIR / "4_Competitive_Price_Checker.py", title="Price Checker", icon="💲", url_path="price-checker"
)
definition_page = st.Page(
    _APP_DIR / "5_Our_Definition_of_Sustainable.py",
    title="Our Definition of Sustainable",
    icon="📗",
    url_path="our-definition-of-sustainable",
)
ghg_page = st.Page(
    _APP_DIR / "6_Food_Categories_and_GHG.py",
    title="Food Categories and Greenhouse Gas Calculations",
    icon="🌎",
    url_path="food-categories-and-ghg",
)
data_sources_page = st.Page(
    _APP_DIR / "7_Data_Sources.py", title="Data Sources", icon="🗂️", url_path="data-sources"
)
nav_sections = {
    "": [home_page],
    "Dashboard": [roadmap_page, dining_page, classifier_page, price_checker_page],
    "Reference": [definition_page, ghg_page, data_sources_page],
}
if _SHOW_ADMIN_PAGE:
    entity_review_page = st.Page(
        _APP_DIR / "Entity_Match_Review.py", title="Entity Match Review", icon="🔍", url_path="entity-review"
    )
    nav_sections["Admin"] = [entity_review_page]

nav = st.navigation(nav_sections)
nav.run()
