import sys
import unittest
from unittest.mock import MagicMock, AsyncMock, patch
import asyncio
import traceback

# Add backend to path relative to this script
os_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'backend'))
sys.path.append(os_path)

# Create a dummy module for ib_insync
class MockIBInsync:
    def __init__(self):
        self.IB = MagicMock
        self.Stock = MagicMock
        self.MarketOrder = MagicMock
        self.LimitOrder = MagicMock
        self.StopOrder = MagicMock
        self.Trade = MagicMock
        self.OrderStatus = MagicMock
        self.util = MagicMock()

# Inject it into sys.modules
mock_ib = MockIBInsync()
sys.modules['ib_insync'] = mock_ib

try:
    from ib_service import IBIntegration
except Exception as e:
    print("Failed to import IBIntegration:")
    traceback.print_exc()
    sys.exit(1)

# We also need to mock parts for main.py testing if we were doing integrated tests,
# but for now we will focus on ib_service logic and then manually verify main.py logic or mock it here.

class TestCloseRobustness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = IBIntegration()
        self.service.ib = MagicMock()
        self.service.ib.isConnected.return_value = True

    async def test_close_from_positions(self):
        mock_pos = MagicMock()
        mock_pos.contract.symbol = "AAPL"
        mock_pos.position = 100
        self.service.ib.positions.return_value = [mock_pos]
        self.service.ib.portfolio.return_value = []
        self.service.ib.openTrades.return_value = []

        trade = await self.service.close_position("AAPL")
        
        self.service.ib.placeOrder.assert_called_once()
        self.assertIsNotNone(trade)

    async def test_close_from_portfolio_fallback(self):
        self.service.ib.positions.return_value = []
        
        mock_item = MagicMock()
        mock_item.contract.symbol = "TSLA"
        mock_item.position = -50
        self.service.ib.portfolio.return_value = [mock_item]
        self.service.ib.openTrades.return_value = []

        with patch('ib_service.MarketOrder', MagicMock(return_value=MagicMock())) as mock_market_order:
            trade = await self.service.close_position("TSLA")
            self.service.ib.placeOrder.assert_called_once()
            mock_market_order.assert_called_with("BUY", 50)

    async def test_close_not_found(self):
        self.service.ib.positions.return_value = []
        self.service.ib.portfolio.return_value = []
        self.service.ib.openTrades.return_value = []

        with self.assertRaises(ValueError):
            await self.service.close_position("MSFT")

    async def test_close_test_ticker(self):
        self.service.ib.wrapper.trades = {
            1: MagicMock(contract=MagicMock(symbol="TEST"))
        }
        self.service.force_delete_order = MagicMock()

        result = await self.service.close_position("TEST")
        
        self.assertTrue(result)
        self.service.force_delete_order.assert_called()

if __name__ == '__main__':
    unittest.main()
