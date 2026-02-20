import nest_asyncio
import asyncio
from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from contextlib import asynccontextmanager
from database import init_db, get_db, Trade, StrategyModel, SessionLocal
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

# Configure logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("TraderBot")

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
        # Create a copy to safely iterate while pruning
        to_remove = []
        for connection in list(self.active_connections):
            try:
                await connection.send_text(json.dumps(message))
            except Exception as e:
                logger.warning(f"⚠️ [WS ERROR] Connection stale, pruning: {e}")
                to_remove.append(connection)
        
        for conn in to_remove:
            self.disconnect(conn)

manager = ConnectionManager()
ib_service = live_get_ib_service()

def log_strategy_event(msg: str):
    """Log an event to the global buffer and broadcast via WS"""
    timestamp = datetime.now().strftime("%H:%M:%S")
    # Add a millisecond part to the timestamp to distinguish rapid logs
    ms = datetime.now().strftime("%f")[:3]
    formatted = f"[{timestamp}.{ms}] {msg}"
    
    # Generate a unique ID for this specific log event to allow UI deduplication
    log_entry = {
        "id": str(uuid.uuid4())[:12],
        "text": formatted,
        "timestamp": timestamp
    }
    
    # store formatted string for backward compatibility in /logs endpoint
    strategy_logs.appendleft(log_entry)
    
    # TRACE: Number of connections we are broadcasting to
    conn_count = len(manager.active_connections)
    logger.debug(f"[CORE LOG] {formatted} (ID: {log_entry['id']}, Clients: {conn_count})")
    
    asyncio.create_task(manager.broadcast({
        "type": "log", 
        "data": log_entry['text'],
        "log_id": log_entry['id']
    }))

def update_db_strategy_status(s_id: str, status: str):
    """Helper to update strategy status in DB in a background thread"""
    db = SessionLocal()
    try:
        db.query(StrategyModel).filter(StrategyModel.id == s_id).update({"status": status})
        db.commit()
        logger.debug(f"🗄️ [DB SYNC] Updated strategy {s_id} status to {status}")
    except Exception as e:
        logger.error(f"❌ [DB SYNC] Failed to update strategy {s_id}: {e}")
    finally:
        db.close()

async def check_strategies(symbol, price, source="UNKNOWN"):
    # Normalize input
    symbol = symbol.strip().upper()
    
    async with strategy_lock:
        # 1. Handle ACTIVE strategies (Breakout Detection)
        active_for_ticker = [s for s in strategies if s.ticker.upper() == symbol and s.status == "active"]
        
        # 2. Handle TRIGGERED strategies (False Breakout Detection)
        triggered_for_ticker = [s for s in strategies if s.ticker.upper() == symbol and s.status == "triggered"]
        
        # DEBUG: Trace the specific call
        logger.debug(f"[check_strategies] Tick {symbol} @ {price} | Source: {source} | Active: {len(active_for_ticker)} | Triggered: {len(triggered_for_ticker)}")

        # --- Part A: Check False Breakouts ---
        for s in triggered_for_ticker:
            if price < s.entry_price:
                # POTENTIAL FALSE BREAKOUT: Price dropped below original entry
                if s.last_seen_price is not None and price < s.last_seen_price:
                    # CONFIRMED FALSE BREAKOUT: Two sequential falling updates below entry
                    log_strategy_event(f"🚨 FALSE BREAKOUT [{s.id}]: {symbol} at ${price:.2f} (< {s.last_seen_price:.2f}). Resetting...")
                    
                    # 1. Close the position & Cancel associated orders
                    try:
                        await ib_service.close_position(symbol)
                        log_strategy_event(f"🧹 Cleaned up {symbol} for [{s.id}]")
                    except Exception as e:
                        logger.error(f"[False Breakout Cleanup] Failed for {symbol}: {e}")
                    
                    # 2. Reset the strategy state
                    s.status = "active"
                    update_db_strategy_status(s.id, "active")
                    s.last_seen_price = None
                    s.current_price = price
                else:
                    # First low or price didn't drop further, track it
                    if s.last_seen_price is None:
                        log_strategy_event(f"👀 Potential False Breakout [{s.id}]: {symbol} at ${price:.2f} (< {s.entry_price})")
                    s.last_seen_price = price
            else:
                # Price is back above entry, clear potential false breakout tracking
                if s.last_seen_price is not None:
                    log_strategy_event(f"🛡️ Breakout Stable [{s.id}]: {symbol} at ${price:.2f} (>= {s.entry_price})")
                s.last_seen_price = None

        # --- Part B: Check New Breakouts ---
        if not active_for_ticker:
            # Update current price for any other trackers and exit
            for s in strategies:
                if s.ticker.upper() == symbol: s.current_price = price
            return

        # Only process the FIRST active strategy found for this ticker
        s = active_for_ticker[0]
        
        # Rule: Two sequential updates must be above Entry.
        
        # Default: Reset sequence if price drops below entry
        if price <= s.entry_price:
            if s.last_seen_price is not None:
                log_strategy_event(f"📉 Reset Sequence: {symbol} at ${price:.2f} (<= {s.entry_price}) [ID: {s.id}]")
            s.last_seen_price = None
        else:
            # Price is above entry price
            if s.last_seen_price is not None:
                # Double check status inside lock
                if s.status != "active":
                    return

                # Check if current is higher than previous (momentum confirmation)
                if price > s.last_seen_price:
                    log_strategy_event(f"🚀 BREAKOUT [{s.id}]: {symbol} at ${price:.2f} > ${s.last_seen_price:.2f} (Target: {s.entry_price})")
                    s.status = "triggered"
                    update_db_strategy_status(s.id, "triggered")
                    s.last_seen_price = None  # Reset for False Breakout tracking
                    try:
                        await ib_service.place_order(
                            ticker_symbol=symbol,
                            action="BUY",
                            quantity=s.quantity,
                            stop_loss_price=s.stop_loss
                        )
                        log_strategy_event(f"✅ Order Placed [{s.id}] for {symbol} ({s.quantity} shares)")
                    except Exception as e:
                        log_strategy_event(f"❌ Order Failed [{s.id}] for {symbol}: {e}")
                else:
                    # Update base price if it's different
                    if s.last_seen_price != price:
                        s.last_seen_price = price
            else:
                # First update above entry
                log_strategy_event(f"👀 Potential Breakout [{s.id}]: {symbol} at ${price:.2f} (> {s.entry_price})")
                s.last_seen_price = price
        
        s.current_price = price
        
        # Calculate Daily % if not already set by on_price_update
        if not s.daily_change_pct:
            for ticker_obj in ib_service.ib.tickers():
                if ticker_obj.contract.symbol == symbol and ticker_obj.close:
                    s.daily_change_pct = (price - ticker_obj.close) / ticker_obj.close * 100
                    break

async def on_bar_update(bars, hasNewBar):
    """Callback for 1-minute bar closes"""
    if not hasNewBar:
        return
        
    # Get the last completed bar (bars[-1] is the one that just closed)
    last_bar = bars[-1]
    symbol = bars.contract.symbol.upper()
    close_price = last_bar.close
    
    logger.info(f"📊 [BAR CLOSED] {symbol} | Close: ${close_price:.2f} | Time: {last_bar.date}")
    
    # Run strategy logic ON BAR CLOSE
    await check_strategies(symbol, close_price, source="BAR_CLOSE")

async def on_price_update(ticker):
    """Callback for real-time price updates"""
    symbol = ticker.contract.symbol.upper()
    
    # IGNORE 'TEST' ticker from live callbacks to prevent duplication with manual injection
    if symbol == "TEST":
        return

    price = ticker.marketPrice() or ticker.last or ticker.close
    if price and price > 0:
        # Calculate Daily %
        daily_change_pct = 0.0
        if ticker.close and ticker.close > 0:
            daily_change_pct = (price - ticker.close) / ticker.close * 100

        # Avoid spamming logs for price updates unless debugging core flow
        asyncio.create_task(manager.broadcast({
            "type": "price",
            "ticker": symbol,
            "price": price,
            "daily_change_pct": daily_change_pct
        }))
        
        # Update in-memory strategy objects Daily % BEFORE checking triggers
        async with strategy_lock:
            for s in strategies:
                if s.ticker.upper() == symbol:
                    s.current_price = price
                    s.daily_change_pct = daily_change_pct

        # await check_strategies(symbol, price, source="LIVE_TICK") # REPLACED BY BAR UPDATES

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
    logger.info("🚀 [BACKEND] Starting Lifespan...")
    init_db()
    
    # LOAD PERSISTENT STRATEGIES
    async with strategy_lock:
        db = SessionLocal()
        try:
            # 1. Cleanup old strategies (End of Day logic)
            today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            deleted_count = db.query(StrategyModel).filter(StrategyModel.created_at < today_start).delete()
            db.commit()
            if deleted_count > 0:
                logger.info(f"🧹 [PERSISTENCE] Cleaned up {deleted_count} expired strategies from previous days")

            # 2. Load active/triggered strategies
            db_strategies = db.query(StrategyModel).filter(StrategyModel.status.in_(["active", "triggered"])).all()
            for ds in db_strategies:
                s = Strategy(
                    id=ds.id,
                    ticker=ds.ticker,
                    entry_price=ds.entry_price,
                    stop_loss=ds.stop_loss,
                    quantity=ds.quantity,
                    status=ds.status,
                    created_at=ds.created_at.isoformat()
                )
                strategies.append(s)
                # Subscribe to market data for loaded strategies
                await ib_service.subscribe_market_data(s.ticker)
                logger.info(f"📋 [PERSISTENCE] Restored strategy: {s.ticker} [{s.id}]")
        finally:
            db.close()
    
    # Register Callbacks
    ib_service.price_callbacks = [] # Clear old ones if re-running
    ib_service.order_callbacks = []
    ib_service.position_callbacks = []
    ib_service.bar_callbacks = []
    ib_service.register_callback(on_price_update)
    ib_service.register_order_callback(on_order_update)
    ib_service.register_position_callback(on_position_update)
    ib_service.register_bar_callback(on_bar_update)
    
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
    logger.info("🚀 [SENTINEL] Background loop started")
    while True:
        try:
            # 1. Check Connectivity
            is_valid = await ib_service.validate_connection()
            
            if not is_valid:
                logger.warning("⚠️ [SENTINEL] Connection check failed. Attempting Reconnect...")
                await ib_service.force_disconnect()
                await ib_service.connect()
                
                if ib_service.ib.isConnected():
                    logger.info("✅ [SENTINEL] Reconnected successfully")
                    # Restore market data for active monitor list
                    async with strategy_lock:
                        for s in strategies:
                            if s.status == "active":
                                await ib_service.subscribe_market_data(s.ticker)
                else:
                    logger.error("❌ [SENTINEL] Reconnection attempt failed")
            else:
                # 2. Periodic State Sync (Every 30s)
                sync_counter += 1
                if sync_counter >= 6:
                    logger.info("🔄 [SENTINEL] Syncing state (Positions/Orders)")
                    sync_counter = 0 
                    await ib_service.sync_open_orders()
                    ib_service.ib.reqPositions()
                        
        except Exception as e:
            import traceback
            logger.error(f"📡 [SENTINEL ERROR] Loop clash: {e}")
            logger.error(traceback.format_exc())
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
    ticker = req['ticker'].strip().upper()
    try:
        # 1. Check for existing positions
        positions = ib_service.ib.positions()
        if any(p.contract.symbol == ticker and p.position != 0 for p in positions):
            raise HTTPException(status_code=400, detail=f"Existing position for {ticker} already found.")

        # 2. Check for existing open orders
        open_trades = ib_service.ib.openTrades()
        if any(t.contract.symbol == ticker for t in open_trades):
            raise HTTPException(status_code=400, detail=f"Existing open order for {ticker} already found.")

        # 3. Place order
        trade = await ib_service.place_order(
            ticker_symbol=ticker,
            action=req['action'],
            quantity=req['quantity'],
            stop_loss_price=req.get('stop_loss')
        )
        # Return only the order ID as an integer to prevent serialization errors
        return {"status": "success", "order_id": int(trade.order.orderId)}
    except HTTPException:
        raise
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
    ticker = ticker.strip().upper()
    try:
        # 1. Close Position in IBKR
        await ib_service.close_position(ticker)
        
        # 2. Cleanup Associated Strategies (Active or Triggered)
        async with strategy_lock:
            global strategies
            # Find IDs to delete from DB
            to_delete_ids = [s.id for s in strategies if s.ticker.upper() == ticker]
            
            if to_delete_ids:
                # Remove from memory
                strategies = [s for s in strategies if s.ticker.upper() != ticker]
                
                # Remove from DB
                db = SessionLocal()
                try:
                    db.query(StrategyModel).filter(StrategyModel.id.in_(to_delete_ids)).delete(synchronize_session=False)
                    db.commit()
                    log_strategy_event(f"🧹 Auto-deleted {len(to_delete_ids)} strategies for {ticker} (Position Closed)")
                except Exception as db_e:
                    logger.error(f"❌ [DB] Failed to cleanup strategies for {ticker}: {db_e}")
                finally:
                    db.close()

        return {"status": "success", "message": f"Closed position and cleaned up strategies for {ticker}"}
    except ValueError as e:
        # Position not found
        logger.warning(f"⚠️ [API] Close failed: {e}")
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"❌ [API] Error closing {ticker}: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/strategies")
async def list_strategies():
    return strategies

@app.post("/strategies")
async def create_strategy(req: StrategyRequest):
    # Normalize ticker
    ticker = req.ticker.strip().upper()
    
    async with strategy_lock:
        # Prevent duplicate active strategies for the same ticker to avoid duplicate orders
        for s in strategies:
            if s.ticker.upper() == ticker and s.status == "active":
                raise HTTPException(status_code=400, detail=f"Active strategy for {ticker} already exists")

        s = Strategy(
            id=str(uuid.uuid4())[:8],
            ticker=ticker,
            entry_price=req.entry_price,
            stop_loss=req.stop_loss,
            quantity=req.quantity,
            status="active",
            created_at=datetime.now().isoformat()
        )
        strategies.append(s)
        
        # Save to DB
        db = SessionLocal()
        try:
            db_s = StrategyModel(
                id=s.id,
                ticker=s.ticker,
                entry_price=s.entry_price,
                stop_loss=s.stop_loss,
                quantity=s.quantity,
                status=s.status
            )
            db.add(db_s)
            db.commit()
        finally:
            db.close()
    
    await ib_service.subscribe_market_data(ticker)
    log_strategy_event(f"🎯 Strategy Set: {ticker} at ${req.entry_price} [ID: {s.id}]")
    return s

@app.delete("/strategies/{strategy_id}")
async def delete_strategy(strategy_id: str):
    async with strategy_lock:
        global strategies
        strategies = [s for s in strategies if s.id != strategy_id]
        
        # Update/Delete in DB
        db = SessionLocal()
        try:
            db.query(StrategyModel).filter(StrategyModel.id == strategy_id).delete()
            db.commit()
        finally:
            db.close()
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
    ticker = req['ticker'].strip().upper()
    price = float(req['price'])
    await manager.broadcast({"type": "price", "ticker": ticker, "price": price})
    await check_strategies(ticker, price, source="MANUAL_TEST")
    return {"status": "price_updated", "ticker": ticker, "price": price}
