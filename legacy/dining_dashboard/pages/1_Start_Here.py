import streamlit as st
import pandas as pd
import base64
from pathlib import Path

if st.session_state.get("authentication_status") != True:
    st.info("Please log in on the app page to continue.")
    st.stop()

st.set_page_config(page_title="Start Here", page_icon="📈")

# Load data
st.title("UC Sustainable Procurement Dashboard")

st.markdown("""
Welcome to the UC Sustainable Procurement Dashboard!

This is a project of UC Berkeley [Bonnie Reiss Global Food Initiative Fellows](https://www.ucop.edu/leading-on-climate/student-involvement/index.html). This tool allows users to see what sustainable items are currently being purchased by UC campuses. It combines 
sustainable food purchasing data from multiple UC campuses, highlighting key attributes such as food category, supplier, distributor, and sustainability certifications. The data for this 
tool were kindly provided during the 2024-25 academic year by stakeholders from various campuses. If you are a UC procurement stakeholder 
and would like to add or edit data, please contact [Celeste Basken](mailto:cbasken@berkeley.edu).
            
It is our hope that this tool will help your campus to identify further opportunities to purchase sustainable products, in alignment with the UC Office of the President's goals.

### Use the menu on the left sidebar to:
- Search for food items by category, certification, campus, or region
- Explore supplier and distributor offerings
- View summaries of sustainability certifications

### What do we consider "Sustainable"
            
As part of the University of California’s continued commitment to enhancing community and environmental sustainability through dining procurement, they have set the following goal:

“Each campus foodservice operation will strive to procure 25% sustainable food products by the year 2030 as defined by AASHE STARS, and each health location foodservice operation will strive to procure 30% sustainable food products by the year 2030 as defined by Practice Greenhealth, while maintaining accessibility and affordability for all students and health location’s foodservice patrons.”
- [University of California Policy on Sustainable Practices 2024, Part H (Page 18)](https://policy.ucop.edu/doc/3100155/SustainablePractices)

For the purposes of UCOP and the Bonnie Reiss fellowship, this database uses two definitions of sustainable:
- For most campuses, sustainable is defined by the [Association for the Advancement of Sustainability in Higher 
Education's Sustainability Tracking, Assessment & Rating System (AASHE STARS)](https://stars.aashe.org/resources-support/technical-manual/). In short, this is a comprehensive list of standards
(with familiar names like USDA Organic, Fair Trade, etc) that they consider as sustainable.
- For health UC Health Campuses, sustainable is defined by [Practice Greenhealth's Healthier Hospitals Food Criteria](https://practicegreenhealth.org/topics/food/food-purchasing-criteria).
- This database includes standards from both, with the ability to toggle between views in the Category Explorer.
            
""")

sustainability_dict = {
    "OG": "Organic",
    "CH": "Certified Humane",
    "FT": "Fair Trade",
    "RAC": "Regenerative Ag.",
    "AGA": "Grassfed Assoc.",
    "AWA": "Animal Welfare",
    "GAP": "Global Animal Partnership",
    "AHC": "American Humane Certified",
    "HFAC": "Humane Farm Care",
    "MSC": "Marine Stewardship Council",
    "BAP": "Best Aquaculture Practices",
    "MBA": "Monterrey Bay Aquarium",
    "WWF": "WWF/Good Fish Foundation",
    "OWR": "Ocean Wise Recommended",
    "SSB": "Sailors for the Sea Blue list",
    "SFSC": "Short Food supply chain",
    "SP": "Small producer",
    "BFC": "Bird Friendly Coffee",
    "BBC": "Bee Better Certified (Xerces Society)",
    "FAC": "Food Alliance Certified",
    "SPP": "Small Producers Symbol",
    "EFI": "Equitable Food Initiative",
    "MWD": "Milk with Dignity",
    "NAE": "No Antibiotics Ever"
}

# Glossary section
st.subheader("Glossary of Certification Terms")
for short, full in sustainability_dict.items():
    st.markdown(f"**{short}**: {full}")

# ---- PDF widget (viewer + download) ----
st.subheader("Brief Guide on UC Sustainable Purchasing")

pdf_path = Path("Brief_guide_on_UC_Sustainable_Purchasing.pdf")

if pdf_path.exists():
    pdf_bytes = pdf_path.read_bytes()

    # Download button
    st.download_button(
        label="📥 Download as PDF",
        data=pdf_bytes,
        file_name=pdf_path.name,
        mime="application/pdf",
    )

    # Scrollable embedded viewer (iframe)
    base64_pdf = base64.b64encode(pdf_bytes).decode("utf-8")
    pdf_display = f"""
        <iframe
            src="data:application/pdf;base64,{base64_pdf}"
            width="100%"
            height="800"
            style="border: 1px solid #ddd;"
        ></iframe>
    """
    st.markdown(pdf_display, unsafe_allow_html=True)

else:
    st.warning(f"PDF not found at: {pdf_path.resolve()}")

st.markdown("""
---
This tool was created by Celeste Basken and Victoria Quach, 2025. For questions or feedback, please reach out to cbasken [at] berkeley [dot] edu. 
We would be very grateful for any feedback you have about features you would like to see, or bugs you spot. Thanks! :-)
""")

