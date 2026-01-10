from ib_insync import *
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
        self.connected = False
        
        # Market Data Type Configuration
        # 1 = Live (Real-time, requires subscription)
        # 2 = Frozen (Last price recorded at market close, requires subscription)
        # 3 = Delayed (15-20 min delayed, free)
        # 4 = Delayed Frozen (Last price recorded at market close, free)
        self.market_data_type = int(os.getenv("IB_MARKET_DATA_TYPE", "4"))

    async def connect(self):
        if not self.ib.isConnected():
            try:
                # wait slightly before connecting to ensure gateway is up in docker
                await self.ib.connectAsync(self.host, self.port, clientId=self.client_id)
                self.connected = True
                logger.info("Connected to IBKR")
            except Exception as e:
                logger.error(f"Could not connect to IBKR: {e}")
                self.connected = False

    async def get_price(self, ticker_symbol):
        if not self.connected:
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
        if not self.connected:
            raise Exception("IBKR not connected")

        contract = Stock(ticker_symbol, 'SMART', 'USD')
        order = MarketOrder(action, quantity)
        
        trade = self.ib.placeOrder(contract, order)
        
        # Wait for fill? For Hello World, we just return the trade object
        # In prod we would await trade.filledEvent
        return trade

ib_service = IBIntegration()
