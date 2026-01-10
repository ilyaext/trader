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
            print(f"Contract qualification warning: {e}")
        
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

        return 0.0

    async def place_order(self, ticker_symbol, action, quantity):
        if not self.check_connection:
            raise Exception("IBKR not connected")

        contract = Stock(ticker_symbol, 'SMART', 'USD')
        order = MarketOrder(action, quantity)
        
        trade = self.ib.placeOrder(contract, order)
        
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
        filename = f"history/{ticker_symbol}_{start_date}_{end_date}_{safe_bar}.csv"
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

ib_service = IBIntegration()
