"""Phase 7: Auto-Classifier page.

CLAUDE.md's exact spec: "campus uploads a purchasing sheet, gets
sustainability_certifications auto-filled by matching against previously
classified products." A self-service, READ-ONLY tool -- uploading a sheet
here never writes to the database. It's a lookup-and-annotate pass a chef
or purchasing staffer can run on a draft sheet before ordering, not a new
ingestion pathway (Phase 1's per-campus header-mapping pipeline remains the
only way data enters the canonical tables).

Matches uploaded product names against `product_aliases` -- every raw name
ever seen, across all 7 campuses, not just one -- reusing Phase 2's exact
fuzzy-matching machinery (lib.entity_matching._clean_for_matching /
_all_gates_match) so a campus benefits from what every OTHER campus has
already classified, per CLAUDE.md's note that product_aliases is "probably
the single highest-leverage table in the project."

Standalone for now, like the other pages in this rebuild -- run directly
with `streamlit run app/3_Auto_Classifier.py`.
"""

import sqlite3
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.auto_classifier import CONFIDENT_MATCH, NEEDS_REVIEW, NO_MATCH, load_match_corpus, match_uploaded_products
from lib.db import DEFAULT_DB_PATH

st.set_page_config(page_title="Auto-Classifier", layout="wide")


@st.cache_resource
def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DEFAULT_DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@st.cache_data(show_spinner=False)
def _load_corpus() -> pd.DataFrame:
    return load_match_corpus(get_conn())


def _read_uploaded_file(uploaded_file) -> pd.DataFrame | None:
    name = uploaded_file.name.lower()
    try:
        if name.endswith(".csv"):
            return pd.read_csv(uploaded_file)
        if name.endswith((".xlsx", ".xls")):
            return pd.read_excel(uploaded_file)
    except Exception as e:
        st.error(f"Couldn't read this file: {e}")
        return None
    st.error("Unsupported file type -- please upload a .csv or .xlsx file.")
    return None


def main() -> None:
    st.title("Auto-Classifier")
    st.markdown(
        "Upload a purchasing sheet and get it back with sustainability certifications, validated sustainable "
        "status, and SIMAP category **auto-filled wherever a confident match is found** against every product "
        "any UC campus has already classified. **Read-only**: this never changes anything in the shared "
        "database -- it's a lookup pass on your own file, meant to help before you order, not a way to submit "
        "new purchasing data (that still goes through the normal per-campus ingestion pipeline)."
    )

    with st.expander("How matching works"):
        st.markdown(
            "Each of your product names is compared against every raw product name any campus has ever "
            "reported, using the same fuzzy-text-matching and hard equality checks (pack size, origin, "
            "halal/frozen status, and more) the project's entity-resolution system uses -- so a match is never "
            "guessed. Three outcomes per row:\n\n"
            f"- **{CONFIDENT_MATCH}** -- a very close textual match that also passes every check; the filled-in "
            "certification/category is highly likely correct.\n"
            f"- **{NEEDS_REVIEW}** -- a plausible match, but close enough to a different product that it's "
            "worth a quick human look before trusting it.\n"
            f"- **{NO_MATCH}** -- nothing in the existing data was a good enough match. This means \"we don't "
            "know yet,\" not \"not sustainable.\""
        )

    uploaded_file = st.file_uploader("Upload a purchasing sheet", type=["csv", "xlsx", "xls"])
    if uploaded_file is None:
        st.info("Upload a CSV or Excel file to get started.")
        return

    uploaded_df = _read_uploaded_file(uploaded_file)
    if uploaded_df is None or uploaded_df.empty:
        if uploaded_df is not None:
            st.warning("This file has no rows.")
        return

    st.subheader("Preview")
    st.dataframe(uploaded_df.head(10), use_container_width=True, hide_index=True)

    name_column = st.selectbox(
        "Which column has the product name?",
        uploaded_df.columns.tolist(),
        help="We never guess this -- pick the column yourself so matching runs against the right text.",
    )

    if st.button("Run Auto-Classification", type="primary"):
        names = uploaded_df[name_column].astype(str).tolist()
        with st.spinner(f"Matching {len(names)} product(s) against every campus's classified products..."):
            corpus = _load_corpus()
            matches = match_uploaded_products(names, corpus)
        result_df = pd.concat([uploaded_df.reset_index(drop=True), matches.drop(columns=["uploaded_name"])], axis=1)
        st.session_state["auto_classifier_result"] = (uploaded_file.name, result_df)

    stored = st.session_state.get("auto_classifier_result")
    if stored is None or stored[0] != uploaded_file.name:
        return
    result_df = stored[1]

    tier_counts = result_df["match_tier"].value_counts()
    col1, col2, col3 = st.columns(3)
    col1.metric(CONFIDENT_MATCH, int(tier_counts.get(CONFIDENT_MATCH, 0)))
    col2.metric(NEEDS_REVIEW, int(tier_counts.get(NEEDS_REVIEW, 0)))
    col3.metric(NO_MATCH, int(tier_counts.get(NO_MATCH, 0)))

    st.subheader("Results")
    st.dataframe(
        result_df.rename(
            columns={
                "matched_name": "Matched product",
                "match_score": "Match score",
                "match_tier": "Match tier",
                "simap_category": "SIMAP category",
                "sustainability_certifications": "Certifications (auto-filled)",
                "validated_sustainable_yn": "Validated sustainable?",
            }
        ),
        use_container_width=True,
        hide_index=True,
    )

    st.download_button(
        "📥 Download annotated sheet (CSV)",
        data=result_df.to_csv(index=False),
        file_name=f"auto_classified_{uploaded_file.name.rsplit('.', 1)[0]}.csv",
        mime="text/csv",
    )


if __name__ == "__main__":
    main()
