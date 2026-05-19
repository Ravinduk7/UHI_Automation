import streamlit as st
import folium
from streamlit_folium import st_folium
from folium.plugins import Draw
import streamlit.components.v1 as components

# Import our custom modules!
from core.utils import process_user_polygon, fetch_stac_data, create_word_report
from engines.uhi_engine import run_uhi_pipeline

# --- INVENTORY (SESSION STATE) SETUP ---
if "layers" not in st.session_state: st.session_state.layers = []
if "map_center" not in st.session_state: st.session_state.map_center = [6.9271, 79.8612]
if "map_zoom" not in st.session_state: st.session_state.map_zoom = 12

# --- THE SMART CAMERA SYSTEM ---
def auto_focus_camera():
    visible = [l for l in st.session_state.layers if st.session_state.get(f"vis_{l['name']}", False)]
    if visible:
        fb = visible[-1]["bounds"]
        st.session_state.map_center = [(fb[0][0] + fb[1][0])/2, (fb[0][1] + fb[1][1])/2]
    else:
        st.session_state.map_center, st.session_state.map_zoom = [6.9271, 79.8612], 12

# --- THE F5 SHIELD ---
components.html("<script>window.parent.addEventListener('beforeunload', function(e){e.preventDefault();e.returnValue='';});</script>", height=0, width=0)

st.title("🏙️ Urban Heat Island Automation Tool")
st.markdown("Automated Land Surface Temperature (LST) extraction using Landsat Thermal Infrared Sensor (TIRS) data.")

# --- SIDEBAR ---
with st.sidebar:
    st.header("Parameters")
    target_year = st.number_input("Select Year", 2013, 2026, 2024)
    layer_opacity = st.slider("Transparency", 0.1, 1.0, 0.7, 0.1)
    
    active_layers = []
    if st.session_state.layers:
        st.divider()
        st.subheader("🗺️ Layers")
        for layer in st.session_state.layers:
            vis_key = f"vis_{layer['name']}"
            if vis_key not in st.session_state: st.session_state[vis_key] = True
            
            c1, c2 = st.columns([5, 1])
            with c1:
                st.checkbox(layer["name"], key=vis_key, on_change=auto_focus_camera)
                if st.session_state[vis_key]: active_layers.append(layer["name"])
            
            with c2:
                # Upgrade: The Popover Confirmation Shield
                with st.popover("🗑️", help="Delete this layer"):
                    st.markdown("**Delete layer?**")
                    # If they click this red button inside the popover, THEN we wipe it.
                    if st.button("Confirm", key=f"confirm_{layer['name']}", type="primary"):
                        st.session_state.layers = [l for l in st.session_state.layers if l["name"] != layer["name"]]
                        auto_focus_camera()
                        st.rerun()

# --- MAP & STATS ---
m = folium.Map(location=st.session_state.map_center, zoom_start=st.session_state.map_zoom)

if st.session_state.layers:
    st.subheader("📊 UHI Statistics Tracker")
    table_data = [{"Layer": l["name"], "Date": l["date"], "Max Temp (°C)": round(l["stats"]["max"],1), "Mean Temp (°C)": round(l["stats"]["mean"],1), "Min Temp (°C)": round(l["stats"]["min"],1), "UHI Threshold (°C)": round(l["stats"]["threshold"],1)} for l in st.session_state.layers]
    st.dataframe(table_data, use_container_width=True, hide_index=True)

for layer in st.session_state.layers:
    if layer["name"] in active_layers:
        folium.raster_layers.ImageOverlay(image=layer["image"], bounds=layer["bounds"], opacity=layer_opacity, colormap=lambda x: (1, 0, 0, x), name=layer["name"]).add_to(m)

Draw(export=True, draw_options={'polyline': False, 'polygon': True, 'rectangle': True, 'circle': False, 'marker': False, 'circlemarker': False}).add_to(m)
output = st_folium(m, width=1000, height=500, key="uhi_master_map", returned_objects=["all_drawings"])

# --- PIPELINE LOGIC ---
if output["all_drawings"]:
    user_polygon = output["all_drawings"][-1]["geometry"]
    
    # 1. Talk to the shared toolbox
    bbox, area_sq_km, max_dim = process_user_polygon(user_polygon)
    
    st.info(f"📐 Area: {area_sq_km:.1f} sq km | Max Length: {max_dim:.1f} km")
    if area_sq_km > 2500 or max_dim > 100:
        st.error("🚨 Polygon too large or stretched! Keep under 2,500 sq km and 100 km length.")
        st.stop()

    with st.spinner("Querying STAC API..."):
        items = fetch_stac_data(bbox, target_year, ["landsat-c2-l2"])
        
    if items:
        dates = [i.datetime.strftime("%Y-%m-%d") for i in items]
        selected_date = st.selectbox("Analyze Date:", dates)
        
        if st.button("Generate Heat Map 🌡️"):
            with st.spinner("Running UHI Engine..."):
                item = next(i for i in items if i.datetime.strftime("%Y-%m-%d") == selected_date)
                thermal_url = item.assets["lwir11"].href
                
                try:
                    # 2. Call the Engine! The UI steps back and waits.
                    engine_output = run_uhi_pipeline(thermal_url, user_polygon, bbox, area_sq_km)
                    
                    # 3. Engine succeeds! Save to Inventory
                    layer_id = len(st.session_state.layers) + 1
                    st.session_state.layers.append({
                        "name": f"UHI ({selected_date}) - Area {layer_id}",
                        "date": selected_date,
                        "image": f"data:image/png;base64,{engine_output['image_b64']}",
                        "report_images": engine_output["report_images"],
                        "bounds": engine_output["bounds"],
                        "tiff_bytes": engine_output["tiff_bytes"],
                        "stats": engine_output["stats"]
                    })
                    auto_focus_camera()
                    st.rerun()
                
                except ValueError as e:
                    # Engine failed? Catch the error and print it cleanly to the user.
                    st.error(str(e))
    else:
        st.error("No clear images found. Try a different year or area.")
else:
    st.warning("Please draw a polygon on the map to start.")

# --- EXPORT HUB ---
if st.session_state.layers:
    st.divider()
    st.subheader("📥 Export Intelligence")
    c1, c2, c3 = st.columns([2, 1, 1])
    with c1: selected_name = st.selectbox("Select Layer:", [l["name"] for l in st.session_state.layers])
    layer = next(l for l in st.session_state.layers if l["name"] == selected_name)
    
    # Talk to the shared toolbox again!
    context = {
        "target_year": target_year, 
        "selected_date": layer["date"], 
        "max_temp": f"{layer['stats']['max']:.1f}", 
        "mean_temp": f"{layer['stats']['mean']:.1f}", 
        "min_temp": f"{layer['stats']['min']:.1f}", 
        "threshold": f"{layer['stats']['threshold']:.1f}", 
        "report_images": layer["report_images"]
    }
    # Notice we point to the new templates folder here!
    report_bytes = create_word_report("templates/uhi_template.docx", context) 
    
    with c2: st.write(""); st.write(""); st.download_button("Download GeoTIFF 🗺️", layer["tiff_bytes"], f"{selected_name}.tif", "image/tiff", use_container_width=True)
    with c3: st.write(""); st.write(""); st.download_button("Download Report 📄", report_bytes, f"Report_{layer['date']}.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", use_container_width=True)