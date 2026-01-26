from ib_insync import *
from datetime import datetime
import pandas as pd
import asyncio
import os
import logging

logger = logging.getLogger("IBService")

import random

class IBIntegration:
    def __init__(self):
        self.ib = IB()
        self.host = os.getenv("IB_HOST", "127.0.0.1")
        self.port = int(os.getenv("IB_PORT", "7497"))
        self.client_id = random.randint(2, 999) # Random ID to avoid conflicts
        self.ib.runTimeout = 30 # Increase run timeout
        self.ib.reqTimeout = 30 # Increase request timeout (default is 4s)
        
        # Market Data Type Configuration
        # 1 = Live (Real-time, requires subscription)
        # 2 = Frozen (Last price recorded at market close, requires subscription)
        # 3 = Delayed (15-20 min delayed, free)
        # 4 = Delayed Frozen (Last price recorded at market close, free)
        # Default to 3 (Delayed) for better updates than 4 (Frozen)
        self.market_data_type = int(os.getenv("IB_MARKET_DATA_TYPE", "3"))
        
        # Event Callbacks
        self.price_callbacks = []
        self.ib.pendingTickersEvent += self.on_pending_tickers
        self.ib.disconnectedEvent += self.on_disconnected
        self.client_id = random.randint(2, 999) 

    def on_order_status(self, trade):
        """Callback for real-time order updates from IBKR"""
        print(f"🔔 IBKR NOTIFICATION: Order {trade.order.orderId} Status: {trade.orderStatus.status} | Filled: {trade.orderStatus.filled}")

    def on_disconnected(self):
        print("⛔ IBKR DISCONNECTED!")

    async def force_disconnect(self):
        """Forcefully disconnect and reset state"""
        try:
            self.ib.disconnect()
            await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"Error disconnecting: {e}")

    async def connect(self):
        if self.ib.isConnected():
            return

        logger.info("Starting connection sequence...")
        await self.force_disconnect()

        # Generate a fresh Client ID for every connection attempt
        self.client_id = random.randint(2, 999)
        logger.info(f"Connecting to {self.host}:{self.port} with Client ID: {self.client_id}...")
        
        try:
            # MONKEYPATCH: ib_insync calls reqExecutionsAsync inside connectAsync.
            # If TWS is busy/blocking, this fails the entire connection.
            # We temporarily disable it to force a connection.
            real_reqExecutionsAsync = self.ib.reqExecutionsAsync
            
            async def noop(*args, **kwargs):
                logger.warning("Skipping internal execution request during connect")
                return []
                
            self.ib.reqExecutionsAsync = noop
            
            # Connect
            await self.ib.connectAsync(self.host, self.port, clientId=self.client_id)
            
            # Restore
            self.ib.reqExecutionsAsync = real_reqExecutionsAsync
            
            logger.info("✅ Connected to IBKR")
            
            # Request Executions (Manual & Safe)
            try:
                # Use a timeout for this specific request to avoid blocking
                await asyncio.wait_for(self.ib.reqExecutionsAsync(), timeout=5.0)
                logger.info("Requested execution history")
            except Exception as e:
                # Log but DO NOT fail the connection
                logger.warning(f"Could not request executions (TWS busy?): {e}")

        except Exception as e:
            import traceback
            logger.error(f"❌ Connection failed: {e}")
            logger.error(traceback.format_exc())
            await self.force_disconnect()
                
    @property
    def check_connection(self):
        return self.ib.isConnected()

    async def get_price(self, ticker_symbol):
        if not self.check_connection:
            return 0.0
        
        contract = Stock(ticker_symbol, 'SMART', 'USD')
        
        # Qualify to ensure we have the unique ConId
        try:
            await self.ib.qualifyContractsAsync(contract)
        except Exception as e:
            print(f"Contract qualification warning/error: {e}")
            return None # Invalid ticker

        if contract.conId == 0:
            print(f"Contract validation failed (conId=0): {ticker_symbol}")
            return None
        
        # Switch to configured Market Data Type
        self.ib.reqMarketDataType(self.market_data_type) 
        
        # reqMktData returns the ticker
        self.ib.reqMktData(contract, '', False, False)
        ticker = self.ib.ticker(contract)
        
        # Wait for data (up to 2 seconds)
        for _ in range(20):
            if ticker.last or ticker.close or ticker.bid or ticker.ask:
                break
            await asyncio.sleep(0.1)
            
        if ticker:
            # Fallback chain for weekend/closed market data
            # 1. Market Price (Midpoint/Last if live)
            price = ticker.marketPrice()
            
            # 2. Last Traded Price (if market closed/delayed)
            if (price != price or price == 0) and ticker.last:
                price = ticker.last
                
            # 3. Close Price (Previous day close)
            if (price != price or price == 0):
                price = ticker.close
                
            if price == price and price > 0:
                return price

        # 4. FINAL FALLBACK: Request Historical Data (Last 1 Day)
        # This is the most reliable way to get 'Friday Close' on a Saturday
        print(f"DEBUG: Streaming failed. Requesting Historical Data for {ticker_symbol}...")
        try:
            bars = await self.ib.reqHistoricalDataAsync(
                contract,
                endDateTime='',
                durationStr='1 D',
                barSizeSetting='1 day',
                whatToShow='TRADES',
                useRTH=True,
                formatDate=1
            )
            if bars:
                print(f"DEBUG: Historical Data Received: Close={bars[-1].close}")
                return bars[-1].close
        except Exception as e:
            print(f"DEBUG: Historical Data failed: {e}")
            raise e

    async def place_order(self, ticker_symbol, action, quantity, order_type="MARKET", limit_price=0.0, stop_loss_price=None):
        if not self.check_connection:
            raise Exception("IBKR not connected")

        contract = Stock(ticker_symbol, 'SMART', 'USD')
        
        # Parent Order
        if order_type.upper() == "LIMIT":
            parent = LimitOrder(action, quantity, limit_price)
        else:
            parent = MarketOrder(action, quantity)
        
        orders_to_place = [parent]

        # Stop Loss (Child)
        if stop_loss_price and stop_loss_price > 0:
            parent.transmit = False # Do not transmit until child is linked
            
            stop_action = "SELL" if action == "BUY" else "BUY"
            child = StopOrder(stop_action, quantity, stop_loss_price)
            child.parentId = parent.orderId
            child.transmit = True # Transmit the whole bracket
            orders_to_place.append(child)
        else:
            parent.transmit = True

        trades = []
        for o in orders_to_place:
             t = self.ib.placeOrder(contract, o)
             trades.append(t)
        
        # Ensure we are subscribed to market data so we can track price in Orders table
        # Check if already subscribed
        is_subscribed = False
        for t in self.ib.tickers():
            if t.contract.symbol == ticker_symbol:
                is_subscribed = True
                break
        
        if not is_subscribed:
            print(f"DEBUG: Auto-subscribing to {ticker_symbol} for order tracking")
            self.ib.reqMktData(contract, '', False, False)
        
        return trades[0] # Return parent trade

    async def download_historical_data(self, ticker_symbol, start_date, end_date, bar_size="1 day"):
        if not self.check_connection:
            raise Exception("IBKR not connected")
            
        contract = Stock(ticker_symbol, 'SMART', 'USD')
        await self.ib.qualifyContractsAsync(contract)
        
        # IBKR requires endDateTime in format 'YYYYMMDD HH:mm:ss'
        # We assume end of day for the end_date
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        # Ensure we cover the full end day by requesting up to 23:59:59
        end_str = end_dt.strftime("%Y%m%d 23:59:59")
        
        # Calculate duration
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        delta = end_dt - start_dt
        duration_days = delta.days + 1
        duration_str = f"{duration_days} D"
        
        print(f"DEBUG: Requesting History for {ticker_symbol}: End={end_str}, Duration={duration_str}, Bar={bar_size}")
        
        bars = await self.ib.reqHistoricalDataAsync(
            contract,
            endDateTime=end_str,
            durationStr=duration_str,
            barSizeSetting=bar_size,
            whatToShow='TRADES',
            useRTH=True,
            formatDate=1
        )
        
        if not bars:
            raise Exception("No data returned from IBKR")
            
        # Convert to DataFrame
        df = util.df(bars)
        
        # 🛡️ FILTER LOGIC: Ensure data strictly within requested range
        # Make sure 'date' column is datetime
        df['date'] = pd.to_datetime(df['date'])
        
        # ⚠️ FIX: Strip timezone if present to compare with naive start_dt/end_dt
        # dictating that we want "Exchange Wall Time" vs "User Wall Time"
        if hasattr(df['date'].dt, 'tz') and df['date'].dt.tz is not None:
             df['date'] = df['date'].dt.tz_localize(None)
        
        # Filter (Start at 00:00:00 of start_date, End at 23:59:59 of end_date)
        filter_end_dt = end_dt.replace(hour=23, minute=59, second=59)
        
        df = df[(df['date'] >= start_dt) & (df['date'] <= filter_end_dt)]
        
        # Select only requested columns
        df = df[['date', 'close', 'volume']]
        
        # Save to history folder
        safe_bar = bar_size.replace(" ", "")
        filename = f"history/{ticker_symbol}_{start_date}_{safe_bar}.csv"
        df.to_csv(filename, index=False)
        
        return filename

    def on_pending_tickers(self, tickers):
        """Event handler for real-time price updates"""
        for ticker in tickers:
            for callback in self.price_callbacks:
                try:
                    # Run callback safely
                    asyncio.create_task(callback(ticker))
                except Exception as e:
                    logger.error(f"Error in price callback: {e}")

    async def subscribe_market_data(self, ticker_symbol):
        if not self.check_connection:
            return
            
        contract = Stock(ticker_symbol, 'SMART', 'USD')
        await self.ib.qualifyContractsAsync(contract)
        
        if contract.conId == 0:
            raise ValueError(f"Invalid Ticker: {ticker_symbol}")
        
        self.ib.reqMarketDataType(self.market_data_type)
        self.ib.reqMktData(contract, '', False, False)
        logger.info(f"Subscribed to {ticker_symbol}")

    def cancel_market_data(self, ticker_symbol):
        # Find the ticker object
        for t in self.ib.tickers():
            if t.contract.symbol == ticker_symbol:
                self.ib.cancelMktData(t.contract)
                logger.info(f"Unsubscribed from {ticker_symbol}")
                return

    async def get_current_position(self, ticker_symbol):
        if not self.check_connection:
            return 0.0
            
        positions = self.ib.positions()
        for p in positions:
            if p.contract.symbol == ticker_symbol:
                return p.position
                
        return 0.0


    def register_callback(self, callback):
        self.price_callbacks.append(callback)

    # - [x] Fetch Active Stop Loss for Portfolio (Backend) <!-- id: 20 -->
    def get_portfolio(self):
        """Returns the current portfolio items with detailed P&L"""
        if not self.check_connection:
            return []
            
        portfolio_items = []
        for item in self.ib.portfolio():
            # Find the contract ticker to get Previous Close and Live Price
            ticker = None
            # Find existing ticker using conId
            for t in self.ib.tickers():
                if t.contract.conId == item.contract.conId:
                    ticker = t
                    break
            
            # If not found, subscribe (STREAMING)
            # We switched back from Snapshot Polling to Streaming because Snapshot spamming might be throttling 
            # or ineffective in sync loops.
            # CRITICAL FIX: Ensure exchange='SMART' is used for the subscription to avoid Error 321.
            if not ticker:
                 contract_for_sub = item.contract
                 contract_for_sub.exchange = 'SMART'
                 
                 logger.info(f"Auto-subscribing to STREAM for {contract_for_sub.symbol} (ID: {contract_for_sub.conId})")
                 
                 self.ib.reqMarketDataType(self.market_data_type)
                 # snapshot=False (Streaming)
                 ticker = self.ib.reqMktData(contract_for_sub, '', False, False)
            
            # Double check lookup (if we just subscribed, reqMktData returns the ticker)
            # If we had it, we have it.
            
            if not ticker:
                 # Fallback by symbol
                 for t in self.ib.tickers():
                    if t.contract.symbol == item.contract.symbol:
                        ticker = t
                        break
            
            # DETERMINE PRICE
            # Use Ticker's calculated market price (robust fallback) if available, otherwise item.marketPrice
            current_price = item.marketPrice
            
            if ticker:
                # Use our robust price logic (Tickers update faster than PortfolioItem sometimes)
                # 1. Market Price (Midpoint/Last if live)
                tp = ticker.marketPrice()
                
                # 2. Last Traded Price (if market closed/delayed)
                if (tp != tp or tp == 0) and ticker.last:
                    tp = ticker.last
                
                # 3. Close Price (Previous day close) 
                if (tp != tp or tp == 0):
                     tp = ticker.close
                
                if tp and tp > 0:
                    current_price = tp
            
            # Use "Live" or "Delayed" data instead of "Frozen" (4) which is static
            # 3 = Delayed (High Volume), 1 = Live
            # Set to 3 (Delayed) if currently 4 (Frozen) to encourage updates if market is open/simulated
            # But only call this once globally usually, but here we enforce it for these tickers.
            # self.ib.reqMarketDataType(3) 

            # Calculate Today's P&L
            # Today P&L = (Market Price - Previous Close) * Position
            today_pnl = 0.0
            today_pnl_pct = 0.0
            
            # We need a robust "Previous Close"
            prev_close = 0.0
            if ticker and ticker.close:
                 prev_close = ticker.close
            
            if prev_close > 0 and current_price > 0:
                 today_pnl = (current_price - prev_close) * item.position
                 today_pnl_pct = (current_price - prev_close) / prev_close * 100
            
            # Find Active Stop Loss for this position
            stop_loss_price = 0.0
            # Get all open orders for this contract
            # We need to find the one that is a Stop Sell (assuming Long)
            # This is a simplification; ideally we match by orderId chain, but matching by contract+action+type is decent
            for t in self.ib.openTrades(): # openTrades returns Trade objects with order info
                if t.contract.conId == item.contract.conId:
                    o = t.order
                    # Assuming Long Position -> Looking for Sell Stop
                    # Assuming Short Position -> Looking for Buy Stop
                    position_direction = 1 if item.position > 0 else -1
                    order_direction = -1 if o.action == 'SELL' else 1
                    
                    if position_direction != order_direction: # Opposite side
                        if o.orderType in ['STP', 'TRAIL', 'STP LMT']:
                             stop_loss_price = o.auxPrice

            # Find Last Fill Date
            last_fill_date = None
            # fills() are populated by reqExecutions() or real-time trades
            # Filter fills for this contract
            relevant_fills = [f for f in self.ib.fills() if f.contract.conId == item.contract.conId]
            if relevant_fills:
                # Sort by time desc
                relevant_fills.sort(key=lambda x: x.time, reverse=True)
                # Format time
                last_fill_date = relevant_fills[0].time

            # Recalculate Unrealized P&L based on new Current Price
            # IBKR's item.unrealizedPNL might be stale if item.marketPrice is stale
            unrealized_pnl = item.unrealizedPNL
            if current_price > 0 and item.averageCost > 0:
                 unrealized_pnl = (current_price - item.averageCost) * item.position

            portfolio_items.append({
                "ticker": item.contract.symbol,
                "quantity": item.position,
                "avg_cost": item.averageCost,
                "market_price": current_price,
                "market_value": current_price * item.position, # Recalc market value
                "unrealized_pnl": unrealized_pnl,
                "realized_pnl": item.realizedPNL,
                "today_pnl": today_pnl,
                "today_pnl_pct": today_pnl_pct,
                "stop_loss": stop_loss_price,
                "last_fill_date": last_fill_date, # New field
                "account": item.account
            })
        return portfolio_items

    def get_today_orders(self):
        """Returns all orders (active and executed) for the current session"""
        if not self.check_connection:
            return []
            
        orders_data = []
        # ib.trades() returns a list of Trade objects for the current session
        for trade in self.ib.trades():
            # Format for frontend
            # Determine "Price" to display (Filled Price vs Current Price)
            # Determine "Price" to display (Filled Price vs Current Price)
            display_price = 0.0
            status = trade.orderStatus.status
            
            # Helper to find active ticker by symbol AND auto-subscribe if missing
            def get_active_ticker(symbol):
                # 1. Try to find existing streamer
                for t in self.ib.tickers():
                    if t.contract.symbol == symbol:
                        return t
                
                # 2. If not found, subscribe (Auto-Recovery for manual/TWS orders)
                print(f"DEBUG: Auto-subscribing to {symbol} found in Orders")
                c = Stock(symbol, 'SMART', 'USD')
                self.ib.reqMktData(c, '', False, False)
                return None # Will be available next tick

            if status == 'Filled':
                 display_price = trade.orderStatus.avgFillPrice
                 # Fallback if avgFillPrice is 0 (paper trading quirk)
                 if display_price == 0.0:
                      t = get_active_ticker(trade.contract.symbol)
                      if t: display_price = t.marketPrice()
            elif status in ['Submitted', 'PreSubmitted', 'PendingSubmit', 'ApiPending']:
                 # Pending: Show live price
                 t = get_active_ticker(trade.contract.symbol)
                 if t: display_price = t.marketPrice()
            else:
                 # Cancelled, Inactive, etc -> 0.0
                 display_price = 0.0

            # Handle NaN prices (IBKR sometimes returns NaN)
            if display_price != display_price: 
                display_price = 0.0
            else:
                 # Cancelled, Inactive, etc -> 0.0
                 display_price = 0.0
                     
            orders_data.append({
                "id": trade.order.orderId,
                "time": trade.log[-1].time.strftime("%H:%M:%S") if trade.log else "-",
                "ticker": trade.contract.symbol,
                "action": trade.order.action,
                "total_qty": trade.order.totalQuantity,
                "filled_qty": trade.orderStatus.filled,
                "price": trade.order.lmtPrice if trade.order.orderType in ['LIMIT', 'LMT'] else 0.0,
                "stop_price": trade.order.auxPrice if trade.order.orderType in ['STP', 'STOP', 'TRAIL', 'STP LMT'] else 0.0,
                "avg_fill_price": trade.orderStatus.avgFillPrice,
                "current_or_filled_price": display_price,
                "status": status,
                "type": trade.order.orderType
            })
        
        # Sort by ID descending (newest first usually)
        orders_data.sort(key=lambda x: x['id'], reverse=True)
        return orders_data

    def get_account_summary(self):
        """Returns account summary metrics"""
        if not self.check_connection:
            return None
            
        # 1. Fetch Account Tags (NetLiquidation, TotalCashValue)
        # We use accountValues because it's simpler for default account
        summary = {
            "net_liquidation": 0.0,
            "total_cash": 0.0,
            "daily_pnl": 0.0,
            "daily_pnl_pct": 0.0
        }
        
        try:
             # This returns a list of AccountValue objects
             acc_vals = self.ib.accountValues()
             for mav in acc_vals:
                 if mav.tag == 'NetLiquidation':
                     try: summary['net_liquidation'] = float(mav.value)
                     except: pass
                 elif mav.tag == 'TotalCashValue': # or AvailableFunds
                     try: summary['total_cash'] = float(mav.value)
                     except: pass
        except Exception as e:
            logger.error(f"Error fetching account values: {e}")

        # 2. Calculate Daily P&L from Portfolio
        # Summing our own calculated daily P&L ensures consistency with the table
        try:
            portfolio = self.get_portfolio()
            total_daily_pnl = sum([item.get('today_pnl', 0.0) for item in portfolio])
            summary['daily_pnl'] = total_daily_pnl
            
            # Calculate % based on NetLiq (Current NetLiq includes today's P&L)
            # So Start Equity = NetLiq - DailyPnL
            if summary['net_liquidation'] != 0:
                start_equity = summary['net_liquidation'] - total_daily_pnl
                if start_equity != 0:
                    summary['daily_pnl_pct'] = (total_daily_pnl / start_equity) * 100
        except Exception as e:
            logger.error(f"Error calculating daily P&L sum: {e}")
            
        return summary

ib_service = IBIntegration()
