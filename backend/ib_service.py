from ib_insync import *
from datetime import datetime
import pandas as pd
import asyncio
import os
import logging

logger = logging.getLogger("IBService")

class IBIntegration:
    def __init__(self):
        self.ib = IB()
        self.host = os.getenv("IB_HOST", "127.0.0.1")
        self.port = int(os.getenv("IB_PORT", "7497"))
        self.client_id = 1
        
        # Market Data Type Configuration
        # 1 = Live (Real-time, requires subscription)
        # 2 = Frozen (Last price recorded at market close, requires subscription)
        # 3 = Delayed (15-20 min delayed, free)
        # 4 = Delayed Frozen (Last price recorded at market close, free)
        self.market_data_type = int(os.getenv("IB_MARKET_DATA_TYPE", "4"))
        
        # Event Callbacks
        self.price_callbacks = []
        self.ib.pendingTickersEvent += self.on_pending_tickers
        self.ib.orderStatusEvent += self.on_order_status

    def on_order_status(self, trade):
        """Callback for real-time order updates from IBKR"""
        print(f"🔔 IBKR NOTIFICATION: Order {trade.order.orderId} Status: {trade.orderStatus.status} | Filled: {trade.orderStatus.filled}")

    async def connect(self):
        if not self.ib.isConnected():
            try:
                # wait slightly before connecting to ensure gateway is up in docker
                await self.ib.connectAsync(self.host, self.port, clientId=self.client_id)
                logger.info("Connected to IBKR")
            except Exception as e:
                logger.error(f"Could not connect to IBKR: {e}")
                
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

    async def place_order(self, ticker_symbol, action, quantity, order_type="MARKET", limit_price=0.0):
        if not self.check_connection:
            raise Exception("IBKR not connected")

        contract = Stock(ticker_symbol, 'SMART', 'USD')
        
        if order_type.upper() == "LIMIT":
            order = LimitOrder(action, quantity, limit_price)
        else:
            order = MarketOrder(action, quantity)
        
        trade = self.ib.placeOrder(contract, order)
        
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
        
        # Wait for fill? For Hello World, we just return the trade object
        # In prod we would await trade.filledEvent
        return trade

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
                "avg_fill_price": trade.orderStatus.avgFillPrice,
                "current_or_filled_price": display_price,
                "status": status,
                "type": trade.order.orderType
            })
        
        # Sort by ID descending (newest first usually)
        orders_data.sort(key=lambda x: x['id'], reverse=True)
        return orders_data

ib_service = IBIntegration()
