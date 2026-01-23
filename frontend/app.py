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
    @st.fragment(run_every=2)
    def render_orders():
         st.subheader("Orders")
         try:
              orders_res = requests.get(f"{ST_BACKEND_URL}/orders")
              if orders_res.status_code == 200:
                  orders = orders_res.json()
                  if orders:
                      df_orders = pd.DataFrame(orders)
                      # Rename columns for display
                      df_orders.rename(columns={'price': 'Limit', 'id': 'ID', 'time': 'Time', 'ticker': 'Ticker', 'action': 'Action', 'total_qty': 'Qty', 'filled_qty': 'Filled', 'status': 'Status', 'type': 'Type'}, inplace=True)
                      st.dataframe(
                          df_orders[['ID', 'Time', 'Ticker', 'Action', 'Qty', 'Filled', 'Limit', 'Status', 'Type']], 
                          hide_index=True,
                          use_container_width=True
                      )
                  else:
                      st.info("No active/executed orders this session.")
              else:
                  st.error(f"Error fetching orders: {orders_res.text}")
         except Exception as e:
              st.error(f"Connection Error: {e}")

    render_orders()

    st.markdown("---")

    st.subheader("Recent Activity (DB)")
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
                s_entry = st.number_input("Entry Alert ($)", min_value=0.0, step=0.01)
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
            
            # Cols: Ticker, Manual, Price, Daily %, Entry Alert, Update, Stop Loss, Qty, Action
            header_cols = st.columns([1, 0.5, 1.2, 1.2, 1.2, 1, 1, 1, 0.5], vertical_alignment="center") 
            header_cols[0].markdown("**Ticker**")
            header_cols[1].markdown("**Manual**")
            header_cols[2].markdown("**Price**")
            header_cols[3].markdown("**Daily %**")
            header_cols[4].markdown("**Entry Alert**")
            header_cols[5].markdown("**Last Update**")
            header_cols[6].markdown("**Stop Loss**")
            header_cols[7].markdown("**Qty**")
            header_cols[8].markdown("**Action**")
            
            for s in active_strats:
                cols = st.columns([1, 0.5, 1.2, 1.2, 1.2, 1, 1, 1, 0.5], vertical_alignment="center")
                cols[0].text(s['ticker'])
                
                # Manual Toggle (Inverted Live)
                is_live = s.get('is_live', True)
                is_manual = not is_live
                new_manual = cols[1].checkbox(" ", value=is_manual, key=f"manual_{s['id']}", label_visibility="collapsed")
                
                if new_manual != is_manual:
                    # If Manual Checked (True) -> Live = False
                    # Update Backend
                    requests.patch(f"{ST_BACKEND_URL}/strategies/{s['id']}", json={"is_live": not new_manual})
                    # Update local state so UI reflects change immediately without rerun
                    s['is_live'] = not new_manual

                # Display Price (Read-Only in Table)
                c_price = s.get('current_price', 0.0) or 0.0
                cols[2].text(f"${c_price:.2f}")

                # Calculate Daily % (Backend Provided)
                daily_pct = s.get('daily_change_pct')
                if daily_pct is not None:
                    color = "green" if daily_pct >= 0 else "red"
                    cols[3].markdown(f":{color}[{daily_pct:+.2f}%]")
                else:
                    cols[3].text("-")

                # Entry Alert
                cols[4].text(f"${s['entry_price']}")

                # Display Last Update
                l_updated = s.get('last_updated')
                cols[5].text(f"{l_updated}" if l_updated else "-")
                
                cols[6].text(f"${s['stop_loss']}" if s['stop_loss'] else "-")
                cols[7].text(s['quantity'])
                
                if cols[8].button("❌", key=f"del_{s['id']}"):
                    requests.delete(f"{ST_BACKEND_URL}/strategies/{s['id']}")
                    # Locally mark as deleted (status changed) so manual_strats filter works if needed
                    s['status'] = 'deleted'
                    st.rerun()
            
            # --- Manual Price Injection Section ---
            form_placeholder = st.empty()
            manual_strats = [s for s in active_strats if not s.get('is_live', True)]
            
            if manual_strats:
                with form_placeholder.container():
                     st.markdown("### 🛠️ Manual Price Injection")
                     # Use a static key to avoid lifecycle issues, relying on the container to redraw
                     with st.form(key="manual_price_update_form"):
                        c1, c2, c3 = st.columns([2, 2, 1], vertical_alignment="bottom") # Align bottom for button
                        
                        strat_options = {s['ticker']: s['id'] for s in manual_strats}
                        
                        with c1:
                            target_ticker = st.selectbox("Select Strategy", options=list(strat_options.keys()))
                        
                        selected_id = strat_options.get(target_ticker)
                        
                        with c2:
                             new_manual_price = st.number_input("New Price ($)", min_value=0.0, step=0.01)
                        
                        with c3:
                            if st.form_submit_button("Update Price"):
                                if selected_id:
                                    requests.patch(f"{ST_BACKEND_URL}/strategies/{selected_id}", json={"current_price": new_manual_price})
                                    st.rerun()
            else:
                form_placeholder.empty()
        else:
            st.info("No active alerts.")



    render_monitored_strategies()

    st.markdown("---")


# --- Live Loop (Manual Refresh fallback) ---
if st.sidebar.button("Refresh Price"):
    st.rerun()
