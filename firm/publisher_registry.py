"""Publisher metadata registry, separate from verified investment evidence."""
from dataclasses import dataclass
from urllib.parse import urlparse

@dataclass(frozen=True)
class Publisher:
    name: str
    host: str
    landing: str
    desks: tuple[str, ...]
    access: str = "metadata_only"

PUBLISHERS = (
    Publisher("Goldman Sachs Insights", "www.goldmansachs.com",
              "https://www.goldmansachs.com/insights/", ("fundamental", "macro", "sentiment")),
    Publisher("Wall Street Journal", "www.wsj.com",
              "https://www.wsj.com/", ("sentiment", "macro")),
    Publisher("CNN Business", "www.cnn.com",
              "https://www.cnn.com/business", ("sentiment", "macro")),
    Publisher("Reuters", "www.reuters.com",
              "https://www.reuters.com/markets/", ("sentiment", "macro")),
    Publisher("BlackRock Insights", "www.blackrock.com",
              "https://www.blackrock.com/corporate/insights", ("macro", "fundamental")),
)

def metadata(publisher: Publisher, title: str, url: str, published_at: str) -> dict:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != publisher.host or parsed.username or parsed.password:
        raise ValueError("Publisher domain mismatch")
    if not title.strip() or not published_at:
        raise ValueError("Report requires title and publication timestamp")
    return {"publisher": publisher.name, "title": title.strip(),
            "url": url, "published_at": published_at,
            "desks": list(publisher.desks), "access": publisher.access,
            "verified": False, "eligible_for_rag": False}
