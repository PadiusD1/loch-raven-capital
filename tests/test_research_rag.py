"""Offline retrieval tests against a realistic SEC-shaped research record."""
import json
import tempfile
import unittest
from pathlib import Path

from firm.research_rag import retrieve
from scripts.fetch_sec_research import extract_facts


class SECResearchTests(unittest.TestCase):
    def test_extract_excludes_future_filings(self):
        payload = {"facts": {"us-gaap": {"Assets": {"units": {"USD": [
            {"val": 100, "filed": "2026-08-01", "form": "10-Q", "end": "2026-06-30"},
            {"val": 200, "filed": "2026-11-01", "form": "10-Q", "end": "2026-09-30"}
        ]}}}}}
        rows = extract_facts(payload, observed_at="2026-10-09T12:00:00Z")
        self.assertEqual([r["value"] for r in rows], [100])

    def test_retrieval_is_point_in_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = {
                "desk": "fundamental",
                "source_url": "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json",
                "published_at": "2026-10-09T00:00:00Z",
                "observed_at": "2026-10-09T12:00:00Z",
                "asof": "2026-10-09T00:00:00Z",
                "title": "SEC XBRL Apple assets",
                "body": "Apple assets financial statement reported from an SEC filing.",
            }
            (root / "apple.json").write_text(json.dumps(record))
            self.assertEqual(len(retrieve("Apple assets", desk="fundamental",
                                          asof="2026-10-10T00:00:00Z", root=root)), 1)
            self.assertEqual(retrieve("Apple assets", desk="fundamental",
                                      asof="2026-10-09T06:00:00Z", root=root), [])
            self.assertEqual(retrieve("Apple assets", desk="macro", root=root), [])


if __name__ == "__main__":
    unittest.main()
