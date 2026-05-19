import streamlit as st

# 1. Global Page Config (Must be the very first Streamlit command)
st.set_page_config(page_title="Azura Geospatial", page_icon="🌍", layout="wide")

# 2. Register your pages
home_page = st.Page("views/home.py", title="Dashboard", icon="🏠")
uhi_page = st.Page("views/uhi_view.py", title="UHI Architect", icon="🏙️")
# ndvi_page = st.Page("views/ndvi_view.py", title="Crop Monitor", icon="🌾") # Uncomment this later!

# 3. Build the Navigation Menu
pg = st.navigation({
    "Main": [home_page],
    "Active Modules": [uhi_page],
    # "Upcoming Modules": [ndvi_page]
})

# 4. Run the App
pg.run()