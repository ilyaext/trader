from ib_insync import *
import asyncio

async def main():
    ib = IB()
    try:
        await ib.connectAsync('127.0.0.1', 4001, clientId=999) # Using 4001 for IB Gateway Paper
        contract = Stock('SPY', 'SMART', 'USD')
        await ib.qualifyContractsAsync(contract)
        
        print(f"Checking schedule for {contract.symbol}...")
        schedule = await ib.reqHistoricalScheduleAsync('', '1 D', True, contract)
        print("Schedule:", schedule)
        
        # Check if RTH is currently active
        from datetime import datetime
        now = datetime.now()
        is_open = False
        for session in schedule.sessions:
            if session.start <= now <= session.end:
                if session.refDate: # This might indicate RTH?
                    is_open = True
                    break
        print(f"Is RTH Open: {is_open}")

    except Exception as e:
        print(f"Error: {e}")
    finally:
        ib.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
