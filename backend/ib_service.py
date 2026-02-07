import nest_asyncio
import asyncio
import os
import logging
from datetime import datetime
import pandas as pd
from ib_insync import *

# Fix for "event loop already running" (essential for FastAPI + ib_insync)
nest_asyncio.apply()

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
        self.market_data_type = int(os.getenv("IB_MARKET_DATA_TYPE", "2"))
        
        self.ib.runTimeout = 30
        self.ib.reqTimeout = 30
        
        # Event Callbacks
        self.price_callbacks = []
        self.order_callbacks = [] 
        self.position_callbacks = []
        
        self.ib.pendingTickersEvent += self.on_pending_tickers
        self.ib.disconnectedEvent += self.on_disconnected
        self.ib.errorEvent += self.on_error
        self.ib.orderStatusEvent += self.on_order_status
        self.ib.positionEvent += self.on_position_update
        self.ib.updatePortfolioEvent += self.on_update_portfolio
        
        self.last_heartbeat = datetime.now() 

    def on_error(self, reqId, errorCode, errorString, contract):
        """Handle IBKR API errors"""
        # Diagnostic print for ALL errors
        print(f"DEBUG: [on_error] reqId={reqId}, code={errorCode}, msg={errorString}", flush=True)

        # 1100=Connectivity lost, 10197=Competing Session, 2110=Connectivity broken, 504=Not connected
        if errorCode in [1100, 10197, 2110, 504]:
            print(f"🚨 [on_error] CRITICAL ERROR {errorCode}. Force disconnecting.", flush=True)
            # Ensure we are considered disconnected
            asyncio.create_task(self.force_disconnect())

    def on_order_status(self, trade):
        """Callback for real-time order updates from IBKR"""
        # Notify subscribers (WebSockets)
        for cb in self.order_callbacks:
            asyncio.create_task(cb(trade))

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
            # Check if it's actually alive
            if (datetime.now() - getattr(self, 'last_heartbeat', datetime(2000,1,1))).total_seconds() < 15:
                return

        logger.info("Starting connection sequence...")
        await self.force_disconnect()
        
        # Wait a bit to ensure TWS cleans up previous session
        await asyncio.sleep(2)

        # Generate a fresh Client ID for every connection attempt
        self.client_id = random.randint(2, 999)
        logger.info(f"Connecting to {self.host}:{self.port} with Client ID: {self.client_id}...")
        
        try:
            # Connect
            await asyncio.wait_for(self.ib.connectAsync(self.host, self.port, clientId=self.client_id), timeout=10.0)
            
            self.last_heartbeat = datetime.now() # Reset heartbeat on success
            logger.info("✅ Connected to IBKR")
            
            # Request All Open Orders (Fixes visibility of TWS orders)
            # Add a small delay and try/except to avoid flooding/pacing violations on startup
            try:
                await asyncio.sleep(1) 
                logger.info("Requesting All Open Orders...")
                await self.ib.reqAllOpenOrdersAsync()
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

            # Explicitly request positions and account updates to ensure they stream
            try:
                self.ib.reqPositions()
                self.ib.reqAccountUpdates(True)
                logger.info("Requested explicit position and account updates")
            except Exception as e:
                logger.warning(f"Could not request positions/account updates: {e}")

        except Exception as e:
            import traceback
            logger.error(f"❌ Connection failed: {e}")
            logger.error(traceback.format_exc())
            await self.force_disconnect()
                
    @property
    def check_connection(self):
        """Returns simplified connection status for UI/Health"""
        return self.ib.isConnected()

    async def validate_connection(self):
        """Actively checks connection health by requesting a lightweight tag"""
        if not self.ib.isConnected():
            return False
            
        try:
            # 2 second timeout for a simple network-roundtrip request
            # We use reqCurrentTimeAsync as it's the lightest possible request
            await asyncio.wait_for(self.ib.reqCurrentTimeAsync(), timeout=2.0)
            return True
        except Exception as e:
            logger.warning(f"Heartbeat validation failed: {e}")
            return False

    async def sync_open_orders(self):
        """Asynchronously pull all open orders (including TWS ones)"""
        if self.ib.isConnected():
             print("🔄 Syncing Open Orders with IBKR...", flush=True)
             try:
                 # Use Async variant to prevent 'loop already running' errors
                 await self.ib.reqAllOpenOrdersAsync()
             except Exception as e:
                 print(f"Failed to sync orders: {e}")

    def register_order_callback(self, cb):
        self.order_callbacks.append(cb)

    def register_position_callback(self, cb):
        self.position_callbacks.append(cb)

    def on_position_update(self, pos):
        """Event handler for real-time position updates from IBKR"""
        for cb in self.position_callbacks:
            asyncio.create_task(cb(pos.account, pos.contract, pos.position, pos.avgCost))

    def on_update_portfolio(self, item):
        """Event handler for portfolio updates (alternative to positionEvent)"""
        # Map item to position update format
        for cb in self.position_callbacks:
            asyncio.create_task(cb(item.account, item.contract, item.position, item.averageCost))

    async def robust_qualify_contract(self, ticker_symbol):
        """Attempts to qualify a stock contract with multiple fallback strategies"""
        if ticker_symbol.upper() == "TEST":
            c = Stock('TEST', 'SMART', 'USD')
            c.conId = 9999999
            return c

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
        if ticker_symbol.upper() == "TEST":
            return 100.0 # Mock price for TEST
            
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

        # Intercept TEST ticker for simulated management
        is_test = ticker_symbol.upper() == "TEST"
        
        contract = None
        if is_test:
            # Create a mock contract for TEST
            contract = Stock('TEST', 'SMART', 'USD')
            contract.conId = 9999999 # Fake ID
        else:
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
        logger.info(f"DEBUG: [place_order] Parent ID: {parent.orderId} for {ticker_symbol}")

        # Stop Loss (Child)
        if stop_loss_price and stop_loss_price > 0:
            parent.transmit = False # Do not transmit until child is linked
            
            stop_action = "SELL" if action == "BUY" else "BUY"
            # Enable outsideRth=True to ensure the stop order is accepted during pre/post market
            child = StopOrder(stop_action, quantity, stop_loss_price, outsideRth=True)
            child.parentId = parent.orderId
            child.transmit = True # Transmit the whole bracket
            orders_to_place.append(child)
            logger.info(f"DEBUG: [place_order] Added StopLoss Child (Parent: {parent.orderId}) at ${stop_loss_price}")
        else:
            parent.transmit = True

        trades = []
        for o in orders_to_place:
             logger.info(f"DEBUG: [place_order] Dispatching -> {o.action} {o.orderType} | ID: {o.orderId} | Parent: {o.parentId} | Transmit: {o.transmit}")
             if is_test:
                 # Manually create a Trade object and put it in memory
                 status = OrderStatus(status='Submitted', filled=0, remaining=o.totalQuantity)
                 t = Trade(contract, o, status, [], [])
                 self.ib.wrapper.trades[o.orderId] = t
             else:
                 t = self.ib.placeOrder(contract, o)
             trades.append(t)
        
        # Ensure we are subscribed to market data so we can track price in Orders table
        if not is_test:
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
        try:
            print(f"DEBUG: [cancel_order] Request for ID {order_id}", flush=True)
            if not self.check_connection:
                raise Exception("IBKR not connected")
                
            order_id = int(order_id)
            
            # Check if it's a TEST order by looking it up in memory
            target_trade = None
            for trade in self.ib.trades():
                if trade.order.orderId == order_id:
                    target_trade = trade
                    break
            
            if target_trade:
                print(f"DEBUG: [cancel_order] Found trade for {target_trade.contract.symbol}", flush=True)
                if target_trade.contract.symbol == "TEST":
                    logger.info(f"Purging TEST ghost order {order_id}")
                    self.force_delete_order(order_id)
                    return True

            # Normal cancellation flow
            target_order = None
            for trade in self.ib.openTrades():
                if trade.order.orderId == order_id:
                    target_order = trade.order
                    break
            
            if not target_order:
                 for order in self.ib.orders():
                     if order.orderId == order_id:
                         target_order = order
                         break
            
            if target_order:
                self.ib.cancelOrder(target_order)
                logger.info(f"Requested cancellation for Order {order_id}")
                return True
            else:
                raise ValueError(f"Order {order_id} not found or already filled/cancelled")
        except Exception as e:
            print(f"ERROR: [cancel_order] failed for {order_id}: {e}", flush=True)
            logger.error(f"Cancellation error: {e}", exc_info=True)
            raise e

    def force_delete_order(self, order_id):
        """Forcefully removes an order and its children from all local in-memory caches (Ghost Orders)"""
        # Handle both int IDs and (clientId, orderId) tuples used by ib_insync wrapper
        raw_key = order_id
        try:
            if isinstance(order_id, (tuple, list)) and len(order_id) >= 2:
                display_id = int(order_id[1])
            else:
                display_id = int(order_id)
        except (TypeError, ValueError, IndexError):
            display_id = 0
            
        # 1. Check if it's already gone to break recursion
        if raw_key not in self.ib.wrapper.trades:
            return False
            
        # 2. Remove the parent FIRST to break cycles
        del self.ib.wrapper.trades[raw_key]
        logger.info(f"Force deleted Ghost Order {display_id} from wrapper.trades (Key: {raw_key})")

        # 3. Recursive cleanup for child orders (e.g. Stop Loss)
        child_keys = []
        try:
            # We use list(dict.items()) to safely iterate while items might be removed in recursion
            for tid, t in list(self.ib.wrapper.trades.items()):
                p_id = getattr(t.order, 'parentId', 0)
                # Compare against the integer ID
                if p_id == display_id and tid != raw_key:
                    child_keys.append(tid)
        except Exception as e:
            logger.warning(f"Error searching for child orders of {display_id}: {e}")

        for ck in child_keys:
            logger.info(f"Cascading force-delete to child order {ck} (Parent: {display_id})")
            self.force_delete_order(ck)

        return True

    async def download_historical_data(self, ticker_symbol, start_date, end_date, bar_size="1 day"):
        if ticker_symbol.upper() == "TEST":
             raise ValueError("Historical data not available for TEST ticker")

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
        if ticker_symbol.upper() == "TEST":
            return

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

    async def close_position(self, ticker_symbol):
        """Closes an entire position at Market price and cleans up open orders"""
        is_test = ticker_symbol.upper() == "TEST"
        
        if not is_test and not self.check_connection:
            raise Exception("IBKR not connected")
            
        # 1. Cancel all open orders for this ticker (Cleanup)
        for trade in self.ib.openTrades():
            if trade.contract.symbol == ticker_symbol:
                print(f"🧹 [Cleanup] Cancelling open order for {ticker_symbol}: {trade.order.orderType} {trade.order.action}", flush=True)
                if is_test:
                    self.force_delete_order(trade.order.orderId)
                else:
                    self.ib.cancelOrder(trade.order)
                # Small sleep to allow TWS to process the cancellation
                await asyncio.sleep(0.5)

        # 2. Find the position to close
        positions = self.ib.positions()
        target_pos = next((p for p in positions if p.contract.symbol == ticker_symbol), None)
        
        if not target_pos or target_pos.position == 0:
            raise ValueError(f"No active position for {ticker_symbol}")
            
        action = "SELL" if target_pos.position > 0 else "BUY"
        quantity = abs(target_pos.position)
        
        print(f"🔄 CLOSING POSITION: {ticker_symbol} ({quantity} shares)", flush=True)
        if is_test:
            # Mock the closing of a TEST position
            logger.info(f"Mock closing TEST position: {quantity} shares")
            # We don't have a direct way to remove a position from self.ib.positions() 
            # as it's managed by the wrapper/API, but we can at least avoid the error.
            return None
            
        contract = target_pos.contract
        order = MarketOrder(action, quantity)
        trade = self.ib.placeOrder(contract, order)
        return trade


    def register_callback(self, callback):
        self.price_callbacks.append(callback)

    # - [x] Fetch Active Stop Loss for Portfolio (Backend) <!-- id: 20 -->
    async def get_portfolio(self):
        """Returns the current portfolio items with detailed P&L (Asynchronous)"""
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
            # BYPASS: Do not request market data for TEST ticker
            if not ticker and item.contract.symbol != "TEST":
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
            
            if item.contract.symbol == "TEST":
                current_price = 100.0
            elif ticker:
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

    async def get_today_orders(self):
        """Returns all orders (active and executed) for the current session (Asynchronous)"""
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
                if symbol == "TEST":
                    return None
                    
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

    async def get_account_summary(self):
        """Returns account summary metrics (Asynchronous)"""
        if not self.check_connection:
            return {
                "net_liquidation": 0.0,
                "total_cash": 0.0,
                "daily_pnl": 0.0,
                "daily_pnl_pct": 0.0
            }
            
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
            portfolio = await self.get_portfolio()
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
