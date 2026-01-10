from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel
from contextlib import asynccontextmanager
from database import init_db, get_db, Trade
from sqlalchemy.orm import Session
from ib_service import ib_service
import asyncio

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    init_db()
    
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
        await asyncio.sleep(5) # Check every 5 seconds

app = FastAPI(title="Trader Bot API", lifespan=lifespan)

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

@app.post("/history/download")
async def download_history(req: HistoryRequest):
    if not ib_service.check_connection:
        raise HTTPException(status_code=503, detail="IBKR Disconnected")
    
    try:
        filename = await ib_service.download_historical_data(req.ticker, req.start_date, req.end_date)
        return {"status": "ok", "file": filename}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/health")
async def health():
    return {"status": "ok", "ib_connected": ib_service.check_connection}
