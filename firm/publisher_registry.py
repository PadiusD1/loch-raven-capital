"""Research publisher registry and safe routing policy.

This module defines metadata-only discovery policies; it does not bypass access
controls, authenticate to publishers, fetch paywalled content, or place orders.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

@dataclass(frozen=True)
class Publisher:
    name: str
    domain: str
    discovery: str
    access: str
    desks: tuple[str, ...]
    interval_hours: int = 24

PUBLISHERS = (
    Publisher("Goldman Sachs Research", "www.goldmansachs.com",
              "https://www.goldmansachs.com/insights/goldman-sachs-research/",
              "public_metadata", ("fundamental", "macro", "sentiment")),
    Publisher("Goldman Sachs Insights", "www.goldmansachs.com",
              "https://www.goldmansachs.com/insights/",
              "public_metadata", ("fundamental", "macro", "sentiment")),
    Publisher("Wall Street Journal", "www.wsj.com", "https://www.wsj.com/",
              "public_metadata_only", ("sentiment", "macro")),
    Publisher("CNN Business", "www.cnn.com", "https://www.cnn.com/business",
              "public_metadata_only", ("sentiment", "macro")),
    Publisher("Reuters", "www.reuters.com", "https://www.reuters.com/markets/",
              "public_metadata_only", ("sentiment", "macro")),
    Publisher("BlackRock Insights", "www.blackrock.com",
              "https://www.blackrock.com/corporate/insights",
              "public_metadata_only", ("macro", "fundamental")),
)

def allowed_discovery(url: str) -> Publisher:
    """Match an exact approved discovery URL; no arbitrary redirects."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Invalid publisher discovery URL")
    for publisher in PUBLISHERS:
        if url.rstrip("/") == publisher.discovery.rstrip("/"):
            return publisher
    raise ValueError("Unregistered publisher discovery URL")

def route_metadata(publisher: Publisher, title: str, url: str,
                   published_at: str | None) -> dict:
    """Produce non-investable discovery metadata; NOT a verified research record."""
    if not title.strip() or not published_at:
        raise ValueError("Undated or untitled report")
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != publisher.domain:
        raise ValueError("Publisher URL/domain mismatch")
    return {"publisher": publisher.name, "title": title.strip(),
            "canonical_url": url, "published_at": published_at,
            "desks": list(publisher.desks), "access": publisher.access,
            "research_status": "discovered_unverified",
            "eligible_for_rag": False,
            "requires_rights_review": True}
