import nest_asyncio
import asyncio
from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from contextlib import asynccontextmanager
from database import init_db, get_db, Trade
from sqlalchemy.orm import Session
from ib_service import get_ib_service as live_get_ib_service
import os
import uuid
import json
import random
import logging
from datetime import datetime
from models import Strategy, StrategyRequest, StrategyUpdate
from collections import deque

# Apply nest_asyncio globally at the absolute start
nest_asyncio.apply()

# CONFIGURATION
BACKEND_URL = os.getenv("BACKEND_URL", "http://backend:8000")
TARGET_INVESTMENT = float(os.getenv("TARGET_INVESTMENT", "3000.0"))
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "3.0"))

# Global context
strategies = []
strategy_lock = asyncio.Lock()
strategy_logs = deque(maxlen=50) # Maintain last 50 entries

# WebSocket Manager
class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_text(json.dumps(message))
            except Exception as e:
                print(f"⚠️ [WS ERROR] Failed to send: {e}", flush=True)
                pass

manager = ConnectionManager()
ib_service = live_get_ib_service()

def log_strategy_event(msg: str):
    """Log an event to the global buffer and broadcast via WS"""
    timestamp = datetime.now().strftime("%H:%M:%S")
    formatted = f"[{timestamp}] {msg}"
    strategy_logs.appendleft(formatted)
    print(f"STRATEGY LOG: {msg}")
    asyncio.create_task(manager.broadcast({"type": "log", "data": formatted}))

async def check_strategies(symbol, price):
    async with strategy_lock:
        for s in strategies:
            if s.ticker == symbol and s.status == "active":
                # Check Breakout Condition (2-Candle/Tick Confirmation)
                # Rule: Two sequential updates must be above Entry.
                # Current > Previous > Entry Price
                
                # Default: Reset sequence if price drops below entry
                if price <= s.entry_price:
                    if s.last_seen_price is not None:
                        log_strategy_event(f"📉 Reset Sequence: {symbol} at ${price:.2f} (<= {s.entry_price})")
                    s.last_seen_price = None
                else:
                    # Price is above entry price
                    if s.last_seen_price is not None:
                        # We have a previous update above entry.
                        # Check if current is higher than previous (momentum confirmation)
                        if price > s.last_seen_price:
                            log_strategy_event(f"🚀 BREAKOUT: {symbol} at ${price:.2f} > ${s.last_seen_price:.2f} (Target: {s.entry_price})")
                            s.status = "triggered"
                            try:
                                await ib_service.place_order(
                                    ticker_symbol=symbol,
                                    action="BUY",
                                    quantity=s.quantity,
                                    stop_loss_price=s.stop_loss
                                )
                                log_strategy_event(f"✅ Order Placed for {symbol} ({s.quantity} shares)")
                            except Exception as e:
                                log_strategy_event(f"❌ Order Failed for {symbol}: {e}")
                        else:
                            # Price is above entry, but not higher than previous.
                            # It becomes the new base for the next check.
                            s.last_seen_price = price
                    else:
                        # First update above entry
                        log_strategy_event(f"👀 Potential Breakout: {symbol} at ${price:.2f} (> {s.entry_price})")
                        s.last_seen_price = price
                
                s.current_price = price

async def on_price_update(ticker):
    """Callback for real-time price updates"""
    symbol = ticker.contract.symbol
    price = ticker.marketPrice() or ticker.last or ticker.close
    if price and price > 0:
        # Avoid spamming logs for price updates unless debugging core flow
        asyncio.create_task(manager.broadcast({
            "type": "price",
            "ticker": symbol,
            "price": price
        }))
        await check_strategies(symbol, price)

async def on_order_update(trade):
    """Callback for real-time order status changes"""
    asyncio.create_task(manager.broadcast({
        "type": "order_update",
        "order_id": trade.order.orderId,
        "status": trade.orderStatus.status
    }))

async def on_position_update(account, contract, position, avgCost):
    """Callback for real-time position changes"""
    asyncio.create_task(manager.broadcast({
        "type": "position_update",
        "account": account,
        "ticker": contract.symbol,
        "position": position,
        "avg_cost": avgCost
    }))

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Setup
    print("🚀 [BACKEND] Starting Lifespan...")
    init_db()
    
    # Register Callbacks
    ib_service.price_callbacks = [] # Clear old ones if re-running
    ib_service.order_callbacks = []
    ib_service.position_callbacks = []
    ib_service.register_callback(on_price_update)
    ib_service.register_order_callback(on_order_update)
    ib_service.register_position_callback(on_position_update)
    
    # Launch background connection and sync loop
    task = asyncio.create_task(check_connection_loop())
    yield
    # Cleanup
    task.cancel()
    await ib_service.force_disconnect()

app = FastAPI(title="Trader Bot API", lifespan=lifespan)

async def check_connection_loop():
    """Background task to maintain IBKR connection and sync order state"""
    sync_counter = 0
    print("🚀 [SENTINEL] Background loop started", flush=True)
    while True:
        try:
            # 1. Check Connectivity
            is_valid = await ib_service.validate_connection()
            
            if not is_valid:
                print("⚠️ [SENTINEL] Connection check failed. Attempting Reconnect...", flush=True)
                await ib_service.force_disconnect()
                await ib_service.connect()
                
                if ib_service.ib.isConnected():
                    print("✅ [SENTINEL] Reconnected successfully", flush=True)
                    # Restore market data for active monitor list
                    async with strategy_lock:
                        for s in strategies:
                            if s.status == "active":
                                await ib_service.subscribe_market_data(s.ticker)
                else:
                    print("❌ [SENTINEL] Reconnection attempt failed", flush=True)
            else:
                # 2. Periodic State Sync (Every 30s)
                sync_counter += 1
                if sync_counter >= 6:
                    print("🔄 [SENTINEL] Syncing state (Positions/Orders)", flush=True)
                    sync_counter = 0 
                    await ib_service.sync_open_orders()
                    ib_service.ib.reqPositions()
                        
        except Exception as e:
            import traceback
            print(f"📡 [SENTINEL ERROR] Loop clash: {e}", flush=True)
            print(traceback.format_exc(), flush=True)
            sync_counter = 0 
        
        await asyncio.sleep(5)

# --- Endpoints ---

@app.get("/health")
async def health():
    return {"status": "ok", "ib_connected": ib_service.check_connection}

@app.get("/config")
async def get_config():
    return {
        "target_investment": TARGET_INVESTMENT,
        "stop_loss_pct": STOP_LOSS_PCT
    }

@app.post("/config")
async def update_config(req: dict):
    global TARGET_INVESTMENT, STOP_LOSS_PCT
    if "target_investment" in req:
        TARGET_INVESTMENT = float(req["target_investment"])
    if "stop_loss_pct" in req:
        STOP_LOSS_PCT = float(req["stop_loss_pct"])
    return {"status": "success", "config": {"target_investment": TARGET_INVESTMENT, "stop_loss_pct": STOP_LOSS_PCT}}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except:
        manager.disconnect(websocket)

@app.get("/account")
async def get_account():
    return await ib_service.get_account_summary()

@app.get("/portfolio")
async def get_portfolio():
    return await ib_service.get_portfolio()

@app.get("/orders")
async def get_orders():
    return await ib_service.get_today_orders()

@app.post("/order")
async def place_order(req: dict):
    try:
        res = await ib_service.place_order(
            ticker_symbol=req['ticker'],
            action=req['action'],
            quantity=req['quantity'],
            stop_loss_price=req.get('stop_loss')
        )
        return {"status": "success", "order_id": res}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/orders/{order_id}/cancel")
async def cancel_order(order_id: int):
    if ib_service.cancel_order(order_id):
        return {"status": "cancelled"}
    raise HTTPException(status_code=400, detail="Cancellation failed")

@app.post("/orders/{order_id}/purge")
async def purge_order(order_id: int):
    if ib_service.force_delete_order(order_id):
        return {"status": "purged"}
    raise HTTPException(status_code=400, detail="Order not found in memory")

@app.post("/positions/close")
async def close_position(ticker: str):
    try:
        await ib_service.close_position(ticker)
        return {"status": "closed"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/strategies")
async def list_strategies():
    return strategies

@app.post("/strategies")
async def create_strategy(req: StrategyRequest):
    s = Strategy(
        id=str(uuid.uuid4())[:8],
        ticker=req.ticker,
        entry_price=req.entry_price,
        stop_loss=req.stop_loss,
        quantity=req.quantity,
        status="active",
        created_at=datetime.now().isoformat()
    )
    async with strategy_lock:
        strategies.append(s)
    await ib_service.subscribe_market_data(req.ticker)
    log_strategy_event(f"🎯 Strategy Set: {req.ticker} at ${req.entry_price}")
    return s

@app.delete("/strategies/{strategy_id}")
async def delete_strategy(strategy_id: str):
    async with strategy_lock:
        global strategies
        strategies = [s for s in strategies if s.id != strategy_id]
    return {"status": "deleted"}

@app.patch("/strategies/{id}")
async def update_strategy(id: str, update: StrategyUpdate):
    async with strategy_lock:
        for s in strategies:
            if s.id == id:
                if update.is_live is not None: s.is_live = update.is_live
                return s
    raise HTTPException(status_code=404, detail="Strategy not found")

@app.get("/logs")
async def get_logs():
    return list(strategy_logs)

@app.post("/history/download")
async def download_history(req: dict):
    try:
        path = await ib_service.download_historical_data(
            req['ticker'], req['start_date'], req['end_date'], req.get('bar_size', '1 min')
        )
        return {"file": path}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/test/price")
async def set_test_price(req: dict):
    ticker = req['ticker']
    price = float(req['price'])
    await manager.broadcast({"type": "price", "ticker": ticker, "price": price})
    await check_strategies(ticker, price)
    return {"status": "price_updated", "ticker": ticker, "price": price}
