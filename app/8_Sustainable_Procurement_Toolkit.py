"""Sustainable Procurement Toolkit.

Renders reference/GCLC_toolkit_2025.pdf ("Sustainable Procurement Toolkit,"
Bonnie Reiss Climate Action Fellowship 2024-2025) as an in-page interactive
PDF viewer, plus a download button. Unlike the other Reference pages, this
one isn't backed by the canonical database at all -- it's a static document
passthrough, so there's no db connection or cached query here, just the PDF
file itself.

The viewer points its <iframe> at a real URL (Streamlit's app-static-file
server, served from app/static/ -- see .streamlit/config.toml's
`enableStaticServing`) rather than a base64 data: URI. A data: URI works for
st.download_button, but embedding one in a nested <iframe> gets silently
navigation-blocked by some browsers/embedded previews, and at ~13MB it would
also mean re-sending a ~17MB base64 blob on every page load. reference/ stays
the single committed copy of the PDF; app/static/ is a gitignored runtime
copy synced from it on each page load (cheap: a size check, then a copy only
if missing or stale).

Part of the unified app/Home.py multi-page shell (also still runnable
standalone via `streamlit run app/8_Sustainable_Procurement_Toolkit.py` for
local debugging).
"""

import shutil
from pathlib import Path

import streamlit as st

# st.set_page_config() now lives in app/Home.py -- see that file's docstring.

REFERENCE_DIR = Path(__file__).resolve().parent.parent / "reference"
TOOLKIT_PDF_NAME = "GCLC_toolkit_2025.pdf"
TOOLKIT_PDF_PATH = REFERENCE_DIR / TOOLKIT_PDF_NAME

APP_STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_PDF_PATH = APP_STATIC_DIR / TOOLKIT_PDF_NAME
STATIC_PDF_URL = f"app/static/{TOOLKIT_PDF_NAME}"


def _sync_static_copy() -> bool:
    """Mirror reference/GCLC_toolkit_2025.pdf into app/static/ so Streamlit's
    app-static-file server (enableStaticServing) can serve it by URL.

    Streamlit's static handler resolves symlinks and rejects any path that
    escapes app/static/, so the file has to be a real copy there, not a
    symlink back into reference/. Returns whether a servable copy exists.
    """
    if not TOOLKIT_PDF_PATH.exists():
        return False
    APP_STATIC_DIR.mkdir(parents=True, exist_ok=True)
    if not STATIC_PDF_PATH.exists() or STATIC_PDF_PATH.stat().st_size != TOOLKIT_PDF_PATH.stat().st_size:
        shutil.copyfile(TOOLKIT_PDF_PATH, STATIC_PDF_PATH)
    return True


@st.cache_data(show_spinner=False)
def _load_toolkit_pdf_bytes() -> bytes | None:
    return TOOLKIT_PDF_PATH.read_bytes() if TOOLKIT_PDF_PATH.exists() else None


def main() -> None:
    st.title("Sustainable Procurement Toolkit")
    st.markdown(
        "This toolkit was designed for anyone working to make food procurement at their campus more "
        "sustainable, whether they are starting from zero or already actively involved in onboarding new "
        "sustainable products. The toolkit breaks down how to conduct sustainability reporting, investigate "
        "your institution's supply chain, and increase sustainable procurement. It's aimed at sustainability "
        "coordinators, dining managers, students, chefs, or anyone in the supply chain interested in shifting "
        "institutional levers."
    )

    pdf_bytes = _load_toolkit_pdf_bytes()
    if not pdf_bytes:
        st.caption("Toolkit PDF not found.")
        return

    st.download_button(
        "📥 Download the toolkit (PDF)",
        data=pdf_bytes,
        file_name=TOOLKIT_PDF_NAME,
        mime="application/pdf",
    )

    st.divider()

    if _sync_static_copy():
        st.markdown(
            f"""
            <iframe
                src="/{STATIC_PDF_URL}"
                width="100%"
                height="900"
                style="border: 1px solid #ddd; border-radius: 4px;"
                type="application/pdf">
            </iframe>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.caption("Toolkit PDF not found.")


if __name__ == "__main__":
    main()
