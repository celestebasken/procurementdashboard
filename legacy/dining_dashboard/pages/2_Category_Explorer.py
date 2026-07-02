import streamlit as st
import pandas as pd
import matplotlib.pyplot as plt
import difflib

if st.session_state.get("authentication_status") != True:
    st.info("Please log in on the app page to continue.")
    st.stop()

st.set_page_config(page_title="Category Explorer", page_icon="📈")

st.sidebar.header("Interactive Tool")

# Load data from public Google Sheet
@st.cache_data
def load_data():
    url = "https://docs.google.com/spreadsheets/d/1qsapyNmZleoL75aIwH57W3nqTc_VLhdbFEieOTwYWiI/export?format=csv"
    df = pd.read_csv(url)
    df.columns = df.columns.str.strip()

    # Defensive: ensure aggregator columns are numeric 0/1 if present
    for col in ["PGH", "AASHE"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)

    return df

df = load_data()

# Region mapping
region_map = {
    "SoCal": ["UCLA", "UCR","UCSD_H","UCLA_H"],
    "Central": ["UCM"],
    "NorCal": ["UCB", "UCD_H", "UCSC","UCD"]
}

campus_cols = ['UCLA', 'UCD_H', 'UCB', 'UCR', 'UCM', 'UCSC','UCSD_H','UCLA_H','UCD']
campus_contacts = {
    "UCLA": "UCLA - Jane Doe (jane.doe@ucla.edu)",
    "UCD_H": "UC Davis Health - Jane Doe (jane.doe@ucla.edu)",
    "UCB": "UC Berkeley - Jane Doe (jane.doe@ucla.edu)",
    "UCR": "UC Riverside - Jane Doe (jane.doe@ucla.edu)",
    "UCM": "UC Merced - Jane Doe (jane.doe@ucla.edu)",
    "UCSC": "UC Santa Cruz - Jane Doe (jane.doe@ucla.edu)",
    "UCSD_H": "UC San Diego Health - Jane Doe (jane.doe@ucla.edu)",
    "UCLA_H": "UCLA Health - Jane Doe (jane.doe@ucla.edu)",
    "UCD": "UC Davis - Jane Doe (jane.doe@ucla.edu)"
}

campus_name_map = {
    "UCLA": "UCLA",
    "UCD_H": "UC Davis Health",
    "UCB": "UC Berkeley",
    "UCR": "UC Riverside",
    "UCM": "UC Merced",
    "UCSC": "UC Santa Cruz",
    "UCSD_H": "UC San Diego Health",
    "UCLA_H": "UCLA Health",
    "UCD": "UC Davis"
}

def list_campuses(row):
    campuses = [c for c in campus_cols if c in row and row[c] == 1]
    return ", ".join(campuses)

def list_tooltips(row):
    campuses = [c for c in campus_cols if c in row and row[c] == 1]
    return ", ".join([f"{c} ({campus_contacts[c]})" for c in campuses])

def list_full_campuses(row):
    campuses = [c for c in campus_cols if c in row and row[c] == 1]
    return ", ".join([campus_name_map[c] for c in campuses])

df['Campuses Procuring'] = df.apply(list_campuses, axis=1)
df['Campus Contacts'] = df.apply(list_tooltips, axis=1)
df['Full Campus Names'] = df.apply(list_full_campuses, axis=1)

# Sustainability standard mapping
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
sustainability_cols = [col for col in sustainability_dict if col in df.columns]

# Sidebar filters
st.sidebar.header("Filter Options")

# NEW: Standards Aggregator filter
aggregator_options = ["Both", "AASHE STARS", "Practice Greenhealth"]
selected_aggregator = st.sidebar.selectbox("Standards Aggregator", aggregator_options)

categories = ["All"] + sorted(df['Category'].dropna().unique())
selected_category = st.sidebar.selectbox("Select Food Category", categories)

filtered_df = df if selected_category == "All" else df[df['Category'] == selected_category]

# Apply NEW aggregator filter (expects columns PGH and AASHE in the sheet)
if selected_aggregator == "AASHE STARS":
    if "AASHE" in filtered_df.columns:
        filtered_df = filtered_df[filtered_df["AASHE"] == 1]
    else:
        filtered_df = filtered_df.iloc[0:0]  # empty
elif selected_aggregator == "Practice Greenhealth":
    if "PGH" in filtered_df.columns:
        filtered_df = filtered_df[filtered_df["PGH"] == 1]
    else:
        filtered_df = filtered_df.iloc[0:0]  # empty
else:  # Both
    has_aashe = "AASHE" in filtered_df.columns
    has_pgh = "PGH" in filtered_df.columns
    if has_aashe and has_pgh:
        filtered_df = filtered_df[(filtered_df["AASHE"] == 1) | (filtered_df["PGH"] == 1)]
    elif has_aashe:
        filtered_df = filtered_df[filtered_df["AASHE"] == 1]
    elif has_pgh:
        filtered_df = filtered_df[filtered_df["PGH"] == 1]
    else:
        filtered_df = filtered_df.iloc[0:0]  # empty

# Region filter
regions = list(region_map.keys())
selected_region = st.sidebar.selectbox("Filter by Region", ["All"] + regions)
if selected_region != "All":
    region_campuses = region_map[selected_region]
    filtered_df = filtered_df[filtered_df[region_campuses].sum(axis=1) > 0]

# Campus filter
campus = st.sidebar.selectbox("Filter by Campus", ["All"] + campus_cols)
if campus != "All":
    filtered_df = filtered_df[filtered_df[campus] == 1]

# Certification filter
cert = st.sidebar.selectbox("Filter by Sustainability Standard", ["All"] + sustainability_cols)
if cert != "All":
    filtered_df = filtered_df[filtered_df[cert] == 1]

st.markdown("""
## Product Explorer
Use the menu on the left to search for sustainable food items by category, campus region, campus, or sustainability certification.
- The default view includes all sustainable products that UC campuses shared with our team, so it is quite large.
- You can choose to see only products that are compliant with a specific "Standards Aggregator": either AASHE STARS for regular campuses or PGH for health campuses. Please refer to the "Start Here" page for more in-depth information about sustainability standards.
- You can download the current table view with the "Download Filtered CSV" button
- You can search for specific products by hovering over the table view and selecting the small search icon in the top right corner.
- Acronyms are used for simplicity under the Filter Options. Please refer to the "Start Here" page for full standard names and definitions.
""")

# Handle case when no data is returned
if filtered_df.empty:
    st.warning("No products found for the selected filters. Please try a different combination.")
else:
    st.title("Filtered Product Table")
    st.dataframe(filtered_df[['ProductName', 'Supplier', 'Distributor', 'Standard', 'Campuses Procuring']])

    st.download_button(
        "📥 Download Filtered CSV",
        data=filtered_df.to_csv(index=False),
        file_name="filtered_data.csv",
        mime="text/csv"
    )

    st.subheader("Suppliers Providing These Products")
    unique_suppliers = sorted(filtered_df['Supplier'].dropna().unique())
    st.write(", ".join(unique_suppliers))

    st.subheader("Campuses Purchasing These Products")
    campus_names = set()
    for row in filtered_df.itertuples():
        if hasattr(row, 'Full_Campus_Names'):
            campus_names.update([x.strip() for x in getattr(row, 'Full_Campus_Names').split(',') if x.strip()])
        else:
            campus_names.update([campus_name_map[c] for c in campus_cols if getattr(row, c) == 1])
    if campus_names:
        st.write(", ".join(sorted(campus_names)))
    else:
        st.write("No campus purchases found in this selection.")

    # Horizontal bar chart of sustainability certifications
    st.subheader("Sustainability Certifications")
    standard_counts = {sustainability_dict[k]: filtered_df[k].sum() for k in sustainability_cols if filtered_df[k].sum() > 0}
    if standard_counts:
        fig2, ax2 = plt.subplots(figsize=(4, 3))
        ax2.barh(list(standard_counts.keys()), list(standard_counts.values()))
        ax2.set_xlabel("Number of Products")
        ax2.set_ylabel("Certification")
        st.pyplot(fig2)
    else:
        st.write("No sustainability certifications in this selection.")