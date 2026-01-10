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
except:
    st.sidebar.markdown("**Backend:** :red[Offline]")
    ib_status = "Unknown"

st.sidebar.markdown("---")
ticker = st.sidebar.text_input("Ticker Symbol", value="SPY").upper()

# --- Main Content ---
tab1, tab2 = st.tabs(["Trading", "Historical Data"])

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
    
    h_col1, h_col2 = st.columns(2)
    with h_col1:
        start_date = st.date_input("Start Date", value=pd.to_datetime("today") - pd.Timedelta(days=7))
    with h_col2:
        end_date = st.date_input("End Date", value=pd.to_datetime("today"))
        
    if st.button("Download Data", type="primary"):
        if ib_status != "Connected":
            st.error("Cannot download: IBKR Disconnected")
        else:
            try:
                payload = {
                    "ticker": ticker,
                    "start_date": str(start_date),
                    "end_date": str(end_date)
                }
                with st.spinner("Downloading data from IBKR..."):
                    res = requests.post(f"{ST_BACKEND_URL}/history/download", json=payload)
                    
                if res.status_code == 200:
                    data = res.json()
                    st.success(f"Success! Saved to: {data['file']}")
                else:
                    st.error(f"Failed: {res.text}")
            except Exception as e:
                st.error(f"Error: {e}")
            
# --- Live Loop (Manual Refresh fallback) ---
if st.sidebar.button("Refresh Price"):
    st.rerun()
