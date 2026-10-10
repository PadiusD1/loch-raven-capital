"""Ensure external research is available to the correct desk only."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from firm import analysts


class DeskResearchIntegrationTests(unittest.TestCase):
    def test_fundamental_only_receives_its_research(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rec = {
                "desk": "fundamental", "tickers": ["AAPL"],
                "source_url": "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json",
                "published_at": "2026-09-01T00:00:00Z",
                "observed_at": "2026-09-02T00:00:00Z",
                "asof": "2026-09-01T00:00:00Z",
                "title": "Apple SEC financial facts",
                "body": "Apple SEC financial statements show historical assets and liabilities.",
            }
            (root / "apple.json").write_text(json.dumps(rec))
            from firm import research_rag
            original = research_rag.retrieve

            def isolated(query, **kwargs):
                kwargs["root"] = root
                return original(query, **kwargs)

            packet = {"ticker": "AAPL", "name": "Apple", "asof": "2026-09-03",
                      "technicals": {"price": 100}}
            with patch.object(research_rag, "retrieve", side_effect=isolated):
                fundamental = analysts._desk_packet("fundamental", packet)
                technical = analysts._desk_packet("technical", packet)
                past = analysts._desk_packet("fundamental", {**packet, "asof": "2026-09-01"})
            self.assertEqual(len(fundamental["external_research"]), 1)
            self.assertNotIn("external_research", technical)
            self.assertNotIn("external_research", past)


if __name__ == "__main__":
    unittest.main()
