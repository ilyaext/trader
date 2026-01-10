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

    def get_price(self, ticker_symbol):
        if not self.connected:
            return 0.0
        
        contract = Stock(ticker_symbol, 'SMART', 'USD')
        # qualify contracts for better accuracy
        # self.ib.qualifyContracts(contract) 
        
        # In a real app we might request market data type
        self.ib.reqMktData(contract, '', False, False)
        
        # Simply return the latest price (delayed or realtime)
        # ib_insync updates tickers automatically in background loop
        ticker = self.ib.ticker(contract)
        if ticker:
            return ticker.marketPrice() or ticker.close
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
