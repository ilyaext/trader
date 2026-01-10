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

# --- Live Loop (Manual Refresh fallback) ---
# In Streamlit, a real infinite loop blocks the UI. 
# We use st.empty() to update, but rely on rerun or simple periodic check.
# For this 'Hello World', we'll just fetch once on load. 
# To make it 'live', we can use st.rerun() with sleep, but that's aggressive.
# Better to user a manual refresh or a specialized component. 
# For simplicity:
if st.sidebar.button("Refresh Price"):
    st.rerun()

# Fetch current price once per render
try:
    response = requests.get(f"{ST_BACKEND_URL}/quote/{ticker}")
    if response.status_code == 200:
        quote = response.json()
        price_val = quote.get('price', 0.0)
        
        with price_container:
            st.metric(label=f"{ticker} Price", value=f"${price_val:.2f}")
    else:
        price_container.error(f"Backend Error ({response.status_code}): {response.text}")
        
except Exception as e:
    price_container.error(f"Connection Error: {e}")
