import streamlit as st
import requests
import pandas as pd
import time
import os
import plotly.graph_objects as go

# Configuration
ST_BACKEND_URL = os.getenv("BACKEND_URL", "http://backend:8000")

st.set_page_config(page_title="Trader Bot", layout="wide", page_icon="📈")

# --- Sidebar ---
st.sidebar.title("🤖 Trader Bot")

# Status Check
try:
    health = requests.get(f"{ST_BACKEND_URL}/health", timeout=2).json()
    status_color = "green" if health['status'] == 'ok' else "red"
    ib_status = "Connected" if health.get('ib_connected') else "Disconnected"
    ib_color = "green" if health.get('ib_connected') else "orange"
    
    st.sidebar.markdown(f"**Backend:** :{status_color}[Online]")
    st.sidebar.markdown(f"**IBKR:** :{ib_color}[{ib_status}]")
    
    trading_mode = health.get('mode', 'live')
    if trading_mode == 'simulation':
        st.sidebar.warning("⚠️ SIMULATION MODE")
except:
    st.sidebar.markdown("**Backend:** :red[Offline]")
    ib_status = "Unknown"

st.sidebar.markdown("---")
ticker = st.sidebar.text_input("Ticker Symbol", value="SPY").upper()

# --- Main Content ---
tab1, tab2, tab3 = st.tabs(["Trading", "Historical Data", "Strategy Agent"])

with tab1:
    col1, col2 = st.columns([2, 1])
    
    with col1:
        st.subheader(f"Market Data: {ticker}")
        
        # Placeholder for live price
        price_container = st.empty()
        chart_container = st.empty()
        
        # Auto-refresh mechanism setup (simple loop for demo)
        if 'history' not in st.session_state:
            st.session_state.history = []
    
    with col2:
        st.subheader("Trade Execution")
        
        quantity = st.number_input("Quantity", min_value=1, value=1)
        
        if st.button("BUY MARKET", type="primary", use_container_width=True):
            if ib_status != "Connected":
                st.error("Cannot trade: IBKR Disconnected")
            else:
                try:
                    payload = {"ticker": ticker, "action": "BUY", "quantity": quantity}
                    res = requests.post(f"{ST_BACKEND_URL}/order", json=payload)
                    if res.status_code == 200:
                        order_info = res.json()
                        st.success(f"Order Submitted! ID: {order_info['ib_id']}")
                    else:
                        st.error(f"Failed: {res.text}")
                except Exception as e:
                    st.error(f"Error: {e}")
    
        st.markdown("---")
        st.subheader("Recent Activity")
        try:
            trades_res = requests.get(f"{ST_BACKEND_URL}/trades")
            if trades_res.status_code == 200:
                trades = trades_res.json()
                if trades:
                    df = pd.DataFrame(trades)
                    st.dataframe(df[['timestamp', 'ticker', 'action', 'quantity', 'ib_order_id']], hide_index=True)
                else:
                    st.info("No trades recorded yet.")
        except:
            st.warning("Could not fetch trades")
            
    # Fetch current price once per render (for Tab 1)
    try:
        response = requests.get(f"{ST_BACKEND_URL}/quote/{ticker}")
        if response.status_code == 200:
            quote = response.json()
            price_val = quote.get('price', 0.0)
            
            if quote.get('status') == 'connected':
                with price_container:
                    st.metric(label=f"{ticker} Price", value=f"${price_val:.2f}")
            else:
                price_container.warning("IBKR Disconnected - Waiting for reconnect...")
        else:
            price_container.error(f"Backend Error ({response.status_code}): {response.text}")
            
    except Exception as e:
        price_container.error(f"Connection Error: {e}")

with tab2:
    st.subheader("Download Historical Data")
    
    h_col1, h_col2, h_col3 = st.columns(3)
    with h_col1:
        start_date = st.date_input("Start Date", value=pd.to_datetime("today") - pd.Timedelta(days=7), key="h_start")
    with h_col2:
        end_date = st.date_input("End Date", value=pd.to_datetime("today"), key="h_end")
    with h_col3:
        bar_size = st.selectbox("Bar Size", ["1 min", "5 mins", "1 hour", "1 day"], index=0, key="h_bar")
        
    st.caption(f"Requesting: {start_date} to {end_date} ({bar_size})")
    
    if st.button("Download Data", type="primary"):
        if ib_status != "Connected":
            st.error("Cannot download: IBKR Disconnected")
        else:
            try:
                payload = {
                    "ticker": ticker,
                    "start_date": str(start_date),
                    "end_date": str(end_date),
                    "bar_size": bar_size
                }
                with st.spinner(f"Downloading {bar_size} data from IBKR..."):
                    res = requests.post(f"{ST_BACKEND_URL}/history/download", json=payload)
                    
                if res.status_code == 200:
                    data = res.json()
                    st.success(f"Success! Saved to: {data['file']}")
                else:
                    st.error(f"Failed: {res.text}")
            except Exception as e:
                st.error(f"Error: {e}")

with tab3:
    st.subheader("Breakout Strategy Manager")
    
    # --- 1. Create Strategy Form ---
    with st.expander("➕ Add New Strategy", expanded=True):
        with st.form("strategy_form"):
            c1, c2, c3, c4 = st.columns(4)
            with c1:
                s_ticker = st.text_input("Ticker", "SPY").upper()
            with c2:
                s_entry = st.number_input("Entry Price ($)", min_value=0.0, step=0.01)
            with c3:
                s_sl = st.number_input("Stop Loss ($)", min_value=0.0, step=0.01, value=0.0)
            with c4:
                s_qty = st.number_input("Qty", min_value=1, value=10)
            
            s_sim_date = None
            if health.get('mode') == 'simulation':
                s_sim_date = st.date_input("Simulation Date", value=pd.to_datetime("2026-01-09"))
                
            if st.form_submit_button("Create Alert"):
                try:
                    req_data = {
                        "ticker": s_ticker, 
                        "entry_price": s_entry, 
                        "stop_loss": s_sl if s_sl > 0 else None, 
                        "quantity": s_qty,
                        "simulation_date": str(s_sim_date) if s_sim_date else None
                    }
                    res = requests.post(f"{ST_BACKEND_URL}/strategies", json=req_data)
                    if res.status_code == 200:
                        st.success(f"Alert set for {s_ticker} > ${s_entry}")
                        st.rerun()
                    else:
                        st.error(f"Error: {res.text}")
                except Exception as e:
                    st.error(f"Req Error: {e}")

    st.markdown("---")

    # --- 2. Monitored Alerts (Strategies) ---
    st.subheader("📡 Monitored Alerts (Waiting for Breakout)")
    try:
        strat_res = requests.get(f"{ST_BACKEND_URL}/strategies")
        if strat_res.status_code == 200:
            strategies = strat_res.json()
            active_strats = [s for s in strategies if s.get('status') == 'active']
            
            if active_strats:
                # Display as a table with "Delete" buttons
                # Using columns for layout
                st.markdown(f"**active: {len(active_strats)}**")
                
                header_cols = st.columns([1, 2, 2, 2, 1])
                header_cols[0].markdown("**Ticker**")
                header_cols[1].markdown("**Entry**")
                header_cols[2].markdown("**Stop Loss**")
                header_cols[3].markdown("**Qty**")
                header_cols[4].markdown("**Action**")
                
                for s in active_strats:
                    cols = st.columns([1, 2, 2, 2, 1])
                    cols[0].text(s['ticker'])
                    cols[1].text(f"${s['entry_price']}")
                    cols[2].text(f"${s['stop_loss']}" if s['stop_loss'] else "-")
                    cols[3].text(s['quantity'])
                    
                    if cols[4].button("❌", key=f"del_{s['id']}"):
                        requests.delete(f"{ST_BACKEND_URL}/strategies/{s['id']}")
                        st.rerun()
            else:
                st.info("No active alerts.")
    except Exception as e:
        st.error(f"Could not load strategies: {e}")

    st.markdown("---")

    # --- 3. Active Positions (Purchased) ---
    st.subheader("💼 Active Positions (Purchased)")
    if st.button("Refresh Positions"):
        st.rerun()
        
    try:
        pos_res = requests.get(f"{ST_BACKEND_URL}/positions")
        if pos_res.status_code == 200:
            positions = pos_res.json()
            if positions:
                # Table Header
                p_cols = st.columns([1, 2, 2, 2])
                p_cols[0].markdown("**Ticker**")
                p_cols[1].markdown("**Quantity**")
                p_cols[2].markdown("**Avg Cost**")
                p_cols[3].markdown("**Action**") # Close
                
                for p in positions:
                    r_cols = st.columns([1, 2, 2, 2])
                    r_cols[0].text(p['ticker'])
                    r_cols[1].text(p['quantity'])
                    r_cols[2].text(f"${p['avg_cost']:.2f}")
                    
                    if r_cols[3].button("💰 Close Position", key=f"close_{p['ticker']}"):
                        with st.spinner(f"Closing {p['ticker']}..."):
                            c_res = requests.post(f"{ST_BACKEND_URL}/positions/close", params={"ticker": p['ticker']})
                            if c_res.status_code == 200:
                                st.success(f"Closed {p['ticker']}!")
                                st.rerun()
                            else:
                                st.error(f"Failed: {c_res.text}")
            else:
                st.info("No open positions.")
    except Exception as e:
        st.error(f"Could not load positions: {e}")
            
# --- Live Loop (Manual Refresh fallback) ---
if st.sidebar.button("Refresh Price"):
    st.rerun()
