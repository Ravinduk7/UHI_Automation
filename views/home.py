import streamlit as st

# The Lobby UI
st.title("🌍 Geospatial Intelligence Suite")
st.caption("Version 2.0.0") # Repping the major version bump!
st.markdown("---")

st.markdown("""
### Welcome to the Command Center
This platform provides automated, end-to-end remote sensing pipelines for environmental and agricultural analysis. 

Please select a module below or from the sidebar to begin processing satellite telemetry.
""")

col1, col2 = st.columns(2)

with col1:
    # 1. Use a bordered container to create the "Card" look
    with st.container(border=True):
        st.markdown("""
        ### 🏙️ UHI Architect
        **Land Surface Temperature Pipeline**
        * **Sensor:** Landsat 8/9 TIRS
        * **Output:** Thermal Anomalies & Reports
        * **Status:** 🟢 Online
        """)
        # 2. Use page_link to connect to the router
        st.page_link("views/uhi_view.py", label="Launch Module", icon="🚀", use_container_width=True)

with col2:
    with st.container(border=True):
        st.markdown("""
        ### 🌊 Flood Risk Assessor
        **SAR Inundation Pipeline**
        * **Sensors:** Open-Meteo & Sentinel-1 RTC
        * **Output:** Probabilistic Flood Mapping
        * **Status:** 🟢 Online 
        """)
        # THE GATES ARE OPEN: Linked to the new flood view!
        st.page_link("views/flood_view.py", label="Launch Module", icon="🚀", use_container_width=True)