import asyncio
import pandas as pd
import glob
import os
import logging
from datetime import datetime

logger = logging.getLogger("MockIBService")

class MockContract:
    def __init__(self, symbol):
        self.symbol = symbol

class MockTicker:
    def __init__(self, symbol, price):
        self.contract = MockContract(symbol)
        self.last = price
        self.close = price
        self.bid = price
        self.ask = price
    
    def marketPrice(self):
        return self.last

class MockOrder:
    def __init__(self):
        self.orderId = 0
        self.permId = 0

class MockTrade:
    def __init__(self, order):
        self.order = order
        self.orderStatus = "Filled"

class MockPosition:
    def __init__(self, ticker, quantity, avg_cost=0.0):
        self.contract = MockContract(ticker)
        self.position = quantity
        self.avgCost = avg_cost

class MockIB:
    def __init__(self, parent_service):
        self.parent = parent_service
    
    def isConnected(self):
        return self.parent.connected
        
    def disconnect(self):
        self.parent.connected = False
        
    def positions(self):
        # Convert dict to list of MockPositions
        pos_list = []
        for ticker, data in self.parent.positions.items():
            qty = data['quantity']
            cost = data['avg_cost']
            if qty != 0:
                pos_list.append(MockPosition(ticker, qty, cost))
        return pos_list

class MockIBService:
    def __init__(self):
        self.connected = True
        self.ib = MockIB(self) # ⚠️ FIX: Add nested mock IB object
        self.price_callbacks = []
        self.positions = {} # {ticker: quantity}
        self.last_prices = {} # {ticker: price}
        self.active_tasks = {} # {ticker: asyncio.Task}

    async def connect(self):
        logger.info("MOCK: Connected (Simulated)")
        return True

    @property
    def check_connection(self):
        return True

    async def get_price(self, ticker_symbol):
        return self.last_prices.get(ticker_symbol, 0.0)

    async def place_order(self, ticker_symbol, action, quantity):
        logger.info(f"MOCK ORDER: {action} {quantity} {ticker_symbol}")
        
        # Get execution price (current simulated price)
        exec_price = self.last_prices.get(ticker_symbol, 0.0)
        
        # Update mock position
        # Structure: {ticker: {'quantity': float, 'avg_cost': float, 'timestamp': datetime}}
        current_data = self.positions.get(ticker_symbol, {'quantity': 0, 'avg_cost': 0.0, 'timestamp': None})
        current_qty = current_data['quantity']
        
        if action == "BUY":
            # Calculate new average cost
            total_cost = (current_qty * current_data['avg_cost']) + (quantity * exec_price)
            new_qty = current_qty + quantity
            new_avg_cost = total_cost / new_qty if new_qty > 0 else 0.0
            
            self.positions[ticker_symbol] = {
                'quantity': new_qty,
                'avg_cost': new_avg_cost,
                'timestamp': datetime.now().isoformat() if current_qty == 0 else current_data['timestamp']
            }
            
        elif action == "SELL":
            new_qty = current_qty - quantity
            if new_qty <= 0:
                if ticker_symbol in self.positions:
                    del self.positions[ticker_symbol]
            else:
                current_data['quantity'] = new_qty
                # Avg cost doesn't change on Sell (FIFO/Weighted Avg rule usually)
        
        # Return fake trade object
        order = MockOrder()
        order.orderId = hash(datetime.now()) % 100000
        return MockTrade(order)

    async def subscribe_market_data(self, ticker_symbol, simulation_date=None):
        logger.info(f"MOCK SUBSCRIBE: {ticker_symbol} Date={simulation_date}")
        
        if not simulation_date:
            logger.warning("MOCK: No simulation date provided for subscription")
            return

        # Find CSV file
        # Pattern: history/{ticker_symbol}*{simulation_date}*.csv
        # The stored format is usually history/SPY_2026-01-02_2026-01-04_1min.csv
        # We need to find a file that CONTAINS the simulation date in its range? 
        # Or just user provides the file's start/end?
        # User said: "Bot should find the relevant file"
        # We'll search for any file containing the ticker and try to load it.
        # Then filter for the simulation date.
        
        search_pattern = f"history/{ticker_symbol}*.csv"
        files = glob.glob(search_pattern)
        
        if not files:
            logger.error(f"MOCK: No history file found for {ticker_symbol}")
            return
            
        # Pick the most recent file or best match?
        # For now, pick the first one and check if date is inside
        target_file = files[0] # Simplification
        logger.info(f"MOCK: Playing {target_file}")
        
        try:
            df = pd.read_csv(target_file)
            # Ensure date column exists
            if 'date' not in df.columns:
                 logger.error("MOCK: CSV missing 'date' column")
                 return
                 
            # Filter for simulation date
            # We assume 'simulation_date' string format "YYYY-MM-DD" matches or is subset
            df['date'] = pd.to_datetime(df['date'])
            target_dt = pd.to_datetime(simulation_date)
            
            # Filter for the specific day
            day_df = df[df['date'].dt.date == target_dt.date()]
            
            if day_df.empty:
                logger.warning(f"MOCK: No data for {simulation_date} in file")
                return
            
            # Sort by date
            day_df = day_df.sort_values('date')
            
            # Start Replay Task
            if ticker_symbol in self.active_tasks:
                self.active_tasks[ticker_symbol].cancel()
                
            self.active_tasks[ticker_symbol] = asyncio.create_task(
                self.replay_loop(ticker_symbol, day_df)
            )
            
        except Exception as e:
            logger.error(f"MOCK Error loading file: {e}")

    async def replay_loop(self, ticker, df):
        logger.info(f"MOCK: Starting replay for {ticker} ({len(df)} rows)")
        
        for _, row in df.iterrows():
            price = row['close']
            self.last_prices[ticker] = price
            
            # Create Mock Ticker
            mt = MockTicker(ticker, price)
            
            # Emit Event
            for cb in self.price_callbacks:
                if asyncio.iscoroutinefunction(cb):
                    asyncio.create_task(cb(mt))
                else:
                    cb(mt)
            
            # Simulate time delay (speed up?)
            # 100ms per candle looks cool
            await asyncio.sleep(0.1) 
            
        logger.info(f"MOCK: Replay finished for {ticker}")

    def cancel_market_data(self, ticker_symbol):
        if ticker_symbol in self.active_tasks:
            self.active_tasks[ticker_symbol].cancel()
            del self.active_tasks[ticker_symbol]
        logger.info(f"MOCK: Unsubscribed {ticker_symbol}")

    def register_callback(self, callback):
        self.price_callbacks.append(callback)
        
    async def get_current_position(self, ticker_symbol):
        pos_data = self.positions.get(ticker_symbol, 0.0)
        if isinstance(pos_data, dict):
            return pos_data.get('quantity', 0.0)
        return pos_data
        
    async def download_historical_data(self, *args, **kwargs):
        return "MOCK_NO_DOWNLOAD"

mock_ib_service = MockIBService()
