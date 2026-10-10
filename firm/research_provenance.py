"""Source provenance gate for externally collected research.

This module validates *metadata*, not the truth of a source's claims.
Use before adding external material to a retrieval index. No network calls.
"""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlparse

REQUIRED = ("desk", "source_url", "published_at", "observed_at", "asof",
            "title", "body")
DESKS = frozenset(("fundamental", "technical", "sentiment", "macro", "quant"))


def _time(value: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("Missing timestamp")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Timestamps must include timezone")
    return dt.astimezone(timezone.utc)


def validate_research(record: dict, decision_at: str | None = None) -> dict:
    """Return a normalized, independently auditable research record.

    Reject sources published or observed after a historical decision. A caller
    must separately verify numerical claims and original publisher authority.
    """
    if not isinstance(record, dict):
        raise ValueError("Research record must be an object")
    missing = [key for key in REQUIRED if not record.get(key)]
    if missing:
        raise ValueError("Missing fields: " + ", ".join(missing))
    if record["desk"] not in DESKS:
        raise ValueError("Unknown research desk")
    parsed = urlparse(record["source_url"])
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Source must have an HTTPS publisher URL without credentials")
    published = _time(record["published_at"])
    observed = _time(record["observed_at"])
    asof = _time(record["asof"])
    if published > observed:
        raise ValueError("Cannot observe research before publication")
    if asof > observed:
        raise ValueError("Research as-of date cannot exceed observation date")
    if decision_at:
        decision = _time(decision_at)
        if published > decision or observed > decision or asof > decision:
            raise ValueError("Research was unavailable at decision time")
    if not isinstance(record["body"], str) or len(record["body"].strip()) < 20:
        raise ValueError("Research body too short")
    return {**record, "published_at": published.isoformat(),
            "observed_at": observed.isoformat(), "asof": asof.isoformat(),
            "source_host": parsed.hostname.lower(), "provenance_checked": True,
            "fact_checked": False}
