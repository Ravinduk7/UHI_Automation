import streamlit as st
import folium
from streamlit_folium import st_folium
from folium.plugins import Draw

# --- UI SETUP ---
st.set_page_config(page_title="Urban Heat Island Architect", layout="wide")
st.title("🏙️ Urban Heat Island Automation Tool")
st.markdown("Draw a boundary and select a year to identify local hotspots.")

# --- SIDEBAR INPUTS ---
with st.sidebar:
    st.header("Search Parameters")
    # Step 1: Select Year
    target_year = st.number_input("Select Year for Analysis", 
                                 min_value=2013, max_value=2026, value=2024)
    
    st.info("Note: Landsat 8/9 data is available from 2013 onwards.")

# --- THE INTERACTIVE MAP ---
st.subheader("1. Define your Area of Interest")

# Initialize the map over Sri Lanka
m = folium.Map(location=[6.9271, 79.8612], zoom_start=12)

# Add the "Draw" plugin to allow users to draw polygons
Draw(export=True).add_to(m)

# Display the map and capture the drawing data
output = st_folium(m, width=1000, height=500)

# --- CAPTURING THE INPUT ---
if output["all_drawings"]:
    # Grab the last thing the user drew
    user_geometry = output["all_drawings"][-1]["geometry"]
    st.success("Boundary captured! Ready to fetch satellite data.")
    
    # This is where we will call our API next
    st.json(user_geometry) 
else:
    st.warning("Please draw a polygon on the map to start.")