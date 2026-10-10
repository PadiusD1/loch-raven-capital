"""Strict read-only brokerage research interface.

Uses the existing configured Public client; no orders, account mutation or
credential export. Research failures never alter the live execution path.
"""
from __future__ import annotations

from datetime import datetime, timezone
from firm import broker

def snapshot() -> dict:
    """Read portfolio exposure without exporting credentials or account number."""
    result = broker.live_account()
    positions = result.get("positions", [])
    return {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "provider": "public.com",
        "positions": [
            {"symbol": str(p.get("symbol", "")),
             "quantity": float(p.get("quantity") or 0),
             "value": float(p.get("value") or 0)}
            for p in positions if p.get("symbol")
        ],
        "cash_buying_power": result.get("cash_buying_power"),
        "options_buying_power": result.get("options_buying_power"),
        "research_only": True,
    }

def capabilities() -> dict:
    """Explicitly avoid claiming unverified Premium API entitlements."""
    return {"portfolio": "supported_by_existing_adapter",
            "equity_quotes": "supported_by_existing_broker",
            "premium_analyst_reports": "unverified",
            "premium_fundamentals": "unverified",
            "options_chain": "requires_sdk_entitlement_test",
            "order_permission": "not_used"}
