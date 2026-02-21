from ib_insync import *
import asyncio
import os
import random

async def main():
    ib = IB()
    host = os.getenv("IB_HOST", "127.0.0.1")
    port = int(os.getenv("IB_PORT", "7497"))
    client_id = random.randint(1000, 9000)
    
    print(f"Connecting to {host}:{port} with clientId {client_id}...")
    try:
        await ib.connectAsync(host, port, clientId=client_id)
        print("Connected.")
    except Exception as e:
        print(f"Connection failed: {e}")
        return

    tickers = ["PLTR", "SPY", "AAPL"]
    
    for t in tickers:
        print(f"\n--- Testing {t} ---")
        # Test 1: Explicit SMART
        c1 = Stock(t, 'SMART', 'USD')
        print(f"Test 1: Stock({t}, 'SMART', 'USD')")
        try:
            await ib.qualifyContractsAsync(c1)
            print(f"Result: conId={c1.conId}, symbol={c1.symbol}, exchange={c1.exchange}")
        except Exception as e:
            print(f"Error qualifying c1: {e}")

        # Test 2: Empty Exchange
        c2 = Stock(t, '', 'USD')
        print(f"Test 2: Stock({t}, '', 'USD')")
        try:
            await ib.qualifyContractsAsync(c2)
            print(f"Result: conId={c2.conId}, symbol={c2.symbol}, exchange={c2.exchange}")
        except Exception as e:
            print(f"Error qualifying c2: {e}")

    ib.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
