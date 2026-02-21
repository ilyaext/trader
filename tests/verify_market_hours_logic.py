import asyncio
from ib_insync import *
import logging

# Mocking the callback for testing
async def mock_on_price_update(ticker):
    symbol = ticker.contract.symbol.upper()
    if symbol == "TEST":
        print(f"✅ [TEST] Bypassing marketDataType check for TEST")
        return True
        
    if ticker.marketDataType != 1:
        print(f"❌ [FILTERED] Skipping {symbol} update: Market is closed (Type: {ticker.marketDataType})")
        return False
    else:
        print(f"🚀 [ALLOWED] Processing {symbol} live update (Type: {ticker.marketDataType})")
        return True

async def main():
    print("--- Starting Market Hours Verification Test ---")
    
    # 1. Test case: TEST ticker
    test_contract = Stock('TEST', 'SMART', 'USD')
    test_ticker = Ticker(contract=test_contract)
    test_ticker.marketDataType = 2 # Frozen
    print(f"Testing TEST ticker with Frozen data (Type 2)...")
    await mock_on_price_update(test_ticker)
    
    # 2. Test case: SPY with Frozen data (Type 2)
    spy_contract = Stock('SPY', 'SMART', 'USD')
    spy_ticker = Ticker(contract=spy_contract)
    spy_ticker.marketDataType = 2 # Frozen
    print(f"\nTesting SPY ticker with Frozen data (Type 2)...")
    result = await mock_on_price_update(spy_ticker)
    if not result:
        print("✅ Correctly filtered out frozen SPY ticker.")
    
    # 3. Test case: SPY with Live data (Type 1)
    print(f"\nTesting SPY ticker with Live data (Type 1)...")
    spy_ticker.marketDataType = 1 # Live
    result = await mock_on_price_update(spy_ticker)
    if result:
        print("✅ Correctly allowed live SPY ticker.")

    # 4. Test case: SPY with Delayed data (Type 3)
    print(f"\nTesting SPY ticker with Delayed data (Type 3)...")
    spy_ticker.marketDataType = 3 # Delayed
    result = await mock_on_price_update(spy_ticker)
    if not result:
        print("✅ Correctly filtered out delayed SPY ticker.")

    print("\n--- Verification Test Complete ---")

if __name__ == "__main__":
    asyncio.run(main())
