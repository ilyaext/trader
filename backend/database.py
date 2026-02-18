from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import os
import datetime

DATABASE_URL = f"postgresql://{os.getenv('POSTGRES_USER', 'postgres')}:{os.getenv('POSTGRES_PASSWORD', 'password')}@{os.getenv('POSTGRES_SERVER', 'localhost')}/{os.getenv('POSTGRES_DB', 'trader')}"

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, index=True)
    ticker = Column(String, index=True)
    action = Column(String) # BUY / SELL
    quantity = Column(Integer)
    price = Column(Float)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)
    ib_order_id = Column(Integer)

class StrategyModel(Base):
    __tablename__ = "strategies"

    id = Column(String, primary_key=True, index=True)
    ticker = Column(String, index=True)
    entry_price = Column(Float)
    stop_loss = Column(Float, nullable=True)
    quantity = Column(Integer)
    status = Column(String) # active, triggered, executed, cancelled
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

def init_db():
    try:
        Base.metadata.create_all(bind=engine)
    except Exception as e:
        print(f"DB Connection failed (expected during build/without db): {e}")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
