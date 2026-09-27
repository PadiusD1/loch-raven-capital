"""Paper dividend accrual (2026-06-10 absence audit).

A live account RECEIVES dividends; paper books that don't quietly understate
returns ~1-2%/yr on the ETF-heavy sleeves (VOO/QQQ/GLD/SPY/CSCO/TXN are all
payers) and drift from the real account on identical positions in live mode.

Credits on/after the EX-DATE — the price drops by the dividend on ex-date,
so crediting cash that day keeps sleeve equity continuous (the economically
correct paper model even though real cash settles on pay-date). Best effort
from the vendor's dividend calendar; every credit journaled; deduplicated
forever in state/dividends_paid.json; a few days of lookback catch ex-dates
missed while the PC was off.
"""
from datetime import datetime, timezone

import config
from firm import state

# 14 (was 5): a 5-day window was SHORTER than a long-weekend-plus-outage, so an
# ex-date the PC slept through aged out before any wrap ran and the dividend was
# silently never credited. dividends_paid.json dedups forever, so a wider lookback
# is free (change-audit verify 2026-06-15).
LOOKBACK_DAYS = 14


def credit_dividends() -> int:
    """Credit pending dividends to every paper equity position. Returns the
    number of credits booked."""
    import yfinance as yf
    paid = state.load("dividends_paid", {"keys": []})
    seen = set(paid.get("keys", []))
    today = datetime.now(timezone.utc).date()
    n = 0
    with state.SLEEVE_LOCK:
        for sleeve in config.SLEEVES:
            s = state.get_sleeve(sleeve)
            changed = False
            for sym, p in s.get("positions", {}).items():
                if (p.get("instrument") != "equity"
                        or p.get("opened_mode", "paper") != "paper"):
                    continue
                try:
                    divs = yf.Ticker(sym).dividends
                except Exception:
                    continue
                if divs is None or not len(divs):
                    continue
                for ts_, amt in divs.tail(4).items():
                    exd = ts_.date()
                    if exd > today or (today - exd).days > LOOKBACK_DAYS:
                        continue
                    key = f"{sleeve}|{sym}|{exd.isoformat()}"
                    if key in seen:
                        continue
                    credit = round(float(amt) * p["quantity"], 2)
                    if credit <= 0:
                        continue
                    s["cash"] += credit
                    seen.add(key)
                    changed = True
                    n += 1
                    state.journal("dividend_credited", sleeve=sleeve, symbol=sym,
                                  ex_date=exd.isoformat(), per_share=float(amt),
                                  qty=p["quantity"], credit=credit)
            if changed:
                state.save_sleeve(sleeve, s)
    paid["keys"] = sorted(seen)[-800:]
    state.save("dividends_paid", paid)
    return n
