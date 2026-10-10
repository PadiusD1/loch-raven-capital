"""Offline checks for external research provenance."""
import unittest
from firm.research_provenance import validate_research

BASE = {
    "desk": "fundamental",
    "source_url": "https://www.sec.gov/Archives/example",
    "published_at": "2026-09-01T12:00:00Z",
    "observed_at": "2026-09-01T13:00:00Z",
    "asof": "2026-08-31T20:00:00Z",
    "title": "Quarterly filing",
    "body": "A sufficiently detailed research passage for audit testing.",
}


class ResearchProvenanceTests(unittest.TestCase):
    def test_valid_record(self):
        result = validate_research(BASE, "2026-09-02T00:00:00Z")
        self.assertTrue(result["provenance_checked"])
        self.assertFalse(result["fact_checked"])

    def test_future_publication_rejected(self):
        with self.assertRaises(ValueError):
            validate_research(BASE, "2026-08-31T00:00:00Z")

    def test_missing_timezone_rejected(self):
        with self.assertRaises(ValueError):
            validate_research({**BASE, "published_at": "2026-09-01T12:00:00"})

    def test_non_https_rejected(self):
        with self.assertRaises(ValueError):
            validate_research({**BASE, "source_url": "http://example.com/report"})

    def test_unpublished_observation_rejected(self):
        with self.assertRaises(ValueError):
            validate_research({**BASE, "observed_at": "2026-08-30T00:00:00Z"})


if __name__ == "__main__":
    unittest.main()
