import streamlit as st

# The Lobby UI
st.title("🌍 Geospatial Intelligence Suite")
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
        **Version 1.1**
        * **Sensor:** Landsat 8/9 TIRS
        * **Output:** Land Surface Temperature (LST) & Heat Anomalies
        * **Status:** 🟢 Online
        """)
        # 2. Use page_link to connect to the router
        st.page_link("views/uhi_view.py", label="Launch Module", icon="🚀", use_container_width=True)

with col2:
    with st.container(border=True):
        st.markdown("""
        ### 🌾 Crop Monitor
        **NDVI Tracker**
        * **Sensor:** Sentinel-2 L2A
        * **Output:** Vegetation Health & Soil Moisture
        * **Status:** 🟡 In Development
        """)
        # 3. Use a disabled button as a placeholder so it doesn't crash!
        st.button("🚧 Coming Soon", disabled=True, use_container_width=True)