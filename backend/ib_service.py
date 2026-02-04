from ib_insync import *
from datetime import datetime
import pandas as pd
import asyncio
import os
import logging

logger = logging.getLogger("IBService")

import random

# Global instance of IBIntegration
_ib_service_instance = None

def get_ib_service():
    """
    Returns a singleton instance of the IBIntegration class.
    """
    global _ib_service_instance
    if _ib_service_instance is None:
        _ib_service_instance = IBIntegration()
    return _ib_service_instance

class IBIntegration:
    def __init__(self):
        self.ib = IB()
        self.host = os.getenv("IB_HOST", "127.0.0.1")
        self.port = int(os.getenv("IB_PORT", "7497"))
        self.client_id = random.randint(2, 999) # Random ID to avoid conflicts
        self.ib.runTimeout = 30 # Increase run timeout
        self.ib.reqTimeout = 30 # Increase request timeout (default is 4s)
        
        # 1 = Live (Real-time, requires subscription)
        # 2 = Frozen (Last price recorded at market close, requires subscription)
        # 3 = Delayed (15-20 min delayed, free)
        # 4 = Delayed Frozen (Last price recorded at market close, free)
        # Default to 2 (Frozen) to match user preference for Static Close
        self.market_data_type = int(os.getenv("IB_MARKET_DATA_TYPE", "2"))
        
        # Event Callbacks
        self.price_callbacks = []
        self.ib.pendingTickersEvent += self.on_pending_tickers
        self.ib.disconnectedEvent += self.on_disconnected
        self.ib.errorEvent += self.on_error
        self.client_id = random.randint(2, 999) 
        self.last_heartbeat = datetime.now() # Initialize to now so startup doesn't fail immediately 

    def on_error(self, reqId, errorCode, errorString, contract):
        """Handle IBKR API errors"""
        # 1100=Connectivity lost, 10197=Competing Session, 2110=Connectivity broken
        if errorCode in [1100, 10197, 2110]:
            print(f"🚨 CRITICAL IBKR ERROR {errorCode}: {errorString}")
            # Force connection check to fail by invalidating heartbeat
            self.last_heartbeat = datetime(2000, 1, 1)

            # Manually disconnect to trigger clean reconnection loop
            asyncio.create_task(self.force_disconnect()) 
        self.last_heartbeat = datetime.now() # Initialize to now so startup doesn't fail immediately 

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
            # Connect
            await self.ib.connectAsync(self.host, self.port, clientId=self.client_id)
            
            logger.info("✅ Connected to IBKR")
            
            # Request All Open Orders (Fixes visibility of TWS orders)
            # Add a small delay and try/except to avoid flooding/pacing violations on startup
            try:
                await asyncio.sleep(1) 
                logger.info("Requesting All Open Orders...")
                self.ib.reqAllOpenOrders()
            except Exception as e:
                logger.warning(f"Could not request open orders: {e}")
            
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
        # Strict verification: Socket connected AND Heartbeat recent (< 15s)
        is_socket_connected = self.ib.isConnected()
        
        # If never validated, rely on socket (initial startup)
        if not hasattr(self, 'last_heartbeat'):
             return is_socket_connected

        # If validated recently, return True
        time_since_heartbeat = (datetime.now() - self.last_heartbeat).total_seconds()
        return is_socket_connected and time_since_heartbeat < 15

    async def validate_connection(self):
        """Actively checks connection health by requesting current time"""
        if not self.ib.isConnected():
            return False
            
        try:
            # 2 second timeout for heartbeat
            await asyncio.wait_for(self.ib.reqCurrentTimeAsync(), timeout=2.0)
            self.last_heartbeat = datetime.now() # Update timestamp
            return True
        except Exception as e:
            logger.warning(f"Heartbeat failed: {e}")
            return False

    async def robust_qualify_contract(self, ticker_symbol):
        """Attempts to qualify a stock contract with multiple fallback strategies"""
        # 1. Try SMART (Preferred)
        c = Stock(ticker_symbol, 'SMART', 'USD')
        try:
            await self.ib.qualifyContractsAsync(c)
            if c.conId != 0: return c
        except: pass
        
        # 2. Try Empty Exchange (Let IBKR decide)
        c = Stock(ticker_symbol, '', 'USD')
        try:
            await self.ib.qualifyContractsAsync(c)
            if c.conId != 0: return c
        except: pass
        
        # 3. Try ISLAND (NASDAQ) - Common fallback for Tech
        c = Stock(ticker_symbol, 'ISLAND', 'USD')
        try:
            await self.ib.qualifyContractsAsync(c)
            if c.conId != 0: return c
        except: pass
        
        # 4. Search via ContractDetails (Exhaustive)
        print(f"DEBUG: robust_qualify_contract failed basic qualification for {ticker_symbol}. Searching details...")
        try:
            proto = Stock(ticker_symbol, '', 'USD')
            details = await self.ib.reqContractDetailsAsync(proto)
            if details:
                # Pick the first one that matches
                c = details[0].contract
                await self.ib.qualifyContractsAsync(c)
                if c.conId != 0: return c
        except Exception as e:
            print(f"DEBUG: reqContractDetails failed for {ticker_symbol}: {e}")
            
        return c # Return the failed contract (conId=0)

    async def get_price(self, ticker_symbol):
        if not self.check_connection:
            return 0.0
        
        contract = await self.robust_qualify_contract(ticker_symbol)
        
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

        contract = await self.robust_qualify_contract(ticker_symbol)
        if contract.conId == 0:
            raise ValueError(f"Invalid Ticker: {ticker_symbol}")
        
        # Parent Order
        if order_type.upper() == "LIMIT":
            parent = LimitOrder(action, quantity, limit_price)
        else:
            parent = MarketOrder(action, quantity)
        
        # Ensure we have an Order ID for the parent to link the child
        parent.orderId = self.ib.client.getReqId()
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

    def cancel_order(self, order_id):
        """Cancels an active order by ID"""
        if not self.check_connection:
            raise Exception("IBKR not connected")
            
        # Look in open orders first (most likely)
        target_order = None
        for trade in self.ib.openTrades():
            if trade.order.orderId == int(order_id):
                target_order = trade.order
                break
        
        # If not found in open trades, check all orders (edge case where it might be in valid list but not active?)
        if not target_order:
             for order in self.ib.orders():
                 if order.orderId == int(order_id):
                     target_order = order
                     break
        
        if target_order:
            self.ib.cancelOrder(target_order)
            logger.info(f"Requested cancellation for Order {order_id}")
            return True
        else:
            raise ValueError(f"Order {order_id} not found or already filled/cancelled")

    async def download_historical_data(self, ticker_symbol, start_date, end_date, bar_size="1 day"):
        if not self.check_connection:
            raise Exception("IBKR not connected")
            
        contract = await self.robust_qualify_contract(ticker_symbol)
        if contract.conId == 0:
             raise ValueError(f"Invalid Ticker: {ticker_symbol}")
        
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
            
        contract = await self.robust_qualify_contract(ticker_symbol)
        
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
                # Use our robust price logic 
                # CHANGE: Prioritize LAST price (matches TradingView/Brokers) over Midpoint (marketPrice)
                
                # 1. Last Traded Price (Primary)
                if ticker.last and ticker.last > 0 and ticker.last == ticker.last: # Check Valid and not NaN
                     current_price = ticker.last
                
                # 2. Market Price (Midpoint fallback if Last is missing)
                elif ticker.marketPrice() and ticker.marketPrice() > 0:
                     current_price = ticker.marketPrice()
                
                # 3. Close Price (Previous day close fallback) 
                elif ticker.close and ticker.close > 0:
                     current_price = ticker.close
            
            # Use "Live" or "Delayed" data instead of "Frozen" (4) which is static
            # 3 = Delayed (High Volume), 1 = Live
            # Set to 3 (Delayed) if currently 4 (Frozen) to encourage updates if market is open/simulated
            # But only call this once globally usually, but here we enforce it for these tickers.
            # self.ib.reqMarketDataType(3) 

            # Find Active Stop Loss (Move Up)
            stop_loss_price = 0.0
            for t in self.ib.openTrades(): 
                if t.contract.conId == item.contract.conId:
                    o = t.order
                    position_direction = 1 if item.position > 0 else -1
                    order_direction = -1 if o.action == 'SELL' else 1
                    if position_direction != order_direction and o.orderType in ['STP', 'TRAIL', 'STP LMT']:
                             stop_loss_price = o.auxPrice
            
            # Find Last Fill Date (Move Up)
            last_fill_date = None
            relevant_fills = [f for f in self.ib.fills() if f.contract.conId == item.contract.conId]
            if relevant_fills:
                relevant_fills.sort(key=lambda x: x.time, reverse=True)
                last_fill_date = relevant_fills[0].time

            # Calculate Today's P&L
            # Logic: If bought TODAY, Today's P/L = (Price - AvgCost) [Same as Unrealized]
            #        If bought BEFORE, Today's P/L = (Price - PrevClose)
            today_pnl = 0.0
            today_pnl_pct = 0.0
            
            # Determine Baseline Price
            baseline_price = 0.0
            
            is_new_position = False
            if last_fill_date:
                # Compare fill date with today's date
                # relevant_fills[0].time is typically a datetime object (localized?)
                # We need to be careful with timezones, but date() comparison usually works if both are reasonably aligned.
                try:
                    fill_date = last_fill_date.date()
                    today_date = datetime.now().date()
                    if fill_date == today_date:
                        is_new_position = True
                except:
                    pass

            if is_new_position:
                # If new, baseline is the cost of the position
                baseline_price = item.averageCost
            else:
                # If old, baseline is yesterday's close
                if ticker and ticker.close:
                    baseline_price = ticker.close
            
            # Calculate Personal Today's P/L $
            if baseline_price > 0 and current_price > 0:
                 today_pnl = (current_price - baseline_price) * item.position
                 
            # Calculate Market Daily Change % (User requested this to be independent of order)
            # Always (Current - PrevClose) / PrevClose
            # We need a robust "Previous Close" separately from baseline logic
            prev_close_for_pct = 0.0
            if ticker and ticker.close:
                 prev_close_for_pct = ticker.close
            
            if prev_close_for_pct > 0 and current_price > 0:
                today_pnl_pct = (current_price - prev_close_for_pct) / prev_close_for_pct * 100
             
            # Recalculate Unrealized P&L based on new Current Price
            # IBKR's item.unrealizedPNL might be stale if item.marketPrice is stale
            unrealized_pnl = item.unrealizedPNL
            if current_price > 0 and item.averageCost > 0:
                 unrealized_pnl = (current_price - item.averageCost) * item.position
            
            # DEBUG DATA DISCREPANCY
            t_last = ticker.last if ticker else 'N/A'
            t_close = ticker.close if ticker else 'N/A' 
            t_bid = ticker.bid if ticker else 'N/A'
            t_ask = ticker.ask if ticker else 'N/A'
            t_mp = ticker.marketPrice() if ticker else 'N/A'
            print(f"DEBUG PRICE: {item.contract.symbol} | Used={current_price} | CalcMP={t_mp} | Last={t_last} | Close={t_close} | Bid={t_bid} | Ask={t_ask}")

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
            # Handle NaN prices (IBKR sometimes returns NaN)
            if display_price != display_price: 
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
                "type": trade.order.orderType,
                "parent_id": trade.order.parentId
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

_service_instance = None
def get_ib_service():
    global _service_instance
    if _service_instance is None:
        _service_instance = IBIntegration()
    return _service_instance
