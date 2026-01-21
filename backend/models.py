from pydantic import BaseModel
from typing import Optional
import uuid

class StrategyRequest(BaseModel):
    ticker: str
    entry_price: float
    stop_loss: Optional[float] = None
    quantity: int = 1
    quantity: int = 1
    quantity: int = 1

class StrategyUpdate(BaseModel):
    is_live: Optional[bool] = None
    current_price: Optional[float] = None

class Strategy(BaseModel):
    id: str
    ticker: str
    entry_price: float
    stop_loss: Optional[float]
    quantity: int
    status: str = "active" # active, executed, cancelled
    created_at: str
    last_seen_price: Optional[float] = None # For tracking sequential candles
    current_price: Optional[float] = None
    last_updated: Optional[str] = None
    is_live: bool = True
