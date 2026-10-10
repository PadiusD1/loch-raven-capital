"""Tests for safe institutional research discovery."""
import unittest
from firm.publisher_registry import PUBLISHERS, metadata

class PublisherTests(unittest.TestCase):
    def test_discovery_does_not_create_verified_evidence(self):
        p = PUBLISHERS[0]
        row = metadata(p, "Market outlook", p.landing, "2026-10-10T12:00:00Z")
        self.assertFalse(row["verified"])
        self.assertFalse(row["eligible_for_rag"])

    def test_other_publisher_domain_rejected(self):
        with self.assertRaises(ValueError):
            metadata(PUBLISHERS[0], "Market outlook", PUBLISHERS[1].landing,
                     "2026-10-10T12:00:00Z")

if __name__ == "__main__":
    unittest.main()
