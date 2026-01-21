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
ticker = st.sidebar.text_input("Ticker Symbol", value="SPY").strip().upper()

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
            elif quote.get('status') == 'not_found':
                price_container.error(f"Unknown Ticker: {ticker}")
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
                s_ticker = st.text_input("Ticker", "SPY").strip().upper()
            with c2:
                s_entry = st.number_input("Entry Price ($)", min_value=0.0, step=0.01)
            with c3:
                s_sl = st.number_input("Stop Loss ($)", min_value=0.0, step=0.01, value=0.0)
            with c4:
                s_qty = st.number_input("Qty", min_value=1, value=10)
            
            if st.form_submit_button("Create Alert"):
                should_rerun = False
                try:
                    req_data = {
                        "ticker": s_ticker, 
                        "entry_price": s_entry, 
                        "stop_loss": s_sl if s_sl > 0 else None, 
                        "quantity": s_qty,
                    }
                    res = requests.post(f"{ST_BACKEND_URL}/strategies", json=req_data)
                    if res.status_code == 200:
                        st.success(f"Alert set for {s_ticker} > ${s_entry}")
                        should_rerun = True
                    else:
                        st.error(f"Error: {res.text}")
                except Exception as e:
                    st.error(f"Req Error: {e}")
                
                if should_rerun:
                    st.rerun()

    st.markdown("---")

    # --- 2. Monitored Alerts (Strategies) ---
    st.subheader("📡 Monitored")

    @st.fragment(run_every=5)
    def render_monitored_strategies():
        strategies = []
        error_msg = None
        
        # 1. Fetch Data
        try:
            strat_res = requests.get(f"{ST_BACKEND_URL}/strategies")
            if strat_res.status_code == 200:
                strategies = strat_res.json()
            else:
                error_msg = f"Error fetching strategies: {strat_res.text}"
        except Exception as e:
             error_msg = f"Could not load strategies: {e}"
        
        # 2. Render Error if any
        if error_msg:
             st.error(error_msg)
             return

        # 3. Render UI
        active_strats = [s for s in strategies if s.get('status') == 'active']
        
        if active_strats:
            # Display as a table with "Delete" buttons
            # Using columns for layout
            st.markdown(f"**active: {len(active_strats)}**")
            
            header_cols = st.columns([1, 0.5, 1.5, 1, 1, 1, 1, 0.5]) 
            header_cols[0].markdown("**Ticker**")
            header_cols[1].markdown("**Live**")
            header_cols[2].markdown("**Price**")
            header_cols[3].markdown("**Last Update**")
            header_cols[4].markdown("**Entry**")
            header_cols[5].markdown("**Stop Loss**")
            header_cols[6].markdown("**Qty**")
            header_cols[7].markdown("**Action**")
            
            for s in active_strats:
                cols = st.columns([1, 0.5, 1.5, 1, 1, 1, 1, 0.5])
                cols[0].text(s['ticker'])
                
                # Live Toggle
                is_live = s.get('is_live', True)
                new_live = cols[1].checkbox(" ", value=is_live, key=f"live_{s['id']}", label_visibility="collapsed")
                if new_live != is_live:
                    requests.patch(f"{ST_BACKEND_URL}/strategies/{s['id']}", json={"is_live": new_live})
                    st.rerun()

                # Display Price (Read-Only in Table)
                c_price = s.get('current_price', 0.0) or 0.0
                cols[2].text(f"${c_price:.2f}")

                # Display Last Update
                l_updated = s.get('last_updated')
                cols[3].text(f"{l_updated}" if l_updated else "-")
                
                cols[4].text(f"${s['entry_price']}")
                cols[5].text(f"${s['stop_loss']}" if s['stop_loss'] else "-")
                cols[6].text(s['quantity'])
                
                if cols[7].button("❌", key=f"del_{s['id']}"):
                    requests.delete(f"{ST_BACKEND_URL}/strategies/{s['id']}")
                    st.rerun()
            
            # --- Manual Price Injection Section ---
            manual_strats = [s for s in active_strats if not s.get('is_live', True)]
            if manual_strats:
                st.markdown("### 🛠️ Manual Price Injection")
                with st.form("manual_price_form"):
                    c1, c2, c3 = st.columns([2, 2, 1])
                    
                    # Create dictionary for selectbox {label: id}
                    # We use Ticker for label, but update by ID
                    strat_options = {s['ticker']: s['id'] for s in manual_strats}
                    
                    with c1:
                        target_ticker = st.selectbox("Select Strategy", options=list(strat_options.keys()))
                    
                    selected_id = strat_options.get(target_ticker)
                    
                    # Try to pre-fill current price if possible (Streamlit forms make dynamic defaults hard, default to 0.0)
                    with c2:
                         new_manual_price = st.number_input("New Price ($)", min_value=0.0, step=0.01)
                    
                    with c3:
                        st.markdown("<br>", unsafe_allow_html=True) # Spacer for alignment
                        if st.form_submit_button("Update Price"):
                            if selected_id:
                                requests.patch(f"{ST_BACKEND_URL}/strategies/{selected_id}", json={"current_price": new_manual_price})
                                st.rerun()
        else:
            st.info("No active alerts.")


    render_monitored_strategies()

    st.markdown("---")


# --- Live Loop (Manual Refresh fallback) ---
if st.sidebar.button("Refresh Price"):
    st.rerun()
