"""Phase 6: Dining Dashboard page.

Rebuild of legacy/dining_dashboard (4 separate Streamlit pages: Start Here,
Category Explorer, Distributor and Supplier View, Sustainability Stats) as
ONE page on the canonical schema, consolidated into tabs -- matching
CLAUDE.md's "one Streamlit app, multi-tab" architecture rather than the
legacy tool's separate multi-page-app navigation.

Two deliberate breaks from the legacy tool, both per CLAUDE.md's Dining
Dashboard spec:

  - SIMAP-57 (`products.simap_category`) replaces the legacy tool's ad hoc,
    hand-maintained "Category" column -- the whole point of rebuilding on
    the canonical schema (per-campus category columns fed a single
    consistent taxonomy back in Phase 3, so this page gets it for free).
  - Price-free by design: `purchases.total_price`/`unit_price` are never
    read or displayed anywhere on this page.

Cross-campus by design (CLAUDE.md's explicit exception to the global-
campus-dropdown rule): the campus dropdown here is a "my campus" reference
point, not a hard filter -- every validated-sustainable product from every
campus is searchable regardless of which campus is selected. The dropdown
is only used to highlight results reachable through a distributor the
selected campus already has a relationship with (a query-time join against
purchases.vendor, no schema change), so a chef can spot "new to us, but our
existing distributor already carries it" opportunities at a glance.

Also drops the legacy tool's hardcoded streamlit-authenticator login gate
(a bcrypt hash and cookie secret were committed in plaintext in
legacy/dining_dashboard/app.py -- read for logic only, per CLAUDE.md, and
that credential should be treated as compromised/rotated, not reused here)
to match every other page in this rebuild, none of which has an auth gate.

Part of the unified app/Home.py multi-page shell (also still runnable
standalone via `streamlit run app/2_Dining_Dashboard.py` for local
debugging). Reuses the same st.session_state["selected_campus"] key as the
Roadmap page, so the two now genuinely agree within one session.
"""

import sqlite3
import sys
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.db import DEFAULT_DB_PATH
from lib.dining_dashboard import get_campus_vendors, load_sustainable_products

# st.set_page_config() now lives in app/Home.py -- see that file's docstring.

_ALREADY_PURCHASING = "Already purchasing"
_NEW_VIA_MY_VENDOR = "⭐ New — via a distributor you already use"

# Extends legacy/dining_dashboard's region_map (which only covered the 9
# campuses with purchasing data at the time) to all 14 UC campuses in the
# `campuses` reference table, so a campus with no purchases yet still has a
# region to belong to.
REGION_MAP = {
    "NorCal": ["UC Berkeley", "UC Davis", "UC Davis Health", "UC San Francisco", "UC Santa Cruz"],
    "SoCal": [
        "UC Irvine",
        "UC Irvine Health",
        "UC Los Angeles",
        "UC Los Angeles Health",
        "UC Riverside",
        "UC San Diego",
        "UC San Diego Health",
        "UC Santa Barbara",
    ],
    "Central": ["UC Merced"],
}


@st.cache_resource
def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DEFAULT_DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@st.cache_data(show_spinner=False)
def _load_products() -> pd.DataFrame:
    return load_sustainable_products(get_conn())


@st.cache_data(show_spinner=False)
def _load_campus_vendors(campus: str) -> set[str]:
    return get_campus_vendors(get_conn(), campus)


def _opportunity_label(row: pd.Series, my_campus: str, my_vendors: set[str]) -> str:
    if my_campus in row["campuses"]:
        return _ALREADY_PURCHASING
    if my_vendors and set(row["vendors"]) & my_vendors:
        return _NEW_VIA_MY_VENDOR
    return ""


def _render_search_tab(df: pd.DataFrame, my_campus: str, my_vendors: set[str]) -> None:
    st.caption(
        "Every validated-sustainable product from every campus is searchable here, regardless of the campus "
        "selected in the sidebar -- that selection only powers the \"new opportunity\" highlight below."
    )

    col1, col2, col3, col4 = st.columns(4)
    search_text = col1.text_input("Search product name", "")
    categories = ["All"] + sorted(df["simap_category"].unique())
    selected_category = col2.selectbox("SIMAP category", categories)
    all_certs = sorted({c for certs in df["cert_list"] for c in certs})
    selected_cert = col3.selectbox("Certification", ["All"] + all_certs)
    all_distributors = sorted({v for vendors in df["vendors"] for v in vendors})
    selected_distributor = col4.selectbox("Distributor", ["All"] + all_distributors)

    col5, col6, col7 = st.columns(3)
    selected_region = col5.selectbox("Purchased by campuses in region", ["All"] + list(REGION_MAP.keys()))
    all_campuses = sorted({c for campuses in df["campuses"] for c in campuses})
    selected_campus_filter = col6.selectbox("Purchased by campus", ["All"] + all_campuses)
    only_new_opportunities = col7.checkbox(
        f"Only show new opportunities via a distributor {my_campus} already uses", value=False
    )

    filtered = df.copy()
    if search_text:
        filtered = filtered[filtered["canonical_name"].str.contains(search_text, case=False, na=False)]
    if selected_category != "All":
        filtered = filtered[filtered["simap_category"] == selected_category]
    if selected_cert != "All":
        filtered = filtered[filtered["cert_list"].apply(lambda certs: selected_cert in certs)]
    if selected_distributor != "All":
        filtered = filtered[filtered["vendors"].apply(lambda vendors: selected_distributor in vendors)]
    if selected_region != "All":
        region_campuses = set(REGION_MAP[selected_region])
        filtered = filtered[filtered["campuses"].apply(lambda campuses: bool(region_campuses & set(campuses)))]
    if selected_campus_filter != "All":
        filtered = filtered[filtered["campuses"].apply(lambda campuses: selected_campus_filter in campuses)]

    filtered = filtered.copy()
    filtered["Opportunity"] = filtered.apply(lambda r: _opportunity_label(r, my_campus, my_vendors), axis=1)
    if only_new_opportunities:
        filtered = filtered[filtered["Opportunity"] == _NEW_VIA_MY_VENDOR]

    st.write(f"**{len(filtered)}** matching product(s)")
    if filtered.empty:
        st.info("No products match these filters. Try loosening one of them.")
        return

    display_df = filtered.copy()
    display_df["Distributors"] = display_df["vendors"].apply(lambda v: ", ".join(v) if v else "—")
    display_df["Brands"] = display_df["brands"].apply(lambda b: ", ".join(b) if b else "—")
    display_df["Campuses purchasing"] = display_df["campuses"].apply(lambda c: ", ".join(c) if c else "—")
    display_df["sustainability_certifications"] = display_df["sustainability_certifications"].fillna("—")

    display_df = display_df.sort_values(
        by="Opportunity", key=lambda s: s.map({_NEW_VIA_MY_VENDOR: 0, "": 1, _ALREADY_PURCHASING: 2})
    )

    st.dataframe(
        display_df[
            [
                "canonical_name",
                "simap_category",
                "sustainability_certifications",
                "Distributors",
                "Brands",
                "Campuses purchasing",
                "Opportunity",
            ]
        ].rename(
            columns={
                "canonical_name": "Product",
                "simap_category": "SIMAP category",
                "sustainability_certifications": "Certifications (as reported)",
            }
        ),
        use_container_width=True,
        hide_index=True,
    )

    st.download_button(
        "📥 Download filtered results (CSV)",
        data=display_df[
            [
                "canonical_name",
                "simap_category",
                "sustainability_certifications",
                "Distributors",
                "Brands",
                "Campuses purchasing",
                "Opportunity",
            ]
        ].to_csv(index=False),
        file_name="dining_dashboard_search_results.csv",
        mime="text/csv",
    )


def _render_distributor_tab(df: pd.DataFrame) -> None:
    st.caption(
        "Explore what a distributor or vendor/supplier already carries -- useful for onboarding a sustainable "
        "item through a supply relationship a campus already has, rather than starting a new one."
    )
    col1, col2 = st.columns(2)

    with col1:
        st.subheader("By distributor")
        all_vendors = sorted({v for vendors in df["vendors"] for v in vendors})
        if not all_vendors:
            st.info("No distributor data available.")
        else:
            selected_vendor = st.selectbox("Select a distributor", all_vendors, key="vendor_explorer")
            vendor_df = df[df["vendors"].apply(lambda vendors: selected_vendor in vendors)]
            brands = sorted({b for brands in vendor_df["brands"] for b in brands})
            campuses = sorted({c for campuses in vendor_df["campuses"] for c in campuses})
            st.write(f"**{len(vendor_df)}** sustainable product(s) from **{selected_vendor}**")
            st.write("**Vendors/suppliers carried:** " + (", ".join(brands) if brands else "—"))
            st.write("**Campuses purchasing through this distributor:** " + (", ".join(campuses) if campuses else "—"))
            st.dataframe(
                vendor_df[["canonical_name", "simap_category", "sustainability_certifications"]].rename(
                    columns={
                        "canonical_name": "Product",
                        "simap_category": "SIMAP category",
                        "sustainability_certifications": "Certifications (as reported)",
                    }
                ),
                use_container_width=True,
                hide_index=True,
            )

    with col2:
        st.subheader("By vendor/supplier")
        all_brands = sorted({b for brands in df["brands"] for b in brands})
        if not all_brands:
            st.info("No vendor/supplier data available.")
        else:
            selected_brand = st.selectbox("Select a vendor/supplier", all_brands, key="brand_explorer")
            brand_df = df[df["brands"].apply(lambda brands: selected_brand in brands)]
            vendors = sorted({v for vendors in brand_df["vendors"] for v in vendors})
            campuses = sorted({c for campuses in brand_df["campuses"] for c in campuses})
            st.write(f"**{len(brand_df)}** sustainable product(s) from **{selected_brand}**")
            st.write("**Distributed through:** " + (", ".join(vendors) if vendors else "—"))
            st.write("**Campuses purchasing this vendor/supplier:** " + (", ".join(campuses) if campuses else "—"))
            st.dataframe(
                brand_df[["canonical_name", "simap_category", "sustainability_certifications"]].rename(
                    columns={
                        "canonical_name": "Product",
                        "simap_category": "SIMAP category",
                        "sustainability_certifications": "Certifications (as reported)",
                    }
                ),
                use_container_width=True,
                hide_index=True,
            )


def main() -> None:
    conn = get_conn()
    st.title("Dining Dashboard")
    st.markdown(
        "Search "
        "<a href='/our-definition-of-sustainable' target='_self'>validated-sustainable</a>"
        " products purchased across every UC "
        "campus -- so chefs can find sustainable items to onboard through distributors they may already use. "
        "<strong>Select your campus from the sidebar at left, then search for new products by category or "
        "distributor to help grow your campus's sustainable purchasing.</strong>",
        unsafe_allow_html=True,
    )

    campuses = [r[0] for r in conn.execute("SELECT campus FROM campuses ORDER BY campus").fetchall()]
    default_campus = "UC Davis" if "UC Davis" in campuses else campuses[0]

    with st.sidebar:
        st.header("Settings")
        my_campus = st.selectbox(
            "My campus (reference point, not a filter)",
            campuses,
            index=campuses.index(st.session_state.get("selected_campus", default_campus)),
        )
        st.session_state["selected_campus"] = my_campus
        st.caption(
            "Every product below is still shown regardless of this selection -- it's only used to highlight "
            "results reachable through a distributor your campus already uses."
        )

    df = _load_products()
    my_vendors = _load_campus_vendors(my_campus)

    if df.empty:
        st.warning("No validated-sustainable products found in the database yet.")
        return

    tab1, tab2 = st.tabs(["Search Products", "Distributor & Vendor/Supplier Explorer"])
    with tab1:
        _render_search_tab(df, my_campus, my_vendors)
    with tab2:
        _render_distributor_tab(df)


if __name__ == "__main__":
    main()
