"""Execution desk — Public.com order routing (live) + full paper simulation.

LIVE-MODE CONTRACT (the part that keeps books honest with real money):
  * Orders are never assumed filled. A live order becomes a PENDING record;
    cash is reserved at placement. `settle_pending()` polls the broker and
    books ONLY actual fills at the ACTUAL average fill price — partial fills
    book the filled part and refund the rest.
  * Live orders route only during regular market hours (market_hours gate),
    plus risk-reducing EQUITY sells in the 8:30/16:00 extended session. PAPER
    obeys the identical window for both sides (2026-07-25 audit: the gate
    covered paper BUYS only, so paper stops filled on a Saturday at Friday's
    marks while live refused them — flattering every stop in the ledger).
  * A position opened in one mode can never be closed by an order in the
    other mode — flipping modes can't touch the wrong book (or the scalper's
    real shares, which live in the same account).
Paper mode simulates instant fills at live quotes, same accounting code.
"""
import math
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import yfinance as yf

import config
from firm import data_engine, market_hours, state

PENDING_MAX_AGE_H = 26   # a DAY order is terminal by next morning; past this = stuck

_client = None


def get_client():
    global _client
    if _client is None:
        from public_api_sdk import (ApiKeyAuthConfig, PublicApiClient,
                                    PublicApiClientConfiguration)
        _client = PublicApiClient(
            ApiKeyAuthConfig(api_secret_key=config.PUBLIC_API_KEY),
            config=PublicApiClientConfiguration(
                default_account_number=config.PUBLIC_ACCOUNT_NUMBER),
        )
        # (2026-09-02 full audit, live-order-path-1) BOUND THE TRANSPORT. The
        # SDK ships timeout=30 / max_retries=3 / backoff 0.3 and PublicApi-
        # ClientConfiguration exposes neither, so a slow-but-not-dead endpoint
        # could take ~30s x 4 + backoff on ONE call. Every quote call runs
        # inside state.SLEEVE_LOCK (monitor._check_equity_sleeve /
        # _check_calls_sleeve), so that stalls every stop in all four sleeves,
        # at the 1 s options cadence, exactly when a degraded network means a
        # fast tape. The lever is the ApiClient INSTANCE attrs consumed by
        # _retry_non_safe -- NOT the mounted HTTPAdapter, whose Retry excludes
        # POST (allowed_methods HEAD/GET/OPTIONS) and so never governed these.
        # Fail fast and let the next sweep retry: a missed quote is a skipped
        # tick, a held lock is an unprotected book.
        for _obj in (getattr(_client, "api_client", None),
                     getattr(getattr(_client, "auth_manager", None),
                             "api_client", None)):
            if _obj is None:
                continue
            try:
                _obj.timeout = int(getattr(config, "PUBLIC_API_TIMEOUT_S", 6))
                _obj._max_retries = int(getattr(config, "PUBLIC_API_MAX_RETRIES", 1))
                _obj._backoff_factor = 0.1
            except Exception:
                pass
    return _client


def live_account() -> dict:
    client = get_client()
    portfolio = client.get_portfolio()
    bp = portfolio.buying_power
    positions = []
    for pos in portfolio.positions:
        positions.append({
            "symbol": pos.instrument.symbol if pos.instrument else "?",
            "quantity": float(pos.quantity or 0),
            "value": float(pos.current_value or 0),
        })
    return {
        "cash_buying_power": float(bp.cash_only_buying_power) if bp else 0.0,
        "options_buying_power": float(bp.options_buying_power) if bp else 0.0,
        "positions": positions,
    }


# ── OSI symbol (Public.com format, no padding) ───────────────────────────────

def build_osi(ticker: str, expiration: str, contract_type: str, strike: float) -> str:
    exp = datetime.strptime(expiration, "%Y-%m-%d").strftime("%y%m%d")
    ct = "C" if contract_type.upper().startswith("C") else "P"
    return f"{ticker}{exp}{ct}{str(int(round(strike * 1000))).zfill(8)}"


# ── Option selection / pricing ────────────────────────────────────────────────

def pick_call(ticker: str, otm_pct: float, min_dte: int, max_dte: int) -> dict | None:

    # LIVE PARITY (operator 2026-07-16): Public disables options trading on
    # many listed names — a paper contract live could never place is a fake
    # edge. Single choke point for both the plan + open paths; fails OPEN
    # when the universe snapshot carries no options data.
    from firm import tradable as _trd
    if not _trd.options_enabled(ticker):
        state.journal("options_skipped_not_tradable", symbol=ticker)
        return None
    try:
        tk = yf.Ticker(ticker)
        spot = data_engine.last_price(ticker)
        if spot <= 0:
            return None
        today = datetime.now(timezone.utc).date()
        target_exp = None
        for exp_str in tk.options or []:
            exp = datetime.strptime(exp_str, "%Y-%m-%d").date()
            if min_dte <= (exp - today).days <= max_dte:
                target_exp = exp_str
                break
        if not target_exp:
            return None
        calls = tk.option_chain(target_exp).calls
        target_strike = spot * (1 + otm_pct / 100)
        calls = calls.assign(dist=(calls["strike"] - target_strike).abs()).sort_values("dist")
        for _, row in calls.head(5).iterrows():
            bid = float(row.get("bid") or 0)
            ask = float(row.get("ask") or 0)
            last = float(row.get("lastPrice") or 0)
            mid = (bid + ask) / 2 if bid > 0 and ask > 0 else last
            oi = float(row.get("openInterest") or 0)
            if mid > 0.05 and (oi >= 50 or bid > 0):
                spread_pct = round((ask - bid) / mid * 100, 1) if bid > 0 and ask > 0 else None
                return {"ticker": ticker, "expiration": target_exp,
                        "strike": float(row["strike"]), "contract_type": "CALL",
                        "mid": round(mid, 2), "spot": round(spot, 2),
                        "spread_pct": spread_pct,
                        "osi": build_osi(ticker, target_exp, "CALL", float(row["strike"]))}
    except Exception:
        return None
    return None


def pick_put(ticker: str, otm_pct: float, min_dte: int, max_dte: int) -> dict | None:
    """OTM PUT nearest to otm_pct BELOW spot — the short-desk mirror of pick_call
    (a long put gains as the underlying FALLS). Same DTE window + liquidity gate
    (mid > $0.05 and OI>=50 or a live bid), same return shape with contract_type
    'PUT' and a P-tagged OSI. Operator 2026-07-07."""
    # LIVE PARITY (operator 2026-07-16): Public disables options trading on
    # many listed names — a paper contract live could never place is a fake
    # edge. Single choke point for both the plan + open paths; fails OPEN
    # when the universe snapshot carries no options data.
    from firm import tradable as _trd
    if not _trd.options_enabled(ticker):
        state.journal("options_skipped_not_tradable", symbol=ticker)
        return None
    try:
        tk = yf.Ticker(ticker)
        spot = data_engine.last_price(ticker)
        if spot <= 0:
            return None
        today = datetime.now(timezone.utc).date()
        target_exp = None
        for exp_str in tk.options or []:
            exp = datetime.strptime(exp_str, "%Y-%m-%d").date()
            if min_dte <= (exp - today).days <= max_dte:
                target_exp = exp_str
                break
        if not target_exp:
            return None
        puts = tk.option_chain(target_exp).puts
        target_strike = spot * (1 - otm_pct / 100)          # OTM put sits BELOW spot
        puts = puts.assign(dist=(puts["strike"] - target_strike).abs()).sort_values("dist")
        for _, row in puts.head(5).iterrows():
            bid = float(row.get("bid") or 0)
            ask = float(row.get("ask") or 0)
            last = float(row.get("lastPrice") or 0)
            mid = (bid + ask) / 2 if bid > 0 and ask > 0 else last
            oi = float(row.get("openInterest") or 0)
            if mid > 0.05 and (oi >= 50 or bid > 0):
                spread_pct = round((ask - bid) / mid * 100, 1) if bid > 0 and ask > 0 else None
                return {"ticker": ticker, "expiration": target_exp,
                        "strike": float(row["strike"]), "contract_type": "PUT",
                        "mid": round(mid, 2), "spot": round(spot, 2),
                        "spread_pct": spread_pct,
                        "osi": build_osi(ticker, target_exp, "PUT", float(row["strike"]))}
    except Exception:
        return None
    return None


def option_mark(osi_ticker: str, expiration: str, strike: float,
                contract_type: str = "CALL") -> float:
    """Mid mark for one option leg (bid when the quote is one-sided; 0.0 = no
    tradable market, see the bid gate below). DIRECTION-AWARE (operator 2026-07-07):
    reads the PUTS chain for a put so the short desk's positions can be priced —
    the old calls-only read returned 0.0 for every put (silently unpriceable).
    Defaults to CALL so every existing calls_only caller is unchanged."""
    try:
        chain = yf.Ticker(osi_ticker).option_chain(expiration)
        rows = (chain.puts if str(contract_type).upper().startswith("P")
                else chain.calls)
        row = rows[rows["strike"] == strike]
        if row.empty:
            return 0.0
        bid = float(row.iloc[0].get("bid") or 0)
        ask = float(row.iloc[0].get("ask") or 0)
        # NO BID = NO MARK (2026-07-25 audit). The old fallback to `last` marked
        # a contract with no two-sided market at a print from a prior session:
        # bid 0.00 / ask 0.10 with last 1.20 read as 1.20, showed +20%, and the
        # ladder booked a TP1 scale-out at a price nobody was bidding. `last` is
        # a HISTORY field, not a quote — an option only has a mark if someone is
        # bidding for it. Returning 0.0 puts the position on the monitor's
        # unpriced path (_mark_fail -> CANNOT PRICE alert), which is the honest
        # state, and sleeve_equity falls back to the last GOOD mark for display.
        if bid <= 0:
            return 0.0
        return round((bid + ask) / 2, 2) if ask > 0 else round(bid, 2)
    except Exception:
        return 0.0


def option_quotes_public(positions: dict) -> dict[str, float]:
    """Batch live option quotes — bid for exit pricing, sub-second."""
    osis = [sym for sym, p in positions.items() if p["instrument"] == "option"]
    if not osis:
        return {}
    try:
        from public_api_sdk import InstrumentType, OrderInstrument
        client = get_client()
        quotes = client.get_quotes(
            instruments=[OrderInstrument(symbol=o, type=InstrumentType.OPTION)
                         for o in osis])
        out: dict[str, float] = {}
        positional_ok = len(quotes or []) == len(osis)
        for i, q in enumerate(quotes or []):
            sym = getattr(q, "symbol", None)
            if not sym and getattr(q, "instrument", None) is not None:
                sym = q.instrument.symbol
            if not sym and positional_ok:
                sym = osis[i]
            bid = float(q.bid) if getattr(q, "bid", None) else 0.0
            ask = float(q.ask) if getattr(q, "ask", None) else 0.0
            # `last` is deliberately NOT read here any more — see below.
            # Mark at MID for ladder math: entries book at mid, so marking at
            # bid would show every fresh call instantly down half the spread
            # and false-fire tight stops within seconds of entry.
            # But NO BID = NO MARK (2026-07-25 audit): the old `else last`
            # fallback priced a contract with no two-sided market off a stale
            # print (bid 0.00 / ask 0.10 / last 1.20 marked at 1.20, read +20%,
            # and the ladder scaled out at a price with no bid). The equity book
            # is protected from exactly this by _mark_stale + _stale_skip — that
            # flag is only ever written in the equity loop below, so the option
            # book's ONLY pricing guard is the monitor's `px <= 0` check, and a
            # stale `last` sailed straight through it. `last` is history, not a
            # quote: an option is priceable only while someone is bidding.
            if bid <= 0:
                continue          # unpriced -> monitor's _mark_fail path
            px = (bid + ask) / 2 if ask > 0 else bid
            if sym and px > 0:
                out[sym] = px
        return out
    except Exception:
        return {}


def option_quote_public(osi: str) -> float:
    """Fresh mid for ONE option contract — used to re-validate an entry after
    the blocking Opus exit-strategist call (the picked mid goes stale while
    the ladder is designed). 0.0 on any failure."""
    try:
        from public_api_sdk import InstrumentType, OrderInstrument
        quotes = get_client().get_quotes(
            instruments=[OrderInstrument(symbol=osi, type=InstrumentType.OPTION)])
        q = (quotes or [None])[0]
        if q is None:
            return 0.0
        bid = float(q.bid) if getattr(q, "bid", None) else 0.0
        ask = float(q.ask) if getattr(q, "ask", None) else 0.0
        # (2026-09-02 full audit, options-desk-first-open-2) NO BID, NO MARK.
        # This was the one pricing sibling never given the PRSU-lesson contract
        # that option_mark/option_quotes_public already carry: it fell back to
        # `last`, which on an illiquid contract can be hours or days old. It is
        # the SOLE pre-trade repricing check before the desk pays premium, and
        # the puts gate became reachable 2026-09-01, so the fund's first live
        # option could be sized on a stale print. A missing quote must read as
        # "unknown" (0.0) and make the desk skip, never as a price.
        return round((bid + ask) / 2, 4) if bid > 0 and ask > 0 else 0.0
    except Exception:
        return 0.0


def option_bid_public(osi: str) -> float:
    """Live BID for ONE option contract — the only price a long position can
    actually SELL into. 0.0 when the quote is missing or one-sided.

    Exit pricing must read this, not the mid: a protective sell limited at the
    mid sits ABOVE the only real bid and never fills (2026-07-25 audit)."""
    try:
        from public_api_sdk import InstrumentType, OrderInstrument
        quotes = get_client().get_quotes(
            instruments=[OrderInstrument(symbol=osi, type=InstrumentType.OPTION)])
        q = (quotes or [None])[0]
        if q is None:
            return 0.0
        bid = float(q.bid) if getattr(q, "bid", None) else 0.0
        return bid if bid > 0 else 0.0
    except Exception:
        return 0.0


# Protective OPTION exits escalate per chase attempt (2026-07-25 audit): the
# limit was the MID and the 150s chase re-fired at the MID, so bid 1.10 / ask
# 1.25 produced limit 1.18 — 8c above the only real bid — cancelled, re-fired at
# 1.18, forever, while the premium decayed. Even the "urgent" path was a no-op
# (1.175 * 0.995 rounds to 1.17, still inside the spread, and under ~$0.50 the
# 0.5% concession rounds away entirely).
OPTION_EXIT_MAX_CHASE = 2   # attempts 0 and 1 are limits; attempt 2+ goes MARKET


def _option_tick(px: float) -> float:
    """OCC minimum price increment: $0.05 at/above $3.00, $0.01 below."""
    return 0.05 if px >= 3.0 else 0.01


def _option_exit_limit(osi: str, mark: float, attempt: int,
                       urgent: bool) -> float | None:
    """Limit for a protective OPTION sell, escalating with the chase attempt.

    attempt 0 -> the live BID (marketable: a sell limit at the bid crosses)
    attempt 1 -> bid minus one tick (the bid moved while we were cancelling)
    attempt 2+ -> None = MARKET. Two failed chases is 300+ seconds of a stop
                  that has NOT protected anything; a dangling protective exit is
                  the more dangerous state than a market fill on the far side.

    No live bid (quote failure, or the book went one-sided between the mark and
    the route) falls back to the passed mark so this can never be LESS
    aggressive than the behaviour it replaces."""
    if attempt >= OPTION_EXIT_MAX_CHASE:
        return None
    bid = option_bid_public(osi)
    base = bid if bid > 0 else (mark * 0.995 if urgent else mark)
    if attempt >= 1:
        base -= _option_tick(base)
    return max(round(base, 2), 0.01)


# Quote BASIS per symbol from the latest equity_quotes_public pass (2026-09-01):
# "mid" = two-sided book inside the spread band, "last_wide" = book junk-wide so
# a real print was used, "last" = no two-sided book at all. The extended-session
# STOP decision accepts only "mid": a last print after the bell can be hours
# old, and a stop judged on it is a stop judged on a price that is not live.
_quote_basis: dict[str, str] = {}
_quote_refused_buf: dict[str, str] = {}
_quote_refused_ts = 0.0
_QUOTE_REFUSED_EVERY_S = 900.0


def _note_quote_refused(sym: str, why: str) -> None:
    """A refused quote is the guard WORKING — but silently refused quotes were
    invisible (IG 2026-08-31 #81: 'the bare-bid/crossed/junk-wide branches drop
    the symbol without journaling'). One aggregated line per 15-min window."""
    global _quote_refused_ts
    try:
        _quote_refused_buf[sym] = why
        now = datetime.now(timezone.utc).timestamp()
        if now - _quote_refused_ts >= _QUOTE_REFUSED_EVERY_S:
            _quote_refused_ts = now
            state.journal("quote_refused", n=len(_quote_refused_buf),
                          symbols=dict(sorted(_quote_refused_buf.items())[:20]))
            _quote_refused_buf.clear()
    except Exception:
        pass


def extended_stop_quote_ok(sym: str) -> bool:
    """May an extended-session STOP be judged on this symbol's latest Public
    quote? Only a two-sided mid inside the spread band qualifies."""
    return _quote_basis.get(sym) == "mid"


def equity_quotes_public(symbols: list[str]) -> dict[str, float]:
    """Real-time equity quotes from Public (includes pre/after-market). In
    live mode, exits priced off yfinance DAILY bars would use yesterday's
    close at 8:30 am — blind to overnight gaps. Mid pricing; {} on failure."""
    if not symbols:
        return {}
    try:
        from public_api_sdk import InstrumentType, OrderInstrument
        quotes = get_client().get_quotes(
            instruments=[OrderInstrument(symbol=s, type=InstrumentType.EQUITY)
                         for s in symbols])
        out: dict[str, float] = {}
        positional_ok = len(quotes or []) == len(symbols)
        for i, q in enumerate(quotes or []):
            sym = getattr(q, "symbol", None)
            if not sym and getattr(q, "instrument", None) is not None:
                sym = q.instrument.symbol
            if not sym and positional_ok:
                sym = symbols[i]
            bid = float(q.bid) if getattr(q, "bid", None) else 0.0
            ask = float(q.ask) if getattr(q, "ask", None) else 0.0
            last = float(q.last) if getattr(q, "last", None) else 0.0
            # A ONE-SIDED BOOK IS NOT A PRICE (operator 2026-07-28, PRSU).
            # This used to end in `(last or bid)`, so when the ask was missing
            # — routine for a thin small-cap at the open or after the bell —
            # the raw BID became the mark. The monitor then compared that bid
            # to the stop and sold. Four equity exits filled at prices that
            # NEVER TRADED: PRSU 39.08 and 37.51 against real day ranges of
            # 51.68-53.21 and 50.60-51.98, INNV 10.20 vs 10.93-11.63, STRZ
            # 22.49 vs 25.98-27.65 — together -$1,516, i.e. 47% of every
            # dollar this fund has lost, booked on quotes that did not exist.
            # (The 2026-07-26 extended-hours work read those same two PRSU
            # exits as proof that stops were disabled after the bell and
            # switched them ON for the extended session. The premise was
            # wrong — PRSU never fell — so that change pointed the stop loop
            # straight AT the bad prints. It stays, but only trustworthy
            # quotes now reach it.)
            # Trustworthy, in order: a two-sided mid, else a real last trade.
            # A bare bid is refused outright — the caller treats a missing
            # symbol as "no quote" and falls back to the daily bar, which is
            # a stale price but never an invented one.
            px = 0.0
            _basis = ""
            if bid > 0 and ask > 0 and ask >= bid:
                _mid = (bid + ask) / 2
                _spread_pct = (ask - bid) / _mid * 100
                if _spread_pct <= float(getattr(config, "QUOTE_MAX_SPREAD_PCT", 8.0)):
                    px, _basis = _mid, "mid"
                elif last > 0:
                    px, _basis = last, "last_wide"   # junk-wide book; a real print is better
            elif last > 0:
                px, _basis = last, "last"
            if sym and px > 0:
                out[sym] = px
                _quote_basis[sym] = _basis
            elif sym:
                _quote_basis.pop(sym, None)
                _note_quote_refused(
                    sym, ("crossed" if bid > 0 and ask > 0 else
                          "one-sided" if (bid > 0) != (ask > 0) else "no quote"))
        return out
    except Exception:
        return {}


def equity_prices(symbols: list[str]) -> dict[str, float]:
    """Cycle-time pricing for the sleeves: Public real-time quotes FIRST
    (gap-aware, immune to Yahoo throttling), yfinance batch for anything
    Public can't quote. A BUY/SELL verdict must never be silently skipped
    just because one data vendor is rate-limiting (2026-06-10: a full cycle
    placed zero orders while Yahoo called AMD 'possibly delisted')."""
    symbols = sorted({s for s in symbols if s})
    if not symbols:
        return {}
    out = equity_quotes_public(symbols)
    missing = [s for s in symbols if s not in out]
    if missing:
        out.update(data_engine.batch_last_prices(missing))
    return out


# mark_missing dedupe: a data outage journals once per sleeve per window with
# ALL missing symbols in ONE line — per-symbol lines were 41% of the journal's
# 3,000-line retention budget during the HI0-HI6 corruption (goal audit fix 6),
# and the in-process 15-min window resets with every headless process anyway
_mark_missing_ts: dict[str, float] = {}
_MARK_MISSING_EVERY_S = 900.0

# Rolling record of symbols that ended a fetch_marks pass UNPRICED (after every
# fallback), across all sleeves. Lets the CANNOT PRICE alert distinguish "this
# one name is broken (halt/delisting?)" from "the data vendor is down for the
# whole book" — on 2026-07-17 a process-wide yfinance outage fired a
# name-specific 'check for a halt/delisting' alert on IWM while 25 other
# positions were failing identically (the operator diagnosis text was wrong).
_recent_unpriced: dict[str, float] = {}


def _note_unpriced(symbols: list[str]) -> None:
    now = datetime.now(timezone.utc).timestamp()
    for s in symbols:
        _recent_unpriced[s] = now


def recent_unpriced(window_s: float = 900.0) -> list[str]:
    """Symbols that failed every pricing source within the last window."""
    now = datetime.now(timezone.utc).timestamp()
    for s in [s for s, t in _recent_unpriced.items() if now - t > window_s]:
        _recent_unpriced.pop(s, None)
    return sorted(_recent_unpriced)


def _journal_mark_missing(sleeve: str, symbols: list[str] | str, **extra) -> None:
    syms = sorted([symbols] if isinstance(symbols, str) else symbols)
    if not syms:
        return
    now = datetime.now(timezone.utc).timestamp()
    key = f"{sleeve}|{','.join(syms)}"
    if now - _mark_missing_ts.get(key, 0) < _MARK_MISSING_EVERY_S:
        return
    _mark_missing_ts[key] = now
    state.journal("mark_missing", symbol=",".join(syms), n=len(syms),
                  sleeve=sleeve, **extra)


def fetch_marks(sleeve_state: dict) -> dict[str, float]:
    """Marks for every open position. A HELD position must never be silently
    repriced at 0 — every batch miss gets an individual fallback attempt.
    Equities price from Public's real-time quotes FIRST in every mode
    (gap-aware, no Yahoo rate limits); yfinance daily bars are the fallback."""
    positions = sleeve_state["positions"]
    equity_syms = [s for s, p in positions.items() if p["instrument"] == "equity"]
    marks: dict[str, float] = {}
    # Real-time quotes ONLY while the market is open: after the bell,
    # drifting after-hours prints marked the book +2.30% while official
    # closes said +2.09% — the fund and SPY must speak the same convention
    # or every comparison line lies (2026-06-11 operator reconciliation)
    mkt_open = market_hours.market_is_open()
    # Staleness matters whenever a stop could ROUTE — that includes the extended
    # session (8:30-9:30 / 16:00-20:00), where equity SELLs do route (see
    # _place_live) but live Public quotes are NOT fetched below, so marks fall back
    # to a daily close. Gating staleness on regular hours only left extended-hours
    # stops firing on yesterday's close (change-audit verify, 2026-06-14).
    pricing_active = mkt_open or market_hours.extended_session_open()
    public_marks: dict[str, float] = {}
    # EXTENDED-HOURS PROTECTION (2026-07-26, operator-found via PRSU). The block
    # above correctly reasons that staleness matters whenever a stop could route
    # — and then only fetched live quotes during REGULAR hours, so every equity
    # mark in the extended session was stale BY CONSTRUCTION and monitor.py:318
    # skipped the protective exit on all of them. Net effect: 5.5 hours of every
    # trading day (8:30-9:30, 16:00-20:00 ET) with the stop loop structurally
    # disabled, and a name that broke down after the bell ran free until the next
    # session. The cost is measurable: PRSU exited at -28.4% and -29.2% against
    # configured stops of 15% and 12% — more than double, past even the 25%
    # vol-aware cap — and those two entries alone are 57% of every dollar this
    # fund has lost. The -29.2% only booked at all because a total yfinance
    # outage forced the Public rescue path, which happened to clear the flag.
    # Fix separates the two jobs a price does: the live quote becomes the STOP
    # DECISION price in extended hours, while the official daily close remains
    # the BOOK mark after the bell (2026-06-11 SPY reconciliation convention —
    # overwriting `marks` here would make every fund-vs-SPY line lie again).
    _ext_stops = bool(getattr(config, "EXTENDED_HOURS_STOPS", True))
    # (2026-08-13 operator decision) CLOSE-FEED PRIMACY: Public.com is the
    # PRIMARY price source for held names in EVERY session state — the feed
    # that was already the in-hours mark AND was quietly carrying the books as
    # the mark_rescued_public last resort becomes the FIRST attempt; yfinance
    # becomes the fallback. The 2026-06-11 reconciliation concern above
    # (after-hours prints vs official closes) is now governed by the nightly
    # mark_books_to_close snap (itself Public-primary per the same decision)
    # plus the third-source session-range audit, which catches a print that
    # never traded regardless of vendor. The stop-decision _live_px semantics
    # below keep their original EXTENDED_HOURS_STOPS gating unchanged.
    if equity_syms:
        public_marks = equity_quotes_public(equity_syms)
        marks = dict(public_marks)          # primary: the Public quote IS the mark
    missing_eq = [s for s in equity_syms if s not in marks]
    if missing_eq:
        marks.update(data_engine.batch_last_prices(missing_eq))
    _mm_syms: list[str] = []
    for sym in equity_syms:
        if sym not in marks:
            px = data_engine.last_price(sym)
            if px > 0:
                marks[sym] = px
            else:
                _mm_syms.append(sym)
        # Mark-staleness flag (2026-06-14 audit A2): during REGULAR hours the live
        # Public quote is expected; if a mark fell back to a yfinance DAILY close
        # (Public throttle/outage) it is yesterday's price wearing today's clothes
        # — the monitor must NOT fire a stop on it (false stop, or it hides a real
        # move). Only meaningful in-hours: after the bell the daily close IS the
        # correct mark and no stop routes anyway.
        p = positions.get(sym)
        if p is not None:
            p["_mark_stale"] = bool(pricing_active and sym in marks and sym not in public_marks)
            # The extended-session STOP DECISION price (see the block above). Set
            # only when a genuine live two-sided Public quote exists and the book
            # mark is the daily close — i.e. exactly the window that used to have
            # no protection. In regular hours it is None because `marks` already
            # IS the live quote and the monitor needs no second opinion.
            # (2026-08-13 operator decision) primacy flip fetches Public marks
            # in every session state, so the extended-session stop-decision
            # gate moves HERE unchanged: _live_px is set only while a stop
            # could actually route (extended session, _ext_stops on) — never
            # from an overnight last print.
            # (2026-09-01) ...and only a TWO-SIDED mid: a `last` print in the
            # extended session can be the 16:00 close wearing a live label.
            _lq = (public_marks.get(sym)
                   if (not mkt_open and pricing_active and _ext_stops
                       and extended_stop_quote_ok(sym)) else None)
            p["_live_px"] = _lq if isinstance(_lq, (int, float)) and _lq > 0 else None
    if _mm_syms and pricing_active:
        # LAST RESORT (2026-07-17 IWM outage): while a stop could still ROUTE
        # (regular or extended session), a live Public quote for a name every
        # yfinance path just missed beats a hole — an unpriced position is an
        # unprotectable one, and that evening the whole book sat unpriceable
        # for hours on a process-wide yfinance failure while Public's extended
        # quotes were one call away. After the bell the official close stays
        # the PREFERRED mark (2026-06-11 reconciliation convention): this
        # fires ONLY when yfinance returned nothing at all for the name. In
        # regular hours it doubles as a second Public attempt after a blip.
        _ext = equity_quotes_public(_mm_syms)
        for _es, _ep in _ext.items():
            if _ep > 0:
                marks[_es] = _ep
                _p2 = positions.get(_es)
                if _p2 is not None:
                    _p2["_mark_stale"] = False   # live quote — stops may act on it
        _rescued = sorted(s for s in _mm_syms if s in _ext and _ext[s] > 0)
        if _rescued:
            # distinct event (not mark_missing): the health desk counts
            # mark_missing as an outage signal, and a rescued mark is the
            # fallback WORKING — but the primary-vendor miss must stay visible
            _now_r = datetime.now(timezone.utc).timestamp()
            _rkey = f"rescued|{sleeve_state['sleeve']}|{','.join(_rescued)}"
            if _now_r - _mark_missing_ts.get(_rkey, 0) >= _MARK_MISSING_EVERY_S:
                _mark_missing_ts[_rkey] = _now_r
                state.journal("mark_rescued_public", symbol=",".join(_rescued),
                              n=len(_rescued), sleeve=sleeve_state["sleeve"])
        _mm_syms = [s for s in _mm_syms if s not in marks]
    if _mm_syms:
        _note_unpriced(_mm_syms)
        _journal_mark_missing(sleeve_state["sleeve"], _mm_syms)
    marks.update(option_quotes_public(positions))
    for sym, pos in positions.items():
        if pos["instrument"] == "option" and sym not in marks:
            underlying = pos.get("underlying", "")
            if not underlying:
                _journal_mark_missing(sleeve_state["sleeve"], sym,
                                      reason="position has no underlying field")
                continue
            m = option_mark(underlying, pos.get("expiration", ""), pos.get("strike", 0),
                            pos.get("contract_type", "CALL"))
            if m > 0 and not math.isnan(m):
                marks[sym] = m
    return marks


# ── Live order placement ──────────────────────────────────────────────────────

def _place_live(symbol: str, instrument_type: str, side: str, quantity: float,
                limit_price: float | None, open_close: str,
                extended: bool = False) -> dict:
    from public_api_sdk import (InstrumentType, OpenCloseIndicator,
                                OrderInstrument, OrderRequest, OrderSide,
                                OrderType, TimeInForce)
    from public_api_sdk.models import EquityMarketSession, OrderExpirationRequest

    client = get_client()
    itype = InstrumentType.OPTION if instrument_type == "option" else InstrumentType.EQUITY
    req = OrderRequest(
        order_id=str(uuid.uuid4()),
        instrument=OrderInstrument(symbol=symbol, type=itype),
        order_side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
        order_type=OrderType.LIMIT if limit_price else OrderType.MARKET,
        quantity=Decimal(str(quantity)),
        limit_price=Decimal(f"{limit_price:.2f}") if limit_price else None,
        open_close_indicator=(OpenCloseIndicator.OPEN if open_close == "open"
                              else OpenCloseIndicator.CLOSE) if instrument_type == "option" else None,
        expiration=OrderExpirationRequest(time_in_force=TimeInForce.DAY),
        equity_market_session=(EquityMarketSession.EXTENDED if extended
                               else EquityMarketSession.CORE) if instrument_type == "equity" else None,
    )
    order = client.place_order(req)
    return {"success": True, "order_id": str(order.order_id), "live": True}


# ── Underlying-referenced option stop (fix brief 2026-07-27 #7) ──────────────

# Live underlying quotes for the options monitor, memoized with a short TTL.
# The calls sweep runs every ~1s (config.CALLS_MONITOR_INTERVAL_S); adding an
# unthrottled second batched quote call per tick would double the hot loop's
# API traffic and it runs INSIDE the sleeve lock, the exact shape that starved
# the 1s loop into the options watchdog before (batch review R3/R4). A 15s TTL
# is far finer than any stop needs — the underlying-stop level is ~1.5 daily
# sigmas away, not a tick-scalping trigger — and collapses ~15 calls into one.
_und_quote_cache: dict[str, tuple[float, float]] = {}   # sym -> (ts, price)
UNDERLYING_QUOTE_TTL_S = 15.0


def underlying_marks(symbols: list[str],
                     max_age_s: float = UNDERLYING_QUOTE_TTL_S) -> dict[str, float]:
    """LIVE underlying quotes for the option book's invalidation stop.

    Deliberately Public-only, with NO daily-close fallback. A stop may only fire
    on a price the firm trusts — the equity path enforces exactly this via
    _mark_stale/_stale_skip, and a daily close standing in for a live quote is
    how a stop fires on yesterday's news. No quote -> the symbol is absent ->
    the monitor skips the underlying check for that position this sweep and the
    catastrophe premium backstop still covers it.
    """
    symbols = sorted({s for s in symbols if s})
    if not symbols:
        return {}
    now = datetime.now(timezone.utc).timestamp()
    out: dict[str, float] = {}
    for s in symbols:
        ts, px = _und_quote_cache.get(s, (0.0, 0.0))
        if px > 0 and now - ts <= max_age_s:
            out[s] = px
    missing = [s for s in symbols if s not in out]
    if missing:
        fresh = equity_quotes_public(missing)
        for s, px in fresh.items():
            if px > 0:
                _und_quote_cache[s] = (now, px)
                out[s] = px
    return out


def _underlying_vol_ann(underlying: str) -> float | None:
    """Annualized vol % of the underlying, computed the SAME way the research
    packet does (data_engine.compute_technicals: 63-day stdev of daily returns
    x sqrt(252)), so the option desk's sigma and the equity desk's sigma are the
    same number. fetch_history is hour-cached, so the entry path pays this at
    most once per name per hour. None on any failure."""
    try:
        import numpy as np
        closes = data_engine.fetch_history([underlying], period="6mo")
        s = closes[underlying].dropna() if underlying in closes else closes.iloc[:, 0].dropna()
        if len(s) < 30:
            return None
        vol = float(s.pct_change().dropna().tail(63).std() * np.sqrt(252)) * 100
        return round(vol, 1) if vol > 0 else None
    except Exception:
        return None


def underlying_stop_stamp(underlying: str, contract_type: str) -> dict:
    """Compute the underlying price level that INVALIDATES a long option thesis.

    Level = underlying price at entry -/+ MULT x its DAILY sigma (annualized
    vol / 15.9, the same conversion sleeves._vol_stops uses). DIRECTION-AWARE:
    a long CALL is invalidated by the underlying falling THROUGH the level, a
    long PUT by it rising through — the desk holds both since 2026-07-07 and a
    single below-entry level would have "breached" instantly on every winning
    put and dumped it. The multiplier is clamped into
    config.OPT_UNDERLYING_STOP_SIGMA_BOUNDS so no tuning can stamp a level
    tighter than 1.0 sigma (pure noise) or wider than 2.5.

    Returns {} on ANY failure (no vol, no quote) — the caller stamps nothing,
    the monitor then keeps the position's ORIGINAL tighter premium SL rather
    than the widened catastrophe backstop. Failure must never mean less
    protection.
    """
    try:
        px = (underlying_marks([underlying], max_age_s=0.0)
              or data_engine.batch_last_prices([underlying])).get(underlying, 0.0)
        vol = _underlying_vol_ann(underlying)
        if not px or px <= 0 or not vol:
            return {}
        lo, hi = getattr(config, "OPT_UNDERLYING_STOP_SIGMA_BOUNDS", (1.0, 2.5))
        mult = min(hi, max(lo, float(getattr(config, "OPT_UNDERLYING_STOP_SIGMA", 1.5))))
        daily_sigma_px = px * (vol / 15.9) / 100.0
        is_put = str(contract_type).upper().startswith("P")
        stop_px = px + daily_sigma_px * mult if is_put else px - daily_sigma_px * mult
        if stop_px <= 0:
            return {}
        return {
            "underlying_entry_px": round(px, 4),
            "underlying_stop_px": round(stop_px, 4),
            "underlying_stop_dir": "above" if is_put else "below",
            "underlying_vol_ann": vol,
            "underlying_stop_sigma": mult,
        }
    except Exception:
        return {}


# ── Booking (shared by paper instant-fills and live settled fills) ───────────

def _book_buy(sleeve_state: dict, *, symbol: str, quantity: float, price: float,
              instrument: str, meta: dict, mode: str) -> None:
    unit = 100 if instrument == "option" else 1
    positions = sleeve_state["positions"]
    pos = positions.get(symbol) or {
        "symbol": symbol, "instrument": instrument, "quantity": 0.0,
        "entry_price": price, "cost": 0.0, "peak_price": price,
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "opened_mode": mode,
        **{k: v for k, v in meta.items() if k not in ("open_close", "force", "reason")},
    }
    # Arm the underlying-referenced invalidation stop at ENTRY (fix brief
    # 2026-07-27 #7). Stamped here rather than on the monitor's first sweep for
    # two reasons: this is the only moment the ENTRY-time underlying price is
    # knowable, and the first sweep runs inside the 1s hot loop under the sleeve
    # lock, where a blocking vol/quote fetch is the known watchdog-starving
    # pattern. Only ever stamped on a genuinely NEW position — averaging into an
    # existing contract must not re-anchor (and so widen) a level the thesis
    # already set.
    if (instrument == "option" and not pos.get("cash_equitization")
            and not pos.get("underlying_stop_px") and pos["quantity"] == 0.0):
        _stamp = underlying_stop_stamp(pos.get("underlying") or "",
                                       pos.get("contract_type") or "CALL")
        if _stamp:
            pos.update(_stamp)
            state.journal("underlying_stop_armed", symbol=symbol,
                          underlying=pos.get("underlying"), **_stamp)
        else:
            # Loud, because the consequence is silent: no stamp means the
            # monitor keeps the tight premium SL and this contract is still
            # exposed to the spread/IV noise exits this brief exists to stop.
            state.journal("underlying_stop_unavailable", symbol=symbol,
                          underlying=pos.get("underlying"),
                          effect="premium SL stays tight (no widening)")
    total_qty = pos["quantity"] + quantity
    pos["entry_price"] = round(
        (pos["entry_price"] * pos["quantity"] + price * quantity) / total_qty, 4)
    pos["quantity"] = total_qty
    pos["cost"] = round(pos["cost"] + price * quantity * unit, 2)
    pos["last_price"] = price
    positions[symbol] = pos


def _book_sell(sleeve_state: dict, *, symbol: str, quantity: float, price: float,
               instrument: str, reason: str) -> None:
    unit = 100 if instrument == "option" else 1
    pos = sleeve_state["positions"].get(symbol)
    if not pos:
        return
    qty = min(quantity, pos["quantity"])
    sleeve_state["cash"] += price * qty * unit
    pnl = (price - pos["entry_price"]) * qty * unit
    pos["quantity"] = round(pos["quantity"] - qty, 6)
    pos["cost"] = round(pos["entry_price"] * pos["quantity"] * unit, 2)
    pos.pop("exit_pending", None)
    # The exit FILLED — the chase escalation ladder resets, so a later, unrelated
    # stop on what's left of this position starts at attempt 0 (the bid) instead
    # of inheriting a stale count and going straight to market (2026-07-25 audit).
    pos.pop("exit_chase_count", None)
    closed = {
        # Unique id (2026-06-10 week-sim finding): Windows datetime.now()
        # advances in ~16ms ticks, so two same-tick closes (alpha + core
        # exiting one SELL verdict together) shared a closed_at and the
        # learning ledger's dedupe silently dropped one of them
        "trade_id": uuid.uuid4().hex[:12],
        "symbol": symbol, "instrument": instrument, "quantity": qty,
        "entry": pos["entry_price"], "exit": price, "pnl": round(pnl, 2),
        "reason": reason, "closed_at": datetime.now(timezone.utc).isoformat(),
        "opened_at": pos.get("opened_at", ""),
        "reentry_after_stop": bool(pos.get("reentry_after_stop")),
        "strategy": pos.get("strategy", ""),
        "momentum_score": pos.get("momentum_score"),
        "sector": pos.get("sector", ""),
        "sector_tailwind": bool(pos.get("sector_tailwind")),
        "mode": pos.get("opened_mode", "paper"),
        # Verdict-at-ENTRY attribution (2026-06-10 audit): the verdict that
        # opened the trade, not whichever re-research verdict came last
        "rating": pos.get("rating"),
        "conviction": pos.get("conviction"),
        "thesis": (pos.get("thesis") or "")[:200],
        "depth": pos.get("depth", ""),
        "degraded": bool(pos.get("degraded")),
        # factor lenses at entry — realized P&L becomes factor-attributed
        "factors": pos.get("factors") or {},
        # annualized vol at entry (2026-07-13): lets the review bucket exit
        # quality by volatility regime and tune stops per-regime (MU vs COST)
        "vol_ann": pos.get("vol_ann"),
        "stop_pct": pos.get("stop_pct"),
        "trail_stop_pct": pos.get("trail_stop_pct"),
        # per-desk scores at entry (2026-07-01 goal audit, fix 4b): the ledger
        # previously back-filled these from the NEWEST research-history hit,
        # which mis-attributed re-researched names (TXN's row carried a later
        # degraded cycle's fabricated all-100 scores instead of its real
        # entry-day committee scores)
        "desk_scores": pos.get("desk_scores"),
    }
    # RE-ENTRY COOLDOWN CARRIER (fix brief 2026-07-27 #7). Both calls_only
    # trades were HPE: the desk stopped out and bought the SAME underlying
    # straight back, so one bad read cost the sleeve twice in an afternoon. The
    # gate that refuses the second entry belongs in sleeves.py (the desk), but
    # a gate can only be written against evidence that survives the close — the
    # position dict is deleted a few lines below and `closed` is what reaches
    # the durable ledger. So the stopped UNDERLYING and the timestamp ride on
    # the ledger row: `underlying` unconditionally (it was never carried, which
    # is why no options exit could be attributed to a name), plus a
    # stop_cooldown block whenever the exit was a stop rather than a take-profit
    # or an expiry write-off.
    if instrument == "option":
        closed["underlying"] = pos.get("underlying", "")
        _r = (reason or "").lower()
        if _r.startswith(("hard sl", "underlying stop", "catastrophe")):
            closed["stop_cooldown"] = {
                "underlying": pos.get("underlying", ""),
                "stopped_at": closed["closed_at"],
                "cooldown_days": getattr(config, "CALLS_REENTRY_COOLDOWN_DAYS", 10),
                "reason": reason,
            }
            state.journal("calls_reentry_cooldown",
                          underlying=pos.get("underlying", ""), symbol=symbol,
                          stopped_at=closed["closed_at"], reason=reason,
                          cooldown_days=getattr(config, "CALLS_REENTRY_COOLDOWN_DAYS", 10),
                          guidance="block a new long option in this underlying "
                                   "until the cooldown elapses (desk gate: sleeves.py)")
    sleeve_state.setdefault("closed_trades", []).append(closed)
    sleeve_state["closed_trades"] = sleeve_state["closed_trades"][-300:]
    if pos["quantity"] <= 0:
        del sleeve_state["positions"][symbol]


# ── Unified execute ───────────────────────────────────────────────────────────

def _paper_fill_price(side: str, price: float, instrument: str,
                      meta: dict, pos: dict) -> float:
    """Slippage model for simulated fills. Equities cross equity_bps per side;
    options cross option_spread_frac of the half-spread (entry meta carries
    spread_pct; exits read it back off the position). No spread data = no
    haircut — a forced $0 write-off stays a $0 write-off."""
    sl = config.PAPER_SLIPPAGE
    if price <= 0:
        return price
    if instrument == "option":
        spread = meta.get("spread_pct")
        if not isinstance(spread, (int, float)) or spread <= 0:
            spread = pos.get("spread_pct") if isinstance(
                pos.get("spread_pct"), (int, float)) else 0
        frac = sl.get("option_spread_frac", 0.0)
        if spread and spread > 0 and frac > 0:
            adj = spread / 200.0 * frac
            return round(price * (1 + adj if side == "buy" else 1 - adj), 4)
        return price
    bps = sl.get("equity_bps", 0.0)
    if bps > 0:
        adj = bps / 10000.0
        return round(price * (1 + adj if side == "buy" else 1 - adj), 4)
    return price


def _queue_open_entry(sleeve_state: dict, symbol: str, quantity: float,
                      price: float, meta: dict | None,
                      instrument: str, mode: str = "paper") -> None:
    """Stash a market-closed-blocked ENTRY as a queued-for-the-open intent
    (operator 2026-07-16: a cycle that finished 90s after the bell silently
    lost four fresh BUYs). EQUITY only — an option priced yesterday has a
    different ladder/greeks by the open; the calls desk re-decides instead.
    The monitor flushes intents at the next open (sleeves.
    flush_pending_open_entries) through the drift band + cash gates; an
    intent that can't fill honestly is re-queued for research, never dropped."""
    if instrument != "equity":
        state.journal("open_queue_skipped_option", symbol=symbol)
        return
    try:
        q = state.load("pending_open_entries", []) or []
        sleeve = sleeve_state.get("sleeve", "?")
        if any(e.get("symbol") == symbol and e.get("sleeve") == sleeve
               for e in q):
            return                            # already queued — idempotent
        # mode ships with the intent (AUDIT 2026-07-16: mode-blind intents
        # survived a paper->live flip and would flush as REAL orders — the
        # flush drops any intent whose mode is not the current mode)
        q.append({"sleeve": sleeve, "symbol": symbol,
                  "quantity": quantity, "thesis_price": price,
                  "meta": dict(meta or {}), "mode": mode,
                  "queued_at": datetime.now(timezone.utc).isoformat()})
        state.save("pending_open_entries", q)
        state.journal("entry_queued_for_open", sleeve=sleeve, symbol=symbol,
                      qty=quantity, thesis_price=round(price, 4))
    except Exception as e:
        state.journal("open_queue_error", symbol=symbol, error=str(e)[:100])


def execute(mode: str, sleeve_state: dict, *, symbol: str, side: str,
            quantity: float, price: float, instrument: str = "equity",
            meta: dict | None = None) -> dict:
    """Validate -> route -> account. Paper books instantly; live goes PENDING
    (cash reserved) and is booked by settle_pending() at actual fill prices."""
    meta = meta or {}
    unit = 100 if instrument == "option" else 1
    positions = sleeve_state["positions"]

    # ── ALL validation before any order can route ────────────────────────────
    if math.isnan(price) or price < 0 or quantity <= 0:
        return {"success": False, "error": f"bad price/qty {price}/{quantity}"}
    if price == 0 and not (side == "sell" and meta.get("force")):
        return {"success": False, "error": "zero price rejected"}

    if side == "sell":
        pos = positions.get(symbol)
        if not pos:
            return {"success": False, "error": "no position to sell"}
        if pos.get("exit_pending"):
            return {"success": False, "error": "exit order already pending"}
        if pos.get("opened_mode", "paper") != mode:
            state.journal("cross_mode_blocked", symbol=symbol, side=side,
                          position_mode=pos.get("opened_mode"), current_mode=mode)
            return {"success": False,
                    "error": f"position opened in {pos.get('opened_mode')} mode — "
                             f"refusing {mode} order (mode mismatch)"}
        quantity = min(quantity, pos["quantity"])
        if quantity <= 0:
            return {"success": False, "error": "nothing to sell"}
    notional = price * quantity * unit

    if side == "buy" and sleeve_state["cash"] < notional:
        return {"success": False, "error": "insufficient sleeve cash"}

    # Paper SELLS respect the bell too (2026-07-25 audit). This gate used to
    # cover BUYS ONLY, on the reasoning that "protecting the book never waits" —
    # but a paper sell does not protect anything, it books a fill at a mark the
    # market is not offering. Live refuses the same order below, so the two
    # books diverged exactly where it flatters us: on 2026-07-25, a SATURDAY,
    # paper exited OSCR at "hard stop -12.1%" and CON at "trailing stop -16.7%"
    # at 07:09 and 07:12 ET, both at FRIDAY's closing marks. Live would have
    # returned "market closed" and carried both positions through the weekend
    # gap. Every such exit understates the true cost of a stop, and the
    # resulting by_exit_reason post-exit-excess is what the Opus exit-ladder
    # designer learns from — the paper book was teaching the desk that stops are
    # cheaper than they are. The routing shape MIRRORS the live gate below
    # (core hours for everything; equity risk-reducing sells may also route the
    # 8:30/16:00 extended session; `force` never gets the extended window), so
    # paper and live refuse and allow the identical set of orders.
    # The error text keeps the "market closed" substring monitor._sell_or_escalate
    # matches on, so a blocked stop is treated as the gate working (no failure
    # escalation) and lands in its `stop_triggered_unrouted` journal line.
    if (mode == "paper" and side == "sell" and config.PAPER_ENFORCE_MARKET_HOURS
            and not market_hours.market_is_open()
            and not (instrument == "equity" and not meta.get("force")
                     and market_hours.extended_session_open())):
        state.journal("order_blocked_market_closed", symbol=symbol, side=side,
                      mode="paper", sleeve=sleeve_state.get("sleeve"),
                      instrument=instrument)
        return {"success": False,
                "error": "market closed — exit not routed; re-evaluates from "
                         "8:30 pre-market"}

    # Paper buys respect the bell like live ones — a 3am catch-up cycle must
    # not book entries at yesterday's close.
    if (mode == "paper" and side == "buy" and config.PAPER_ENFORCE_MARKET_HOURS
            and not market_hours.market_is_open()):
        # sleeve on the line (2026-07-25 audit): the routing auditors match a
        # block to an unplaced name by SYMBOL alone, so a sleeve-less block let
        # one sleeve's bell refusal be accepted as the explanation for another
        # sleeve's silently-dropped order — the detector chasing the standing
        # HPE silent-drop brief. Its sibling entry_queued_for_open already
        # carries the field.
        state.journal("order_blocked_market_closed", symbol=symbol, side=side,
                      mode="paper", sleeve=sleeve_state.get("sleeve"),
                      instrument=instrument)
        # QUEUED FOR THE OPEN (operator 2026-07-16: four fresh BUY verdicts
        # evaporated when a cycle finished 90s after the close — an entry the
        # committee decided must never silently die on the bell). The intent
        # is stashed; the monitor flushes it at the next open through the
        # drift band + cash gates (sleeves.flush_pending_open_entries).
        if instrument == "equity":
            _queue_open_entry(sleeve_state, symbol, quantity, price, meta,
                              instrument, mode)
            return {"success": False, "error": "market closed — entry QUEUED "
                                               "for the next open", "queued": True}
        # Options are NEVER queued (greeks/ladder stale by the open — the desk
        # re-decides); saying queued:True here was a lie (audit 2026-07-16).
        return {"success": False, "error": "market closed — option entry not "
                                           "queued; desk re-decides next cycle"}

    if mode == "live":
        # Core hours: everything routes. Extended hours (8:30 pre-market /
        # 4-8 pm): RISK-REDUCING equity sells only, as EXTENDED limit orders —
        # a stop must not wait until 9:30 when it can protect at 8:30.
        extended = False
        if not market_hours.market_is_open():
            if (instrument == "equity" and side == "sell"
                    and market_hours.extended_session_open()
                    and not meta.get("force")):
                extended = True
            else:
                # sleeve on the line (2026-07-25 audit — same finding as the
                # paper gate above): symbol-only matching let one sleeve's bell
                # block explain another sleeve's unplaced name.
                state.journal("order_blocked_market_closed", symbol=symbol,
                              side=side, mode="live",
                              sleeve=sleeve_state.get("sleeve"),
                              instrument=instrument)
                if side == "buy" and instrument == "equity":
                    # Same queued-for-the-open contract as paper (2026-07-16);
                    # options never queue — greeks stale by the open.
                    _queue_open_entry(sleeve_state, symbol, quantity, price,
                                      meta, instrument, mode)
                    return {"success": False, "error": "market closed — entry "
                            "QUEUED for the next open", "queued": True}
                return {"success": False, "error": "market closed — order not routed; "
                                                   "re-evaluates from 8:30 pre-market"}
        if side == "buy":
            try:
                acct = live_account()
                avail = (acct["options_buying_power"] if instrument == "option"
                         else acct["cash_buying_power"])
            except Exception as e:
                return {"success": False, "error": f"cannot verify live buying power: {e}"}
            if notional > avail - config.MIN_CASH_BUFFER:
                state.journal("live_buy_blocked", symbol=symbol,
                              needed=round(notional, 2), available=round(avail, 2))
                return {"success": False,
                        "error": f"live buying power ${avail:,.0f} < ${notional:,.0f} needed"}
        open_close = meta.get("open_close") or ("open" if side == "buy" else "close")
        # Which CHASE attempt this exit is being placed under (0 = first try).
        # The counter lives on the POSITION, not on the pending row: the chase
        # CANCELS the order and settle_pending drops the row, so the next sweep
        # re-fires a brand-new order — a row-local counter would read 0 every
        # time and the limit could never escalate (2026-07-25 audit). Copied
        # onto the row below for journal/forensic visibility. Cleared by
        # _book_sell when an exit actually fills.
        chase = 0
        if side == "sell":
            try:
                chase = int((positions.get(symbol) or {}).get("exit_chase_count", 0))
            except (TypeError, ValueError):
                chase = 0
        try:
            # URGENT exits (hard stops in a falling market) price THROUGH the
            # quote (-0.5%) for immediate fill — a flash crash must not be
            # chased politely at mid while the price runs away
            if meta.get("force"):
                limit = None
            elif side == "sell" and instrument == "option":
                # OPTIONS EXIT AT THE BID, NOT THE MID (2026-07-25 audit). The
                # mid is a mark, not a price anyone will pay: on bid 1.10 / ask
                # 1.25 the old limit of 1.18 sat 8c above the only real bid and
                # never filled, and the 150s chase re-fired the SAME 1.18 until
                # expiry while the premium decayed. Escalates per attempt.
                limit = _option_exit_limit(symbol, price, chase,
                                           bool(meta.get("urgent")))
            elif meta.get("urgent") and side == "sell":
                limit = round(price * 0.995, 2)
            else:
                limit = round(price, 2)
            res = _place_live(symbol, instrument, side, quantity, limit,
                              open_close, extended=extended)
        except Exception as e:
            return {"success": False, "error": f"live order failed: {e}"}
        # PENDING: reserve cash on buys, lock the position on sells; booking
        # happens only when settle_pending confirms actual fills.
        pending = {
            "order_id": res["order_id"], "symbol": symbol, "side": side,
            "quantity": quantity, "limit_price": price, "instrument": instrument,
            "meta": {k: v for k, v in meta.items()},
            "reserved": notional if side == "buy" else 0.0,
            "placed_at": datetime.now(timezone.utc).isoformat(),
            "chase_count": chase,
            # what actually went to the exchange (None = MARKET) — the row used
            # to record only `limit_price` = the MARK, so a post-mortem could
            # not tell what price the order was really resting at
            "routed_limit": limit,
        }
        if side == "buy":
            sleeve_state["cash"] -= notional
        else:
            positions[symbol]["exit_pending"] = res["order_id"]
        sleeve_state.setdefault("pending_orders", []).append(pending)
        state.journal("order_pending", mode=mode, sleeve=sleeve_state["sleeve"],
                      symbol=symbol, side=side, qty=quantity, price=price,
                      order_id=res["order_id"], limit=limit,
                      **({"chase": chase} if chase else {}))
        return {"success": True, "pending": True, "order_id": res["order_id"]}

    # ── Paper: instant simulated fill ─────────────────────────────────────────
    # Honest fills (2026-06-10 audit): live orders pay the spread; a paper
    # track record that fills at MID flatters the calls desk by the whole TP1
    # tier. Cross part of the spread and pay per-contract fees so paper P&L
    # is the number the live switch will actually deliver.
    px_fill = _paper_fill_price(side, price, instrument, meta,
                                positions.get(symbol, {}))
    fee = 0.0
    if instrument == "option" and price > 0:
        fee = config.PAPER_SLIPPAGE.get("option_fee_per_contract", 0.0) * quantity
    if side == "buy" and sleeve_state["cash"] < px_fill * quantity * unit + fee:
        # (2026-08-13 IG audit, PEB defect) this refusal journaled NOTHING —
        # an options open sized on the raw mid could pass every desk gate and
        # then die HERE against the slippage-adjusted fill, leaving zero trace
        # for the routing reconciler (which now allowlists this event).
        if instrument == "option":
            state.journal("options_open_skipped",
                          sleeve=sleeve_state.get("sleeve"), symbol=symbol,
                          reason="insufficient_cash_after_slippage",
                          need=round(px_fill * quantity * unit + fee, 2),
                          have=round(sleeve_state["cash"], 2))
        return {"success": False, "error": "insufficient sleeve cash (after slippage)"}
    res = {"success": True, "order_id": f"paper-{uuid.uuid4().hex[:10]}", "live": False}
    if side == "buy":
        sleeve_state["cash"] -= px_fill * quantity * (100 if instrument == "option" else 1)
        sleeve_state["cash"] -= fee
        _book_buy(sleeve_state, symbol=symbol, quantity=quantity, price=px_fill,
                  instrument=instrument, meta=meta, mode=mode)
    else:
        sleeve_state["cash"] -= fee
        _book_sell(sleeve_state, symbol=symbol, quantity=quantity, price=px_fill,
                   instrument=instrument, reason=meta.get("reason", ""))
    state.journal("order", mode=mode, sleeve=sleeve_state["sleeve"], symbol=symbol,
                  side=side, qty=quantity, price=px_fill, instrument=instrument,
                  order_id=res.get("order_id"),
                  **({"reason": meta["reason"]} if meta.get("reason") else {}))
    return res


# ── Live fill settlement ──────────────────────────────────────────────────────

_TERMINAL = {"FILLED", "CANCELLED", "QUEUED_CANCELLED", "REJECTED", "EXPIRED",
             "REPLACED"}
# Statuses that are NOT terminal but must never pin an exit forever (2026-07-25
# audit). The installed SDK defines UNKNOWN as its explicit fallback for any
# status string it does not recognise (OrderStatus._missing_), so a broker-side
# rename silently lands here — and PENDING_CANCEL is where our own chase-cancel
# parks while the venue processes it. Neither ever reaches _TERMINAL on its own,
# so `exit_pending` stayed set, execute() returned "exit order already pending"
# on every retry, and the stop was invisible. Settled after a grace window (see
# settle_pending) rather than on sight, so a cancel still in flight cannot be
# duplicated into an oversell.
_STUCK_STATUSES = {"UNKNOWN", "PENDING_CANCEL"}
STUCK_STATUS_GRACE_S = 300   # a real cancel resolves in seconds; 5 min = stuck
EXIT_CHASE_S = 150   # an unfilled protective SELL gets cancelled after this
                     # so the next sweep re-fires it at the CURRENT price —
                     # a fast-moving market can't leave a stop dangling all day


def settle_pending(sleeve_state: dict) -> int:
    """Poll every pending live order; book ACTUAL fills at ACTUAL prices.
    Partial fills book the filled part and refund the unfilled reservation.
    Returns the number of orders settled (removed from pending)."""
    pending = sleeve_state.get("pending_orders", [])
    if not pending:
        return 0
    client = get_client()
    settled = 0
    keep = []
    for p in pending:
        try:
            order = client.get_order(p["order_id"])
            status = str(getattr(order, "status", "")).split(".")[-1].upper()
            filled = float(getattr(order, "filled_quantity", 0) or 0)
            avg = float(getattr(order, "average_price", 0) or 0) or p["limit_price"]
        except Exception:
            keep.append(p)          # transient API failure — retry next sweep
            continue

        age_h = 0.0
        try:
            age_h = (datetime.now(timezone.utc)
                     - datetime.fromisoformat(p["placed_at"])).total_seconds() / 3600
        except (ValueError, TypeError):
            pass

        # STUCK STATUSES (2026-07-25 audit): UNKNOWN and PENDING_CANCEL were in
        # neither _TERMINAL nor any other branch, so an exit in either state
        # pinned `exit_pending` forever — execute() then answered every retry
        # with "exit order already pending" and monitor._sell_or_escalate
        # returned False WITHOUT journaling or escalating. A protective sell
        # could sit dead with no evidence anywhere. They are not force-settled
        # on sight either: PENDING_CANCEL means our cancel is in flight and the
        # order CAN still fill, so clearing instantly would let the monitor
        # re-fire a duplicate sell and oversell the position. Grace window, then
        # settle whatever actually filled — loudly.
        _stuck = status in _STUCK_STATUSES
        _stuck_s = 0.0
        if _stuck:
            _now_ts = datetime.now(timezone.utc).timestamp()
            if not p.get("stuck_since"):
                p["stuck_since"] = _now_ts
                state.journal("order_status_stuck", sleeve=sleeve_state["sleeve"],
                              symbol=p["symbol"], side=p["side"], status=status,
                              order_id=p["order_id"],
                              note="non-terminal broker status; force-settles "
                                   f"after {STUCK_STATUS_GRACE_S}s")
            _stuck_s = _now_ts - float(p["stuck_since"])
        elif p.get("stuck_since"):
            p.pop("stuck_since", None)      # resolved into a real status
        forced = bool(_stuck and _stuck_s >= STUCK_STATUS_GRACE_S)

        if status not in _TERMINAL and not forced and age_h < PENDING_MAX_AGE_H:
            # CHASE protective exits: if the market ran away from our limit,
            # cancel it; the cancellation settles (booking any partial fill),
            # exit_pending clears, and the monitor re-fires at the new price.
            # Entries are never chased — missing a buy costs nothing.
            if (p["side"] == "sell" and not p.get("cancel_sent")
                    and age_h * 3600 > EXIT_CHASE_S):
                try:
                    client.cancel_order(p["order_id"])
                    p["cancel_sent"] = True
                    # Escalate the NEXT attempt (2026-07-25 audit): the re-fire
                    # used to repeat the identical limit forever. The count is
                    # carried on the POSITION because this row disappears when
                    # the cancellation settles — see execute()'s `chase`.
                    p["chase_count"] = int(p.get("chase_count", 0)) + 1
                    _cpos = sleeve_state["positions"].get(p["symbol"])
                    if _cpos is not None:
                        _cpos["exit_chase_count"] = p["chase_count"]
                    state.journal("exit_chase_cancel", sleeve=sleeve_state["sleeve"],
                                  symbol=p["symbol"], order_id=p["order_id"],
                                  chase=p["chase_count"],
                                  note="unfilled exit cancelled to re-price "
                                       "(next attempt prices more aggressively)")
                except Exception:
                    pass
            keep.append(p)
            continue

        unit = 100 if p["instrument"] == "option" else 1
        if p["side"] == "buy":
            if filled > 0:
                actual_cost = avg * filled * unit
                refund = max(0.0, p["reserved"] - actual_cost)
                sleeve_state["cash"] += refund
                _book_buy(sleeve_state, symbol=p["symbol"], quantity=filled,
                          price=avg, instrument=p["instrument"],
                          meta=p.get("meta", {}), mode="live")
            else:
                sleeve_state["cash"] += p["reserved"]   # nothing filled — full refund
        else:
            pos = sleeve_state["positions"].get(p["symbol"])
            if filled > 0:
                _book_sell(sleeve_state, symbol=p["symbol"], quantity=filled,
                           price=avg, instrument=p["instrument"],
                           reason=p.get("meta", {}).get("reason", "") + " (live fill)")
            if pos is not None and p["symbol"] in sleeve_state["positions"]:
                sleeve_state["positions"][p["symbol"]].pop("exit_pending", None)

        state.journal("order_settled", sleeve=sleeve_state["sleeve"],
                      symbol=p["symbol"], side=p["side"], status=status,
                      filled=filled, avg_price=avg, requested=p["quantity"],
                      # `forced` = settled on the stuck-status grace, not on a
                      # terminal broker status. The unfilled remainder is NOT
                      # protected: the position is unlocked so the monitor can
                      # re-fire, which is the intended recovery.
                      **({"forced": True, "stuck_s": round(_stuck_s)}
                         if forced else {}))
        settled += 1
    sleeve_state["pending_orders"] = keep
    return settled


def sleeve_equity(sleeve_state: dict, marks: dict[str, float] | None = None,
                  from_fetch_marks: bool = False) -> float:
    """Cash + reserved pending-buy cash + marked value of positions.

    `from_fetch_marks` MUST be set by callers whose `marks` came from fetch_marks()
    — that is the only path where the per-position `_mark_stale` flag is fresh and
    consistent with the supplied marks. Other callers pass fresh, trusted prices
    (cycle equity_prices) or no marks at all (display), where the lingering flag
    must NOT be honored (adversarial review 2026-06-17: a fresh cycle quote was
    being discarded because a flag set by an earlier monitor sweep still read stale)."""
    total = sleeve_state["cash"]
    total += sum(p.get("reserved", 0) for p in sleeve_state.get("pending_orders", []))
    for sym, pos in sleeve_state["positions"].items():
        unit = 100 if pos["instrument"] == "option" else 1
        incoming = (marks or {}).get(sym)
        prior = pos.get("last_price")
        # FIX 1: a fetch_marks mark flagged _mark_stale is a daily-close FALLBACK
        # during a pricing-active window with no live Public quote (after-hours /
        # pre-market). It may be YESTERDAY's price wearing today's clothes — never
        # let it OVERWRITE a prior good last_price (that regressed the whole book one
        # trading day off-hours: Fri close read back as 'current' => fake -2.2% day,
        # 2026-06-16 operator). Value the position at the trusted prior mark and do
        # NOT persist the stale value. ONLY on the fetch_marks path (the flag is
        # authored there); a fresh cycle mark is trusted and clears the leftover flag.
        if from_fetch_marks and incoming is not None and pos.get("_mark_stale") and prior:
            mark = prior                      # value at the last GOOD mark, no regression
        else:
            mark = incoming or prior or pos["entry_price"]
            pos["last_price"] = mark          # only persist a trusted (or first-ever) mark
            if incoming is not None and not from_fetch_marks and pos.get("_mark_stale"):
                pos["_mark_stale"] = False     # a fresh, trusted quote recovers the mark
        pos["market_value"] = round(mark * pos["quantity"] * unit, 2)
        # peak_price drives the TRAILING STOP — never ratchet it up on a mark we
        # flagged stale (a stale daily close above the real peak would permanently
        # tighten the trail and force an early exit on the next live quote). Display
        # fields (last_price/market_value) may use the stale mark; the peak may not
        # (change-audit verify, 2026-06-14).
        _pk = pos.get("peak_price", 0)
        if mark > _pk and not pos.get("_mark_stale"):
            # Peak-poisoning guard (2026-07-02): a one-sweep mark jump beyond
            # PEAK_MAX_JUMP_PCT above the prior peak is a bad tick — do NOT
            # ratchet the trailing-stop high-water-mark on it (a poisoned peak
            # fired a FALSE -18% trailing stop on AMAT). This is the PRIMARY
            # advance site (marking runs constantly); silent here to avoid
            # flooding, monitor.reconcile_peaks corrects a genuine gap from real
            # daily OHLC and the monitor sweep journals the spike.
            if not _pk or mark <= _pk * (1 + config.PEAK_MAX_JUMP_PCT / 100):
                pos["peak_price"] = mark
        total += pos["market_value"]
    return round(total, 2)


def sleeve_risk_equity(sleeve_state: dict, equity: float | None = None) -> float:
    """Sleeve equity with cash-equitization PARKING VALUED AT COST — the basis the
    drawdown kill switch must measure (operator 2026-07-25).

    The kill switch asks "how much has this sleeve's DECISION-MAKING lost?", but
    it was fed plain sleeve_equity, which marks parking to market. On the calls
    desk that made the measure 86.9% RSP: a -14.5% RSP move would have HALTED the
    options desk having taken $0 of option losses, and every RSP rally ratcheted
    peak_equity upward, permanently tightening the desk's risk budget with gains
    the options book never earned (peak_equity 25,456.93 was set 2026-07-10 with
    zero options open).

    Parking is CAPITAL, so it belongs in the denominator — dropping it entirely
    would shrink the base so far that a single max-size option loss would trip the
    15% kill, inverting the intent recorded at config.max_position_pct ("premium
    at risk per trade must fit INSIDE the sleeve's kill switch"). Holding it at
    COST keeps the capital base intact while removing parking's mark-to-market
    P&L from the numerator: RSP moves now shift this by exactly $0, and an option
    loss shifts it in full.

    Pass `equity` when the caller already computed sleeve_equity() this sweep —
    that call is what refreshes each position's market_value, which this reads."""
    eq = equity if equity is not None else sleeve_equity(sleeve_state)
    drift = 0.0
    for pos in (sleeve_state.get("positions") or {}).values():
        if not pos.get("cash_equitization"):
            continue
        mv = pos.get("market_value")
        if mv is None:
            continue                      # never marked this sweep — no drift to strip
        cost = pos.get("cost")
        if cost is None:
            cost = (pos.get("entry_price") or 0) * pos.get("quantity", 0)
        drift += mv - cost
    return round(eq - drift, 2)


def next_friday_after(min_dte: int) -> str:
    d = datetime.now(timezone.utc).date() + timedelta(days=min_dte)
    while d.weekday() != 4:
        d += timedelta(days=1)
    return d.isoformat()
