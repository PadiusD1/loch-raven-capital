"""Tests for safe institutional research discovery."""
import unittest
from firm.publisher_registry import PUBLISHERS, allowed_discovery, metadata


def _publisher(name):
    return next(p for p in PUBLISHERS if p.name == name)


class PublisherTests(unittest.TestCase):
    def test_discovery_does_not_create_verified_evidence(self):
        p = PUBLISHERS[0]
        row = metadata(p, "Market outlook", p.landing, "2026-10-10T12:00:00Z")
        self.assertFalse(row["verified"])
        self.assertFalse(row["eligible_for_rag"])

    def test_other_publisher_domain_rejected(self):
        with self.assertRaises(ValueError):
            metadata(PUBLISHERS[0], "Market outlook", _publisher("Wall Street Journal").landing,
                     "2026-10-10T12:00:00Z")

    def test_metadata_flags_rights_review(self):
        p = _publisher("Goldman Sachs Research")
        row = metadata(p, "Market outlook", p.landing, "2026-10-10T12:00:00Z")
        self.assertIs(row["requires_rights_review"], True)
        self.assertEqual(row["research_status"], "discovered_unverified")
        self.assertFalse(row["verified"])
        self.assertFalse(row["eligible_for_rag"])

    def test_registered_discovery_url_returns_publisher(self):
        for p in PUBLISHERS:
            with self.subTest(publisher=p.name):
                self.assertIs(allowed_discovery(p.landing), p)

    def test_unsafe_discovery_url_raises(self):
        for url in ("http://www.wsj.com/",
                    "https://reader@www.wsj.com/",
                    "https://:token@www.wsj.com/",
                    "https://www.wsj.com/#latest"):
            with self.subTest(url=url):
                with self.assertRaisesRegex(ValueError, "Invalid publisher discovery URL"):
                    allowed_discovery(url)

    def test_unregistered_discovery_url_raises(self):
        for url in ("https://www.example.com/",
                    "https://www.wsj.com/news/markets",
                    "https://www.wsj.com/?page=2",
                    "https://www.goldmansachs.com/insights/goldman-sachs-research/2026/"):
            with self.subTest(url=url):
                with self.assertRaisesRegex(ValueError, "Unregistered publisher discovery URL"):
                    allowed_discovery(url)

    def test_trailing_slash_is_the_only_tolerance(self):
        self.assertIs(allowed_discovery("https://www.cnn.com/business/"),
                      _publisher("CNN Business"))
        self.assertIs(allowed_discovery("https://www.goldmansachs.com/insights"),
                      _publisher("Goldman Sachs Insights"))

    def test_goldman_sachs_research_row_registered(self):
        research = _publisher("Goldman Sachs Research")
        insights = _publisher("Goldman Sachs Insights")
        self.assertEqual(research.host, "www.goldmansachs.com")
        self.assertEqual(research.landing,
                         "https://www.goldmansachs.com/insights/goldman-sachs-research/")
        self.assertEqual(research.access, "metadata_only")
        self.assertEqual(research.desks, ("fundamental", "macro", "sentiment"))
        # Both rows share a host, so only the exact discovery URL tells them apart.
        self.assertEqual(research.host, insights.host)
        self.assertIs(allowed_discovery(research.landing), research)
        self.assertIs(allowed_discovery(insights.landing), insights)

    def test_discovery_urls_are_unique(self):
        keys = [p.landing.rstrip("/") for p in PUBLISHERS]
        self.assertEqual(len(keys), len(set(keys)))

if __name__ == "__main__":
    unittest.main()
