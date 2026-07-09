"""Our Definition of Sustainable.

Split out of app/2_Dining_Dashboard.py's "About this tool" expander into
its own top-level page -- the definition, eligible certifications, and
purchasing guide apply to every tool in this app (Campus Roadmap, Dining
Dashboard, Auto-Reporting, Price Checker all key off
`products.validated_sustainable_yn`), not just the Dining Dashboard, so it
belongs in the nav directly rather than nested inside one tool's expander.

Part of the unified app/Home.py multi-page shell (also still runnable
standalone via `streamlit run app/5_Our_Definition_of_Sustainable.py` for
local debugging).
"""

import sqlite3
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.db import DEFAULT_DB_PATH
from lib.dining_dashboard import load_certification_types

# st.set_page_config() now lives in app/Home.py -- see that file's docstring.

REFERENCE_DIR = Path(__file__).resolve().parent.parent / "reference"
PDF_GUIDE_PATH = REFERENCE_DIR / "Brief_guide_on_UC_Sustainable_Purchasing.pdf"


@st.cache_resource
def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DEFAULT_DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@st.cache_data(show_spinner=False)
def _load_cert_types() -> pd.DataFrame:
    return load_certification_types(get_conn())


@st.cache_data(show_spinner=False)
def _load_pdf_guide_bytes() -> bytes | None:
    return PDF_GUIDE_PATH.read_bytes() if PDF_GUIDE_PATH.exists() else None


def main() -> None:
    st.title("Our Definition of Sustainable")

    st.markdown("#### How we define \"sustainable\"")
    st.markdown(
        "> \"Each campus foodservice operation will strive to procure 25% sustainable food products by the "
        "year 2030 as defined by AASHE STARS, and each health location foodservice operation will strive to "
        "procure 30% sustainable food products by the year 2030 as defined by Practice Greenhealth, while "
        "maintaining accessibility and affordability for all students and health location's foodservice "
        "patrons.\"\n>\n"
        "> — *University of California Policy on Sustainable Practices 2024, Part H (Page 18)*"
    )
    st.markdown("#### Eligible sustainability certifications from AASHE STARS and/or PGH")
    st.markdown(
        "- **AASHE STARS** -- the Association for the Advancement of Sustainability in Higher Education's "
        "Sustainability Tracking, Assessment & Rating System, the standard academic (non-health) UC campuses "
        "report sustainable food purchasing against. [stars.aashe.org](https://stars.aashe.org/)\n"
        "- **Practice Greenhealth (PGH)** -- specifically its Healthy Food in Health Care purchasing "
        "standard, used by UC Health locations. "
        "[practicegreenhealth.org/topics/food](https://practicegreenhealth.org/topics/food)"
    )
    st.dataframe(
        _load_cert_types().rename(
            columns={
                "certification_name": "Certification",
                "abbreviation": "Abbreviation",
                "frameworks": "Recognized under",
                "qualifier": "Notes / restrictions",
            }
        ),
        use_container_width=True,
        hide_index=True,
    )

    st.markdown("#### Guide: Brief Guide on UC Sustainable Purchasing")
    pdf_guide_bytes = _load_pdf_guide_bytes()
    if pdf_guide_bytes:
        st.download_button(
            "📥 Download the guide (PDF)",
            data=pdf_guide_bytes,
            file_name="Brief_guide_on_UC_Sustainable_Purchasing.pdf",
            mime="application/pdf",
        )
    else:
        st.caption("Guide PDF not found.")


if __name__ == "__main__":
    main()
