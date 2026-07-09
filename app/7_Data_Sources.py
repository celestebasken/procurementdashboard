"""Data Sources.

Explains where the underlying purchasing data comes from, its scope
(FY2025, all 7 reporting campuses), and how to reach the project owner to
report an error or submit/edit/remove data -- referenced by name from
app/3_Auto_Classifier.py's "Sustainability Auto-Reporting" page (that page
is read-only; this is where a campus is pointed for an actual data
submission).

Part of the unified app/Home.py multi-page shell (also still runnable
standalone via `streamlit run app/7_Data_Sources.py` for local debugging).
"""

import streamlit as st

# st.set_page_config() now lives in app/Home.py -- see that file's docstring.


def main() -> None:
    st.title("Data Sources")
    st.markdown(
        "All data was shared by campuses and is from the Fiscal 2025 year (July 1, 2024 through June 30, "
        "2025). All price data is not shared explicitly in this tool, and this resource is exclusively for "
        "internal use. However, the data must include prices to be usable for the roadmap and price-checker "
        "tools. Price-free data can be included in the dining dashboard and sustainability auto-reporting "
        "tool.\n\n"
        "Thank you so much to the campuses that contributed their data for this tool. If you spot any errors "
        "in the data or the dashboard overall, we would be grateful if you would share them with Celeste.\n\n"
        "If you would like to submit, edit, or remove any data, either for FY2025 or another time, please "
        "contact Celeste.\n\n"
        "To contact Celeste Basken: [cbasken@ucdavis.edu](mailto:cbasken@ucdavis.edu)"
    )


if __name__ == "__main__":
    main()
