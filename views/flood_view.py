import streamlit as st
import folium
from streamlit_folium import st_folium

from engines.flood_engine import run_flood_pipeline
from core.utils import process_user_polygon

def render_flood_module():
    st.title("🌊 Flood Risk Assessor")
    st.markdown("### SAR Inundation Probability Pipeline")
    st.divider()

    # --- BACKGROUND INVENTORY INIT ---
    if "flood_inventory" not in st.session_state:
        st.session_state.flood_inventory = []
    if "current_drawing" not in st.session_state:
        st.session_state.current_drawing = None

    # --- SIDEBAR CONTROLS ---
    with st.sidebar:
        st.header("Risk Parameters")
        years_back = st.slider("Historical Window (Years)", 1, 5, 3)
        layer_opacity = st.slider("Result Opacity", 0.1, 1.0, 0.7, 0.1)
        
        st.divider()

        # The Cleaned-Up Generate Button
        if st.button("🚀 Run Risk Assessment", type="primary", use_container_width=True):
            if st.session_state.current_drawing is not None:
                with st.spinner("Scouting historical storms & fetching radar... (Takes 30-60s)"):
                    result = run_flood_pipeline(st.session_state.current_drawing, years_back)
                    
                    if "error" in result:
                        st.error(result["error"])
                    else:
                        # THE FIX: Append to our background array instead of overwriting!
                        run_id = len(st.session_state.flood_inventory) + 1
                        st.session_state.flood_inventory.append({
                            "id": run_id,
                            "polygon": st.session_state.current_drawing,
                            "result": result,
                            "show_master": True,
                            "active_layers": {}
                        })
                        st.rerun()
            else:
                st.error("Please draw an Area of Interest (AOI) on the map first.")

        # --- DYNAMIC MULTI-LAYER MANAGER ---
        if st.session_state.flood_inventory:
            st.success(f"✅ Inventory Active ({len(st.session_state.flood_inventory)} regions)")
            
            for i, item in enumerate(st.session_state.flood_inventory):
                # Only keep the most recent one expanded by default to save space
                with st.expander(f"📍 Area {item['id']} Manager", expanded=(i == len(st.session_state.flood_inventory)-1)):
                    res = item["result"]
                    
                    # Master Toggle
                    item["show_master"] = st.checkbox(
                        "🌊 Master Probability Compound", 
                        value=item.get("show_master", True), 
                        key=f"master_{item['id']}"
                    )
                    
                    st.divider()
                    st.write("**Toggle Individual Storms**")
                    
                    # Loop through the tiers (No more giant captions!)
                    for tier_key, tier_data in res['individual_layers'].items():
                        storm_date = tier_data["date"]
                        is_checked = item["active_layers"].get(tier_key, False)
                        
                        item["active_layers"][tier_key] = st.checkbox(
                            f"🌩️ {tier_key} ({storm_date})", 
                            value=is_checked, 
                            key=f"chk_{item['id']}_{tier_key}"
                        )
                    
                    st.divider()
                    # Upgrade: The Popover Confirmation Shield from UHI
                    with st.popover(f"🗑️ Delete Area {item['id']}", help="Remove this analysis"):
                        st.markdown("**Delete this area?**")
                        if st.button("Confirm", key=f"confirm_del_{item['id']}", type="primary"):
                            st.session_state.flood_inventory.pop(i)
                            st.rerun()

    # --- MAP HUD ---    
    
    st.write("📍 **Step 1: Draw your Area of Interest (AOI) and hit Run.**")

    m = folium.Map(
        location=[7.8731, 80.7718], 
        zoom_start=7, 
        control_scale=True, 
        tiles="CartoDB dark_matter"
    )
    
    folium.plugins.Draw(
        export=True,
        position='topleft',
        draw_options={'polyline': False, 'polygon': True, 'circle': False, 'marker': False, 'circlemarker': False}
    ).add_to(m)

    # Break the loop definition anti-pattern by defining this static style once
    def get_aoi_style(feature):
        return {'color': '#ff0000', 'fillColor': 'transparent', 'weight': 2}
    
    # --- MULTI-OVERLAY PAINTER ---
    for item in st.session_state.flood_inventory:
        res = item["result"]
        
        # DEFENSIVE SHIELD: Prevent a corrupted session state from taking down the whole UI
        try:
            bbox, _, _ = process_user_polygon(item["polygon"])
        except ValueError:
            st.error(f"Area {item['id']} contains corrupted geometry and could not be rendered.")
            continue
            
        bounds = [[bbox[1], bbox[0]], [bbox[3], bbox[2]]]
        
        # Auto-zoom to the MOST RECENT drawn area
        if item == st.session_state.flood_inventory[-1]:
            m.fit_bounds(bounds)
        
        # Paint the red bounding box
        feature = {
            "type": "Feature",
            "geometry": item["polygon"],
            "properties": {}
        }
            
        folium.GeoJson(
            feature,
            style_function=get_aoi_style
        ).add_to(m)
        
        # Paint Individual Storms
        for tier_key, is_active in item["active_layers"].items():
            if is_active:
                # Fetch the image and date from the nested dictionary!
                layer_b64 = res['individual_layers'][tier_key]['image_b64']
                storm_date = res['individual_layers'][tier_key]['date']
                
                folium.raster_layers.ImageOverlay(
                    image=f"data:image/png;base64,{layer_b64}",
                    bounds=bounds,
                    opacity=layer_opacity,
                    name=f"Storm {storm_date}"
                ).add_to(m)
        
        # Paint Master Map
        if item["show_master"]:
            folium.raster_layers.ImageOverlay(
                image=f"data:image/png;base64,{res['heatmap_b64']}",
                bounds=bounds,
                opacity=layer_opacity,
                name="Master Probability"
            ).add_to(m)

    # Render the map
    output = st_folium(m, width=1000, height=500, key="flood_master_map", returned_objects=["last_active_drawing"])

    # --- SILENT BACKGROUND DRAWING CATCHER ---
    # This replaces the clunky "Lock to Inventory" button!
    if output and isinstance(output, dict):
        if output.get("last_active_drawing"):
            st.session_state.current_drawing = output["last_active_drawing"]["geometry"]
    
    # --- STATS TRACKER TABLE ---
    if st.session_state.flood_inventory:
        st.subheader("📊 Flood Statistics Tracker")
        
        table_data = []
        for item in st.session_state.flood_inventory:
            area_id = f"Area {item['id']}"
            res = item["result"]
            
            # Grab BOTH correct stats!
            analyzed_area = res.get('master_stats', {}).get('analyzed_area_sqkm', 'N/A')
            total_risk = res.get('master_stats', {}).get('total_risk_sqkm', 'N/A')
            
            for tier_key, tier_data in res['individual_layers'].items():
                rp = tier_data.get('return_period', tier_data.get('return_period_years', 'N/A'))
                
                table_data.append({
                    "Area": area_id,
                    "Box Area (sq km)": analyzed_area,
                    "Compound Risk (sq km)": total_risk,
                    "Storm Tier": tier_key,
                    "Date": tier_data["date"],
                    "Rainfall (mm)": tier_data["rainfall_mm"],
                    "Tier Flooded (sq km)": tier_data["flooded_sqkm"],
                    "Return Period": f"1-in-{rp} yr" if rp != "N/A" else "N/A"
                })
                
        st.dataframe(table_data, use_container_width=True, hide_index=True)

render_flood_module()