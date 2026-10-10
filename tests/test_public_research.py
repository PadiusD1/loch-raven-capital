"""Regression checks: research must never call Public order methods."""
import unittest
from unittest.mock import patch
from firm import public_research

class PublicResearchTests(unittest.TestCase):
    def test_snapshot_uses_existing_read_only_account(self):
        sample = {"positions": [{"symbol": "AAPL", "quantity": 2, "value": 400}],
                  "cash_buying_power": 100, "options_buying_power": 50}
        with patch.object(public_research.broker, "live_account", return_value=sample) as read:
            result = public_research.snapshot()
        read.assert_called_once()
        self.assertEqual(result["positions"][0]["symbol"], "AAPL")
        self.assertTrue(result["research_only"])
        self.assertNotIn("account_number", result)
        self.assertNotIn("api_key", result)

    def test_entitlements_not_assumed(self):
        c = public_research.capabilities()
        self.assertEqual(c["premium_analyst_reports"], "unverified")
        self.assertEqual(c["order_permission"], "not_used")

if __name__ == "__main__":
    unittest.main()
