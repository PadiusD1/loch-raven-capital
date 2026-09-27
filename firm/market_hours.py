"""US equity market-hours gate. Live orders only route during regular
trading hours — a stop that 'triggers' at 7 pm on a stale print must wait
for the open and re-evaluate, not queue a blind overnight order."""
from datetime import datetime, time
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
OPEN_T = time(9, 30)
CLOSE_T = time(16, 0)
EARLY_CLOSE_T = time(13, 0)   # NYSE half-day close

# NYSE full-closure holidays (observed dates)
HOLIDAYS = {
    # 2026
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    # 2027
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
    "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}

# NYSE EARLY CLOSES — 1:00 pm ET half days (2026-07-25 audit). The gate knew
# only full closures, so on 2026-11-27 at 14:30 ET market_is_open() returned
# True: LIVE would have sent a CORE-session order into a shut market (execute
# returns PENDING, sell_fail_count resets, the monitor believes the position is
# protected) while PAPER booked an instant fill at a 13:00 close — the exact
# paper/live divergence the bell gate exists to prevent.
# Half days here: the day after Thanksgiving, and Christmas Eve when Dec 24
# falls on a weekday that is not itself a holiday. (July 3 is NOT listed for
# either year — 2026-07-03 is the observed Independence Day full closure and
# 2027's July 4 falls on a Sunday, observed Monday 2027-07-05, which carries no
# early close. Add a July-3 entry only for years where July 4 lands Tue-Fri.)
EARLY_CLOSES = {
    "2026-11-27",   # day after Thanksgiving (Thanksgiving 2026-11-26)
    "2026-12-24",   # Christmas Eve, Thursday (2026-12-25 is the holiday)
    "2027-11-26",   # day after Thanksgiving (Thanksgiving 2027-11-25)
    # 2027-12-24 is NOT here: Christmas 2027 falls Saturday, so 12-24 is the
    # OBSERVED full holiday (already in HOLIDAYS above), not a half day.
}

# Last date HOLIDAYS + EARLY_CLOSES are known-good. Past this the tables are
# silent, and "no entry" would otherwise mean "every weekday is a full normal
# session" — trading blind through unknown holidays and half days. Extend BOTH
# tables (and this date) each year; `calendar_expired()` makes the gap loud.
CALENDAR_THROUGH = "2027-12-31"


EXT_PRE_T = time(8, 30)    # protective exits may route from 8:30 pre-market
EXT_POST_T = time(20, 0)   # ... and through the 4:00-8:00 pm after-hours session
EARLY_CLOSE_POST_T = time(17, 0)   # half days: after-hours ends at 5:00 pm ET

_horizon_warned = False


def session_close(now: datetime | None = None) -> time:
    """The ET time regular trading ENDS on this date — 13:00 on an NYSE half
    day, 16:00 otherwise (2026-07-25 audit). Every close-time read goes through
    here so the early-close table can never be honored in one gate and ignored
    in another."""
    now_et = (now or datetime.now(ET)).astimezone(ET)
    return (EARLY_CLOSE_T if now_et.strftime("%Y-%m-%d") in EARLY_CLOSES
            else CLOSE_T)


def is_early_close(now: datetime | None = None) -> bool:
    """True on an NYSE 1:00 pm ET half day (the calendar's own answer, so
    callers never hard-code the dates)."""
    return session_close(now) == EARLY_CLOSE_T


def calendar_expired(now: datetime | None = None) -> bool:
    """True once the date has run past the hand-maintained holiday/early-close
    tables. Callers that care about calendar TRUTH (health checks, the operator
    dashboard) can surface this; the gates themselves keep working off the
    weekday rule so protective exits never stop routing — but they no longer do
    it silently (2026-07-25 audit: past 2027-12-24 every holiday would have
    become an ordinary trading day with nothing said)."""
    now_et = (now or datetime.now(ET)).astimezone(ET)
    return now_et.strftime("%Y-%m-%d") > CALENDAR_THROUGH


def _warn_calendar_expired(day: str) -> None:
    """Say it ONCE per process — loudly, then never again (a per-call journal
    line would flood: these gates are polled every sweep)."""
    global _horizon_warned
    if _horizon_warned:
        return
    _horizon_warned = True
    _msg = (f"The NYSE holiday/early-close tables in firm/market_hours.py end "
            f"{CALENDAR_THROUGH} but today is {day}. Until they are extended, "
            "every weekday is being treated as a full 9:30-16:00 session — a "
            "holiday or 1:00 pm half day inside this gap would route orders "
            "into a closed market (live) or book phantom fills (paper). "
            "Extend HOLIDAYS + EARLY_CLOSES + CALENDAR_THROUGH.")
    try:
        from firm import state
        state.journal("market_calendar_expired", day=day,
                      through=CALENDAR_THROUGH)
    except Exception:
        pass
    try:
        from firm import alerts
        alerts.alert("MARKET CALENDAR EXPIRED", _msg)
    except Exception:
        pass


def _is_trading_day(now_et: datetime) -> bool:
    day = now_et.strftime("%Y-%m-%d")
    if day > CALENDAR_THROUGH:
        # Fail LOUD, not silent. Refusing to trade at all past the horizon would
        # also freeze protective EXITS (strictly more dangerous than an extra
        # session), so the weekday rule still answers — but the operator is told
        # the calendar is stale the first time it matters.
        _warn_calendar_expired(day)
    return now_et.weekday() < 5 and day not in HOLIDAYS


def is_trading_day(now: datetime | None = None) -> bool:
    """Public trading-day gate (weekend + NYSE holiday aware). Day-P&L anchors,
    the dashboard's TODAY line and the weekend health checks all key off this —
    a calendar day with no session must never mint a new 'day' (2026-07-04:
    anchors rolling on the Jul-3 holiday + Saturday showed phantom day P&L,
    'Day +$8' with the market closed)."""
    return _is_trading_day((now or datetime.now(ET)).astimezone(ET))


def market_is_open(now: datetime | None = None) -> bool:
    # Close time is per-DATE, not a constant: an NYSE half day ends 13:00 ET
    # (2026-07-25 audit — see EARLY_CLOSES).
    now_et = (now or datetime.now(ET)).astimezone(ET)
    return _is_trading_day(now_et) and OPEN_T <= now_et.time() < session_close(now_et)


def code_reload_should_defer(force: bool = False) -> bool:
    """A non-urgent CODE-RELOAD restart should DEFER while regular trading is
    open — it briefly pauses the exit monitors, and a code change that only
    affects the next research cycle must never interrupt live position
    monitoring (operator 2026-06-23: an operator restarted mid-session on a
    wrong 'market is closed' belief; this moves the check off fallible human
    judgment onto the fund's own clock). CRASH recovery never uses this —
    keepalive relaunches a DOWN process directly, not through the restart
    endpoint — so a crashed fund still restarts instantly in-hours. `force`
    overrides for a genuinely urgent in-hours reload. Fail-OPEN: if the clock
    check errors, allow the restart (availability beats the guard)."""
    if force:
        return False
    try:
        return bool(market_is_open())
    except Exception:
        return False


def session_phase(now: datetime | None = None) -> str:
    """WHICH session we are in, by name — one of:

        "pre_market"    08:30-09:30 ET
        "market_hours"  09:30-16:00 ET (to 13:00 on an NYSE half day)
        "after_hours"   close-20:00 ET (close-17:00 on a half day)
        "closed"        everything else, incl. weekends and holidays

    Operator 2026-08-21: the firm had exactly two booleans, market_is_open()
    and extended_session_open(), and the second lumps PRE-MARKET together with
    AFTER-HOURS. Every message built from it therefore said things like
    "session live (regular or extended)", which tells the reader neither which
    session it is nor which side of the close they are on — and that ambiguity
    is not cosmetic: a reconciliation skip at 08:45 (pre-market, yesterday's
    close is stale) and one at 16:45 (after-hours, today's close is fresh) mean
    opposite things and want opposite responses. Name the phase.

    The booleans stay as they are — they encode ROUTING permission (protective
    exits may route in both extended windows) and that grouping is correct for
    routing. This is the REPORTING view, and the two questions are different."""
    now_et = (now or datetime.now(ET)).astimezone(ET)
    if not _is_trading_day(now_et):
        return "closed"
    t = now_et.time()
    close_t = session_close(now_et)
    post_end = EARLY_CLOSE_POST_T if close_t == EARLY_CLOSE_T else EXT_POST_T
    if EXT_PRE_T <= t < OPEN_T:
        return "pre_market"
    if OPEN_T <= t < close_t:
        return "market_hours"
    if close_t <= t < post_end:
        return "after_hours"
    return "closed"


PHASE_LABEL = {"pre_market": "PRE-MARKET", "market_hours": "MARKET HOURS",
               "after_hours": "AFTER HOURS (post-close)", "closed": "MARKET CLOSED"}


def session_phase_label(now: datetime | None = None) -> str:
    """Human-facing name for the current phase, for alerts and journal lines."""
    return PHASE_LABEL.get(session_phase(now), "MARKET CLOSED")


def extended_session_open(now: datetime | None = None) -> bool:
    """Extended-hours window where RISK-REDUCING equity orders may route:
    8:30-9:30 pre-market and 4:00-8:00 pm after-hours (limit orders only,
    EXTENDED session flag). New risk is never added outside core hours.

    On an NYSE half day the post window starts at the 13:00 close and ends at
    17:00, not 20:00 (2026-07-25 audit): the after-hours session is shortened
    with the trading day, and claiming 17:00-20:00 is routable would let a stop
    'protect' into a session that isn't there."""
    now_et = (now or datetime.now(ET)).astimezone(ET)
    if not _is_trading_day(now_et):
        return False
    t = now_et.time()
    close_t = session_close(now_et)
    post_end = EARLY_CLOSE_POST_T if close_t == EARLY_CLOSE_T else EXT_POST_T
    return (EXT_PRE_T <= t < OPEN_T) or (close_t <= t < post_end)


def last_regular_close(now: datetime | None = None) -> datetime | None:
    """ET datetime of the most recent COMPLETED regular session's close (16:00,
    or 13:00 on a half day). Today counts once its close has passed. None if no
    trading day is found in the last 14 calendar days (2026-09-01; used by the
    'exit monitor' check so a closed market cannot excuse a sweep that predates
    the last close — IG #72/#78)."""
    from datetime import timedelta
    now_et = (now or datetime.now(ET)).astimezone(ET)
    for back in range(0, 15):
        d = now_et - timedelta(days=back)
        if not _is_trading_day(d):
            continue
        close_dt = datetime.combine(d.date(), session_close(d), tzinfo=ET)
        if close_dt <= now_et:
            return close_dt
    return None
