from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel
from contextlib import asynccontextmanager
from database import init_db, get_db, Trade
from sqlalchemy.orm import Session
from ib_service import ib_service as live_ib_service
# from mock_ib_service import mock_ib_service
import asyncio
import os
import uuid
from datetime import datetime
from models import Strategy, StrategyRequest

# CONFIGURATION
# CONFIGURATION
# TRADING_MODE = os.getenv("TRADING_MODE", "live")
# print(f"🚀 STARTING IN LIVE MODE")

ib_service = live_ib_service

# In-memory storage for strategies (for now)
strategies = []
strategy_lock = None # Will be initialized in lifespan

async def on_price_update(ticker):
    """Callback triggered by ib_service when price updates"""
    if strategy_lock is None: 
        return
        
    async with strategy_lock:
        # Filter active strategies for this ticker
        active_strategies = [s for s in strategies if s.ticker == ticker.contract.symbol and s.status == "active"]
        
        if not active_strategies:
            return

        # Get current price
        price = 0.0
        if ticker.last and not float(ticker.last) != float(ticker.last): # Check NaN
             price = ticker.last
        elif ticker.marketPrice():
             price = ticker.marketPrice()
        
        if price <= 0:
            return

        # DEBUG: Trace ticks
        # print(f"DEBUG: Tick {ticker.contract.symbol} @ {price} | Active Strategies: {len(active_strategies)}")

        for strategy in active_strategies:
            print(f"DEBUG: {strategy.ticker} Price={price} Entry={strategy.entry_price} LastSeen={strategy.last_seen_price}")
            # Check Breakout Condition (2-Candle Confirmation)
            # Rule: Two sequential candles must be above Entry.
            #       Current Close > Previous Close > Entry Price
            
            # Default: Reset sequence if price drops below entry
            if price <= strategy.entry_price:
                if strategy.last_seen_price is not None:
                     print(f"📉 Reset Sequence: {strategy.ticker} {price} <= {strategy.entry_price}")
                strategy.last_seen_price = None
                continue
            
            # Price > Entry. Check sequence.
            if strategy.last_seen_price is not None:
                # We have a previous candle above entry.
                # Check if current is higher than previous (momentum confirmation)
                if price > strategy.last_seen_price:
                    print(f"🚀 BREAKOUT TRIGGERED: {strategy.ticker} @ {price} > {strategy.last_seen_price} > {strategy.entry_price}")
                    
                    try:
                        # 1. Place Buy Order
                        trade = await ib_service.place_order(strategy.ticker, "BUY", strategy.quantity)
                        
                        # 2. (Optional) Place Stop Loss
                        # if strategy.stop_loss:
                        #    ... implement bracket order later ...
                        
                        # 3. Mark Executed
                        strategy.status = "executed"
                        
                        # 4. Cleanup subscription if no other strategies for this ticker
                        # (Simplified: just keep subscribed for now to see P&L)
                        
                    except Exception as e:
                        print(f"❌ Failed to execute strategy {strategy.id}: {e}")
                else:
                    # Price is above entry, but not higher than previous.
                    # Update tracking to this candle? 
                    # User said: "second is closed larger then previous"
                    # If C2 <= C1, the sequence C1->C2 fails.
                    # Does C2 become the new "first" candle? Yes, it's > Entry.
                    print(f"DEBUG: Sequence Stalled: {price} <= {strategy.last_seen_price} (New Base)")
                    strategy.last_seen_price = price
            else:
                # First candle above entry
                print(f"DEBUG: Potential Breakout Start: {price} > {strategy.entry_price}")
                strategy.last_seen_price = price

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    global strategy_lock
    strategy_lock = asyncio.Lock()
    
    init_db()
    
    # Register Protocol Callback
    ib_service.register_callback(on_price_update)
    
    # Start background connection loop
    task = asyncio.create_task(check_connection_loop())
    
    yield
    
    # Shutdown
    task.cancel()
    if ib_service.ib.isConnected():
        ib_service.ib.disconnect()

async def check_connection_loop():
    while True:
        if not ib_service.check_connection:
            print("Detected API Disconnect. Attempting to reconnect...")
            await ib_service.connect()
            
            # Re-subscribe to active strategies
            async with strategy_lock:
                for s in strategies:
                    if s.status == "active":
                        await ib_service.subscribe_market_data(s.ticker)
                        
        await asyncio.sleep(5) # Check every 5 seconds

app = FastAPI(title="Trader Bot API", lifespan=lifespan)

# --- Strategy Endpoints ---

@app.post("/strategies")
async def create_strategy(req: StrategyRequest):
    # Sanitize ticker
    # Sanitize ticker
    clean_ticker = req.ticker.strip().upper()
    
    async with strategy_lock:
        # Check for duplicates
        for s in strategies:
            if s.ticker == clean_ticker and s.status == "active":
                raise HTTPException(status_code=400, detail=f"Active strategy already exists for {clean_ticker}")

    id = str(uuid.uuid4())
    strategy = Strategy(
        id=id,
        ticker=clean_ticker,
        entry_price=req.entry_price,
        stop_loss=req.stop_loss,
        quantity=req.quantity,
        created_at=datetime.now().isoformat()
    )
    
    async with strategy_lock:
        strategies.append(strategy)
    
    # Subscribe to market data
    try:
        await ib_service.subscribe_market_data(clean_ticker)
    except Exception as e:
        # Rollback: Remove strategy if subscription failed
        async with strategy_lock:
            strategies.remove(strategy)
        print(f"❌ Strategy creation failed: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    
    return strategy

@app.get("/strategies")
async def list_strategies():
    return strategies

@app.delete("/strategies/{strategy_id}")
async def delete_strategy(strategy_id: str):
    async with strategy_lock:
        for i, s in enumerate(strategies):
            if s.id == strategy_id:
                # Unsubscribe if it was active
                if s.status == "active":
                    ib_service.cancel_market_data(s.ticker)
                
                del strategies[i]
                return {"status": "deleted", "id": strategy_id}
    
    raise HTTPException(status_code=404, detail="Strategy not found")

@app.post("/positions/close")
async def close_position(ticker: str):
    if not ib_service.check_connection:
        raise HTTPException(status_code=503, detail="IBKR Disconnected")

    try:
        # Get current position size
        pos = await ib_service.get_current_position(ticker)
        if pos == 0:
             return {"status": "no_position", "message": "No position to close"}
        
        # Determine action
        action = "SELL" if pos > 0 else "BUY"
        quantity = abs(pos)
        
        # Place Market Order to close
        trade = await ib_service.place_order(ticker, action, quantity)
        
        # Update any local strategy status if needed? 
        # For now, just return success
        return {
            "status": "submitted", 
            "ib_id": trade.order.orderId, 
            "description": f"Closing position: {action} {quantity} {ticker}"
        }
    except Exception as e:
         raise HTTPException(status_code=500, detail=str(e))

@app.get("/positions")
async def list_positions():
    if not ib_service.check_connection:
        return []
        
    positions_data = []
    ib_positions = ib_service.ib.positions()
    
    for p in ib_positions:
        if p.position != 0:
            ticker = p.contract.symbol
            qty = p.position
            avg_cost = p.avgCost
            
            # Fetch current market price for P&L
            # In Simulation Mode, use get_portfolio_price() for End-of-Day view
            # Fetch current market price for P&L
            current_price = await ib_service.get_price(ticker)
            
            # Calculate P&L
            # Calculate P&L
            # Unrealized P&L = (Current Price - Avg Cost) * Quantity
            pnl = (current_price - avg_cost) * qty
            
            # P&L %
            # (Current Price - Avg Cost) / Avg Cost
            pnl_pct = 0.0
            if avg_cost > 0:
                pnl_pct = (current_price - avg_cost) / avg_cost * 100
            
            # Timestamp 
            purchased_at = None
            # TODO: Fetch real execution time if needed
            
            positions_data.append({
                "ticker": ticker,
                "quantity": qty,
                "avg_cost": avg_cost,
                "current_price": current_price,
                "pnl": pnl,
                "pnl_percent": pnl_pct,
                "purchased_at": purchased_at
            })
            
    return positions_data 

class OrderRequest(BaseModel):
    ticker: str
    action: str = "BUY"
    quantity: int = 1


@app.get("/quote/{ticker}")
async def get_quote(ticker: str):
    if not ib_service.check_connection:
        return {"ticker": ticker, "price": 0.0, "status": "disconnected"}
    
    try:
        price = await ib_service.get_price(ticker)
        
        if price is None:
             return {"ticker": ticker, "price": 0.0, "status": "not_found", "error": "Invalid Ticker"}

        # Handle NaN values explicitly using 0.0 or valid float
        if price != price: # Check for NaN
            price = 0.0 
        return {"ticker": ticker, "price": price, "status": "connected"}
    except Exception as e:
        # Return error as JSON instead of crashing
        print(f"Error fetching quote for {ticker}: {e}")
        return {"ticker": ticker, "price": 0.0, "status": "error", "error": str(e)}

@app.post("/order")
async def place_order(order: OrderRequest, db: Session = Depends(get_db)):
    if not ib_service.check_connection:
        raise HTTPException(status_code=503, detail="IBKR Disconnected")
    
    try:
        # Place order on IBKR
        ib_trade = await ib_service.place_order(order.ticker, order.action, order.quantity)
        
        # Record to DB (Optimistic recording for Hello World)
        db_trade = Trade(
            ticker=order.ticker, 
            action=order.action, 
            quantity=order.quantity, 
            price=0.0, # Filled price unknown yet
            ib_order_id=ib_trade.order.orderId
        )
        db.add(db_trade)
        db.commit()
        db.refresh(db_trade)
        
        return {"status": "submitted", "order_id": db_trade.id, "ib_id": ib_trade.order.orderId}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/trades")
async def get_trades(db: Session = Depends(get_db)):
    trades = db.query(Trade).order_by(Trade.timestamp.desc()).limit(10).all()
    return trades

class HistoryRequest(BaseModel):
    ticker: str
    start_date: str
    end_date: str
    bar_size: str = "1 day"

@app.post("/history/download")
async def download_history(req: HistoryRequest):
    if not ib_service.check_connection:
        raise HTTPException(status_code=503, detail="IBKR Disconnected")
    
    try:
        filename = await ib_service.download_historical_data(req.ticker, req.start_date, req.end_date, req.bar_size)
        return {"status": "ok", "file": filename}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
async def health():
    return {"status": "ok", "ib_connected": ib_service.check_connection}
