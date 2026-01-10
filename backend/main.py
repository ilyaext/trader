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
    # Attempt connection in background to not block startup
    asyncio.create_task(ib_service.connect())
    yield
    # Shutdown
    if ib_service.ib.isConnected():
        ib_service.ib.disconnect()

app = FastAPI(title="Trader Bot API", lifespan=lifespan)

class OrderRequest(BaseModel):
    ticker: str
    action: str = "BUY"
    quantity: int = 1

@app.get("/quote/{ticker}")
async def get_quote(ticker: str):
    if not ib_service.connected:
        return {"ticker": ticker, "price": 0.0, "status": "disconnected"}
    
    price = ib_service.get_price(ticker)
    return {"ticker": ticker, "price": price, "status": "connected"}

@app.post("/order")
async def place_order(order: OrderRequest, db: Session = Depends(get_db)):
    if not ib_service.connected:
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

@app.get("/health")
async def health():
    return {"status": "ok", "ib_connected": ib_service.connected}
