"""Food Categories and Greenhouse Gas Calculations.

Split out of app/2_Dining_Dashboard.py's "About this tool" expander into
its own top-level page -- SIMAP-57 categorization and GHG-equivalent
reporting apply project-wide (Campus Roadmap's optimizer and PDF report
both key off `products.simap_category`), not just the Dining Dashboard.

Part of the unified app/Home.py multi-page shell (also still runnable
standalone via `streamlit run app/6_Food_Categories_and_GHG.py` for local
debugging).
"""

import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# st.set_page_config() now lives in app/Home.py -- see that file's docstring.

REFERENCE_DIR = Path(__file__).resolve().parent.parent / "reference"
SIMAP_CATEGORIES_PATH = REFERENCE_DIR / "simap_categories.csv"


@st.cache_data(show_spinner=False)
def _load_simap_csv_bytes() -> bytes:
    return SIMAP_CATEGORIES_PATH.read_bytes()


def main() -> None:
    st.title("Food Categories and Greenhouse Gas Calculations")

    st.markdown("#### How Food Categories are Determined")
    st.markdown(
        "The Sustainability Indicator Management & Analysis Platform, or SIMAP, is a greenhouse-gas "
        "accounting tool widely used in higher education. It comes from Poore and Nemecek's 2018 paper in "
        "Science ([https://www.science.org/doi/10.1126/science.aaq0216]"
        "(https://www.science.org/doi/10.1126/science.aaq0216)). We use their updated 57-category food "
        "framework to group similar foods and estimate emissions. Importantly, this reference is not precise "
        "enough to differentiate carbon emissions stemming from conventional vs sustainable sources, or from "
        "local vs nonlocal sources. For example, Certified Humane beef and conventional beef have the same "
        "carbon equivalent in our accounting. Fruit from Chile and California also have the same carbon "
        "equivalents. To see the estimated carbon emissions associated with each food category, please "
        "reference the full spreadsheet, attached below."
    )
    st.download_button(
        "📥 Download SIMAP-57 category reference (CSV)",
        data=_load_simap_csv_bytes(),
        file_name="simap_categories.csv",
        mime="text/csv",
    )


if __name__ == "__main__":
    main()
