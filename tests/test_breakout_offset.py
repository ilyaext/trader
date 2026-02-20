import requests
import time
import uuid

BACKEND_URL = "http://localhost:8000"

def test_breakout_offset():
    print("🚀 Starting Breakout Offset Safety Test...")
    
    # 1. Setup - Configure 3% offset (should be default but let's be sure)
    print("⚙️ Configuring Offset to 3%...")
    resp = requests.post(f"{BACKEND_URL}/config", json={"max_breakout_offset_pct": 3.0})
    print(f"Config update: {resp.json()}")

    ticker = "AAPL"
    entry_price = 100.0
    
    # --- Case 1: Valid Breakout (within 3%) ---
    print(f"\n--- Case 1: Valid Breakout for {ticker} at $101.00 (1% above $100) ---")
    
    # Create strategy
    strat_resp = requests.post(f"{BACKEND_URL}/strategies", json={
        "ticker": ticker,
        "entry_price": entry_price,
        "stop_loss": 97.0,
        "quantity": 10
    })
    strat = strat_resp.json()
    strat_id = strat['id']
    print(f"Strategy created: {strat_id}")

    # Set price to 101.0 (Price 1 of sequence)
    requests.post(f"{BACKEND_URL}/test/price", json={"ticker": ticker, "price": 101.0})
    time.sleep(1)
    
    # Set price to 101.5 (Price 2 of sequence - Trigger)
    print("Triggering breakout within limit...")
    resp = requests.post(f"{BACKEND_URL}/test/price", json={"ticker": ticker, "price": 101.5})
    
    print("Check logs or UI: Should see 'Order Placed' for AAPL")
    
    # Cleanup Case 1
    requests.delete(f"{BACKEND_URL}/strategies/{strat_id}")

    # --- Case 2: Invalid Breakout (outside 3%) ---
    ticker_msft = "MSFT"
    print(f"\n--- Case 2: Invalid Breakout for {ticker_msft} at $104.00 (4% above $100) ---")
    
    # Create strategy
    strat_resp = requests.post(f"{BACKEND_URL}/strategies", json={
        "ticker": ticker_msft,
        "entry_price": entry_price,
        "stop_loss": 97.0,
        "quantity": 10
    })
    strat = strat_resp.json()
    strat_id = strat['id']
    print(f"Strategy created: {strat_id}")

    # Set price to 101.0 (Price 1 of sequence)
    requests.post(f"{BACKEND_URL}/test/price", json={"ticker": ticker_msft, "price": 101.0})
    time.sleep(1)
    
    # Set price to 104.0 (Price 2 of sequence - Trigger attempt)
    print("Attempting trigger with price too high (4%)...")
    resp = requests.post(f"{BACKEND_URL}/test/price", json={"ticker": ticker_msft, "price": 104.0})
    
    print("Check logs or UI: Should see '⚠️ Breakout Skipped [ID]: Price too high'")
    
    # Cleanup Case 2
    requests.delete(f"{BACKEND_URL}/strategies/{strat_id}")

if __name__ == "__main__":
    test_breakout_offset()
