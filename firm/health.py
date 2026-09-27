"""Health desk — keeps the firm healthy as the account grows, unattended.

Three jobs, all journaled and surfaced on the dashboard:

  health_report()   self-checks: cycle freshness, scan freshness, monitor
                    heartbeat, halted sleeves, journal size, Claude CLI,
                    broker connectivity
  rebalance_firm()  when one sleeve outgrows the firm (share outside the
                    12-40% band), free CASH flows from rich sleeves to poor
                    ones so every mandate keeps working capital — winners
                    fund the next opportunity instead of concentrating risk
  reconcile_live()  live mode only: compares internal books against the REAL
                    Public.com account and flags drift (manual trades,
                    changed fills) instead of trading on fiction
"""
import json
from datetime import datetime, time, timedelta, timezone

import config
from firm import brain, broker, market_hours, state


def _age_hours(iso: str) -> float:
    try:
        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat(iso)).total_seconds() / 3600
        return 1e9 if age < 0 else age   # clock jumped back = treat as stale
    except (ValueError, TypeError):
        return 1e9


def _halt_age_hours(iso: str) -> float:
    """Age of a HALT stamp, failing CLOSED (2026-07-25 audit finding 3).

    `_age_hours` returns 1e9 on a missing/unparseable timestamp — correct for
    "is this artifact STALE?" but exactly INVERTED for a cooling-off gate:
    `_age_hours(halt_ts) < 72` PASSES for a halt that carries no halt_ts at
    all, so the most severe halt in the system (the Opus CRO escalation in
    review_risk_events, which set halted/halt_reason/halt_equity and never
    halt_ts/halt_spy) was the one with NO cooling-off. Unknown age = 0.0 =
    still cooling off. Callers pair this with `_ensure_halt_ts`, which stamps
    a missing halt_ts so the clock starts at first observation instead of
    pinning the sleeve halted forever."""
    try:
        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat(iso)).total_seconds() / 3600
    except (ValueError, TypeError):
        return 0.0
    return 0.0 if age < 0 else age   # clock jumped back = treat as brand new


def _ensure_halt_ts(sleeve: str, s: dict) -> float:
    """Return the halt's age in hours, stamping halt_ts when it is missing.

    Companion to `_halt_age_hours` (2026-07-25 audit finding 3): failing
    closed on an absent stamp is only safe if the stamp eventually appears —
    otherwise a legacy/escalation halt could never cool off. The sleeve is
    NOT saved here; every caller already holds SLEEVE_LOCK and saves."""
    ts = str(s.get("halt_ts") or "")
    if ts:
        return _halt_age_hours(ts)
    s["halt_ts"] = datetime.now(timezone.utc).isoformat()
    s["halt_ts_backfilled"] = True      # visible: the clock started late
    state.journal("halt_ts_backfilled", sleeve=sleeve,
                  reason=str(s.get("halt_reason", ""))[:90])
    return 0.0


def _coverage_signals(hours: float = 24.0) -> dict:
    """Degradation counts in the last `hours` from the journal — pricing failures
    and process restarts a point-in-time heartbeat can't see. The IG (2026-06-14)
    caught that a session can FLAP or go unpriceable for an hour, then show all-
    green afterward; this gives the health desk a rolling-window memory.
    failed_restarts (keepalive verified_up=False) is the crash-loop signal —
    benign sleep/wake restarts verify up, a crash loop does not."""
    now = datetime.now(timezone.utc)
    sig = {"restarts": 0, "failed_restarts": 0, "unpriceable": 0,
           "mark_missing": 0, "restored": 0, "restored_ephemeral": 0,
           "restored_derived": 0, "derived_files": [], "book_files": [],
           # DENOMINATOR (IG 2026-07-27 #1): how many journal events actually
           # fell inside the window. Zero means the counters below are all zero
           # because nothing was READ, not because nothing went wrong — the
           # consumers ('data coverage', 'state integrity') must report that as
           # undetermined instead of a green "no corruption in 24h".
           "window_events": 0}
    _last_instab = None        # most-recent REAL instability event (recovery clock)
    for ev in state.read_journal(1200):
        try:
            ets = datetime.fromisoformat(ev.get("ts", ""))
            age = (now - ets).total_seconds() / 3600
        except (ValueError, TypeError):
            continue
        if not (0 <= age <= hours):
            continue
        sig["window_events"] += 1
        e = ev.get("event")
        if e == "keepalive_restart":
            sig["restarts"] += 1
            if ev.get("verified_up") is False:
                sig["failed_restarts"] += 1
        elif e == "watchdog_restart":
            sig["restarts"] += 1
        elif e == "position_unpriceable":
            sig["unpriceable"] += 1
        elif e == "mark_missing":
            sig["mark_missing"] += 1
        elif e in ("state_restored_from_backup", "state_corrupt_unrecoverable"):
            # An EPHEMERAL file (heartbeat/status — rewritten every sweep) recovering
            # from .bak, OR even failing to (it just regenerates next sweep), is NOT
            # a red-alert. Only a CRITICAL file (the books: sleeves, ledger, watchlist,
            # lessons, research, settings) is a real disk/write signal. state_corrupt_
            # unrecoverable is counted too (the IG noted the check ignored it) — for a
            # book file it HALTS the sleeve, which is the real alarm (2026-06-14).
            # Grade by DECLARED TIER, not by a two-name blacklist (operator
            # 2026-08-19). The blacklist made every one of ~60 non-ephemeral
            # state files "CRITICAL ... the books", so the health desk's own
            # reopen_path_inert tracker restoring from its .bak read as money
            # corruption and held this line red. state.state_tier() is the one
            # definition, and it fails UNKNOWN names into BOOK so a new book
            # file can never be silently downgraded.
            _tier = state.state_tier(ev.get("file"))
            if _tier == "EPHEMERAL":
                sig["restored_ephemeral"] += 1
            elif _tier == "DERIVED":
                sig["restored_derived"] += 1
                sig["derived_files"] = sorted(
                    set(sig.get("derived_files") or []) | {str(ev.get("file"))})
            else:
                sig["restored"] += 1
                sig["book_files"] = sorted(
                    set(sig.get("book_files") or []) | {str(ev.get("file"))})
        # Recovery clock: track the most-recent REAL instability (pricing failures,
        # crash-loops, watchdog kills) — NOT a benign verified-up restart, so a deploy
        # or sleep/wake restart (incl. ours) can't reset it and keep the WARN red after
        # the incident is over (2026-06-22 operator: a fixed morning outage stayed red).
        if (e in ("position_unpriceable", "mark_missing", "watchdog_restart")
                or (e == "keepalive_restart" and ev.get("verified_up") is False)):
            if _last_instab is None or ets > _last_instab:
                _last_instab = ets
    sig["quiet_h"] = ((now - _last_instab).total_seconds() / 3600
                      if _last_instab else None)
    return sig


# Trading-day-scheduled jobs run at a fixed ET time and are only "missed" once a
# slightly-later deadline passes. The DEADLINE selects WHICH trading day's run we
# expect to be done; the RUN time is what a job that DID run is stamped at-or-
# after (comparing to the run time, not the deadline, keeps an on-time run from
# falsely reading overdue). Premarket scan: 8:30 run / 9:30 deadline. Research
# cycle: 9:45 run / 10:30 deadline (9:45 + 10:00 catch-up + margin).
_SCAN_RUN_T = time(8, 30)
_SCAN_DEADLINE_T = time(9, 30)
_CYCLE_RUN_T = time(9, 45)
_CYCLE_DEADLINE_T = time(10, 30)
# Bounds the calendar-day WALK in report_missed_session_coverage() — never the
# minute count it reports (2026-08-28). One iteration per calendar day, so even
# a multi-year hole is measured for nothing.
_COVERAGE_WALK_MAX_DAYS = 3660          # ~10 years of calendar days


def _expected_run_overdue(latest_iso: str, run_t: time, deadline_t: time,
                          now_et: datetime | None = None,
                          day_only: bool = False) -> bool:
    """Is a trading-day-scheduled job genuinely MISSING (vs the market simply
    being closed)? True only once the most recent trading day's deadline has
    passed with no run stamped at/after that day's run time. Market-aware: over a
    weekend/holiday the most recent passed deadline is the last trading day's,
    which that day's run already satisfied — so it never cries wolf just because
    the market is closed (a flat hours-threshold flagged every weekend). The
    grace window between run and deadline lets a not-yet-run job pass.

    day_only (operator 2026-07-07): count a run that happened ANY time on the
    target trading day as done, not only at/after its scheduled run time. The
    scan catch-up can legitimately refresh BEFORE the 8:30 premarket (recovering
    a failed premarket / after a reboot) and then the 8:30 run skips via the
    same-ET-day gate; comparing to the 8:30 run time flagged that 7:54 catch-up
    scan as overdue all session. A scan that ran today IS today's scan."""
    now_et = now_et or datetime.now(market_hours.ET)
    target_day = None
    cur = now_et
    for _ in range(10):                       # most recent trading day whose deadline has passed
        if market_hours._is_trading_day(cur):
            deadline = cur.replace(hour=deadline_t.hour, minute=deadline_t.minute,
                                   second=0, microsecond=0)
            if deadline <= now_et:
                target_day = cur
                break
        cur = cur - timedelta(days=1)
    if target_day is None:
        return False                          # no passed deadline yet -> not overdue
    try:
        last = datetime.fromisoformat(latest_iso)
    except (ValueError, TypeError):
        return True                           # never ran -> overdue
    if day_only:
        return last.astimezone(market_hours.ET).date() < target_day.date()
    expected_run = target_day.replace(hour=run_t.hour, minute=run_t.minute,
                                      second=0, microsecond=0)
    return last < expected_run.astimezone(timezone.utc)


def _cycle_overdue(latest_iso: str, now_et: datetime | None = None) -> bool:
    return _expected_run_overdue(latest_iso, _CYCLE_RUN_T, _CYCLE_DEADLINE_T, now_et)


def _scan_overdue(latest_iso: str, now_et: datetime | None = None) -> bool:
    # day_only: a same-day catch-up scan (even pre-8:30) satisfies "today's scan".
    return _expected_run_overdue(latest_iso, _SCAN_RUN_T, _SCAN_DEADLINE_T, now_et,
                                 day_only=True)


def _options_monitor_ok(calls_age_h: float, has_options: bool, market_open: bool) -> bool:
    """The options loop sweeps every 1s while positions are open — a hung
    leveraged book is the most dangerous failure, so its freshness is checked on
    a TIGHT threshold (config.OPTIONS_MONITOR_STALE_S, ~3 min) vs the equity
    loop's 12 min. Staleness only MATTERS when options are actually open AND the
    market is open (the loop idles otherwise, and nothing could be exited)."""
    if not (has_options and market_open):
        return True
    return calls_age_h * 3600 < config.OPTIONS_MONITOR_STALE_S


def heartbeat(name: str) -> None:
    hb = state.load("heartbeat", {})
    hb[name] = datetime.now(timezone.utc).isoformat()
    state.save("heartbeat", hb)


def _host_down_sessions(sessions: list[str], gaps: list[dict],
                        mark_utc_hhmm: str = "20:30") -> list[str]:
    """Sessions (UTC dates) whose nightly mark time falls inside a journaled
    session_coverage_gap — the firm was OFF, so the close FEED cannot be graded
    on them (2026-09-01: the 08-23..08-28 dead-battery outage left five
    sessions unmarked and 'eod mark-to-close' graded the feed on days the
    host was down, poisoning three consumers for ten days after recovery)."""
    out = []
    for day in sessions:
        mark_ts = f"{day}T{mark_utc_hhmm}:00"
        for g in gaps:
            if (str(g.get("from") or "")[:19] <= mark_ts
                    <= str(g.get("to") or "")[:19]):
                out.append(day)
                break
    return sorted(out)


def _sweep_predates_last_close(sweep_iso: str, grace_min: float = 5.0) -> str:
    """'' when the last exit sweep is at/after the close of the most recent
    completed regular session (minus a grace), else a one-line reason. A
    closed market must not EXCUSE a sweep that predates the close (IG
    2026-08-28 #72/#78: 'exit monitor' read ok through a 1445-minute blackout
    because the clock said closed)."""
    try:
        if not sweep_iso:
            return ""
        last_close = market_hours.last_regular_close()
        if last_close is None:
            return ""
        swept = datetime.fromisoformat(sweep_iso)
        if swept.tzinfo is None:
            swept = swept.replace(tzinfo=timezone.utc)
        if swept < last_close - timedelta(minutes=grace_min):
            gap_h = (last_close - swept).total_seconds() / 3600
            return (f"last sweep PREDATES the {last_close.strftime('%Y-%m-%d %H:%M')} "
                    f"ET close by {gap_h:.1f}h — exit protection was DOWN at the close")
    except Exception:
        return ""
    return ""


def report_missed_session_coverage() -> dict | None:
    """On boot, tell the operator if the fund was DOWN across trading hours.

    Origin (operator 2026-07-25): on Friday 2026-07-24 the host entered Modern
    Standby at 08:05 ET and did not wake until 17:50 ET. The fund logged 2 sweeps
    that day against 462-674 on adjacent days — a 993-minute hole covering the
    entire session — and OSCR/CON consequently stopped out ~19h late, on Saturday,
    against Friday's closing marks. NOTHING reported it: the in-process watchdog
    only fires while the process is alive, the 5-minute keepalive task cannot run
    on a sleeping machine, and the 'exit monitor' health line was reading a
    thread-liveness beat as if it were sweep coverage.

    This is deliberately a REPORT, not a repair — no code can protect a book on a
    machine that is powered down. The operator decides the remedy (keep the host
    awake through the session, or move the fund to an always-on host).

    Returns the gap dict when one is reported, else None.
    """
    hb = state.load("heartbeat", {})
    last = max([t for t in (hb.get("equity_sweep", ""), hb.get("equity_monitor", "")) if t],
               default="")
    if not last:
        return None
    gap_h = _age_hours(last)
    if gap_h >= 1e8 or gap_h < 0.5:
        return None
    now = datetime.now(timezone.utc)
    try:
        start = datetime.fromisoformat(last)
    except (ValueError, TypeError):
        return None
    # How many REGULAR trading minutes fell inside the gap — an overnight or
    # weekend hole is normal and must never alert (that noise is why the old
    # off-hours limit was loosened to 2h in the first place).
    #
    # The WALK is bounded; the REPORTED TOTAL is not (2026-08-28). The original
    # 5-minute probe loop carried `missed <= 24 * 60` as its termination test,
    # which silently capped the ANSWER too: every outage past a day reported the
    # same 1445 minutes, so a 4-day hole and a 4-month hole read identically —
    # the number went blind exactly when the outage turned catastrophic. The
    # 2026-08-23 05:50 -> 08-27 21:42 dead-battery outage really cost 1560
    # regular minutes (four full 390-min sessions, Mon-Thu) and the alert, the
    # health desk and the IG audit all said 1445. Walk CALENDAR DAYS instead of
    # 5-minute probes — one iteration per day keeps a months-long gap cheap —
    # and clip each day's session to the gap. _COVERAGE_WALK_MAX_DAYS bounds the
    # loop only: hitting it understates the hole, it never rounds it to a
    # constant.
    missed = 0
    start_et = start.astimezone(market_hours.ET)
    end_et = now.astimezone(market_hours.ET)
    day = start_et.date()
    for _ in range(_COVERAGE_WALK_MAX_DAYS):
        if day > end_et.date():
            break
        try:
            open_dt = datetime.combine(day, market_hours.OPEN_T,
                                       tzinfo=market_hours.ET)
            if market_hours.is_trading_day(open_dt):
                # session_close() is per-DATE (13:00 on an NYSE half day).
                close_dt = datetime.combine(day,
                                            market_hours.session_close(open_dt),
                                            tzinfo=market_hours.ET)
                lo, hi = max(open_dt, start_et), min(close_dt, end_et)
                if hi > lo:
                    missed += int((hi - lo).total_seconds() // 60)
        except Exception:
            break
        day += timedelta(days=1)
    if missed < 15:
        return None
    # Display only — the integer stays TRUE (2026-08-28). If the operator wants
    # "that is more than a day" said out loud, it is said in WORDS next to the
    # real number, never by clamping it.
    missed_txt = f"{missed} min" + (" (>24h of session time)"
                                    if missed > 24 * 60 else "")
    gap = {"from": last, "to": now.isoformat(), "gap_hours": round(gap_h, 2),
           "missed_session_minutes": missed}
    state.journal("session_coverage_gap", **gap)
    from firm import alerts
    alerts.alert(
        f"EXIT PROTECTION WAS DOWN {missed_txt} of trading",
        f"The fund logged no exit sweep from {last[:16]} to {now.isoformat()[:16]} "
        f"({gap_h:.1f}h), of which about {missed_txt} were REGULAR trading hours. "
        "Stops could not fire in that window and any that triggered were applied "
        "late, on the next sweep's marks. The most common cause on this host is "
        "the PC sleeping through the session (Modern Standby), which no in-app "
        "watchdog or keepalive task can recover from. Keep the host awake during "
        "market hours, or run the fund on an always-on machine.")
    return gap


def internet_up(timeout: float = 3.0) -> bool:
    """Cheap connectivity probe. The catch-up logic must not relaunch a cycle
    into a dead network — it would 'complete' with nothing and waste the retry
    (2026-06-10 outage report). Raw-IP probes go first (no DNS needed), but
    some host security layers refuse ALL direct-to-IP connections while
    DNS-resolved traffic works fine (observed 2026-07-01: ConnectionRefused on
    8.8.8.8 yet yfinance healthy) — so fall back to a resolved hostname on
    443, which matches how the fund actually reaches its data providers."""
    import socket
    for host, port in (("8.8.8.8", 53), ("1.1.1.1", 53),
                       ("www.google.com", 443)):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def midday_refresh_warranted() -> tuple[bool, str]:
    """Should a NOON catch-up cycle run (operator 2026-07-08)? A morning cycle can
    degrade on a CLI/network blip; the opportunistic retry usually recovers it, but
    a guaranteed fresh full pass at ~noon (calm mid-session) catches anything still
    thin AND re-processes overdue reopens. Warranted when the day's research is NOT
    already complete-and-healthy: the last completed cycle was mostly degraded (>=
    HEALTH_MAX_DEGRADED_SHARE_PCT), OR bench names are overdue for reopen. Pure read
    — the app's health loop owns the timing gate (once/day, 12:00 ET, trading day,
    online, CLI up). Returns (warranted, reason)."""
    try:
        hist = (state.load("research", {}) or {}).get("history", [])
        if hist:
            vd = hist[-1].get("verdicts", {}) or {}
            if vd:
                deg = sum(1 for v in vd.values() if v.get("degraded"))
                thr = float(getattr(config, "HEALTH_MAX_DEGRADED_SHARE_PCT", 30)) / 100
                if deg / len(vd) >= thr:
                    return True, f"last cycle {deg}/{len(vd)} verdicts degraded"
        from firm import watchlist as _wl
        od = _wl.overdue_reopens()
        if od:
            return True, (f"{len(od)} name(s) overdue for reopen: "
                          + ", ".join(o["ticker"] for o in od[:4]))
    except Exception:
        return False, "midday check failed"
    return False, "day already complete/healthy"


def _backfill_equity_history(now_et: datetime, lookback_days: int = 10) -> int:
    """Fill HOLES in the durable EOD equity curve left by fully-missed days.

    eod_catchup only ever booked TODAY (2026-07-25 audit finding 6), so a day
    the machine was off end-to-end left a PERMANENT hole: state/equity_history
    was missing 2026-07-08 and 2026-07-15 (both trading days) while carrying a
    row for 2026-07-03, an NYSE holiday the old weekday-only gate let through.

    A hole is reconstructable from evidence the sleeves already carry — each
    sleeve's equity_curve keeps its marks, so the last mark of that ET day IS
    that day's close. Nothing is invented: a day is written only when EVERY
    sleeve has a mark (a partial firm total would fabricate a drawdown), rows
    are tagged backfilled=True, and days older than the curve's 2000-point
    retention simply stay missing rather than being guessed."""
    try:
        hist = state.load("equity_history", []) or []
        have = {r.get("date") for r in hist}
        curves: dict[str, dict] = {}
        for k in config.SLEEVES:
            per_day: dict[str, float] = {}
            for pt in (state.get_sleeve(k).get("equity_curve") or []):
                try:                       # curve is chronological -> last wins
                    d_et = datetime.fromisoformat(pt["ts"]).astimezone(market_hours.ET)
                except (ValueError, KeyError, TypeError):
                    continue
                if isinstance(pt.get("equity"), (int, float)):
                    per_day[d_et.strftime("%Y-%m-%d")] = float(pt["equity"])
            curves[k] = per_day
        added = 0
        for back in range(1, lookback_days + 1):
            day_et = now_et - timedelta(days=back)
            day = day_et.strftime("%Y-%m-%d")
            if day in have or not market_hours._is_trading_day(day_et):
                continue
            vals = {k: curves.get(k, {}).get(day) for k in config.SLEEVES}
            if any(not isinstance(v, (int, float)) for v in vals.values()):
                continue          # no evidence for some sleeve — never guess it
            hist.append({"date": day,
                         "firm_equity": round(sum(vals.values()), 2),
                         "sleeves": {k: round(v, 2) for k, v in vals.items()},
                         "backfilled": True})
            added += 1
        if added:
            hist.sort(key=lambda r: str(r.get("date")))
            state.save("equity_history", hist[-750:])
            state.journal("equity_history_backfilled", days=added)
        return added
    except Exception:
        state.journal("equity_history_backfill_failed")
        return 0


def eod_catchup(now_et: datetime | None = None) -> None:
    """If the 4:30 postmarket wrap was missed (offline/PC asleep), book the
    official EOD record + dividends as soon as we're back — outages must
    not leave holes in the P&L history. Runs from the app's health loop;
    no-ops unless it's past 16:35 ET on a TRADING day and today isn't booked.

    2026-07-25 audit finding 6, two defects: (a) the gate was `weekday() >= 5`,
    so an NYSE holiday booked a phantom EOD row (07-03 is in the history) —
    it is now the real trading-day calendar; (b) only TODAY was ever booked,
    so a fully-missed day was never recovered — `_backfill_equity_history`
    now fills reconstructable holes on the first post-close sweep of a day."""
    now_et = now_et or datetime.now(market_hours.ET)
    today = now_et.strftime("%Y-%m-%d")
    if (not market_hours._is_trading_day(now_et)
            or now_et.hour * 60 + now_et.minute < 16 * 60 + 35):
        return
    if state.load("eod", {}).get("date") == today:
        return
    if not internet_up():
        return
    _backfill_equity_history(now_et)
    try:
        from firm import dividends, risk
        # MARK TO THE CLOSE BEFORE BOOKING THE CLOSE (operator 2026-08-19).
        # This function exists so an outage "must not leave holes in the P&L
        # history" — but it booked the hole instead of the close. It never
        # called mark_books_to_close(), so broker.sleeve_equity() below read
        # whatever mark each position happened to be carrying (the stale-guard
        # deliberately refuses to let a daily-close fallback overwrite
        # last_price intraday), and record_equity_history stamped THAT as the
        # session's official equity. Measured on 2026-08-19: only 3 of the last
        # 7 sessions had ANY successful mark — 08-10, 08-13, 08-14 and 08-18
        # had none — so the equity of record for those days is an intraday
        # print, and every consumer of it (verdict grading, deployment,
        # drawdown breakers, all realised-P&L accounting) inherited the error.
        # The nightly 20:30 wrap already marks first; the catch-up path is the
        # one that skipped it, which is precisely the path that runs when the
        # box was asleep and the marks are STALEST. Fail-soft by construction:
        # mark_books_to_close() touches nothing when no closes are fetched, so
        # a dead feed degrades to exactly the old behaviour rather than
        # blocking the booking.
        try:
            _mk = mark_books_to_close()
            state.journal("eod_catchup_marked", marked=(_mk or {}).get("marked", 0))
        except Exception:
            state.journal("eod_catchup_mark_failed")
        dividends.credit_dividends()
        summary, total, total_day = {}, 0.0, 0.0
        for k in config.SLEEVES:
            s = state.get_sleeve(k)
            eq = broker.sleeve_equity(s)
            day = risk.day_pnl(s, eq)
            summary[k] = {"equity": round(eq, 2), "day_pnl": round(day, 2),
                          "positions": len(s["positions"])}
            total += eq
            total_day += day
        state.journal("eod_summary", firm_equity=round(total, 2),
                      firm_day_pnl=round(total_day, 2), sleeves=summary,
                      via="health-loop catch-up")
        record_equity_history(summary, total, date=today)
        state.save("eod", {"date": today})
    except Exception:
        state.journal("eod_catchup_failed")


def record_equity_history(summary: dict, firm_equity: float,
                          date: str | None = None) -> None:
    """Append today's per-sleeve EOD equity to a DURABLE capped state file.

    The 2026-07-01 state corruption proved that per-sleeve equity curves live
    only in wipeable sleeve files and the journal rotates in ~26 days — when
    alpha_stocks was overwritten, its performance history was unrecoverable
    (goal audit fix 6). ~750 rows = 3 years of trading days; one row per
    calendar day (idempotent on re-book).

    The day key is the ET SESSION date (2026-07-25 audit finding 6): rows were
    keyed on the UTC date while every producer gates on ET, so a catch-up run
    after 19:00 ET (= next-day UTC in winter) filed the session under
    TOMORROW's date — the same drift that leaves real sessions looking absent.
    Callers that already know the session date pass it explicitly."""
    try:
        today = date or datetime.now(market_hours.ET).strftime("%Y-%m-%d")
        hist = state.load("equity_history", []) or []
        hist = [r for r in hist if r.get("date") != today]   # re-book overwrites
        hist.append({"date": today, "firm_equity": round(firm_equity, 2),
                     "sleeves": {k: v.get("equity") for k, v in summary.items()}})
        state.save("equity_history", hist[-750:])
    except Exception:
        state.journal("equity_history_write_failed")


# ── Escalation ladder (IG 2026-07-27 #14) ────────────────────────────────────
# `eod mark-to-close` failed for DAYS with the equity of record riding the
# mark_rescued_public fallback, and the only consequence was another
# `health_warning` journal line every 30-minute sweep. A red that repeats
# forever is a red nobody acts on (the same lesson _escalate_recon_drift,
# _escalate_ig_findings and _escalate_conviction_spread each learned one check
# at a time). This is the GENERIC ladder: every check in the report gets a
# consecutive-failing-sweep streak, the streak rides in the check's own detail
# so the operator sees the age of the failure without reading the journal, and
# crossing the budget raises ONE critical alert per escalation (latched — never
# once per sweep). Deliberately not an auto-halt: a broken health check is an
# engineering defect, so the ladder puts a human on it.
_STREAK_MARK = " · FAILING "


# ── Dependency poisoning (fix brief 2026-07-28 #3) ───────────────────────────
# THE DEFECT THIS EXISTS FOR: `conviction distribution` had been RED for 36
# consecutive sweeps — the committee's conviction score is an informationless
# constant (stdev 3.1, p90-p10 of 8 over 132 verdicts) — while every health
# line certifying a decision path that CONSUMES that score reported ok. The
# desk read "1 red, the rest green" when the truth was "one broken input, and
# eight green lines computed on top of it". A check that grades a gate is only
# as trustworthy as the signal the gate reads.
#
# `checks` are the health lines that certify a path consuming the signal; they
# are forced to NOT-OK while the signal is flagged, and labelled with the root
# so the operator fixes one thing, not eight. `gates` is the audited list of
# real consumers in the trading code — it is what makes this registry
# falsifiable instead of a guess, and the grep that regenerates it is
# `grep -rn conviction firm/sleeves.py firm/risk.py firm/routing.py
#  firm/watchlist.py firm/allocation.py`.
SIGNAL_CONSUMERS: dict[str, dict] = {
    "conviction distribution": {
        "signal": "the committee's conviction score",
        "gates": ("alpha + core entry and ORDERING via "
                  "sleeves._ranked_buys(min_conviction); the calls and puts "
                  "entry bars via sleeves._options_conviction_min(); "
                  "conviction-swap eviction of a held name; core satellite "
                  "selection by conviction rank; bench promotion at "
                  "conviction >= 55"),
        "checks": ("sleeve routing", "options gate reachability",
                   "calls_only gate reachability", "book-full waiter coverage",
                   "bench reviewability", "held deterioration",
                   "deployment", "parking concentration"),
    },
    "eod mark-to-close": {
        "signal": "the closing-price feed that sets equity of record",
        "gates": ("every sleeve equity / position mark, and therefore position "
                  "sizing, the drawdown kill switches, the daily breaker and "
                  "all realized-P&L accounting"),
        "checks": ("display reconciliation", "deployment",
                   "parking concentration", "verdict grading"),
    },
}


def record_standing_defects(streaks: dict) -> dict:
    """Promote a long-running failure into a DECLARED standing-defect state.

    Fix brief 2026-07-28 #9(a): `conviction distribution` failed 46 consecutive
    sweeps while eight dependent gates kept trading on it, and the only
    consequence was one latched alert. An alert nobody can act on is not a
    control. This records which signals are standing defects and, from
    SIGNAL_CONSUMERS, which sleeves depend on them — so the state is a fact the
    rest of the firm can read rather than a sentence in a dashboard.

    GATING IS OPT-IN AND OFF BY DEFAULT, deliberately. The brief asks that the
    dependent sleeves be BLOCKED from acting. For conviction that means halting
    new entries firm-wide, and the cost of the defect it would be preventing is
    position sizing converging on the starter band — benchmark-hugging, not
    ruin. Halting a fund to avoid benchmark-hugging is the larger harm, and the
    root was fixed the same day (debate._calibrate_conviction), so the measure
    is about to clear on its own. The mechanism is built, tested and one flag
    from live; flipping STANDING_DEFECT_BLOCKS_SLEEVES is the operator's call,
    not a default this code should make for them."""
    budget = int(getattr(config, "STANDING_DEFECT_SWEEPS", 20))
    now = datetime.now(timezone.utc).isoformat()
    prior = state.load("standing_defects", {}) or {}
    out: dict[str, dict] = {}
    for name, row in (streaks or {}).items():
        if int((row or {}).get("streak") or 0) < budget:
            continue
        spec = SIGNAL_CONSUMERS.get(name) or {}
        out[name] = {
            "since": (prior.get(name) or {}).get("since") or row.get("since") or now,
            "streak": int(row.get("streak") or 0),
            "signal": spec.get("signal") or name,
            "dependent_checks": list(spec.get("checks") or ()),
            "gates": spec.get("gates"),
            "blocking": bool(getattr(config, "STANDING_DEFECT_BLOCKS_SLEEVES", False)),
        }
    if out != prior:
        state.save("standing_defects", out)
        for n in set(out) - set(prior):
            state.journal("standing_defect_declared", check=n,
                          streak=out[n]["streak"], blocking=out[n]["blocking"])
        for n in set(prior) - set(out):
            state.journal("standing_defect_cleared", check=n)
    return out


def standing_defect_block(consumer_check: str) -> str | None:
    """Reason a consumer should stand down, or None. Returns None unless
    STANDING_DEFECT_BLOCKS_SLEEVES is on — see record_standing_defects."""
    if not getattr(config, "STANDING_DEFECT_BLOCKS_SLEEVES", False):
        return None
    for name, row in (state.load("standing_defects", {}) or {}).items():
        if consumer_check in (row.get("dependent_checks") or ()):
            return (f"standing defect '{name}' ({row.get('streak')} sweeps) — "
                    f"{row.get('signal')} is not trustworthy")
    return None


def _apply_dependency_poisoning(checks: list[dict]) -> list[str]:
    """Force the registered consumers of a FLAGGED signal out of `ok`.

    Returns the poisoned check names. A poisoned line carries `poisoned_by`, and
    `_track_health_streaks` deliberately does NOT escalate it — one broken root
    must raise ONE critical alert, not one per consumer, or the ladder that
    exists to make standing defects visible becomes the noise that buries them.
    Consumers already failing (their own verdict is louder) and undetermined
    ones (which never claimed health) are left alone."""
    by_name = {str(c.get("name")): c for c in checks}
    poisoned: list[str] = []
    for signal, spec in SIGNAL_CONSUMERS.items():
        sig = by_name.get(signal)
        if not sig or sig.get("ok") or sig.get("undetermined"):
            continue
        for cname in spec["checks"]:
            c = by_name.get(cname)
            if c is None or not c.get("ok") or c.get("undetermined"):
                continue
            c["ok"] = False
            c["poisoned_by"] = signal
            c["detail"] = (
                f"POISONED by '{signal}' — its own reading was: {c['detail']} "
                f"— but this line grades a path that consumes {spec['signal']}, "
                f"and that signal is currently FAILING, so a green here is not "
                f"evidence of health. Fix the root first. Gates that read it: "
                f"{spec['gates']}.")
            poisoned.append(cname)
    return poisoned


def _dependency_registry_gaps(checks: list[dict]) -> list[str]:
    """Names in SIGNAL_CONSUMERS that no longer exist among the emitted checks.

    A registry keyed on strings stops protecting anything the moment a check is
    renamed, and does it SILENTLY — the same failure class as the reconciler
    allowlist that reported a working options gate as inert (2026-07-28). This
    turns that silence into a red line."""
    known = {str(c.get("name")) for c in checks}
    gaps: list[str] = []
    for signal, spec in SIGNAL_CONSUMERS.items():
        if signal not in known:
            gaps.append(f"signal '{signal}'")
        gaps += [f"'{signal}' -> consumer '{c}'"
                 for c in spec["checks"] if c not in known]
    return gaps


def _annotate_streaks(report: dict, streaks: dict) -> None:
    """Stamp each failing check's consecutive-sweep streak into its detail.

    Called from health_report (reads the persisted streaks, so the dashboard
    poll path shows the same age as the saved report) AND again from
    run_health_sweep after the streaks advance. Idempotent by construction: any
    previously-stamped segment is cut before a fresh one is appended."""
    for c in (report.get("checks") or []):
        c["detail"] = str(c.get("detail", "")).split(_STREAK_MARK)[0]
        st = (streaks or {}).get(c.get("name")) or {}
        n = int(st.get("streak") or 0)
        if c.get("ok") or n < 2:
            c.pop("streak", None)
            continue
        c["streak"] = n
        c["detail"] += (f"{_STREAK_MARK}{n} consecutive health sweeps"
                        + ("; CRITICAL alert raised — this is not new, it is a "
                           "standing defect" if st.get("alerted") else ""))


def _track_health_streaks(report: dict) -> dict:
    """Advance the per-check failing streaks and escalate past the budget.

    One record per check name in state/health_streaks:
      {streak, since, alerted, last_detail}. A pass resets the record (and
    journals the recovery when it ends a real streak) so the ladder can fire
    again on a relapse. The alert is latched on `alerted`, so a check that
    stays red for a week alerts exactly ONCE, not 336 times."""
    budget = int(getattr(config, "HEALTH_ESCALATE_AFTER_SWEEPS", 6))
    streaks = state.load("health_streaks", {}) or {}
    now_iso = datetime.now(timezone.utc).isoformat()
    live: dict = {}
    for c in (report.get("checks") or []):
        name = str(c.get("name") or "")
        if not name:
            continue
        prev = streaks.get(name) or {}
        if c.get("ok"):
            if int(prev.get("streak") or 0) >= budget:
                state.journal("health_check_recovered", check=name,
                              after_sweeps=int(prev.get("streak") or 0))
            continue                      # dropped from the map = streak reset
        if c.get("poisoned_by"):
            # Not-ok because its INPUT is broken, not because it found
            # anything (fix brief 2026-07-28 #3). The root signal runs its own
            # ladder and alerts once; letting eight consumers each accrue a
            # streak and fire their own CRITICAL would turn one real defect
            # into an alert storm and train the operator to ignore the ladder.
            # The streak is also RESET here, so a consumer's own history is not
            # polluted by an outage it did not cause.
            continue
        rec = {"streak": int(prev.get("streak") or 0) + 1,
               "since": prev.get("since") or now_iso,
               "alerted": bool(prev.get("alerted")),
               "last_detail": str(c.get("detail", ""))[:160]}
        if rec["streak"] >= budget and not rec["alerted"]:
            try:
                from firm import alerts
                alerts.alert(
                    f"HEALTH CHECK FAILING: {name}",
                    f"'{name}' has failed {rec['streak']} consecutive health "
                    f"sweeps (since {str(rec['since'])[:16]}) with nothing "
                    f"escalating: {str(c.get('detail', ''))[:120]}",
                    "critical")
                rec["alerted"] = True
                state.journal("health_check_escalated", check=name,
                              sweeps=rec["streak"], since=str(rec["since"])[:19])
            except Exception as e:
                state.journal("health_escalation_error", check=name,
                              error=str(e)[:120])
        live[name] = rec
    state.save("health_streaks", live)
    try:
        record_standing_defects(live)
    except Exception as e:
        state.journal("standing_defect_error", error=str(e)[:120])
    return live


def _puts_gate_decisions(history: list, window: int = 30) -> dict:
    """Per-name decision ledger for the PUTS entry gate (IG 2026-07-27 #13).

    'options gate reachability' reported `puts 0/5 qualified ... P
    0open/0explained of 0` and read GREEN, because both of its legs measure
    something DOWNSTREAM of the gate: the hard-fail leg needs
    HEALTH_OPTIONS_GATE_MIN_EMITTED (25) sell-family verdicts before it can
    fire at all, and the conversion leg's denominator is decision-time
    `options_gate_qualified` EVENTS — which the desk only journals when the
    gate qualifies someone. Zero qualifications therefore produce a zero
    denominator and a green "0 of 0": the check certifies the gate by
    measuring a proxy that zero itself.

    This measures the GATE. Walk the same history window's real SELL-family
    verdicts and record, per name, whether the puts gate ACCEPTED or REJECTED
    it and why. A non-zero qualification OR an explicit per-name rejection is
    evidence the gate ran; zero of both means nothing was measured.

    Read-only and network-free by the same contract routing.route_coverage
    uses: `_puts_entry_ok` is called with no packet (its earnings leg fails
    open), and the rejection REASON is derived from the verdict's own fields
    rather than by re-implementing the gate. Trading behaviour is untouched."""
    out: dict = {"evaluated": 0, "qualified": [], "rejected": [], "reasons": {}}
    try:
        from firm import sleeves as _slv
        _strong_bar = float(config.SLEEVES["calls_only"]["min_conviction"])
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:60]}"
        return out
    try:
        _sell_bar = float(_slv._options_conviction_min("puts"))
    except Exception:
        _sell_bar = float(getattr(config, "CALLS_BUY_CONVICTION_MIN", 72))
    for h in (history or [])[-window:]:
        for sym, v in (h.get("verdicts") or {}).items():
            v = v or {}
            # Degraded/quant-only verdicts can never qualify BY DESIGN, so
            # counting them as "rejections" would manufacture evidence the
            # gate is live. route_coverage excludes them for the same reason.
            if v.get("degraded") or v.get("rating") not in ("SELL", "STRONG_SELL"):
                continue
            out["evaluated"] += 1
            try:
                if _slv._puts_entry_ok(v, sym):
                    out["qualified"].append(sym)
                    continue
            except Exception as e:
                out.setdefault("error", f"gate raised: {str(e)[:60]}")
                continue
            conv = v.get("conviction") or 0
            bar = _strong_bar if v.get("rating") == "STRONG_SELL" else _sell_bar
            out["rejected"].append(sym)
            out["reasons"][sym] = (
                f"{v.get('rating')} conviction {conv:g} < {bar:g}"
                if conv < bar else
                f"{v.get('rating')} cleared conviction but failed the judge "
                "options-veto / mechanical screen")
    return out


def _held_deterioration_scan(min_cycles: int, min_dd_pct: float) -> dict:
    """Deterioration streaks across EVERY sleeve, seats separated (IG #7).

    The persisted tracker snapshot covers a SUBSET of the book, so the health
    line graded 21 of 29 open positions and the omitted core_blend names — the
    worst single-day P&L in the fund — were invisible to it. A coverage hole in
    a watchdog is worse than no watchdog: it reports a clean number over the
    part it happens to see.

    The real design constraint stays honoured. core_blend's MECHANICAL SEATS
    (allocation.core_weights) are deliberately exempt from per-name SELLs —
    sleeves.py journals `core_sell_suppressed` and allocation swaps the seat
    instead — so flagging "the committee should have sold it" would be flagging
    a decision the desk was never allowed to make. Seats are therefore reported
    in their own labelled bucket (visible, never a red); core SATELLITES and
    every other sleeve are graded normally, because a per-name SELL genuinely
    was available to them.

    Pure read (research history + live sleeves), same predicate as the
    learning-side tracker: N consecutive non-degraded BUY-family verdicts while
    the position is at least min_dd_pct underwater."""
    from firm import allocation as _alloc
    try:
        _seats = set(_alloc.core_weights() or {})
    except Exception:
        _seats = set(config.SLEEVES["core_blend"].get("core_weights") or {})
    hist = (state.load("research", {}) or {}).get("history") or []
    out: dict = {"checked": 0, "flagged": [], "seats_flagged": [],
                 "by_sleeve": {}, "seat_symbols": sorted(_seats)}
    for sl in config.SLEEVES:
        sv = state.get_sleeve(sl)
        for sym, p in (sv.get("positions") or {}).items():
            if p.get("cash_equitization") or p.get("instrument") != "equity":
                continue
            entry = p.get("entry_price") or 0
            if entry <= 0:
                continue
            out["checked"] += 1
            out["by_sleeve"][sl] = out["by_sleeve"].get(sl, 0) + 1
            pnl = ((p.get("last_price") or entry) / entry - 1) * 100
            streak, convs = 0, []
            for h in reversed(hist):          # newest-first walk back
                v = (h.get("verdicts") or {}).get(sym)
                if not isinstance(v, dict) or not v.get("rating"):
                    continue
                if v.get("degraded") or v["rating"] not in ("BUY", "STRONG_BUY"):
                    break
                streak += 1
                if isinstance(v.get("conviction"), (int, float)):
                    convs.append(v["conviction"])
            if streak < min_cycles or pnl > -min_dd_pct:
                continue
            row = {"ticker": sym, "sleeve": sl, "buy_streak": streak,
                   "pnl_pct": round(pnl, 1),
                   "conviction_trend": (round(convs[0] - convs[-1], 1)
                                        if len(convs) > 1 else 0.0)}
            if sl == "core_blend" and sym in _seats:
                row["sell_suppressed"] = True   # mechanical seat: no per-name SELL exists
                out["seats_flagged"].append(row)
            else:
                out["flagged"].append(row)
    out["flagged"].sort(key=lambda r: (r["pnl_pct"], -r["buy_streak"]))
    out["seats_flagged"].sort(key=lambda r: (r["pnl_pct"], -r["buy_streak"]))
    return out


def _cycle_can_run_today(now=None) -> bool:
    """Can a research cycle still act TODAY? Severity gate for the reopen
    watchdogs (2026-07-09): trading day + before the close. Off-hours the
    overdue queue cannot drain, so redding on it is alarm noise — the checks
    go green-with-story and re-red the moment a session can act."""
    now = now or datetime.now(market_hours.ET)
    return bool(market_hours.is_trading_day(now)) and now.hour < 16


def health_report() -> dict:
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str, undetermined: bool = False) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail,
                       **({"undetermined": True} if undetermined else {})})

    def add_undet(name: str, denominator: str, note: str = "", n: int = 0) -> None:
        """A verdict computed over an EMPTY population is a lie by construction
        (IG 2026-07-27 #1). "all reopens within 5d" with zero reopens, "no halt
        trips in 14d" over a journal that does not reach back 14 days, "puts
        0/5 qualified ... 0 of 0" — each ASSERTS something it never measured,
        and the desk read them as evidence of health. `undetermined` keeps
        ok=True (an unmeasurable check must never red the desk spuriously) but
        the detail LEADS with `undetermined (n=…)` and NAMES the denominator,
        so "verified clean" and "nothing to verify" can never look alike again.
        The flag also rides on the check dict for machine consumers (dashboard,
        IG snapshot) and keeps the escalation ladder from counting a check that
        did not run."""
        add(name, True, f"undetermined (n={n}) — {denominator}"
            + (f"; {note}" if note else ""), undetermined=True)

    def add_errored(name: str, exc: Exception) -> None:
        """A check that RAISES must FAIL LOUDLY, never vanish (2026-07-25 audit
        finding 5). Eight checks below called add() inside a bare
        `try/except Exception: pass`, so an exception DELETED the line from the
        report instead of failing it — and since report["ok"] is all() over the
        checks that SURVIVED, the desk went greener the more broken it got. A
        malformed state/ig_findings_latest.json, for instance, silently removed
        the currently-RED 'IG findings' line and flipped the whole report
        all-green with nothing told to the operator."""
        add(name, False, f"check errored: {type(exc).__name__}: {str(exc)[:90]}")

    research = state.load("research", {})
    latest_iso = research.get("latest_asof", "")
    cyc_age = _age_hours(latest_iso)
    overdue = _cycle_overdue(latest_iso)
    cyc_detail = f"last cycle {cyc_age:.1f}h ago" if cyc_age < 1e8 else "never ran"
    if not overdue and cyc_age >= 30:
        cyc_detail += " (market closed — next cycle next session)"
    add("research cycle", not overdue, cyc_detail)

    scan = state.load("market_scan", {})
    scan_iso = scan.get("asof", "")
    scan_age = _age_hours(scan_iso)
    scan_overdue = _scan_overdue(scan_iso)   # trading-day aware; won't cry wolf over a weekend
    scan_detail = (f"{scan.get('symbols_tradable', 0)} stocks, {scan_age:.1f}h old"
                   if scan_age < 1e8 else "no scan yet")
    # Market-aware severity (operator 2026-07-07): a MISSED scan (e.g. an evening
    # reboot on a trading day whose 8:30 premarket never ran) can only be remedied
    # at the NEXT premarket, so once the session is over a hard red just spams
    # health_warning overnight on a non-actionable state — same gate as 'IG
    # findings'/'edge integrity'/'claude cli'. A stale scan only actually corrupts
    # decisions while the market is OPEN (the 9:45 cycle trades on it); after the
    # close it is informational.
    if not scan_overdue:
        if scan_age >= 36:
            scan_detail += " (market closed — refreshes next premarket)"
        add("market scan", True, scan_detail)
    elif not market_hours.market_is_open():
        # Derive the label from the SAME predicate this branch tested, not from
        # an independent clock read: session_phase() re-reads the wall clock, so
        # a caller (or a test) that has established "market closed" could get a
        # line saying "market hours" — the two views must not disagree inside
        # one branch.
        _scan_ph = ("pre-market" if market_hours.extended_session_open()
                    else "market closed")
        add("market scan", True,
            scan_detail + f" — refreshes next premarket ({_scan_ph})")
    else:
        add("market scan", False, scan_detail)

    # Exit monitor: during a session it sweeps ~every 60s and a stale heartbeat
    # means exit protection is DOWN (critical, tight 12-min limit). When the
    # market is closed the loop idles (still beats every 60s but does no sweeps)
    # and a stale beat is harmless — a brief gap from PC sleep/restart is normal,
    # so use a lenient 2h limit that still catches a genuinely dead thread. The
    # old flat 0.2h limit cried wolf on every overnight sleep/restart (2026-06-13).
    hb = state.load("heartbeat", {})
    mon_age = _age_hours(hb.get("equity_monitor", ""))
    # The BOOLEAN governs the tolerance (a sweep must be fresh in any live
    # session); the PHASE is what gets reported, so a red line says PRE-MARKET
    # / MARKET HOURS / AFTER HOURS instead of the ambiguous "session live"
    # (operator 2026-08-21 — a gap at 08:45 and one at 16:45 are different
    # incidents and the detail line should not make the reader guess).
    _phase = market_hours.session_phase()
    _plabel = market_hours.session_phase_label()
    session_live = market_hours.market_is_open() or market_hours.extended_session_open()
    mon_limit = 0.2 if session_live else 2.0
    # COVERAGE vs LIVENESS (operator 2026-07-25). This line used to read the
    # `equity_monitor` beat, which app.py writes on every loop pass whether or not
    # a sweep ran, and labelled it "last sweep" — so it could only ever detect a
    # DEAD thread, never a skipped sweep. On 2026-07-24 the process was down for
    # the entire session (2 sweeps vs 462-674 on adjacent days, a 993-minute gap)
    # and OSCR/CON stopped out ~19h late against Friday's close. In-session the
    # check now grades the `equity_sweep` beat, written only when check_all_exits
    # actually ran; the liveness beat still governs the idle/out-of-hours case.
    swp_age = _age_hours(hb.get("equity_sweep", ""))
    if session_live:
        # No equity_sweep beat yet (first run after this upgrade) — fall back to
        # the liveness beat rather than crying wolf on a missing key.
        eff_age = swp_age if swp_age < 1e8 else mon_age
        mon_ok = eff_age < mon_limit and mon_age < mon_limit
        mon_detail = (f"[{_plabel}] last sweep {eff_age * 60:.0f} min ago"
                      if eff_age < 1e8
                      else f"[{_plabel}] no sweep recorded")
        if mon_age < mon_limit <= eff_age:
            mon_detail += " — monitor thread ALIVE but NOT SWEEPING (exit protection is down)"
    else:
        mon_ok = mon_age < mon_limit
        mon_detail = (f"[{_plabel}] last sweep {swp_age * 60:.0f} min ago"
                      if swp_age < 1e8
                      else f"[{_plabel}] thread beat {mon_age * 60:.0f} min ago"
                      if mon_age < 1e8 else f"[{_plabel}] no heartbeat")
        # (2026-09-01, IG #72/#78) the clock saying "closed" must not excuse a
        # sweep older than the last session's close — that is a blackout AT
        # the close, the moment the day's stops mattered most.
        _pre = _sweep_predates_last_close(hb.get("equity_sweep", ""))
        if _pre:
            mon_ok = False
            mon_detail += f" — {_pre}"
        elif mon_ok:
            mon_detail += f" (idle — {_plabel.lower()})"
    add("exit monitor", mon_ok, mon_detail)

    # SESSION COVERAGE — DID THE SWEEPER ACTUALLY COVER THE SESSION?
    # (fix brief 59, closed 2026-08-19.) 'exit monitor' above is an
    # INSTANTANEOUS recency reading: the moment the loop resumes it reads
    # "last sweep 0 min ago" and goes green, so a hole that has CLOSED is
    # invisible forever. report_missed_session_coverage() does detect the
    # holes and journals `session_coverage_gap` — but it is called once, from
    # app.py at BOOT, and NO health check consumed its evidence. The firm was
    # therefore emitting critical alerts ("EXIT PROTECTION WAS DOWN 260 min of
    # trading", 2026-08-17) that no health line owned and no streak tracked.
    # Measured 2026-08-19 over 2026-07-27..08-19: 8 distinct holes totalling
    # 1,235 regular-session minutes (325 on 08-04, 260 on 08-17, 180 + 90 + 65
    # on 08-13, 155 on 07-29, 105 + 55 on 08-12), plus 368 `equity_sweep_skipped`
    # passes where the loop was alive but stood down for the cycle lock. Two of
    # the seven graded stop exits fired after a 16.07-HOUR blackout (CRS -13.27%,
    # TXN -18.92% against a 15% trail). Unparked notional carrying stops right
    # now is ~$60k. This line routes nothing and repairs nothing — no code can
    # protect a book on a sleeping host — but the operator cannot choose a
    # remedy for a number nobody prints.
    try:
        _sc_days = float(getattr(config, "EXIT_COVERAGE_SLA_DAYS", 10.0))
        _sc_maxgap = float(getattr(config, "EXIT_COVERAGE_MAX_GAP_MIN", 30.0))
        _sc_minpct = float(getattr(config, "EXIT_COVERAGE_MIN_PCT", 98.0))
        _sc_floor = (datetime.now(timezone.utc)
                     - timedelta(days=_sc_days)).isoformat()
        _sc_j = state.read_journal(4000)
        # De-duplicate: the reporter can fire twice for one hole (observed
        # 2026-08-12T18:00 and 2026-08-19T16:31 both logged an exact pair).
        # Key on (from, to-minute) so one hole counts once.
        _sc_seen, _sc_gaps = set(), []
        for _e in _sc_j:
            if _e.get("event") != "session_coverage_gap":
                continue
            if str(_e.get("ts") or "") < _sc_floor:
                continue
            _k = (str(_e.get("from") or ""), str(_e.get("to") or "")[:16])
            if _k in _sc_seen:
                continue
            _sc_seen.add(_k)
            _sc_gaps.append(_e)
        _sc_skipped = sum(1 for _e in _sc_j
                          if _e.get("event") == "equity_sweep_skipped"
                          and str(_e.get("ts") or "") >= _sc_floor)
        _sc_missed = sum(float(_e.get("missed_session_minutes") or 0)
                         for _e in _sc_gaps)
        _sc_worst = max((float(_e.get("missed_session_minutes") or 0)
                         for _e in _sc_gaps), default=0.0)
        # Scheduled regular-session minutes inside the window (390/day).
        _sc_now = datetime.now(timezone.utc)
        _sc_sessions = sum(1 for _i in range(1, int(_sc_days) + 1)
                           if (_sc_now - timedelta(days=_i)).weekday() < 5)
        _sc_sched = _sc_sessions * 390.0
        _sc_blind = _sc_missed + _sc_skipped        # skipped passes are ~1/min
        _sc_pct = (100.0 * (1 - _sc_blind / _sc_sched)) if _sc_sched else 100.0
        if not _sc_sched:
            add_undet("session coverage",
                      "no scheduled sessions inside the "
                      f"{_sc_days:.0f}d window, so coverage was not measured")
        else:
            _sc_ok = _sc_pct >= _sc_minpct and _sc_worst <= _sc_maxgap
            _sc_d = (f"{_sc_pct:.1f}% of {_sc_sched:.0f} scheduled session "
                     f"minute(s) covered over {_sc_days:.0f}d "
                     f"(bar {_sc_minpct:.0f}%); {len(_sc_gaps)} blackout(s) "
                     f"totalling {_sc_missed:.0f} min, worst {_sc_worst:.0f} min "
                     f"(bar {_sc_maxgap:.0f}); {_sc_skipped} sweep(s) stood "
                     "down for the cycle lock")
            if _sc_worst > _sc_maxgap:
                _sc_d += (" — EXIT PROTECTION WAS DOWN: stops could not route "
                          "during those minutes, and a stop that fires late "
                          "fires against a price that already moved")
            add("session coverage", _sc_ok, _sc_d)
    except Exception as _e:
        add_errored("session coverage", _e)

    # Options monitor — the fast (1s) leveraged-book loop, checked SEPARATELY on a
    # tight threshold. The "exit monitor" line above only watches the 60s equity
    # loop; without this, a hung options loop showed green (2026-06-13 operator
    # catch: "12 min is too lenient when every second counts, maybe with options").
    calls_age = _age_hours(hb.get("calls_monitor", ""))
    # Real OPTIONS only — the BIL parking is an equity row and must not make
    # an idle calls loop read as a hung options monitor (batch review R4)
    _calls_s = state.get_sleeve("calls_only")
    opt_pos = {k: p for k, p in (_calls_s.get("positions") or {}).items()
               if p.get("instrument") == "option" and not p.get("cash_equitization")}
    opt_market = market_hours.market_is_open()   # options trade regular hours only
    opt_ok = _options_monitor_ok(calls_age, bool(opt_pos), opt_market)
    if not opt_pos:
        opt_detail = "no open options"
    elif not opt_market:
        opt_detail = f"{len(opt_pos)} open · idle (options market closed)"
    elif calls_age < 1e8:
        opt_detail = f"{len(opt_pos)} open · last sweep {calls_age * 3600:.0f}s ago"
    else:
        opt_detail = f"{len(opt_pos)} open · NO HEARTBEAT"
    add("options monitor", opt_ok, opt_detail)

    # Catalyst readiness — LIVE-STATE watchdog (2026-06-14): the test suite proves
    # the reopen LOGIC works; only this proves it actually FIRED. A benched HOLD
    # whose earnings/macro (FOMC/CPI/jobs) catalyst landed and a cycle has since
    # run without re-judging it is a silent reopen failure (the class of bug —
    # inert bench, FOMC-only catalysts — that reached prod undetected).
    try:
        from firm import watchlist as _wl
        overdue = _wl.overdue_reopens()
    except Exception:
        overdue = []
    # Actionability gating (2026-07-09, same doctrine as 'claude cli' / 'IG
    # findings'): overdue reopens can only drain when a research cycle can
    # still run TODAY — a red that cannot clear overnight/weekend is alarm
    # noise (the 07-09 evening journal filled with 30-min catalyst-readiness
    # warnings for one name no cycle could touch until the next session).
    # Green-with-story off-hours; the real red on a trading day pre-close.
    _can_cycle = _cycle_can_run_today()
    _od_names = ", ".join(o["ticker"] for o in overdue[:6])
    add("catalyst readiness", (not overdue) or (not _can_cycle),
        "waiting names current" if not overdue else
        (f"{len(overdue)} overdue for reopen: {_od_names}" if _can_cycle else
         (f"{len(overdue)} queued for reopen — next session's cycle picks them "
          f"up (market closed; nothing can act now): {_od_names}")))

    # Reopen SLA (IG 2026-07-06 critical, ratified): 'catalyst readiness' reds on
    # ANY overdue name (normal transient churn), but the real failure the IG caught
    # is overdue names that never DRAIN — aging 9-11 days while the promotion queue
    # recycled high-quant names. This asserts the per-ITEM age: red if any single
    # overdue reopen is older than REOPEN_SLA_DAYS, independent of the count. The
    # oldest-first drain fix (watchlist._promotions_locked) keeps this green; a red
    # here means the drain regressed or research cycles stopped running.
    _sla = float(getattr(config, "REOPEN_SLA_DAYS", 5))
    _sla_today = datetime.now(timezone.utc).date()
    _sla_breached, _sla_aged = [], 0
    for o in (overdue or []):
        try:
            _o_age = (_sla_today - datetime.fromisoformat(o["last_verdict"]).date()).days
        except (ValueError, TypeError, KeyError):
            continue
        _sla_aged += 1                     # the DENOMINATOR this line asserts over
        if _o_age > _sla:
            _sla_breached.append((o["ticker"], _o_age))
    _sla_breached.sort(key=lambda x: x[1], reverse=True)
    # Same actionability gate as 'catalyst readiness' above: an SLA breach is
    # a multi-day drain failure, but off-hours nothing can drain it — carry
    # the story in the detail, red again the moment a session can act.
    _sla_names = ", ".join(f"{t} {d}d" for t, d in _sla_breached[:6])
    if _sla_aged == 0:
        # VACUOUS PASS (IG 2026-07-27 #1): this read "ok — all reopens within
        # 5d" on the very sweep the IG counted ZERO reopens. "All of nothing is
        # within the SLA" is true and worthless — and indistinguishable from a
        # drain that is genuinely keeping up, which is the only thing the line
        # exists to tell the operator.
        add_undet("reopen SLA",
                  f"0 overdue reopen(s) carry a datable last_verdict, so the "
                  f"{_sla:.0f}d SLA aged nothing"
                  + (f" ({len(overdue or [])} overdue entr(y/ies) present but "
                     "undatable)" if overdue else ""))
    else:
        add("reopen SLA", (not _sla_breached) or (not _can_cycle),
            f"all {_sla_aged} overdue reopen(s) within {_sla:.0f}d"
            if not _sla_breached else
            ((f"{len(_sla_breached)}/{_sla_aged} reopen(s) past the {_sla:.0f}d "
              f"SLA (drain starving): {_sla_names}") if _can_cycle else
             (f"{len(_sla_breached)}/{_sla_aged} reopen(s) past the {_sla:.0f}d "
              f"SLA — drain resumes next session (market closed): {_sla_names}")))

    # Bench reviewability (IG 2026-06-14): a HOLD must always have a path back to
    # review — earnings catalyst, technical trigger, macro wait, OR the time-
    # cadence recheck that ends in eviction. A name with none of those past its
    # recheck budget is a stuck zombie the eviction pass missed. Makes the
    # "permanent zombie hold" concern falsifiable rather than asserted-safe.
    try:
        stuck = _wl.unreviewable_holds()
    except Exception:
        stuck = []
    # EVERY entry, not just holds (IG 2026-07-03 high, ratified): non-hold
    # entries (near_miss, stop_out) are evicted by the age/vanish/decay pass
    # whose universal backstop — the 30-day ceiling — needs a parseable
    # `added`. An entry without one is un-evictable (WDC sat live like this).
    try:
        stray = _wl.unreviewable_entries()
    except Exception:
        stray = []
    # Per-hold inert-catalyst callout (fix brief 2026-07-17 #4, CLYM): a hold
    # the vendor has no earnings date for is REVIEWABLE (its fallback below is
    # a real dated path) but its earnings-catalyst reopen is silently inert —
    # name it in the detail rather than absorbing it into the aggregate green,
    # so 'dates never captured' can never go invisible again. Green stays
    # green: the dated fallback exists by construction (validate-before-
    # harden); only stuck/stray — no path at all — red this line.
    try:
        _no_ed = _wl.holds_missing_earnings_date()
    except Exception:
        _no_ed = []
    _no_ed_note = ("" if not _no_ed else
                   f" · {len(_no_ed)} hold(s) with an INERT earnings-catalyst "
                   "path (vendor has no date): "
                   + ", ".join(f"{h['ticker']} (fallback: {h['fallback']})"
                               for h in _no_ed[:4]))
    add("bench reviewability", not (stuck or stray),
        ("every entry has a review-or-eviction path" + _no_ed_note)
        if not (stuck or stray) else
        ((f"{len(stuck)} stuck (no reopen path, not evicted): "
          + ", ".join(stuck[:6]) if stuck else "")
         + ("; " if stuck and stray else "")
         + (f"{len(stray)} un-evictable (no `added`, age ceiling dead — "
            "backfills next cycle): " + ", ".join(stray[:6]) if stray else "")
         + _no_ed_note))

    # Reopen-kind liveness (fix brief 2026-07-17 #5): a supported trigger kind
    # can go structurally dead two ways — the debate schema stops OFFERING it,
    # or the write-time normalizer silently DROPS it — and either is invisible
    # until a thesis needed the kind and couldn't arm it. Verified per kind
    # with a synthetic round-trip every sweep; a kind that verifies but has
    # zero armed instances is observed-zero (benign, shown, never red).
    try:
        _rkl = _wl.reopen_kind_liveness()
        _rkl_dead = sorted(k for k, v in _rkl.items()
                           if not (v.get("offered_in_schema")
                                   and v.get("survives_normalization")))
        _rkl_zero = sorted(k for k, v in _rkl.items()
                           if k not in _rkl_dead and not v.get("armed_now"))
        if not _rkl:
            # VACUOUS PASS (IG 2026-07-27 #1): "all kinds live" over an empty
            # kind map asserts nothing — and an empty map is itself the exact
            # regression this check exists to catch (the schema stopped
            # offering trigger kinds), so it must never read as health.
            add_undet("reopen-kind liveness",
                      "the liveness probe returned 0 reopen kinds — nothing was "
                      "round-tripped through the schema or the normalizer")
        else:
            add("reopen-kind liveness", not _rkl_dead,
                (f"all {len(_rkl)} kinds offered by the schema + survive "
                 "normalization"
                 + (" · observed-zero (can fire, never chosen): "
                    f"{', '.join(_rkl_zero)}" if _rkl_zero else ""))
                if not _rkl_dead else
                ("DECLARED-BUT-INERT: " + ", ".join(
                    f"{k} ({'not offered' if not _rkl[k]['offered_in_schema'] else 'dropped by normalizer'})"
                    for k in _rkl_dead)
                 + " — a thesis needing this kind cannot arm it"))
    except Exception as e:
        # A probe that could not run measured NOTHING — "check skipped" with
        # ok=True was a vacuous pass wearing a different word (IG #1).
        add_undet("reopen-kind liveness",
                  "the reopen-kind liveness probe could not run, so 0 kinds "
                  "were round-tripped", f"error: {str(e)[:50]}")

    # Seat eligibility, asserted EVERY SWEEP (fix brief 2026-07-27 #4). SOXL
    # (3x semis) sat in a core seat for ~5 days because the eligibility
    # predicate ran only at eviction, and a name-feed outage had cached
    # `leveraged: False` across the whole pool so nothing objected on the way
    # in. The classification bug is fixed and the predicate now runs at
    # selection — this is the standing assertion that would CATCH it coming
    # back, stated as the brief asked: zero leveraged/unclassifiable in the
    # kept pool, zero in any open position across all four sleeves.
    # `is False` is deliberate throughout: None means the feed could not
    # classify the product, which is ineligible, not innocent.
    try:
        from firm import alerts as _alerts, allocation as _alc
        _pool = (state.load("etf_pool", {}) or {}).get("etfs") or []
        _pool_bad = [e.get("symbol") for e in _pool
                     if e.get("symbol") and e.get("leveraged") is not False]
        _seats = set(_alc.core_weights() or {}) | {_alc.parking_symbol("core_blend"),
                                                   _alc.parking_symbol("alpha_stocks"),
                                                   _alc.parking_symbol("calls_only")}
        _held_bad, _held_unclassified = [], []
        for _slv in config.SLEEVES:
            for _sym, _p in (state.get_sleeve(_slv).get("positions") or {}).items():
                if _p.get("instrument") != "equity":
                    continue
                _lev = _alc._is_leveraged(_sym)
                if _lev is True:
                    _held_bad.append(f"{_sym}[{_slv}]")
                elif _lev is None and _sym in _seats:
                    # Only SEATED names must be classifiable — a conviction name
                    # in alpha is not a seat and has no eligibility contract.
                    _held_unclassified.append(f"{_sym}[{_slv}]")
        _seat_bad = [s for s in _seats if s and _alc._is_leveraged(s) is True]
        _sv_parts = []
        if _seat_bad:
            _sv_parts.append("LEVERAGED PRODUCT IN A SEAT: " + ", ".join(sorted(_seat_bad)))
        if _held_unclassified:
            _sv_parts.append("UNCLASSIFIABLE seated position(s): "
                             + ", ".join(sorted(_held_unclassified)))
        # The brief asked for "0 leveraged in the kept pool", but the pool is
        # etf_rotation's UNIVERSE and dual momentum may legitimately rank a
        # leveraged product into a rotation slot — that is why a separate
        # `_pool_symbols(seats_only=True)` subset exists at all. Asserting an
        # all-unleveraged pool would break a validated strategy to enforce a
        # constraint that only binds on SEATS. So the split is reported as
        # information; the assertions above are what actually bind.
        _sv_note = (f" · pool universe {len(_pool)} of which "
                    f"{len(_pool) - len(_pool_bad)} are seat-eligible "
                    f"(the rest are leveraged/unclassified and may still be "
                    "ranked into a ROTATION slot by design)")
        add("seat eligibility", not _sv_parts,
            (f"every seat unleveraged and classified; 0 leveraged across "
             f"{sum(len(state.get_sleeve(s).get('positions') or {}) for s in config.SLEEVES)} "
             f"open position(s){_sv_note}")
            if not _sv_parts else
            "; ".join(_sv_parts) + " — a daily-reset product cannot hold a "
            "strategic seat; its path decays against the index it proxies"
            + _sv_note)
        if _seat_bad or _held_unclassified:
            _alerts.alert("SEAT ELIGIBILITY BREACH",
                          "; ".join(_sv_parts)[:300], severity="critical")
    except Exception as e:
        add_undet("seat eligibility",
                  "the seat-eligibility probe could not run, so 0 seats and 0 "
                  "positions were classified", f"error: {str(e)[:50]}")

    # Reopen-PATH liveness (fix brief 2026-07-27 #1). The check above measures
    # the debate schema's seven TRIGGER KINDS; the design declares five PATHS,
    # and two of them (macro-catalyst, time-cadence) have no schema kind at all.
    # Nothing reconciled the taxonomies, which is how the auditor came to file a
    # CRITICAL calling the macro path inert while the journal showed it firing
    # 165 times — the single busiest reopen path in the fund. Per the brief's
    # ask: one row per DECLARED path, and a path only reds after it has been
    # inert for 5 consecutive DAYS (not sweeps — a 30-minute cadence would turn
    # a quiet afternoon into an alarm).
    try:
        from firm import watchlist as _wlp
        _rpl = _wlp.reopen_path_liveness()
        _inert_since = state.load("reopen_path_inert", {}) or {}
        _now_iso = datetime.now(timezone.utc).isoformat()
        _aged: list[str] = []
        for _p, _row in _rpl.items():
            if _row.get("inert"):
                _inert_since.setdefault(_p, _now_iso)
                if _age_hours(_inert_since[_p]) >= 24 * int(
                        getattr(config, "REOPEN_PATH_INERT_DAYS", 5)):
                    # Name WHICH failure — "nothing can arm it" and "everything
                    # arms it and it never converts" have different fixes, and
                    # the second is the one that stranded the near_miss cohort
                    # (armed on 102 entries, 0 fires in 30d).
                    _why = ("never armed" if _row.get("never_armed")
                            else f"armed on {_row.get('armed_now')} entries but "
                                 f"0 fires in {_row.get('window_days')}d")
                    _aged.append(f"{_p} [{_why}] "
                                 f"({_age_hours(_inert_since[_p]) / 24:.1f}d)")
            else:
                _inert_since.pop(_p, None)
        state.save("reopen_path_inert", _inert_since)
        _fired_txt = ", ".join(
            f"{_p.split('_')[0]} {_row['armed_now']}armed/{_row['fired_in_window']}fired"
            for _p, _row in _rpl.items())
        # Part-window rows are UNDETERMINED, never green (2026-08-13 IG audit):
        # journal rotation can shrink the fires census below the 30d window, and
        # "0 fires" over a ~8d read is not evidence of inertness — the liveness
        # probe now stamps those rows undetermined and this line must not read
        # them as "live". never_armed verdicts stay assertable (bench census,
        # journal-independent), so _aged reds still fire on them.
        _undet_paths = [
            _p for _p, _row in _rpl.items() if _row.get("undetermined")]
        if _aged:
            add("reopen-path liveness", False,
                ("INERT PATH(S) past the "
                 f"{int(getattr(config, 'REOPEN_PATH_INERT_DAYS', 5))}-day bar: "
                 + "; ".join(_aged)
                 + " — declared in the design, reachable by nothing: "
                 + _fired_txt))
        elif _undet_paths:
            add_undet("reopen-path liveness",
                      (f"the journal window does not cover the full 30d "
                       f"(rotation), so fire-conversion on {len(_undet_paths)} "
                       f"armed path(s) was not measured: "
                       + ", ".join(_undet_paths)),
                      _fired_txt)
        else:
            add("reopen-path liveness", True,
                f"all {len(_rpl)} declared paths live over 30d — {_fired_txt}")
    except Exception as e:
        add_undet("reopen-path liveness",
                  "the declared-path liveness probe could not run, so 0 of the "
                  "5 declared reopen paths were measured", f"error: {str(e)[:50]}")

    # Macro calendar: dates resolve through tiers (FRED -> Forex Factory -> cache
    # -> rules), so it can't "run out" — but SHOW the live tier so reliance on the
    # keyless/unofficial feed is visible, not assumed. A drop to 'rule' (no FRED
    # key AND the Forex Factory feed down) is surfaced in the detail.
    from firm import macro_calendar as _mc
    _tier = _mc.calendar_tier()
    _mc_today = datetime.now(market_hours.ET).date()
    fut = _mc.future_events(_mc_today)
    # Live-feed guard (IG 2026-07-07 critical, ratified): a 'rule'-tier calendar is
    # rule-GENERATED — it can't error, so its blindness (a moved CPI/FOMC date, an
    # added release) is invisible and 'source: rule' read as healthy. The rule tier
    # is a survives-an-outage FLOOR, not a healthy state; fail when the live feed
    # (FRED / Forex Factory) is unreachable so a stuck feed is SEEN, not assumed OK.
    #
    # PER-CLASS COVERAGE (IG 2026-07-27 #11). The old assertion was a TOTAL —
    # "26 events ahead" — against 24 tracked release classes, which cannot
    # possibly mean coverage: weekly jobless claims ALONE resolves ~17 dates
    # over the same 120-day horizon, so one live class can carry the entire
    # total while 23 others go silently dark and the line still reads green.
    # (The total is also `future_events`, which counts only HIGH-impact
    # classes, so it could never speak for the medium-impact siblings the
    # taxonomy work deliberately added — it asserted over a population that
    # excludes most of the registry.) Assert what the calendar is FOR: every
    # tracked class must resolve at least one FUTURE date. A class with none
    # has gone dark — seed exhausted, rule stopped generating, or dropped by
    # the live tier — which is exactly the failure the total hides.
    _mc_days = int(getattr(config, "HEALTH_MACRO_FORWARD_DAYS", 120))
    _mc_hi = _mc_today + timedelta(days=_mc_days)
    _mc_dark: list = []
    _mc_cov = 0
    for _rel in _mc.RELEASES:
        try:
            _rel_ahead = [d for d in _mc._release_dates(_rel, _mc_today, _mc_hi)
                          if d >= _mc_today]
        except Exception:
            _rel_ahead = []
        if _rel_ahead:
            _mc_cov += 1
        else:
            _mc_dark.append(_rel.key)
    _mc_n = len(_mc.RELEASES)
    if _mc_cov == 0:
        # No class resolves ANY forward date: the calendar SOURCE is empty, so
        # "dark" cannot be attributed to a class (everything looks dark for one
        # shared reason). Measure nothing, claim nothing.
        add_undet("macro calendar",
                  f"0 of {_mc_n} tracked release classes resolve a forward date "
                  f"within {_mc_days}d — the calendar source is EMPTY (tier: "
                  f"{_tier}), so per-class darkness is unattributable")
    else:
        try:
            _mc_gg = _mc.grounding()
            _mc_gc, _mc_gr = (len(_mc_gg["fred_confirmed"]),
                              len(_mc_gg["rule_derived"]))
        except Exception:
            _mc_gc = _mc_gr = 0
        add("macro calendar", not _mc_dark and _tier != "rule",
            # SAY WHICH DATES A PROVIDER ACTUALLY CONFIRMED (operator
            # 2026-08-21). "24/24 have a forward date" was true and misleading:
            # a rule-derived date is generated by _rule_dates from a recurrence
            # pattern and no upstream feed has confirmed it, so a release whose
            # real schedule shifts still shows a confident forward date here.
            # The split is the honest form of the same sentence.
            f"{_mc_cov}/{_mc_n} tracked release classes have a forward date "
            f"within {_mc_days}d ({_mc_gc} provider-confirmed, "
            f"{_mc_gr} rule-derived) "
            f"· {fut} high-impact dates ahead · source: {_tier}"
            + ("" if not _mc_dark else
               " — DARK CLASS(ES) with no future date at all: "
               + ", ".join(_mc_dark[:8])
               + "; a release the desk believes it tracks has stopped "
               "resolving (seed exhausted / rule stopped generating / dropped "
               "by the live tier)")
            + ("" if _tier != "rule" else
               " — on the RULE tier: the live feed (FRED/Forex Factory) is "
               "unreachable, so the calendar is BLIND to any moved/added "
               "release date (check FRED key / connectivity)"))

    # Self-test cadence (IG 2026-06-14): the weekly suite-runner is the watchdog
    # OF the watchdogs — if it goes dark, inert paths stop being caught. Scheduled
    # Sundays; warn if it has not run in over a week (market-independent —
    # correctness doesn't care whether the market is open).
    # SOURCE ORDER (operator 2026-07-31, after this check false-red'd for 8
    # straight sweeps and escalated a CRITICAL on a HEALTHY watchdog). The old
    # code read ONLY read_journal(1500) — a fixed EVENT-COUNT window used to
    # answer a multi-DAY question. At the firm's current ~320 events/day, 1500
    # events spans ~4.7 days, so the WEEKLY self_test event aged out of the
    # window before its own next run; `max(..., default="")` then returned ""
    # and `_age_hours("")` returned the 1e9 sentinel, which the detail line
    # renders as "has not run in >8d (or never) — the watchdog-of-watchdogs is
    # dark". The task had in fact run on schedule (2026-07-26 18:00, rc=0,
    # 819/819 + 53/53). A count window can never answer "when did X last
    # happen": as the firm gets BUSIER the window covers LESS time, so the
    # blindness worsens silently and the check goes permanently red — which is
    # indistinguishable from a genuinely dark watchdog, the one signal this
    # check exists to carry. Read the durable stamp FIRST (survives both the
    # window and journal rotation); keep the journal scan as a fallback so a
    # pre-stamp install still measures something rather than claiming "never".
    _st_stamp = str((state.load("self_test", {}) or {}).get("ts") or "")
    _st_journal = max((ev.get("ts", "") for ev in state.read_journal(1500)
                       if ev.get("event") == "self_test"), default="")
    last_st = max(_st_stamp, _st_journal)   # ISO-8601 UTC sorts lexically
    st_age_d = _age_hours(last_st) / 24
    # 8-day bound (operator 2026-07-07): the self-test runs WEEKLY (Sundays), so its
    # age naturally brushes 7.0d right before each run — a 7d bound (my 07-06 over-
    # tighten) false-reds ~1 day every week on a healthy cadence. 8d = the 7-day
    # cadence + 1 day grace: green on time, red only once a Sunday run is genuinely
    # missed (the IG's real intent — catch a DARK watchdog, not nurse the cadence).
    add("self-test cadence", st_age_d <= 8.0,
        f"last self-test {st_age_d:.1f}d ago" if st_age_d < 1e7 else
        "self-test has not run in >8d (or never) — the watchdog-of-watchdogs is dark")

    # SELF-TEST RESULT — RECENCY IS NOT OUTCOME (fix briefs 58 + 71, closed
    # 2026-08-19). 'self-test cadence' above answers "did it run?" and nothing
    # answered "did it PASS?", so from 2026-08-17 the durable stamp read
    # {passed: false, firm_suite: "crashed: ... timed out after 900 seconds"}
    # while the health desk reported "last self-test 2.3d ago" in green. The
    # regression suite — the firm's only guard against every safety property it
    # owns — was dead for two days behind a green watchdog, and the answer was
    # sitting in the SAME FILE the cadence check had already loaded, one key
    # away. (Root cause of the crash itself: cycle._preflight_ram_recovery had
    # no sandbox guard, so every simulated cycle fired the host janitor and
    # slept 25s; fixed 2026-08-19, suite back to 928/928 in 7m39s.)
    # A TIMEOUT is called out separately from a FAILURE: they have different
    # repairs (an environment/perf problem vs a broken invariant), and the
    # 900s crash was invisible partly because nothing named that distinction.
    _st_res = state.load("self_test", {}) or {}
    _st_firm = str(_st_res.get("firm_suite") or "")
    _st_week = str(_st_res.get("week_sim") or "")
    _st_timeout = any("timed out" in x or "crashed" in x
                      for x in (_st_firm, _st_week))
    if not _st_res:
        # VACUOUS PASS (IG #1): "no stamp" is the absence of the evidence this
        # check exists to grade, never health.
        add_undet("self-test result",
                  "no self-test artefact has ever been written, so the "
                  "regression suite's OUTCOME is ungraded — 'self-test "
                  "cadence' above grades only when it last ran")
    elif "passed" not in _st_res:
        add_undet("self-test result",
                  "the self-test artefact carries no pass/fail key "
                  f"({sorted(_st_res)[:4]}) — outcome ungraded")
    else:
        add("self-test result", bool(_st_res.get("passed")),
            (f"regression suite {'CRASHED/TIMED OUT' if _st_timeout else 'FAILED'} "
             f"— firm[{_st_firm[:70]}] week_sim[{_st_week[:40]}] "
             "— the firm's only guard on its own safety properties is down; "
             "repair before the next session"
             if not _st_res.get("passed") else
             f"firm[{_st_firm[:40]}] week_sim[{_st_week[:30]}] as of "
             f"{str(_st_res.get('ts'))[:19]}"))

    # Verdict distribution / monoculture (IG 2026-07-06 high, ratified): the
    # deterministic checks all pass while the alpha engine silently stops
    # generating differentiated calls — it emitted 27/27 + 41/41 HOLD and could not
    # clear its own BUY bar, so overlooked winners piled up unseen. Red when the
    # last N completed cycles that HAD verdicts all produced ZERO BUY-side
    # (STRONG_BUY/BUY) verdicts — the engine is inert as a signal generator. (Market
    # regime can legitimately yield few buys; N consecutive all-HOLD cycles is the
    # miscalibration signal, not a single cautious cycle.)
    _mono_n = int(getattr(config, "HEALTH_MONOCULTURE_CYCLES", 6))
    _hist = [h for h in (research.get("history") or []) if h.get("verdicts")]
    _recent = _hist[-_mono_n:]

    def _buy_side(cyc):
        return sum(1 for v in (cyc.get("verdicts") or {}).values()
                   if str(v.get("rating", "")).upper() in ("BUY", "STRONG_BUY"))
    _mono = len(_recent) >= _mono_n and all(_buy_side(c) == 0 for c in _recent)
    if not _recent:
        # VACUOUS PASS (IG #1): "no completed cycles with verdicts yet" read as
        # a green distribution check. Zero cycles is precisely the state in
        # which a monoculture cannot be ruled out.
        add_undet("verdict distribution",
                  "0 completed cycles carry verdicts in retained history, so "
                  f"the {_mono_n}-cycle monoculture window measured nothing")
    else:
        add("verdict distribution", not _mono,
            (f"MONOCULTURE: 0 BUY-side verdicts across the last {_mono_n} cycles — the "
             "alpha engine can't clear its own action bar (check calibration / the bar)")
            if _mono else
            # A GREEN LINE MUST NOT STATE A ZERO WITHOUT ITS STREAK (fix brief
            # 2026-07-28 #9). "last cycle 0 BUY-side of 28 verdicts" with ok=True
            # is technically correct — one quiet cycle is not a monoculture —
            # but it reads as a false green, and the IG (reasonably) filed it as
            # one. Show how far along the streak is, so "not yet a defect" and
            # "nothing wrong" can never look alike.
            ((f"last cycle 0 BUY-side of "
              f"{len(_recent[-1].get('verdicts') or {})} verdicts — "
              f"{sum(1 for c in reversed(_recent) if _buy_side(c) == 0)} of "
              f"{_mono_n} consecutive zero-BUY cycles toward the monoculture bar")
             if _buy_side(_recent[-1]) == 0 else
             f"last cycle {_buy_side(_recent[-1])} BUY-side of "
             f"{len(_recent[-1].get('verdicts') or {})} verdicts"
             + (f" ({len(_recent)}/{_mono_n} cycles in the monoculture window)"
                if len(_recent) < _mono_n else "")))

    # Disk space (2026-06-14 audit A10): every state write swallows OSError by
    # design (a full disk must never crash a monitor loop) — but that means a full
    # disk turns the fund into a read-only zombie that LOOKS alive while silently
    # persisting nothing. Surface low free space BEFORE it gets there.
    try:
        import shutil
        free_gb = shutil.disk_usage(str(config.STATE_DIR)).free / 1e9
        add("disk space", free_gb >= config.HEALTH_MIN_FREE_DISK_GB,
            f"{free_gb:.1f} GB free" + ("" if free_gb >= config.HEALTH_MIN_FREE_DISK_GB
             else " — LOW, state writes may start silently failing"))
    except Exception as e:
        add("disk space", False, f"could not read disk usage: {str(e)[:60]}")

    # RAM headroom (audit 2026-07-04 F1a — IG critical ratified): host-RAM
    # starvation is this box's CHRONIC failure mode (workers scale down, Opus
    # calls time out into degraded quant-only mode, retries defer), yet no
    # health line watched it. Operator 2026-07-07: FAIL only when free RAM is
    # AT/BELOW config.SCAN_DOWNGRADE_RAM_GB (1.0 GB) — the exact point at which the
    # research cycle actually downgrades (workers -> 1, full -> standard). Above
    # the floor the fund runs full research, so no red. (Was a two-tier warn <
    # 2.5 / crit < 1.5 GB that reddened almost every cycle on this ~2 GB-free
    # box.) Also FAIL if the journal shows the retry loop deferring on RAM
    # repeatedly (cycle_retry_deferred_ram
    # >= HEALTH_MAX_RAM_DEFERRALS_24H in 24h — the exact 2026-07-03 signature:
    # 5 deferrals while every point-in-time check read green). psutil-based,
    # matching cycle.py/app.py; no psutil = RAM unreadable, but the journal
    # deferral count still gates (it needs no psutil to have been written).
    try:
        _ram_cut = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        _ram_defers = sum(1 for e in state.read_journal(1500)
                          if e.get("event") == "cycle_retry_deferred_ram"
                          and e.get("ts", "") >= _ram_cut)
        try:
            import psutil as _ps_h
            _avail_ram = _ps_h.virtual_memory().available / 1e9
        except ImportError:
            _avail_ram = None
        _scan_floor = float(getattr(config, "SCAN_DOWNGRADE_RAM_GB", 1.0))
        _max_defers = int(getattr(config, "HEALTH_MAX_RAM_DEFERRALS_24H", 2))
        _defer_bad = _ram_defers >= _max_defers
        # Operator 2026-07-07: research only downgrades (worker count -> 1 AND
        # 'full' depth -> 'standard') when free RAM is AT/BELOW the scan floor
        # (config.SCAN_DOWNGRADE_RAM_GB, 1.0 GB). Above it the fund runs full
        # research, so 'ram headroom' must NOT red for merely-low RAM — only when
        # a real downgrade is actually in force. (Was a two-tier warn < 2.5 GB /
        # crit < 1.5 GB that reddened essentially every cycle on this ~2 GB-free
        # box and trained the operator to ignore the health desk.)
        _ram_low = (_avail_ram is not None and _avail_ram <= _scan_floor)
        _defer_note = (f", {_ram_defers} RAM-deferred cycle retries in 24h"
                       if _ram_defers else "")
        # Market-aware severity (operator 2026-07-04): low RAM only bites when a
        # research cycle can actually run — on a closed market keep the TEXT but
        # report ok=True. Trading day: red exactly as the downgrade fires.
        _ram_trading = market_hours.is_trading_day()
        if _avail_ram is None:
            add("ram headroom", not (_defer_bad and _ram_trading),
                "psutil unavailable — free RAM unreadable" + _defer_note
                + (" — retry loop starving, close other sessions/apps"
                   if _defer_bad else ""))
        elif _ram_low:
            add("ram headroom", not _ram_trading,
                f"{_avail_ram:.1f} GB free — at/below the {_scan_floor:.1f} GB scan "
                "floor: research downgrades to 1 worker / quant-only degraded mode; "
                "close other sessions/apps before the next cycle" + _defer_note
                + ("" if _ram_trading else
                   " (market closed — free RAM / reboot before the next open)"))
        elif _defer_bad:
            add("ram headroom", not _ram_trading,
                f"{_avail_ram:.1f} GB free but{_defer_note} — the retry loop is "
                "starved; close other sessions/apps"
                + ("" if _ram_trading else " (market closed)"))
        else:
            add("ram headroom", True,
                f"{_avail_ram:.1f} GB free{_defer_note}")
    except Exception as e:
        add("ram headroom", True, f"RAM check skipped: {str(e)[:60]}")

    # Starvation CHAIN (IG 2026-07-16 item 5): low RAM -> workers throttled ->
    # deadline pressure -> degraded verdicts is ONE failure that read as three
    # independent green/informational lines. Assert the linkage explicitly:
    # when the SAME 24h window shows a worker throttle AND degraded verdicts
    # (with any RAM signal), fail ONE named check so the causal chain is seen
    # — not three panels that each individually look survivable.
    try:
        _ch_cut = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        _ch_j = state.read_journal(1500)
        _ch_throttle = next(
            (e for e in reversed(_ch_j)
             if e.get("event") == "research_workers" and e.get("ts", "") >= _ch_cut
             and (e.get("effective") or 0) < (e.get("base") or 0)), None)
        _ch_deg = sum(1 for e in _ch_j
                      if e.get("event") in ("research_degraded",
                                            "research_pace_skipped_degraded")
                      and e.get("ts", "") >= _ch_cut)
        _ch_ram = _ram_low or _defer_bad
        # Restart-storm leg (IG 2026-07-20 #9): a RAM storm that kills the
        # process expresses as a keepalive restart CLUSTER, not as a worker
        # throttle — 19 kills + 3 restarts in 10 min on 07-20 while this
        # check read 'no chain'. Fold ram_cleaner storm=true + a restart
        # cluster (>=3 keepalive_restarts inside 30 min) into the same named
        # failure so the causal chain is one line, not two green panels.
        # ORGANIC restarts only (operator 2026-07-20 "still showing up?"):
        # a keepalive relaunch that follows a restart_requested (API code
        # reload) within 10 min is a DELIBERATE deploy, not a crash — six
        # deploy reloads in one hardening session inflated the count to 10
        # and would false-cluster on any future iterate-and-restart day.
        _ch_req = [e.get("ts", "") for e in _ch_j
                   if e.get("event") == "restart_requested"]

        def _deliberate(ts: str) -> bool:
            try:
                t = datetime.fromisoformat(ts)
                return any(0 <= (t - datetime.fromisoformat(rq)).total_seconds()
                           <= 600 for rq in _ch_req if rq)
            except (ValueError, TypeError):
                return False

        _ch_all_re = sorted(e.get("ts", "") for e in _ch_j
                            if e.get("event") == "keepalive_restart"
                            and e.get("ts", "") >= _ch_cut)
        _ch_deploys = sum(1 for ts in _ch_all_re if _deliberate(ts))
        _ch_restarts = [ts for ts in _ch_all_re if not _deliberate(ts)]
        _ch_cluster = False
        for _i in range(len(_ch_restarts) - 2):
            try:
                _span = (datetime.fromisoformat(_ch_restarts[_i + 2])
                         - datetime.fromisoformat(_ch_restarts[_i]))
                if _span <= timedelta(minutes=30):
                    _ch_cluster = True
                    break
            except (ValueError, TypeError):
                continue
        _ch_storm = next(
            (e for e in reversed(_ch_j)
             if e.get("event") == "ram_cleaner" and e.get("storm")
             and e.get("ts", "") >= _ch_cut), None)
        if _ch_throttle and _ch_deg:
            add("starvation chain", False,
                f"RAM->workers->degraded chain live in 24h: workers throttled to "
                f"{_ch_throttle.get('effective')} (base {_ch_throttle.get('base')})"
                + (f", RAM {'at floor' if _ram_low else 'deferrals'}" if _ch_ram else "")
                + f", {_ch_deg} degraded-research event(s) — the degraded verdicts "
                "are MANUFACTURED by host starvation; free RAM before the next "
                "cycle (close other sessions), buys from degraded verdicts stay "
                "blocked by design")
        elif _ch_storm and (_ch_cluster or _ch_throttle or _ch_deg):
            # Say WHEN the look-back clears (operator 2026-07-20 asked "is it
            # really fixed?" at a red that was correctly aging out — a 24h
            # window line must carry its own expiry or it reads as live).
            try:
                _ch_clear = (datetime.fromisoformat(_ch_storm.get("ts", ""))
                             + timedelta(hours=24)).strftime("%H:%M UTC %m-%d")
            except (ValueError, TypeError):
                _ch_clear = "24h after the storm"
            add("starvation chain", False,
                f"RAM STORM->restart chain in the 24h window: ram_cleaner "
                f"storm killed {_ch_storm.get('killed')} process(es) "
                f"(before {_ch_storm.get('before_gb')} GB)"
                + (f", {len(_ch_restarts)} organic crash restart(s) incl. a "
                   "3-in-30-min cluster" if _ch_cluster
                   else f", {len(_ch_restarts)} organic crash restart(s)")
                + (f" ({_ch_deploys} deliberate code-reload restart(s) "
                   "excluded)" if _ch_deploys else "")
                + " — free RAM (close other sessions); this look-back line "
                f"self-clears {_ch_clear}")
        elif not any(e.get("ts", "") >= _ch_cut for e in _ch_j):
            # VACUOUS PASS (IG #1): every leg of the chain is a journal event
            # in the last 24h. With no event inside the window there is no
            # chain to find, and "no chain" said nothing.
            add_undet("starvation chain",
                      "0 journal events fall inside the 24h window, so the "
                      "RAM->workers->degraded chain had nothing to link")
        else:
            add("starvation chain", True,
                "no linked RAM->workers->degraded chain in 24h"
                + (" (workers throttled but 0 degraded)" if _ch_throttle else "")
                + (f" ({len(_ch_restarts)} isolated organic restart(s), "
                   "no storm linkage)" if _ch_restarts and not _ch_storm else "")
                + (f" ({_ch_deploys} code-reload restart(s), deliberate)"
                   if _ch_deploys and not _ch_restarts else ""))
    except Exception as e:
        add("starvation chain", True, f"chain check skipped: {str(e)[:50]}")

    net = internet_up()
    add("internet", net, "connected" if net else
        "OFFLINE — cycles/exits retry automatically when connectivity returns")

    # Parking concentration (re-grade audit 2026-07-16: 39% of firm equity in
    # one parking ETF sat outside every firm-wide cap). Visibility, not a hard
    # block — parking IS benchmark beta by design; past the alert share the
    # desk should know most of the fund is one unresearched instrument.
    # PER-SLEEVE leg (2026-07-25 audit finding 8): the firm-level bar sat ABOVE
    # the incident its own comment cites and had no per-book leg, so 27.9%
    # firm-wide read GREEN while calls_only was 86.9% RSP holding ZERO option
    # positions — a $25k options desk that is, in substance, a beta proxy. The
    # firm number can never see that: one sleeve fully parked is only ~25% of
    # the firm. Each sleeve is now measured against its own share bar and the
    # real (non-parking) position count rides in the detail either way, so the
    # number can never be invisible again.
    try:
        _pk_val, _firm_eq = 0.0, 0.0
        _pk_by_sleeve: dict[str, tuple] = {}
        for _k in config.SLEEVES:
            _sv = state.get_sleeve(_k)
            _eqv = (_sv.get("cash") or 0)
            _s_park, _s_real = 0.0, 0
            for _p in (_sv.get("positions") or {}).values():
                _v = (_p.get("quantity") or 0) * (_p.get("last_price")
                                                  or _p.get("entry_price") or 0)
                _eqv += _v
                if _p.get("cash_equitization"):
                    _pk_val += _v
                    _s_park += _v
                else:
                    _s_real += 1
            _firm_eq += _eqv
            _pk_by_sleeve[_k] = (_s_park / _eqv * 100 if _eqv > 0 else 0.0,
                                 _s_real)
        _pk_pct = _pk_val / _firm_eq * 100 if _firm_eq > 0 else 0.0
        _pk_alert = float(getattr(config, "PARKING_FIRM_ALERT_PCT", 45.0))
        _pk_sl_alert = float(getattr(config, "PARKING_SLEEVE_ALERT_PCT", 85.0))
        # A FLAT BOOK IS NOT A DEAD MANDATE (operator 2026-08-19, "lock raven
        # capital needs to be fixed"). This leg fired on an INSTANTANEOUS
        # reading — parked% and open-real-position count RIGHT NOW — and a desk
        # that is correctly flat between trades is indistinguishable from one
        # whose gate is broken: both read "95% parked, 0 real". It escalated
        # calls_only as "that mandate is not being expressed" while that sleeve
        # was the fund's BEST performer: on 2026-08-14 it sold RSP parking,
        # opened GEO/IOVA/VAC, ran its TP ladder (+$1,672.38 realised in three
        # days) and closed the last leg 08-17, ending at $26,688 on $25,000
        # (+6.8% lifetime). Parking between trades is the mandate WORKING —
        # idle cash earning beta until the next qualifying signal.
        # So the per-sleeve leg now needs BOTH conditions: parked past the bar
        # AND no real (non-parking) activity for PARKING_SLEEVE_IDLE_DAYS. The
        # firm-wide leg is untouched — a whole fund in parking is still a red
        # regardless of recency. Trade recency is read from SLEEVE STATE, never
        # from a journal tail: a count window cannot answer a time question
        # (health.py:1287), and this line must not inherit that defect.
        _pk_idle_d = float(getattr(config, "PARKING_SLEEVE_IDLE_DAYS", 10.0))

        def _last_real_activity(_sv: dict) -> str:
            """Newest ISO ts at which this sleeve held or closed a REAL
            (non-parking) position. Parking legs are excluded by every marking
            the books have used: the meta flag, strategy=cash_equitization,
            and the 'equitization' reason text."""
            _stamps = [str(_p.get("opened_at") or "")
                       for _p in (_sv.get("positions") or {}).values()
                       if not _p.get("cash_equitization")]
            for _t in (_sv.get("closed_trades") or []):
                if (_t.get("cash_equitization")
                        or str(_t.get("strategy")) == "cash_equitization"
                        or "equitization" in str(_t.get("reason", "")).lower()):
                    continue
                _stamps.append(str(_t.get("closed_at") or ""))
            return max([x for x in _stamps if x], default="")

        _pk_idle: dict[str, float] = {}
        _pk_hot, _pk_recent = [], []
        for _k, (_pct, _n) in sorted(_pk_by_sleeve.items()):
            if _pct < _pk_sl_alert:
                continue
            _age_d = _age_hours(_last_real_activity(state.get_sleeve(_k))) / 24
            _pk_idle[_k] = _age_d
            (_pk_hot if _age_d > _pk_idle_d else _pk_recent).append(_k)
        _pk_ok = _pk_pct < _pk_alert and not _pk_hot
        if _firm_eq <= 0:
            # VACUOUS PASS (IG #1): with zero firm equity every share is 0% and
            # "0% of firm equity in parking" reads as the healthiest possible
            # book — over a denominator that does not exist.
            add_undet("parking concentration",
                      f"firm equity is {_firm_eq:.0f} across "
                      f"{len(config.SLEEVES)} sleeve(s), so every parking share "
                      "divides by zero and no concentration was measured")
        else:
            add("parking concentration", _pk_ok,
                f"{_pk_pct:.0f}% of firm equity in parking ("
                + ", ".join(f"{k.split('_')[0]} {pct:.0f}%/{n} real"
                            for k, (pct, n) in sorted(_pk_by_sleeve.items()))
                + ")"
                + ("" if _pk_pct < _pk_alert else
                   f" — above the {_pk_alert:.0f}% firm visibility bar; most of "
                   "the fund is one unresearched instrument (deployment scaler + "
                   "weight anchors should be pulling this down)")
                + ("" if not _pk_hot else
                   " — SLEEVE(S) EFFECTIVELY ALL PARKING: "
                   + ", ".join(f"{k} {_pk_by_sleeve[k][0]:.0f}% parked with "
                               f"{_pk_by_sleeve[k][1]} real position(s), no real "
                               f"trade in {_pk_idle.get(k, 0):.0f}d"
                               for k in _pk_hot)
                   + f" (past the {_pk_sl_alert:.0f}% per-sleeve bar AND idle "
                   f"past the {_pk_idle_d:.0f}d bar; that mandate is not being "
                   "expressed — the capital is holding benchmark beta, not "
                   "running its strategy)")
                # A green line must show its streak, never a bare zero (fix
                # brief 2026-07-28 #9): name the between-trades sleeves so
                # "parked and working" can never look like "parked and broken".
                + ("" if not _pk_recent else
                   " — BETWEEN TRADES (parked past the "
                   f"{_pk_sl_alert:.0f}% bar but traded recently, so the "
                   "mandate IS being expressed): "
                   + ", ".join(f"{k} {_pk_by_sleeve[k][0]:.0f}% parked, last "
                               f"real trade {_pk_idle.get(k, 0):.1f}d ago "
                               f"(idle bar {_pk_idle_d:.0f}d)"
                               for k in _pk_recent)))
    except Exception as e:
        add_undet("parking concentration",
                  "the parking-concentration scan could not run, so 0 sleeves "
                  "were measured", f"error: {str(e)[:50]}")

    # Live-parity universe (operator 2026-07-16): paper must see what Public
    # can trade — a missing/stale/thin snapshot means the parity guardrails
    # are down. Off-hours it carries the story instead of redding.
    try:
        from firm import tradable as _trd
        _tu_ok, _tu_detail = _trd.universe_health()
        add("tradable universe", _tu_ok or not market_hours.is_trading_day(),
            _tu_detail + ("" if _tu_ok else
                          (" (market closed — premarket refresh rebuilds it)"
                           if not market_hours.is_trading_day() else "")))
    except Exception as e:
        add("tradable universe", True, f"check skipped: {str(e)[:50]}")

    halted = [k for k in config.SLEEVES if state.get_sleeve(k).get("halted")]
    add("risk halts", not halted,
        f"halted: {', '.join(halted)}" if halted else "all sleeves trading")

    # Halt churn (IG 2026-07-01 medium): an oscillating breaker (trip/clear/
    # trip) hides behind the instantaneous "all sleeves trading" above — count
    # the trip EVENTS over a rolling window, not the current state. Restart
    # frequency is deliberately NOT re-counted here: _coverage_signals already
    # budgets restarts/failed_restarts (verified_up=False = the true
    # crash-loop signal) into the coverage-over-time check below.
    #
    # WINDOW MATCHED TO THE PRODUCER (2026-07-25 audit finding 7): the check
    # could not detect the oscillation it exists for. The breaker journals at
    # most one trip per sleeve per calendar day (risk.py gates on `not halted`
    # / `not risk_event_active`, and a kill switch cools off 72h), so a sleeve
    # tripping EVERY day contributes 2 events against a 48h/4 firm-wide bar.
    # Verified: etf_rotation tripped 06-23, 07-02, 07-07, 07-13 and 07-16 and
    # the line read "no halt trips in 48h" on four of those five days. The
    # window is now 14 days and the assertion is PER SLEEVE (3+ trips = one
    # book repeatedly breaking), which fires on exactly that pattern; the
    # firm-wide sum stays as a second leg for a correlated day.
    try:
        _churn_win = float(getattr(config, "HEALTH_HALT_CHURN_WINDOW_H", 336.0))
        _churn_min = int(getattr(config, "HEALTH_HALT_CHURN_MIN_TRIPS", 3))
        _churn_firm = int(getattr(config, "HEALTH_HALT_CHURN_FIRM_TRIPS", 6))
        _cut_ch = (datetime.now(timezone.utc)
                   - timedelta(hours=_churn_win)).isoformat()
        _trip_ev = ("breaker_tripped", "kill_switch", "soft_risk_on",
                    "sleeve_halted_reconcile", "sleeve_halted_exit_failure")
        _trips: dict = {}
        _ch_journal = state.read_journal(3000)        # newest-first, capped
        for e in _ch_journal:
            if e.get("ts", "") >= _cut_ch and e.get("event") in _trip_ev:
                if e.get("advisory"):
                    continue   # rotation's non-binding trips gate nothing (fix brief 2026-07-16 #5)
                _s = e.get("sleeve", "?")
                _trips[_s] = _trips.get(_s, 0) + 1
        _n_trips = sum(_trips.values())
        _churny = sorted(k for k, v in _trips.items() if v >= _churn_min)
        _win_d = _churn_win / 24
        # WINDOW COVERAGE (IG 2026-07-27 #1). The 14-day assertion is only as
        # deep as the journal tail we can actually READ — read_journal caps at
        # the 3,000 newest lines, and on a busy desk those can span far less
        # than 14 days. "no halt trips in 14d" over a tail that reaches back 3
        # days is a claim about 11 days nobody looked at: the vacuous pass in
        # its most dangerous form, because the check was BUILT to catch a
        # slow-oscillating breaker that only trips once per sleeve per day.
        _ch_oldest = str((_ch_journal[-1] if _ch_journal else {}).get("ts") or "")
        _ch_covered_d = (_age_hours(_ch_oldest) / 24) if _ch_oldest else 0.0
        _ch_short = bool(_ch_oldest) and _ch_oldest > _cut_ch
        if not _ch_journal:
            add_undet("risk-halt churn",
                      f"0 journal events readable, so the {_win_d:.0f}d halt-trip "
                      "window covers nothing")
        elif not _n_trips and _ch_short:
            add_undet("risk-halt churn",
                      f"0 halt trips found, but the 3,000-line journal tail only "
                      f"reaches back {_ch_covered_d:.1f}d of the {_win_d:.0f}d "
                      "window — the rest of the window was never read, so "
                      "'no trips' is unverified",
                      "raise the journal read budget or shorten "
                      "HEALTH_HALT_CHURN_WINDOW_H to make this assertable")
        else:
            add("risk-halt churn", not _churny and _n_trips < _churn_firm,
                (f"no halt trips in the {_win_d:.0f}d window "
                 f"({_ch_covered_d:.1f}d of journal read)") if not _n_trips else
                (", ".join(f"{k}×{v}" for k, v in sorted(_trips.items()))
                 + f" in {_win_d:.0f}d"
                 + ("" if not _churny else
                    f" — OSCILLATING: {', '.join(_churny)} tripped "
                    f"{_churn_min}+ times; the instantaneous halt state above "
                    "understates it (one trip per sleeve per day is the "
                    "producer's ceiling, so this IS a daily breaker)")
                 + ("" if _churny or _n_trips < _churn_firm else
                    " — firm-wide trip count over budget; correlated risk events")))
    except Exception as e:
        add("risk-halt churn", False, f"could not read journal: {str(e)[:60]}")

    # (Removed 2026-07-02: the cross-sleeve overlap check. Holding the same name
    # in >1 sleeve is INTENDED — a real multi-strategy fund does this; the
    # sleeves are independent books. It is not a defect and must not red.)

    # Cross-sleeve routing (operator 2026-07-02: "make sure a buy is placed
    # across the right sleeves — if it qualifies for core too, put it there or
    # say why"). RED only on a SILENT DROP — a name that qualified for a book
    # with an open slot yet was never placed and left no recorded reason.
    try:
        from firm import routing
        _rt_ok, _rt_detail = routing.routing_health()
        add("sleeve routing", _rt_ok, _rt_detail)
    except Exception as e:
        add("sleeve routing", True, f"routing check skipped: {str(e)[:50]}")

    # Options-gate reachability (IG 2026-07-16 item 7): every verdict class the
    # committee emits must have a route that CAN fire — the original calls gate
    # sat unreachable for weeks keyed on a rating the judge never emitted, and
    # the puts mirror could silently repeat that. Fail only on a LARGE real
    # sample with ZERO gate-qualified verdicts (validate-before-harden: a young
    # desk with few sell-side verdicts must not false-red).
    try:
        from firm import routing as _rtg
        _cov = _rtg.route_coverage()
        # Threshold keyed to the OBSERVED emission rate (audit 2026-07-16 #3:
        # at >=40 the watchdog could never fire — the committee emits ~32
        # BUY-family verdicts per 30-cycle window).
        _min_emit = int(getattr(config, "HEALTH_OPTIONS_GATE_MIN_EMITTED", 25))
        _dead = []
        if _cov.get("sell_family_emitted", 0) >= _min_emit \
                and _cov.get("puts_gate_qualified", 0) == 0:
            _dead.append(f"puts gate: {_cov['sell_family_emitted']} real "
                         "SELL-family verdicts, 0 ever qualified")
        if _cov.get("buy_family_emitted", 0) >= _min_emit \
                and _cov.get("calls_gate_qualified", 0) == 0:
            _dead.append(f"calls gate: {_cov['buy_family_emitted']} real "
                         "BUY-family verdicts, 0 ever qualified")
        # Per-side open-vs-qualified reconcile (fix brief 2026-07-17 #1): an
        # option open with NO decision-time qualification event for its
        # underlying in the same journal window is a gate bypass or broken
        # accounting — a HARD fail, unlike the display drift between today's
        # re-evaluated gate and weeks-old opens that triggered the brief.
        _rec = _cov.get("gate_reconcile") or {}
        _unm_c = _rec.get("calls_unmatched_opens") or []
        _unm_p = _rec.get("puts_unmatched_opens") or []
        if _unm_c or _unm_p:
            _unm_parts = []
            if _unm_c:
                _unm_parts.append(f"calls: {', '.join(_unm_c)}")
            if _unm_p:
                _unm_parts.append(f"puts: {', '.join(_unm_p)}")
            _dead.append(
                "GATE BYPASS: option open(s) with no matching decision-time "
                "qualification — " + "; ".join(_unm_parts))
        # Qualified-but-never-converted (fix brief 2026-07-21 #6): the inverse
        # of the bypass — a decision-time qualified symbol that neither opened
        # nor journaled a skip/block reason in its own cycle evaporated
        # SILENTLY. Explained skips (no liquid contract, earnings inside the
        # window, size zero, caps) are the desk being honest, not inert — they
        # ride in the green detail instead of redding the line.
        _gc = _cov.get("gate_conversion") or {}
        _silent_parts = []
        for _sd in ("calls", "puts"):
            _sil = (_gc.get(_sd) or {}).get("unexplained_qualified") or []
            if _sil:
                _silent_parts.append(f"{_sd}: {', '.join(_sil)}")
        if _silent_parts:
            _dead.append(
                "INERT CONVERSION: gate-qualified symbol(s) with no open AND "
                "no journaled skip reason — " + "; ".join(_silent_parts))
        # (2026-08-13 IG audit) qualification EVENTS with a ZERO conversion
        # denominator must FAIL, not pass: the denominator is built FROM those
        # events' symbol lists, so this state means the events carry no
        # symbols — the conversion measurement itself is broken, and "0 of 0"
        # would otherwise render as health (the IG #13 self-zeroing shape).
        for _sd in ("calls", "puts"):
            _evn = int(_rec.get(f"{_sd}_qualified_events") or 0)
            _den = int((_gc.get(_sd) or {}).get("qualified_symbol_cycles") or 0)
            if _evn and not _den:
                _dead.append(
                    f"{_sd}: {_evn} decision-time gate-qualified event(s) "
                    "recorded but the conversion denominator is 0 — the "
                    "qualification events carry no symbols, so conversion "
                    "cannot be measured")
        _gc_c = _gc.get("calls") or {}
        _gc_p = _gc.get("puts") or {}
        # PUTS LIVENESS MEASURES THE GATE, NOT A PROXY (IG 2026-07-27 #13).
        # The line read `puts 0/5 qualified ... P 0open/0explained of 0` and
        # passed, because BOTH legs above sit downstream of the gate: the
        # hard-fail leg needs >= _min_emit (25) sell-family verdicts before it
        # can fire, and the conversion leg's denominator is decision-time
        # `options_gate_qualified` EVENTS, which only exist when the gate
        # qualifies someone. Zero qualifications zero the denominator, and
        # "0 of 0" rendered as health. The ledger below asks the gate itself:
        # over the same window's real SELL-family verdicts, did it produce a
        # qualification OR an explicit per-name rejection? Either is evidence
        # it ran. NEITHER means the puts gate is UNMEASURED — undetermined,
        # never ok. Measurement only; no trading path changes.
        _pg = _puts_gate_decisions(research.get("history") or [])
        _pg_q = len(_pg.get("qualified") or [])
        _pg_r = len(_pg.get("rejected") or [])
        _pg_why = "; ".join(f"{s}: {w}" for s, w
                            in list((_pg.get("reasons") or {}).items())[:3])
        _pg_txt = (f"; puts-gate ledger {_pg_q} qualified / {_pg_r} explicitly "
                   f"rejected of {_pg.get('evaluated', 0)} real SELL-family "
                   "verdict(s)" + (f" ({_pg_why})" if _pg_why else ""))
        if _dead:
            add("options gate reachability", False,
                "; ".join(_dead) + " — the gate is unreachable in practice "
                "(the exact prior calls-gate bug class); revisit its conditions"
                + _pg_txt)
        elif not (_pg_q or _pg_r):
            add_undet("options gate reachability",
                      "the puts gate recorded 0 qualifications AND 0 per-name "
                      f"rejections over {_cov.get('sell_family_emitted', 0)} "
                      f"SELL-family verdict(s) in the "
                      f"{_cov.get('window_cycles', 0)}-cycle window — the gate "
                      "was never exercised, so nothing about its reachability "
                      "was measured"
                      + (f"; ledger error: {str(_pg['error'])[:50]}"
                         if _pg.get("error") else ""),
                      f"calls side: {_cov.get('calls_gate_qualified', 0)}/"
                      f"{_cov.get('buy_family_emitted', 0)} qualified")
        else:
            add("options gate reachability", True,
                f"calls {_cov.get('calls_gate_qualified', 0)}/"
                f"{_cov.get('buy_family_emitted', 0)} qualified, "
                f"puts {_cov.get('puts_gate_qualified', 0)}/"
                f"{_cov.get('sell_family_emitted', 0)} qualified "
                # "window" not "lifetime" when the rotated journal no longer
                # reaches the reconcile epoch (2026-08-13 IG audit)
                f"({'lifetime' if _rec.get('journal_window_covered', True) else 'journal-window'} "
                f"opens {_rec.get('calls_opened_full_retention', 0)}C/"
                f"{_rec.get('puts_opened_full_retention', 0)}P all reconcile to "
                f"decision-time gate records since {_rec.get('since', '?')}; "
                "conversion C "
                f"{_gc_c.get('converted_opens', 0)}open/"
                f"{_gc_c.get('explained_skips', 0)}explained of "
                f"{_gc_c.get('qualified_symbol_cycles', 0)} qualified, P "
                f"{_gc_p.get('converted_opens', 0)}open/"
                f"{_gc_p.get('explained_skips', 0)}explained of "
                f"{_gc_p.get('qualified_symbol_cycles', 0)})"
                # Aged-out silent drops stay VISIBLE on the green line
                # (operator 2026-08-19): the conversion leg now bounds its
                # silent-drop search to OPTIONS_GATE_CONVERSION_WINDOW_DAYS so a
                # remediated defect can clear, and the price of that is that a
                # real historical drop must never become invisible. Named here,
                # not hidden — green with a footnote, not green by forgetting.
                + (f"; aged out of the "
                   f"{_gc_c.get('conversion_window_days', 14)}d silent-drop "
                   f"window (fixed, retained for audit): "
                   + ", ".join(sorted(set(
                       (_gc_c.get('aged_unexplained') or [])
                       + (_gc_p.get('aged_unexplained') or []))))
                   if (_gc_c.get('aged_unexplained')
                       or _gc_p.get('aged_unexplained')) else "")
                + _pg_txt)
        # ESCALATION (fleet re-grade action 1): a red reachability line aged
        # past the watchdog bar for 5+ weeks as a passive "revisit" note. An
        # unreachable gate on 25% of firm capital is an ENGINEERING defect —
        # file it as a fix brief (deduped weekly) so it reaches the same
        # queue every other firm-found defect does.
        if _dead:
            try:
                from firm import learning as _lrn
                _fb = state.load("fix_briefs", {"items": []}) or {}
                _already = any(
                    "options gate unreachable" in str(it.get("problem", ""))
                    and _age_hours(str(it.get("filed_at", ""))) < 24 * 7
                    for it in (_fb.get("items") or []))
                if not _already:
                    _lrn._file_fix_requests([{
                        "problem": "options gate unreachable: " + "; ".join(_dead),
                        # THE EVIDENCE LINE MUST REPORT WHAT WAS MEASURED
                        # (operator 2026-08-19). This string used to append the
                        # LITERAL "real verdicts, 0 qualified" regardless of
                        # _cov — while the green branch 20 lines above printed
                        # the true `calls_gate_qualified` from the same dict.
                        # On the run that filed fix brief 68 the real numbers
                        # were 15 calls-qualified and 1 puts-qualified, and the
                        # brief still read "139 BUY-family / 31 SELL-family real
                        # verdicts, 0 qualified". Fix brief 69 then inherited
                        # that fabricated zero as its CRITICAL headline — "no
                        # verdict the fund produces can clear the conviction>=70
                        # bar" — about the desk that had just realised +$1,672
                        # on GEO/IOVA/VAC (2026-08-14). Two engineering tickets
                        # and a CRITICAL alert manufactured from one hardcoded
                        # constant. A brief that fabricates its own evidence is
                        # worse than no brief: it sends every reader — operator,
                        # IG and Claude — to fix a gate that is working.
                        "evidence": (f"route_coverage window: "
                                     f"{_cov.get('buy_family_emitted', 0)} BUY-family / "
                                     f"{_cov.get('sell_family_emitted', 0)} SELL-family "
                                     f"real verdicts, "
                                     f"{_cov.get('calls_gate_qualified', 0)} calls / "
                                     f"{_cov.get('puts_gate_qualified', 0)} puts qualified; "
                                     f"conversion C {_gc_c.get('converted_opens', 0)}open/"
                                     f"{_gc_c.get('explained_skips', 0)}explained of "
                                     f"{_gc_c.get('qualified_symbol_cycles', 0)}, "
                                     f"P {_gc_p.get('converted_opens', 0)}open/"
                                     f"{_gc_p.get('explained_skips', 0)}explained of "
                                     f"{_gc_p.get('qualified_symbol_cycles', 0)}"),
                        "suspected_area": "firm/sleeves.py gate battery "
                                          "(_calls_entry_ok/_puts_entry_ok/"
                                          "_mech_options_ok) or firm/debate.py "
                                          "suitability prompt",
                        "ask_claude": "trace why zero verdicts qualify for the "
                                      "options desk despite the veto demotion "
                                      "and mechanical screen; fix the gate or "
                                      "the prompt so reachable ideas reach it",
                    }])
            except Exception:
                pass
    except Exception as e:
        add_undet("options gate reachability",
                  "route_coverage could not run, so 0 verdict-to-route pairs "
                  "were measured", f"error: {str(e)[:50]}")

    # Conviction compression (fleet re-grade action 7): BUY-side conviction
    # ran mean 62.7 / stdev 3.8 with zero STRONG_BUYs in 693 verdicts — every
    # conviction-keyed mechanism (sizing, the calls bar, acceptance bands)
    # then operates on a constant. The judge sees a calibration note; THIS
    # line makes regression OPERATOR-visible, which the static guidance of
    # 07-01 never was while it silently failed for 15 days.
    try:
        _hist_cc = (state.load("research", {}) or {}).get("history", [])[-30:]
        _convs = sorted(
            float(v.get("conviction"))
            for h in _hist_cc for v in (h.get("verdicts") or {}).values()
            if not (v or {}).get("degraded")
            and (v or {}).get("rating") in ("BUY", "STRONG_BUY")
            and isinstance(v.get("conviction"), (int, float)))
        if len(_convs) >= 15:
            _n = len(_convs)
            _mean_cc = sum(_convs) / _n
            _sd = (sum((c - _mean_cc) ** 2 for c in _convs) / _n) ** 0.5
            _spread = (_convs[int(0.9 * (_n - 1))]
                       - _convs[int(0.1 * (_n - 1))])
            _okc = _sd >= 5.0 or _spread > 8.0
            # Escalation streak (fix brief 2026-07-17 #3): warn-only redlines
            # proved ignorable — run_health_sweep counts consecutive FAILING
            # CYCLES (not sweeps) in state/conviction_spread and files a fix
            # brief past the budget; the streak is shown here so the line
            # carries its own history. health_report stays read-only.
            _cs_st = state.load("conviction_spread", {}) or {}
            _cs_n = int(_cs_st.get("streak") or 0)
            add("conviction distribution", _okc,
                f"BUY-family conviction stdev {_sd:.1f}, p90-p10 "
                f"{_spread:.0f} over {_n} verdicts"
                + ("" if _okc else
                   " — COMPRESSED: sizing, the calls bar and the acceptance "
                   "bands all key on a signal that is effectively a constant"
                   + (f" (failing {_cs_n} consecutive cycle(s); escalates to "
                      f"a fix brief at "
                      f"{int(getattr(config, 'CONVICTION_SPREAD_ESCALATE_CYCLES', 10))})"
                      if _cs_n else "")))
        else:
            # The check used to emit NOTHING below the 15-sample bar — worse
            # than a vacuous pass: the LINE DISAPPEARED from the report, and
            # report["ok"] is all() over the checks that survived, so a thin
            # sample made the desk look greener (the same mechanism
            # add_errored was written for). Say the sample is short instead.
            add_undet("conviction distribution",
                      f"{len(_convs)} non-degraded BUY-family conviction value(s) "
                      "in the last 30 cycles — below the 15 needed to measure "
                      "stdev / p90-p10 spread", n=len(_convs))
    except Exception as _e:
        add_errored("conviction distribution", _e)

    # Book-full waiter coverage (fix brief 2026-07-21 #7): every
    # buy_skipped_book_full must resolve, within its own cycle, to a bench
    # waiter (book_full_to_bench) or an explicit discard record
    # (book_full_discarded — e.g. the name placed in ANOTHER sleeve, which is
    # what every "evaporated" 07-20/21 skip had actually done). An uncovered
    # skip is a silently-dying signal — the exact inert-reopen class that bit
    # the bench before. Gated to skips after the discard records shipped so
    # pre-deploy history can't red the line forever.
    try:
        _bfc_since = str(getattr(config, "BOOK_FULL_COVERAGE_SINCE",
                                 "2026-07-21"))
        _bfc_j = state.read_journal(900)
        _bfc_cover: set = set()
        for _e in _bfc_j:
            if _e.get("event") == "book_full_to_bench":
                _bfc_cover.add((str(_e.get("ts", ""))[:10],
                                str(_e.get("ticker", ""))))
            elif _e.get("event") == "book_full_discarded":
                _bfc_cover.add((str(_e.get("ts", ""))[:10],
                                str(_e.get("symbol", ""))))
        _bfc_skips = _bfc_naked = 0
        _bfc_names: list = []
        for _e in _bfc_j:
            if (_e.get("event") != "buy_skipped_book_full"
                    or str(_e.get("ts", ""))[:10] < _bfc_since):
                continue
            for _sy in (_e.get("symbols") or []):
                _bfc_skips += 1
                if (str(_e.get("ts", ""))[:10], str(_sy)) not in _bfc_cover:
                    _bfc_naked += 1
                    _bfc_names.append(str(_sy))
        if _bfc_skips == 0:
            # VACUOUS PASS (IG #1): "every one resolved" over zero skips. The
            # green read identically whether the desk was covering its skips or
            # had simply never skipped — and it is the FIRST uncovered skip
            # this line exists to catch.
            add_undet("book-full waiter coverage",
                      f"0 buy_skipped_book_full event(s) on/after {_bfc_since} in "
                      f"the {len(_bfc_j)}-line journal tail — no skip existed to "
                      "trace to a bench waiter or a discard record")
        else:
            add("book-full waiter coverage", _bfc_naked == 0,
                (f"{_bfc_skips} book-full skip(s) since {_bfc_since}, every one "
                 "resolved to a bench waiter or an explicit discard record")
                if _bfc_naked == 0 else
                (f"{_bfc_naked}/{_bfc_skips} book-full skip(s) with NO waiter and "
                 f"NO discard record ({', '.join(sorted(set(_bfc_names))[:6])}) — "
                 "the signal evaporated with no re-entry path"))
    except Exception as _e:
        add_errored("book-full waiter coverage", _e)

    # Sell-path utilization (fix brief 2026-07-21 #8): HOLD as a dumping
    # ground. When many graded HOLDs ran DEEPLY negative forward excess while
    # the committee emitted ~zero sell-family ratings, the SELL route is
    # structurally under-used AND the buy-vs-sell calibration check can never
    # collect its sample — "insufficient sample" green forever is starvation,
    # not health. Reads only the persisted report-card stats (no network).
    try:
        _spu = state.load("report_card_stats", {}) or {}
        _spu_hold = ((_spu.get("by_rating_summary") or {}).get("HOLD")
                     or {})
        _spu_deep = int(_spu_hold.get("deep_neg_n") or 0)
        _spu_hist = (state.load("research", {}) or {}).get("history", [])[-30:]
        _spu_sells = sum(
            1 for _h in _spu_hist
            for _v in (_h.get("verdicts") or {}).values()
            if not (_v or {}).get("degraded")
            and (_v or {}).get("rating") in ("SELL", "STRONG_SELL"))
        _spu_min_deep = int(getattr(config, "SELL_PATH_DEEP_NEG_MIN", 5))
        _spu_ok = not (_spu_deep >= _spu_min_deep and _spu_sells <= 1)
        _spu_starved = (_spu.get("calibration_undecidable") is True
                        and _spu_sells <= 1)
        # Both legs need a population (IG #1). The deep-negative leg divides
        # into GRADED HOLD rows; the emission leg divides into cycles carrying
        # verdicts. With either at zero the conjunction is trivially satisfied
        # and the green says only "the report card has not graded anything
        # yet" — which is itself the pipeline failure 'verdict grading' hunts,
        # laundered here as health.
        _spu_hold_n = int(_spu_hold.get("n") or 0)
        if _spu_hold_n == 0 or not _spu_hist:
            add_undet("sell-path utilization",
                      f"{_spu_hold_n} graded HOLD row(s) and {len(_spu_hist)} "
                      "cycle(s) with verdicts — the HOLD-dumping-ground leg and "
                      "the sell-emission leg cannot both be measured",
                      n=min(_spu_hold_n, len(_spu_hist)))
        else:
            add("sell-path utilization", _spu_ok,
                (f"{_spu_deep} graded HOLD(s) ran <-15% fwd excess vs "
                 f"{_spu_sells} sell-family verdict(s) in 30 cycles"
                 + (" — calibration 'insufficient sample' is structurally "
                    "STARVED (needs 8 sell-side graded; unfalsifiable at this "
                    "emission rate)" if _spu_starved else ""))
                if not _spu_ok else
                (f"{_spu_sells} sell-family verdict(s)/{len(_spu_hist)} cycles, "
                 f"{_spu_deep} deep-negative of {_spu_hold_n} graded HOLD(s)"
                 + (" — calibration sample still starved on the sell side"
                    if _spu_starved else "")))
    except Exception as _e:
        add_errored("sell-path utilization", _e)

    # Macro taxonomy completeness (fix brief 2026-07-21 #10): the tracked
    # release registry is diffed against the KNOWN high-impact universe every
    # sweep — a missing sibling (the ADP/new-home-sales/IP/trade-balance/
    # beige-book class) is a deterministic red, not an Opus discovery.
    # Documented exceptions (no deterministic schedule) ride in the detail.
    try:
        from firm import macro_calendar as _mc_tx
        _tx = _mc_tx.taxonomy_gaps()
        _tx_un = _tx.get("untracked") or []
        _tx_ex = _tx.get("documented_exceptions") or {}
        # HONEST SCOPE (IG 2026-07-26): "universe covered" overstated what this
        # check can know — taxonomy_gaps() diffs the tracked registry against a
        # HARD-CODED sibling list in the same module, so a release class absent
        # from BOTH is invisible. That is a useful regression tripwire, not
        # external coverage. Say what it actually verifies; grounding against a
        # provider feed (FRED releases) is filed as a fix brief.
        # EXTERNALLY GROUNDED NOW (operator 2026-08-21, closing the IG brief).
        # The sibling diff stays as a regression tripwire, but it is no longer
        # the whole check and it no longer gets to imply coverage. Two real
        # legs added, both of which can FAIL for a real reason:
        #   1. UNDECLARED NULLS — a tracked release with no fred_id and no
        #      declared provider reason is an unfinished lookup, i.e. nobody has
        #      established whether FRED carries it. Ten such nulls were resolved
        #      against the live API in this same change (4/24 -> 14/24 grounded);
        #      the remaining ten are genuinely not FRED releases and now say so.
        #   2. GROUNDING SPLIT — report FRED-confirmed vs rule-derived instead
        #      of "24/24 have a forward date", which was true and misleading:
        #      20 of those dates came from _rule_dates, not from any provider.
        _g = _mc_tx.grounding()
        _undeclared = _g.get("undeclared_nulls") or []
        _tx_ok_detail = (
            f"{_g['tracked']} release classes tracked — "
            f"{len(_g['fred_confirmed'])} FRED-confirmed, "
            f"{len(_g['rule_derived'])} rule-derived "
            f"({len(_g['unavailable_reasons'])} declared not-on-FRED); "
            "sibling list covered"
            + (f" ({len(_tx_ex)} documented exception(s): "
               + ", ".join(sorted(_tx_ex)) + ")" if _tx_ex else ""))
        if _tx_un:
            add("macro taxonomy", False,
                "high-impact release class(es) UNTRACKED: " + ", ".join(_tx_un))
        elif _undeclared:
            add("macro taxonomy", False,
                f"{len(_undeclared)} tracked release(s) carry NO fred_id and no "
                "declared provider reason — nobody has established whether the "
                "upstream feed carries them, so their dates are rule-derived by "
                "default rather than by decision: " + ", ".join(_undeclared))
        else:
            add("macro taxonomy", True, _tx_ok_detail)
    except Exception as _e:
        add_errored("macro taxonomy", _e)

    # Verdict-grading liveness (fix brief 2026-07-17 #2): the report-card /
    # calibration layer produced no live output for weeks while 174 verdicts
    # aged past the grading window — report_card_digest_empty was journaled
    # and nothing read it. Deterministic: once enough REAL verdicts have aged
    # past min_age_days, a zero-graded card is a dead pipeline (price history
    # fetch broken), not a young sample. Reads only the stats learning
    # persists each digest refresh — no network here.
    try:
        _rcs = state.load("report_card_stats", {}) or {}
        _hist_vg = (state.load("research", {}) or {}).get("history", [])[-50:]
        _now_vg = datetime.now(timezone.utc)
        _aged = 0
        for _h in _hist_vg:
            try:
                if (_now_vg - datetime.fromisoformat(_h["asof"])).days < 3:
                    continue
            except (KeyError, ValueError, TypeError):
                continue
            _aged += sum(1 for _v in (_h.get("verdicts") or {}).values()
                         if not (_v or {}).get("degraded"))
        _min_aged = int(getattr(config, "HEALTH_GRADING_MIN_AGED", 25))
        if _aged >= _min_aged:
            _g = int(_rcs.get("graded") or 0)
            _rcs_age = _age_hours(_rcs.get("asof", "")) if _rcs.get("asof") else 1e9
            # COVERAGE, not just liveness (2026-07-25 audit finding 10): the
            # assertion was `graded > 0`, which one single graded verdict
            # satisfies — while the detail printed graded-of-rows_considered as
            # if the RATIO had been checked. The live card read 114 graded of
            # 180 rows and passed; the missing 66 were a real population hole
            # (the alphabetical-truncation class), silently biasing every
            # downstream surface. Now the ratio must clear a bar.
            _rows_vg = int(_rcs.get("rows_considered") or 0)
            _cov_vg = (_g / _rows_vg * 100) if _rows_vg > 0 else 0.0
            _cov_bar = float(getattr(config,
                                     "HEALTH_GRADING_MIN_COVERAGE_PCT", 70.0))
            _cov_ok = _rows_vg <= 0 or _cov_vg >= _cov_bar
            # rows_dropped is learning.py's own accounting of aged rows it could
            # not price (added 2026-07-25). Code defensively: derive the gap when
            # the key is absent (older stats file / a refresh that predates it),
            # and flag rows that are in NEITHER bucket — an accounting leak the
            # ratio alone would not name.
            _drop_vg = _rcs.get("rows_dropped")
            _drop_known = isinstance(_drop_vg, (int, float))
            _drop_n = int(_drop_vg) if _drop_known else max(0, _rows_vg - _g)
            _unacct = (_rows_vg - _g - _drop_n) if _drop_known else 0
            _cov_txt = (f"{_g}/{_rows_vg} rows graded ({_cov_vg:.0f}% coverage, "
                        f"{_drop_n} unpriceable"
                        + ("" if _drop_known else " derived") + ")")
            _ok_vg = _g > 0 and _rcs_age < 48 and _cov_ok and _unacct <= 0
            add("verdict grading", _ok_vg,
                (f"{_cov_txt} ({_rcs_age:.0f}h ago); {_aged} aged real verdicts")
                if _ok_vg else
                (f"{_aged} real verdicts aged past the grading window; {_cov_txt}"
                 + ((f"; stats are {_rcs_age:.0f}h stale"
                     if _rcs_age < 1e6 else "; stats were NEVER written")
                    if _rcs_age >= 48 else "")
                 + (f"; {_unacct} row(s) in NEITHER graded nor dropped — the "
                    "card's own accounting does not close" if _unacct > 0 else "")
                 + ((" — the grading/report-card pipeline is NOT producing "
                     f"output (last note: '{_rcs.get('note', 'never ran')}'); "
                     "sizing/calibration learn nothing while this is red")
                    if _g <= 0 or _rcs_age >= 48 else "")
                 + ((f" — coverage is below the {_cov_bar:.0f}% bar: the graded "
                     "population is a BIASED SUBSET of the aged verdicts, and "
                     "every calibration/sizing surface reads it as the whole")
                    if not _cov_ok else "")))
        elif _aged == 0:
            # VACUOUS PASS (IG #1): "young sample (0 aged verdicts)" is not a
            # young sample, it is NO sample — and a research floor that has
            # stopped recording real verdicts produces exactly this reading
            # while the line stays green.
            add_undet("verdict grading",
                      f"0 real verdicts have aged past the grading window across "
                      f"the last {len(_hist_vg)} retained cycle(s) — nothing was "
                      "gradeable, so grading coverage is unmeasured")
        else:
            add("verdict grading", True,
                f"young sample ({_aged} aged verdicts < {_min_aged}) — "
                "grading asserts once the window fills")
    except Exception as e:
        add_undet("verdict grading",
                  "the grading-coverage check could not run, so 0 aged verdicts "
                  "were measured", f"error: {str(e)[:50]}")

    # Edge integrity (IG 2026-07-01 medium: a tracked edge can invert — the
    # factor/news signal actively hurting — with nothing flagging it). Pure
    # read of the snapshot learning persists alongside the scorecard digest.
    _eh = state.load("edge_health", {}) or {}
    if _eh.get("asof"):
        _inv = _eh.get("inverted") or []
        _eh_age = _age_hours(_eh.get("asof", ""))
        _stale = _eh_age > 48
        # An inverted edge is only a RED when it is UNHANDLED. The digest now
        # injects an explicit down-weight directive for every inverted edge
        # (the judge discounts it), so a surfaced+compensated inversion is a
        # MANAGED risk = green-with-note; RED only if the watchdog snapshot is
        # stale (the compensation may not reflect current data) or, defensively,
        # an inversion is present without the compensation flag (2026-07-02).
        _managed = (not _inv) or (_eh.get("compensated") and not _stale)
        if not _inv and not (_eh.get("edges") or []):
            # VACUOUS PASS (IG #1): "0 tracked edge(s), none inverted" — an
            # empty edge set cannot contain an inverted edge, so the green
            # certified a watchdog that was tracking nothing.
            add_undet("edge integrity",
                      "the edge snapshot tracks 0 edges, so 'none inverted' "
                      "ranges over an empty set")
        elif not _inv:
            add("edge integrity", True,
                f"{len(_eh.get('edges') or [])} tracked edge(s), none inverted")
        elif _managed:
            add("edge integrity", True,
                "inverted edge(s) flagged to the committee for DOWN-WEIGHT "
                "(managed): "
                + ", ".join(f"{e['edge']} {e['value']:+.1f}pts (n={e['n']})"
                            for e in _inv))
        else:
            _edge_detail = (
                ("edge snapshot STALE — inversion may be uncompensated: " if _stale
                 else "INVERTED edge(s), compensation NOT active: ")
                + ", ".join(f"{e['edge']} {e['value']:+.1f}pts (n={e['n']})"
                            for e in _inv)
                + (f" ({_eh_age / 24:.1f}d old)" if _stale else ""))
            # Market-aware severity (operator 2026-07-05): a STALE edge snapshot
            # only goes stale because no cycle has run to rebuild it — impossible
            # on a weekend/holiday, so a hard red just spammed health_warning for
            # days on a non-actionable state (identical class to the IG-findings /
            # 'claude cli' / 'ram headroom' weekend reds). Closed market + STALE =
            # informational (ok=True, story kept). A FRESH uncompensated inversion
            # is a real logic gap and stays red on any day.
            if _stale and not market_hours.is_trading_day():
                add("edge integrity", True,
                    _edge_detail + " — refreshes next trading session "
                    "(market closed)")
            else:
                add("edge integrity", False, _edge_detail)
    else:
        add_undet("edge integrity",
                  "no edge snapshot has ever been written, so 0 edges were "
                  "checked for inversion (builds with the scorecard digest at "
                  "cycle start)")

    # Deployment: cash drag loses to SPY by default; over-deployment is
    # structurally impossible (no leverage + per-position/sector caps).
    # Warn when an equitization-mandated sleeve sits under-deployed while
    # NOT de-risking — that means the parking mechanism itself is failing.
    dep_parts, dep_ok = [], True
    from firm import risk as _risk
    for k in config.SLEEVES:
        sv = state.get_sleeve(k)
        eq = broker.sleeve_equity(sv)
        inv = (1 - sv["cash"] / eq) * 100 if eq > 0 else 0.0
        npos = len(sv.get("positions") or {})
        ecfg = config.CASH_EQUITIZATION.get(k)
        # calls_only's dry powder waits in BIL since 2026-07-01 (goal audit
        # fix 3) — a 0-position calls book now means the PARKING failed too,
        # which the deployment gate below catches. The tag survives for the
        # transition window before the first BIL park.
        tag = " (dry powder)" if k == "calls_only" and npos == 0 else ""
        # MANDATE vs CAPITAL deployment (IG 2026-07-26): `inv` counts the
        # parking reservoir as deployed, so calls_only read "87%" while holding
        # ZERO option positions — parking is a cash SUBSTITUTE, and a line that
        # counts it as deployment certifies an inert mandate as healthy. Show
        # the mandate number (parking excluded) alongside; the per-sleeve
        # parking-concentration leg carries the red so one condition doesn't
        # red two checks.
        _mand_val = sum(
            (p.get("quantity") or 0) * (p.get("last_price")
                                        or p.get("entry_price") or 0)
            for p in (sv.get("positions") or {}).values()
            if not p.get("cash_equitization"))
        mand_inv = (_mand_val / eq) * 100 if eq > 0 else 0.0
        if abs(mand_inv - inv) >= 1.0:
            dep_parts.append(
                f"{k.split('_')[0]} {inv:.0f}% (mandate {mand_inv:.0f}%){tag}")
        else:
            dep_parts.append(f"{k.split('_')[0]} {inv:.0f}%{tag}")
        # Threshold mirrors the equitization trigger (target-5, goal audit fix 7)
        # with the same 15pt grace below it — the health line must reflect what
        # the sleeve is actually TRYING to hold, not the old hard floor.
        if (ecfg and not sv.get("halted") and _risk.risk_scale(sv) >= 1.0
                and inv < ecfg["target_invested_pct"] - 5 - 15):
            dep_ok = False
    add("deployment", dep_ok, ", ".join(dep_parts)
        + ("" if dep_ok else " — equitization failing, cash drag"))

    # The binary being on PATH is NOT enough: an expired OAuth login makes every
    # `claude -p` return HTTP 401 while `which claude` still succeeds (operator
    # 2026-07-03: research floor degraded a full day behind a false-green
    # "reachable"). Surface a persisted auth-failure loudly and actionably.
    _cli_auth = state.load("cli_auth", {}) or {}
    # REAL OUTPUT beats reachability (audit 2026-07-04 F1b — IG critical
    # ratified): the 07-03 outage signature was a green "reachable" ping while
    # a full cycle produced ZERO real committee verdicts. cycle.py persists
    # exactly that outcome as cycle_retry.needed=True (set only when a cycle
    # completed with candidates but no non-degraded verdicts; cleared to False
    # the moment a cycle produces real ones) — so key the brain's health on the
    # fund's own last completed cycle, not on the ping alone.
    _cyc_retry = state.load("cycle_retry", {}) or {}
    if not brain.available():
        add("claude cli", False, "CLI missing — degraded quant mode")
    elif _cli_auth.get("ok") is False:
        add("claude cli", False,
            "CLI LOGIN EXPIRED (HTTP 401) — run `claude login` in a terminal to "
            "restore the research brain")
    elif _cyc_retry.get("needed"):
        # Market-aware severity (operator 2026-07-04): the catch-up retry is
        # trading-day gated, so on a weekend/holiday this red CANNOT clear —
        # it just alarmed for two straight days over a state nothing can act
        # on until the next session. Closed market = informational (ok=True,
        # story in the detail); trading day = the real red it was built to be.
        _since = (f" since {str(_cyc_retry.get('ts', ''))[:16]}"
                  if _cyc_retry.get("ts") else "")
        if market_hours.is_trading_day():
            add("claude cli", False,
                "CLI ping OK but the last completed cycle produced ZERO real "
                f"committee verdicts (quant-only degraded{_since}) — research "
                "brain NOT delivering; catch-up retry pending")
        else:
            add("claude cli", True,
                f"CLI ping OK; last cycle was quant-only degraded{_since} — "
                "catch-up retry is queued and fires automatically next trading "
                "session (market closed now, nothing to act on today)")
    else:
        add("claude cli", True, "research brain reachable")

    # Degraded-verdict watchdog (IG 2026-07-03 high, ratified — audit
    # 2026-07-04 F3): when Opus times out, a quant-composite fallback verdict
    # still flows. Its buys are BLOCKED (sleeves._ranked_buys journals
    # degraded_buy_blocked) and its graded rows are quarantined out of every
    # evidence gate — but nothing ALARMED when the degraded share of a cycle
    # rose (07-03: 20/20) or when the cohort's trailing accuracy ran far below
    # real verdicts (n=34, 32% correct, −12.2% avg excess). Share reads the
    # last completed cycle in research history (cheap state read); accuracy
    # reads the degraded_health snapshot learning persists with the scorecard
    # digest — this check must never recompute the price-fetching card.
    try:
        _dgh_hist = state.load("research", {}).get("history", [])
        _dgh_vs = (_dgh_hist[-1].get("verdicts") or {}) if _dgh_hist else {}
        _dgh_n = len(_dgh_vs)
        _dgh_deg = sum(1 for v in _dgh_vs.values() if (v or {}).get("degraded"))
        _dgh_share = _dgh_deg / _dgh_n * 100 if _dgh_n else 0.0
        _dgh_max = float(getattr(config, "HEALTH_MAX_DEGRADED_SHARE_PCT", 30.0))
        _dgh = state.load("degraded_health", {}) or {}
        _dgh_gap = float(getattr(config, "HEALTH_DEGRADED_ACC_GAP_PTS", 15.0))
        # PERMANENT FIX (operator 2026-07-16 "fix this type of thing
        # permanently"): fabricated fallback verdicts are no longer RECORDED at
        # all — a failed workup is backlogged and retried (cycle.py), never
        # graded, never traded. The old cohort stat (n=34, 24% correct) is a
        # FROZEN tombstone that can never change, so it must never red the
        # desk again — it rides the green detail as the lesson it is. The
        # share/flood legs below stay ARMED: they read verdicts actually
        # recorded in history, which the filter makes impossible for degraded
        # output — so they fire only on a genuine filter regression.
        _acc_story = ""
        if (isinstance(_dgh.get("correct_pct"), (int, float))
                and isinstance(_dgh.get("real_correct_pct"), (int, float))
                and _dgh.get("n", 0) >= 15
                and _dgh["correct_pct"]
                <= _dgh["real_correct_pct"] - _dgh_gap):
            _acc_story = (f"; legacy degraded cohort n={_dgh['n']} graded "
                          f"{_dgh['correct_pct']:.0f}% vs "
                          f"{_dgh['real_correct_pct']:.0f}% real — the flood "
                          "path is closed (failed workups backlog, never "
                          "record); stat is frozen history")
        _share_bad = _dgh_n >= 4 and _dgh_share >= _dgh_max
        # Multi-cycle flood leg (IG 2026-07-16 item 8): the last-cycle share
        # alone lets alternating good/degraded cycles read green forever. Count
        # degraded-heavy cycles over the trailing 48h of research history too.
        _cut48h = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        _recent_cycles = [h for h in _dgh_hist[-12:]
                          if (h.get("asof") or "") >= _cut48h
                          and (h.get("verdicts") or {})]
        _deg_cycles = sum(
            1 for h in _recent_cycles
            if sum(1 for v in h["verdicts"].values() if (v or {}).get("degraded"))
            / max(1, len(h["verdicts"])) * 100 >= _dgh_max)
        _flood_bad = len(_recent_cycles) >= 2 and _deg_cycles > len(_recent_cycles) / 2
        if _share_bad or _flood_bad:
            _parts = []
            if _share_bad:
                _parts.append(f"last cycle {_dgh_deg}/{_dgh_n} verdicts "
                              f"degraded ({_dgh_share:.0f}% >= "
                              f"{_dgh_max:.0f}% ceiling)")
            if _flood_bad:
                _parts.append(f"{_deg_cycles}/{len(_recent_cycles)} cycles in "
                              "48h were degraded-heavy")
            add("degraded verdicts", False,
                "; ".join(_parts) + " — degraded verdicts are being RECORDED "
                "again, which the 2026-07-16 filter (failed workups backlog, "
                "never record) makes impossible without a regression — "
                "inspect cycle.py's degraded filter and the research floor "
                "(RAM/CLI) it degrades around")
        elif not _dgh_n and not _recent_cycles:
            # VACUOUS PASS (IG #1): both legs divide by verdicts — the share leg
            # by the last cycle's verdict count, the flood leg by cycles in 48h.
            # With neither present the ceiling is trivially respected.
            add_undet("degraded verdicts",
                      "the last completed cycle carries 0 verdicts and 0 cycles "
                      "fall in the 48h flood window — neither the share ceiling "
                      "nor the flood leg had a denominator", _acc_story.strip("; "))
        else:
            add("degraded verdicts", True,
                f"last cycle {_dgh_deg}/{_dgh_n} degraded" + _acc_story)
    except Exception as e:
        add_undet("degraded verdicts",
                  "the degraded-share check could not run, so 0 verdicts were "
                  "measured", f"error: {str(e)[:50]}")

    # Fundamentals source: FMP (audit-grade) when keyed, else yfinance fallback
    try:
        from firm import fmp
        add("fundamentals", True,
            "FMP (clean) + yfinance fallback" if fmp.available()
            else "yfinance (screening-grade) — set FMP_API_KEY for cleaner data")
    except Exception as _e:
        add_errored("fundamentals", _e)

    # Coverage-over-time (IG 2026-06-14): the heartbeat checks above are point-in-
    # time — a session that flapped (crash-loop) or went unpriceable for an hour
    # reads all-green once recovered. Scan the last 24h for the rate of pricing
    # failures and FAILED restarts. Benign laptop sleep/wake restarts verify up
    # and don't count; a crash loop (verified_up=False) or a flood of unpriceable
    # marks does. mark_missing is shown for context (noisy, not gated).
    sig = _coverage_signals(24.0)
    over_budget = (sig["failed_restarts"] >= config.HEALTH_MAX_FAILED_RESTARTS_24H
                   or sig["restarts"] > config.HEALTH_MAX_RESTARTS_24H
                   or sig["unpriceable"] > config.HEALTH_MAX_UNPRICEABLE_24H)
    # A blown 24h budget that has been QUIET (no new real instability) for
    # HEALTH_COVERAGE_RECOVERY_H hours is a RESOLVED incident aging out of the window —
    # report it green ("recovered, aging out") instead of a permanent red WARN that reads
    # as an ongoing emergency for a full day after a fixed outage (2026-06-22 operator).
    qh = sig.get("quiet_h")
    recovered = over_budget and qh is not None and qh >= config.HEALTH_COVERAGE_RECOVERY_H
    cov_ok = (not over_budget) or recovered
    detail = (f"24h: {sig['restarts']} restarts ({sig['failed_restarts']} failed), "
              f"{sig['unpriceable']} unpriceable, {sig['mark_missing']} mark-misses "
              f"over {sig['window_events']} journal event(s)")
    if over_budget and recovered:
        detail += f" — recovered, quiet {qh:.1f}h (incident aging out of 24h window)"
    elif over_budget:
        detail += " — instability/pricing degraded, investigate"
    if not sig["window_events"]:
        # VACUOUS PASS (IG #1): every counter here is a journal tally, so an
        # empty 24h window makes them all zero and the line reports a perfectly
        # stable day — including on a box where the journal writer itself died.
        add_undet("data coverage",
                  "0 journal events fall inside the 24h window, so the restart / "
                  "unpriceable / mark-miss budgets counted nothing")
    else:
        add("data coverage", cov_ok, detail)

    # HELD-POSITION DETERIORATION (operator 2026-07-26: "grading with cycles should
    # highlight — hey, still hold? or no sell by now!"). The fund rates ~96% of
    # everything BUY, held or not, winning or losing; three hypotheses for why were
    # tested and refuted, so the symptom is instrumented instead of guessed at. A
    # name re-affirmed BUY for N straight cycles while its position bleeds is the
    # measurable form of "the committee never updates on evidence".
    #
    # WHOLE-BOOK COVERAGE (IG 2026-07-27 #7). The line read the persisted
    # tracker snapshot and printed its `held_checked` as if it were the book:
    # 21 of 29 open positions, with core_blend's 8 — the sleeve that posted the
    # worst day P&L of the fund (-970.57) — outside the denominator entirely.
    # A watchdog that reports a clean number over the part of the book it
    # happens to see is worse than no watchdog. The scan below covers EVERY
    # sleeve from live state, so the denominator is the book.
    #
    # The design constraint is honoured rather than papered over: core_blend's
    # MECHANICAL SEATS are deliberately exempt from per-name SELLs (sleeves.py
    # journals `core_sell_suppressed` and allocation swaps the seat instead),
    # so flagging "no sell by now?" on a seat would demand a decision the desk
    # is not allowed to make. Seats therefore get their own labelled bucket —
    # fully visible, never a red. Core SATELLITES and every other sleeve are
    # graded normally, because for them a per-name SELL genuinely existed.
    try:
        _det = state.load("held_deterioration", {}) or {}
        _det_age = _age_hours(_det.get("asof", "")) if _det.get("asof") else 1e9
        _scan = _held_deterioration_scan(
            int(_det.get("min_cycles") or getattr(config, "HELD_DET_MIN_CYCLES", 3)),
            float(_det.get("min_drawdown_pct")
                  or getattr(config, "HELD_DET_MIN_DRAWDOWN", 5.0)))
        _hd_rows = _scan["flagged"]
        _hd_seats = _scan["seats_flagged"]
        _hd_mix = ", ".join(f"{k.split('_')[0]} {n}"
                            for k, n in sorted(_scan["by_sleeve"].items()))
        if _scan["checked"] == 0:
            add_undet("held deterioration",
                      "0 real equity positions are open across "
                      f"{len(config.SLEEVES)} sleeve(s), so no name could be "
                      "graded for a BUY streak against a bleeding position")
        else:
            _d = (f"{len(_hd_rows)} of {_scan['checked']} held name(s) across "
                  f"ALL sleeves ({_hd_mix}) re-affirmed BUY while bleeding")
            if _hd_rows:
                _w = _hd_rows[0]
                _d += (f" — worst {_w['ticker']} [{_w['sleeve']}]: "
                       f"{_w['buy_streak']} straight BUY verdicts, position "
                       f"{_w['pnl_pct']:+.1f}%, conviction trend "
                       f"{_w['conviction_trend']:+.1f}")
            if _hd_seats:
                # Labelled bucket, NOT a red: no per-name sell was available.
                _d += ("; MECHANICAL CORE SEATS (per-name SELL suppressed by "
                       "design — the seat rotates, it is not sold on a verdict): "
                       + ", ".join(f"{r['ticker']} {r['pnl_pct']:+.1f}% "
                                   f"×{r['buy_streak']}" for r in _hd_seats[:4]))
            if _det_age > 48:
                _d += ("; the learning-side tracker snapshot is "
                       + (f"{_det_age / 24:.1f}d stale" if _det_age < 1e6
                          else "MISSING")
                       + " — this line is computed live and does not depend on it")
            add("held deterioration", not _hd_rows, _d)
    except Exception as _e:
        add_errored("held deterioration", _e)

    # EOD MARK-TO-CLOSE (IG 2026-07-24 finding #4, closed 2026-07-26). The nightly
    # mark journals `eod_mark_to_close ok:false "no closes fetched"` when the yf
    # batch comes back empty — and NOTHING read it. 'data coverage' above reported
    # "0 mark-misses" through repeated failures on 2026-07-23 (yf_batch_failed
    # n=14,16,20) because it counts a different, position-scoped event; the marks
    # only survived via an ad-hoc mark_rescued_public fallback. That makes the
    # equity/PnL OF RECORD depend on a rescue path nobody is watching. Fail on the
    # most recent mark having failed, and flag reliance on the rescue path even
    # when the mark eventually succeeded.
    try:
        # (2026-08-20) VOCABULARY FOLLOWS THE FEED. The 2026-08-13 flip made
        # Public's QUOTE endpoint primary for the nightly mark; that was wrong
        # for a mark-to-CLOSE (broker.equity_quotes_public is "Mid pricing" —
        # a live bid/ask mid, not a close) and it is what put the alpha book
        # $119.48 under its real-close value. mark_books_to_close now takes the
        # daily bar's CLOSE as primary and a quote only as a fallback, so the
        # degradation signal inverts: a QUOTE standing in for a close is the
        # degraded state now, and the yfinance close is the healthy one.
        # mark_fallback_yf stays in the vocabulary because historical rows in
        # this window WERE degradation under the old regime and must keep
        # grading as such until they age out. Adding the new event name in the
        # same change is the point — a renamed emitter with an unchanged
        # consumer is how a signal silently becomes invisible.
        _eodj = [e for e in state.read_journal(2000)
                 if e.get("event") in ("eod_mark_to_close", "mark_rescued_public",
                                       "mark_fallback_yf", "mark_quote_fallback",
                                       "mark_refused_outside_session",
                                       "eod_mark_third_source_mismatch")]
        _eodj.sort(key=lambda e: e.get("ts") or "")          # read_journal is newest-first
        _marks = [e for e in _eodj if e.get("event") == "eod_mark_to_close"]
        _last = _marks[-1] if _marks else None
        _fails = sum(1 for e in _marks[-10:] if e.get("ok") is False)
        _rescues = sum(1 for e in _eodj[-40:]
                       if e.get("event") in ("mark_rescued_public",
                                             "mark_fallback_yf",
                                             "mark_quote_fallback"))
        # A refused quote is the guard WORKING, not a feed failure — surfaced
        # separately so a healthy refusal never reads as degradation, and so a
        # rising refusal rate stays visible instead of hiding inside _rescues.
        _refused_n = sum(int(e.get("n") or 0) for e in _eodj[-40:]
                         if e.get("event") == "mark_refused_outside_session")
        if _last is None:
            # VACUOUS PASS (IG #1): the equity OF RECORD is marked by this job.
            # "No EOD mark recorded yet" is not health — it is the absence of
            # the very evidence the check exists to grade.
            add_undet("eod mark-to-close",
                      "0 eod_mark_to_close events in the 2,000-line journal "
                      "tail — the nightly mark has never been observed, so the "
                      "close feed is ungraded")
        else:
            # FEED-OF-RECORD SLA, NOT TWO LATCHES (operator 2026-08-19).
            # This line read `_ok_eod = last.ok and _fails == 0 and
            # _rescues == 0` over `_marks[-10:]` / `_eodj[-40:]`. Measured:
            # the WHOLE journal holds 24 eod_mark_to_close, 8
            # mark_rescued_public and 0 mark_fallback_yf — 32 events in 71
            # days — so neither slice has ever truncated and the real window
            # is "the last 2,000 journal lines", a span set by how chatty the
            # rest of the firm is. Consequence: ANY single rescue pins the
            # line red until journal rotation evicts it. The three rescues
            # holding it red are all dated 2026-08-11 (18:28, 21:31, 21:55),
            # eight days stale, each covering 100% of its sleeve's book, all
            # emitted by broker.sleeve_equity's INTRADAY quote path rather
            # than mark_books_to_close, and all PREDATING the 2026-08-13
            # primacy flip that made Public primary — i.e. under the regime
            # they were logged in they meant "the then-primary yfinance
            # missed and Public covered", the opposite of today's reading.
            # 105 consecutive sweeps with no clearing path, poisoning
            # 'verdict grading', 'deployment', 'display reconciliation' and
            # 'parking concentration' with it. A control that cannot return
            # to green is not a control; it is a permanently-on light that
            # trains the desk to stop reading four healthy lines.
            #
            # It was also BLIND to the actual defect. Grade what matters:
            # did each recent COMPLETED SESSION get an after-close mark?
            # Measured, that answer is no for several — the equity of record
            # for those days was written from whatever intraday mark the book
            # happened to carry. So: a per-session coverage RATE over a
            # declared window (both legs age out), plus fallback reliance
            # measured over the SAME window instead of latching forever.
            _sla_d = float(getattr(config, "EOD_MARK_SLA_DAYS", 10.0))
            _sla_floor = (datetime.now(timezone.utc)
                          - timedelta(days=_sla_d)).isoformat()
            _win_marks = [e for e in _marks if str(e.get("ts") or "") >= _sla_floor]
            _win_ok = [e for e in _win_marks if e.get("ok")]
            _win_fail = len(_win_marks) - len(_win_ok)
            # Distinct UTC days on which an after-close mark SUCCEEDED. The
            # nightly wrap runs at 20:30Z; an intraday-hours success (e.g. the
            # 14:50 catch-up) still marks the book but is not an after-close
            # mark, so it is counted separately rather than silently credited.
            _mk_days = {str(e.get("ts"))[:10] for e in _win_ok}
            _ac_days = {str(e.get("ts"))[:10] for e in _win_ok
                        if str(e.get("ts"))[11:16] >= "20:00"}
            # Sessions the window SHOULD have marked: weekday UTC dates inside
            # the window, excluding today when the close has not happened yet.
            _now_dt = datetime.now(timezone.utc)
            _sessions = []
            for _i in range(1, int(_sla_d) + 1):
                _dd = (_now_dt - timedelta(days=_i))
                if _dd.weekday() < 5:
                    _sessions.append(_dd.date().isoformat())
            # Host-down sessions leave the FEED denominator (2026-09-01) and
            # are named separately below — an outage is not a feed failure.
            _gaps_eod = [e for e in state.read_journal(4000)
                         if e.get("event") == "session_coverage_gap"]
            _host_down = [d for d in _host_down_sessions(_sessions, _gaps_eod)
                          if d not in _mk_days]
            _sessions = [d for d in _sessions if d not in _host_down]
            _miss = sorted(d for d in _sessions if d not in _mk_days)
            _cov_pct = (100.0 * (len(_sessions) - len(_miss)) / len(_sessions)
                        if _sessions else 100.0)
            _cov_bar = float(getattr(config, "EOD_MARK_SESSION_COVERAGE_PCT", 80.0))
            _rescues = sum(1 for e in _eodj
                           if str(e.get("ts") or "") >= _sla_floor
                           and e.get("event") in ("mark_rescued_public",
                                                  "mark_fallback_yf"))
            _resc_bar = int(getattr(config, "EOD_MARK_FALLBACK_MAX", 3))
            _ok_eod = (bool(_last.get("ok"))
                       and _cov_pct >= _cov_bar
                       and _rescues <= _resc_bar)
            _d = (f"last mark {'ok' if _last.get('ok') else 'FAILED'} "
                  f"({str(_last.get('ts'))[:19]}); {_sla_d:.0f}d SLA: "
                  f"{len(_sessions) - len(_miss)}/{len(_sessions)} session(s) "
                  f"marked ({_cov_pct:.0f}%, bar {_cov_bar:.0f}%), "
                  f"{len(_ac_days)} after-close, {_win_fail} failed mark(s), "
                  f"{_rescues} fallback event(s) (bar {_resc_bar})")
            if _miss:
                _d += (" — SESSION(S) WITH NO SUCCESSFUL MARK: "
                       + ", ".join(_miss[:6])
                       + " — equity of record for those days was carried from "
                       "whatever intraday mark the book held, not the close")
            if _host_down:
                _d += (f"; {len(_host_down)} host-down session(s) excluded from "
                       f"the feed grade ({', '.join(_host_down[:5])}) — the firm "
                       "was OFF, that is a coverage outage, not a feed failure")
            if _rescues > _resc_bar:
                _d += (f", {_rescues} fallback-feed event(s) over the SLA "
                       "window — the PRIMARY close feed (Public) is degraded "
                       "and equity of record is riding a fallback")
            if _ok_eod:
                _d += "; primary (Public) close feed within SLA"
            # THIRD-SOURCE VERDICT (fix brief 2026-07-28 #10) — the only leg
            # here that can catch a mark that is WRONG rather than merely
            # missing. A close outside its own session high/low never traded.
            _ts = _last.get("third_source") or {}
            _ts_bad = (_ts.get("outside_session_range") or [])
            if _ts_bad:
                _ok_eod = False
                _d += (f" — THIRD-SOURCE MISMATCH: {len(_ts_bad)} of "
                       f"{_ts.get('checked')} sampled marks fall outside their "
                       f"own session range (first {_ts_bad[0].get('symbol')} "
                       f"{_ts_bad[0].get('mark')} vs "
                       f"{_ts_bad[0].get('session_low')}-{_ts_bad[0].get('session_high')})"
                       " — equity of record is carrying a price that never traded")
            elif _ts.get("checked"):
                # A POINT-IN-TIME VERDICT MUST NOT BE STATED AS A PERIOD ONE
                # (IG CRITICAL 2026-08-20, upheld). This read "third-source
                # audit clean (N marks inside their own session range)" from
                # the LATEST mark alone, while the same 7-day window held 10
                # eod_mark_third_source_mismatch events. Both facts were true —
                # the mismatches were the pre-fix quote-mid marks (CORT 110.83
                # against a 118.37-121.11 session) and the latest mark really
                # was clean — but a line that says "clean" without naming its
                # window reads as "no marks were ever wrong", which is the
                # opposite of what the journal showed. Report the LATEST verdict
                # and the SLA-window history side by side, and never let a clean
                # latest mark alone imply a clean period.
                _tsm_n = sum(int(e.get("n") or 0) for e in _eodj
                             if e.get("event") == "eod_mark_third_source_mismatch")
                _tsm_last = max((str(e.get("ts") or "") for e in _eodj
                                 if e.get("event")
                                 == "eod_mark_third_source_mismatch"), default="")
                _d += (f"; third-source audit clean on the LATEST mark "
                       f"({_ts['checked']} marks inside their own session range)")
                if _tsm_n:
                    _d += (f", but {_tsm_n} mismatch(es) in the journal window "
                           f"(most recent {_tsm_last[:19]}) — a clean latest "
                           "mark is not a clean period")
            elif _ts:
                _d += f"; third-source {_ts.get('note', 'unavailable')}"
            add("eod mark-to-close", _ok_eod, _d)
    except Exception as _e:
        add_errored("eod mark-to-close", _e)

    # Benchmark data integrity (IG 2026-07-07 critical, ratified): the report card
    # grades every trade RELATIVE to SPY (config.BENCHMARK). When SPY marks drop,
    # those trades record excess=None and are silently SKIPPED from the scorecard
    # (verified: learning.py records null, does NOT corrupt the math) — but the
    # grading loses coverage with no alarm, and 'data coverage' above lumps a
    # benchmark miss in with routine single-name misses. Count benchmark-specific
    # mark_missing over 24h and fail LOUDLY + SEPARATELY when the index the whole
    # relative-return grade depends on is unreliable.
    #
    # DIRECT FEED CHECK (2026-07-25 audit finding 9): mark_missing only ever
    # fires for a symbol HELD in a sleeve, and no sleeve has held SPY since the
    # parking reservoir moved to RSP — so this counter is pinned at 0 by
    # construction and a stone-dead SPY history feed still reported "SPY priced
    # cleanly". The authoritative leg is now `_probe_benchmark_feed`, which
    # actually fetches the benchmark series on the health sweep (never on this
    # dashboard-poll path) and persists the result; this reads that probe. The
    # mark_missing count survives as a second leg — it is still real evidence
    # whenever the benchmark IS held.
    try:
        _bench = str(getattr(config, "BENCHMARK", "SPY"))
        _bench_cut = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        _bench_miss = sum(1 for e in state.read_journal(3000)
                          if e.get("event") == "mark_missing"
                          and e.get("ts", "") >= _bench_cut
                          and _bench in str(e.get("symbol", "")).split(","))
        _bench_max = int(getattr(config, "HEALTH_MAX_BENCHMARK_MISS_24H", 10))
        _miss_bad = _bench_miss >= _bench_max
        _probe = state.load("benchmark_feed", {}) or {}
        _probe_age = _age_hours(_probe.get("asof", "")) if _probe.get("asof") else 1e9
        _probe_max_h = float(getattr(config, "HEALTH_BENCHMARK_PROBE_MAX_AGE_H", 12.0))
        # Bar staleness is measured in CALENDAR days against the last close, so
        # a long weekend is normal; 5 days means the series stopped advancing.
        _bar_age_d = float(_probe.get("last_bar_age_days") or 0.0)
        if not _probe:
            _feed_bad, _feed_txt = False, (
                f"{_bench} feed probe has not run yet (first health sweep "
                "writes it)")
        elif _probe_age > _probe_max_h:
            _feed_bad, _feed_txt = True, (
                f"{_bench} feed probe is {_probe_age:.0f}h stale — the health "
                "sweep that fetches the benchmark is not running")
        elif not _probe.get("ok"):
            _feed_bad, _feed_txt = True, (
                f"{_bench} history feed is DOWN ({str(_probe.get('error'))[:60]}) "
                "— every report-card excess grade is measured against this "
                "series; grading silently loses coverage while it is dead")
        elif _bar_age_d > 5:
            _feed_bad, _feed_txt = True, (
                f"{_bench} series has not advanced in {_bar_age_d:.0f} days "
                f"(last bar {_probe.get('last_bar', '?')}) — the feed answers "
                "but is frozen")
        else:
            _feed_bad, _feed_txt = False, (
                f"{_bench} feed live: {_probe.get('bars', 0)} bars, last "
                f"{_probe.get('last_bar', '?')} @ {_probe.get('last_close', 0):.2f} "
                f"({_probe_age:.0f}h ago)")
        if not _probe and not _bench_miss:
            # VACUOUS PASS (IG #1): with no probe and a mark_missing counter
            # that is pinned at 0 by construction (nothing holds the benchmark
            # since parking moved to RSP), BOTH legs are empty — and the line
            # still reported green on the series every report-card grade is
            # measured against.
            add_undet("benchmark data",
                      f"the {_bench} feed probe has never run and 0 "
                      f"{_bench} mark_missing events exist in 24h — neither leg "
                      "observed the benchmark series (first health sweep writes "
                      "the probe)")
        else:
            add("benchmark data", not (_miss_bad or _feed_bad),
                _feed_txt
                + ("" if not _miss_bad else
                   f" · {_bench} — the SPY-relative BENCHMARK — was mark_missing "
                   f"{_bench_miss}x in 24h: those trades' report-card excess grades "
                   "are SKIPPED (recorded null, not corrupted); the index feed is "
                   "unreliable — check connectivity/vendor")
                + ("" if _miss_bad or not _bench_miss else
                   f" · {_bench_miss} mark-miss(es) in 24h (under the "
                   f"{_bench_max} budget)"))
    except Exception as e:
        add_undet("benchmark data",
                  "the benchmark check could not run, so neither the feed probe "
                  "nor the mark-miss count was read", f"error: {str(e)[:50]}")

    # State integrity: a state file CORRUPTED and was auto-recovered from its .bak
    # in the last 24h. Recovery is graceful (and tested), but recurrence is a real
    # disk/partial-write signal the operator must SEE, not just have buried in the
    # journal — the IG flagged a silent 06-11 restore as a plausible data-loss root.
    _eph = sig.get("restored_ephemeral", 0)
    _drv = sig.get("restored_derived", 0)
    _eph_note = f" ({_eph} ephemeral heartbeat/status recovered — benign)" if _eph else ""
    # A DERIVED recovery is reported, never buried — a rising rate is still a
    # real disk/write signal, it just is not a book-corruption alarm. It also
    # must not be able to buy green: a chronic rate reds the line on its own.
    if _drv:
        _eph_note += (f" · {_drv} derived/telemetry file(s) recovered "
                      f"({', '.join(sig.get('derived_files') or [])}) — "
                      "recomputable, not book state")
    if not sig["window_events"]:
        add_undet("state integrity",
                  "0 journal events fall inside the 24h window, so no "
                  "state_restored_from_backup record could have been seen")
    else:
        # DERIVED recoveries cannot be ignored forever either — past the budget
        # they mean the host really is losing writes, which is the signal this
        # line exists to carry. Below it, they are reported and stay green.
        _drv_max = int(getattr(config, "STATE_DERIVED_RESTORE_BUDGET_24H", 3))
        _drv_bad = _drv > _drv_max
        add("state integrity", sig["restored"] == 0 and not _drv_bad,
            (f"no book corruption in {sig['window_events']} journal event(s) "
             f"(24h){_eph_note}") if sig["restored"] == 0 and not _drv_bad else
            (f"{sig['restored']} BOOK state file(s) recovered from backup in 24h "
             f"({', '.join(sig.get('book_files') or [])}) — money state, "
             "check disk/writes" if sig["restored"] else
             f"{_drv} derived state file(s) recovered from backup in 24h "
             f"({', '.join(sig.get('derived_files') or [])}) — over the "
             f"{_drv_max}/24h budget, so the host is losing writes even though "
             "no book was hit"))

    settings = state.get_settings()
    if settings["mode"] == "live":
        try:
            acct = broker.live_account()
            add("broker link", True,
                f"live BP ${acct['cash_buying_power']:,.0f} cash / "
                f"${acct['options_buying_power']:,.0f} options")
            # AGE-GATED, and an empty reconcile FAILS (2026-07-25 audit finding
            # 4c): the old line was `not drift.get("drift")` — on an empty dict
            # `not None` is True, so a LIVE fund whose health loop had never run
            # a reconciliation reported "no reconciliation yet" as a PASS,
            # forever, with no age gate at all (the display-reconciliation check
            # below already gates on age; this one, guarding real money against
            # the real broker, did not). reconcile_live runs every health sweep
            # in live mode, so anything past a few hours means it stopped.
            _rc = state.load("reconcile", {}) or {}
            _rc_age = _age_hours(_rc.get("asof", "")) if _rc.get("asof") else 1e9
            _rc_max = float(getattr(config, "HEALTH_RECONCILE_MAX_AGE_H", 6.0))
            if not _rc.get("asof"):
                add("account reconciliation", False,
                    "NO reconciliation has ever completed — the live books are "
                    "UNVERIFIED against the broker account"
                    + (f" (last attempt: {_rc.get('summary')})"
                       if _rc.get("summary") else ""))
            elif _rc_age > _rc_max:
                add("account reconciliation", False,
                    f"last reconciliation {_rc_age:.1f}h ago (budget "
                    f"{_rc_max:.0f}h) — the reconciler stopped running; drift "
                    "would go undetected: " + str(_rc.get("summary", ""))[:70])
            else:
                add("account reconciliation", not _rc.get("drift"),
                    str(_rc.get("summary") or "books match account")
                    + f" ({_rc_age:.1f}h ago)")
        except Exception as e:
            add("broker link", False, f"Public.com unreachable: {e}")
    else:
        add("broker link", True, "paper mode — broker only used for quotes")

    # Display reconciliation visibility (IG 2026-07-01): the nightly rebuild's
    # verdict must be a health LINE, not just a journal entry — a critical
    # drift alert coexisted with an all-green health report. Stale (>2 days,
    # weekends allowed) reads as its own problem: the reconciler stopped running.
    try:
        _rd = state.load("reconcile_display", {}) or {}
        _rd_age_h = _age_hours(_rd.get("asof", ""))
        if not _rd:
            # VACUOUS PASS (IG #1): a reconciler that has never produced a
            # record has never verified the displayed books against the raw
            # rebuild — the reason this line exists.
            add_undet("display reconciliation",
                      "0 nightly reconcile records have ever been written, so "
                      "the displayed books were never compared to a raw-close "
                      "rebuild")
        elif _rd_age_h > 78:   # > ~3 sessions incl. a weekend
            add("display reconciliation", False,
                f"last reconcile {_rd_age_h / 24:.1f} days ago — nightly rebuild not running")
        else:
            add("display reconciliation", bool(_rd.get("ok")),
                "displayed numbers match raw-close rebuild" if _rd.get("ok") else
                f"DRIFT vs rebuild: {str((_rd.get('drifts') or ['?'])[0])[:80]}")
    except Exception as _e:
        add_errored("display reconciliation", _e)

    # Structurally-inert sleeve watchdog (2026-07-01 goal audit, fix 8): a sleeve
    # whose ENTRY GATE keys on a rating the judge never emits can sit at zero
    # entries forever while every per-cycle line looks routine — calls_only ran
    # 22 days/339 verdicts with 0 STRONG_BUYs and nothing escalated. Gate-level
    # inertness is a WIRING defect, not market conditions: if the gate's rating
    # has ZERO occurrences across the whole retained research history AND the
    # sleeve has zero real positions, no market outcome could ever have opened
    # a position — say so, loudly.
    # A gate is a WIRING DEFECT only if it keys on rating(s) the judge
    # structurally NEVER emits. calls_only was STRONG_BUY-only (0/357 =
    # unreachable); it now ALSO accepts a high-conviction options-suitable
    # BUY (operator 2026-07-02) AND — as the COMBINED options desk (operator
    # 2026-07-07) — the SELL-side put path (STRONG_SELL/SELL). Reachable if the
    # committee emits actionable signal in EITHER direction.
    # Declared OUTSIDE the try (2026-07-25 audit finding 5) so a raise can name
    # the checks it owes and fail them, instead of deleting them from the report.
    _gates = {"calls_only": ["STRONG_BUY", "BUY", "STRONG_SELL", "SELL"]}
    _gate_done: set = set()
    try:
        _hist = state.load("research", {}).get("history", [])
        _ratings_seen: dict[str, int] = {}
        for _h in _hist:
            for _v in (_h.get("verdicts") or {}).values():
                _r = (_v or {}).get("rating")
                if _r:
                    _ratings_seen[_r] = _ratings_seen.get(_r, 0) + 1
        for _sl, _grs in _gates.items():
            _gr = _grs[0]
            _sv = state.get_sleeve(_sl)
            _real = sum(1 for p in (_sv.get("positions") or {}).values()
                        if not p.get("cash_equitization"))
            _n_seen = sum(_ratings_seen.get(_g, 0) for _g in _grs)
            if len(_hist) >= 10 and _real == 0 and _n_seen == 0:
                # Journal only on the False->True TRANSITION: health_report is
                # a read-only path recomputed on every dashboard poll (batch
                # review R11 — it wrote 2 identical lines 50s apart against the
                # 3,000-line journal budget). The red check line still renders
                # every time; the journal marks the onset once.
                _inert = state.load("inert_sleeves", {})
                if not _inert.get(_sl):
                    _inert[_sl] = True
                    state.save("inert_sleeves", _inert)
                    state.journal("structurally_inert_sleeve", sleeve=_sl,
                                  gate_rating=_gr, cycles=len(_hist))
                add(f"{_sl} gate reachability", False,
                    f"entry gate accepts {'/'.join(_grs)} but 0/"
                    f"{sum(_ratings_seen.values())} verdicts across {len(_hist)} "
                    "cycles ever rated any of them — gate unreachable, not "
                    "market conditions")
            elif _sl in config.SLEEVES:
                # Recovered (rating emitted or position opened) -> clear the
                # onset flag so a future relapse journals again
                _inert = state.load("inert_sleeves", {})
                if _inert.get(_sl):
                    _inert[_sl] = False
                    state.save("inert_sleeves", _inert)
                if not _n_seen and not _hist:
                    # VACUOUS PASS (IG #1): "watching (0 cycles retained)" over
                    # an empty history proves nothing about whether the gate's
                    # ratings are emittable — the exact question it answers.
                    add_undet(f"{_sl} gate reachability",
                              "0 research cycles are retained, so no verdict "
                              f"ever tested whether {'/'.join(_grs)} is emitted")
                else:
                    add(f"{_sl} gate reachability", True,
                        f"reachable — {'/'.join(_grs)} emitted {_n_seen}x in "
                        "retained history" if _n_seen
                        else f"watching ({len(_hist)} cycles retained)")
            _gate_done.add(_sl)     # line rendered; a later raise can't erase it
    except Exception as _e:
        for _sl in _gates:
            if _sl not in _gate_done:
                add_errored(f"{_sl} gate reachability", _e)

    # IG findings reach the operator (fix 8): scripts/ig_audit.py writes
    # severity-graded findings that previously had ZERO consumers — the
    # 06-22 IG caught the inert calls gate 9 days before the goal audit did,
    # and nobody saw it. High-severity open findings now surface here.
    try:
        _ig = state.load("ig_findings_latest", None)
        if _ig is None:
            _igp = config.BASE_DIR / "state" / "ig_findings_latest.json"
            _ig = json.loads(_igp.read_text(encoding="utf-8")) if _igp.exists() else {}
        _findings = (_ig.get("findings") or []) if isinstance(_ig, dict) else []
        _high = [f for f in _findings
                 if str(f.get("severity", "")).lower() in ("high", "critical")]
        _ig_asof = (_ig.get("asof") or "")[:10] if isinstance(_ig, dict) else ""
        _ig_age_h = _age_hours(_ig.get("asof") or "") if isinstance(_ig, dict) else 1e9
        # Broken-auditor recurrence (IG 2026-07-16 item 6): an errored refresh
        # (opus_unparseable) leaves the OLD snapshot in place — staleness alone
        # eventually reds, but a REPEATING parse failure is the meta-watchdog
        # itself being down and must be named as such, hard, on the first sweep
        # after the second failure — never reported around.
        _err_cut = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        # Only errors NEWER than the last good snapshot count — a recovered
        # auditor must not stay red for 48h on its own healed history.
        _ig_errs = [e for e in state.read_journal(1500)
                    if e.get("event") == "ig_audit" and e.get("error")
                    and e.get("ts", "") >= max(_err_cut, str(_ig.get("asof") or "")
                                               if isinstance(_ig, dict) else "")]
        # Freshness horizon (2026-07-02): a stale snapshot's findings are of
        # UNKNOWN current validity — flagging them red forever pins the desk on
        # issues that may already be fixed. Past the daily-refresh horizon the
        # actionable signal is the STALE AUDIT itself, so surface that instead.
        if len(_ig_errs) >= 2:
            add("IG findings", False,
                f"IG auditor BROKEN: {len(_ig_errs)} failed runs in 48h "
                f"(last error: {str(_ig_errs[-1].get('error', ''))[:40]}) — the "
                "gap-discovery watchdog is inert; snapshot frozen at "
                f"[{_ig_asof}]. Run scripts/ig_audit.py manually.")
        elif _ig_age_h > 36.0 and (_findings or _ig_asof):
            # Market-aware severity (operator 2026-07-05): the IG snapshot only
            # refreshes on a trading-day premarket/postmarket cycle or the weekly
            # Sunday task — so across a long holiday weekend NOTHING can refresh
            # it and this hard-red just spammed health_warning every 30 min for
            # days on a non-actionable state. Same actionability gate the 07-04
            # sweep applied to 'claude cli'/'ram headroom' (this stale check was
            # missed then). Closed market = informational (ok=True, story kept);
            # trading day = the real red — a refresh should have run and did not.
            _ig_age_d = _ig_age_h / 24
            if market_hours.is_trading_day():
                add("IG findings", False,
                    f"IG audit STALE ({_ig_age_d:.1f}d old [{_ig_asof}]) — the "
                    f"daily refresh has not run; {len(_high)} prior high "
                    "finding(s) may already be resolved. Re-run "
                    "scripts/ig_audit.py.")
            else:
                add("IG findings", True,
                    f"IG audit {_ig_age_d:.1f}d old [{_ig_asof}] — refreshes next "
                    "trading session (market closed; weekly Sun audit also "
                    f"covers it); {len(_high)} prior high finding(s) pending "
                    "that refresh.")
        else:
            # The IG is an ADVERSARIAL auditor, FORBIDDEN from concluding "all
            # clear" — it always emits ranked gap HYPOTHESES by design, so
            # "zero high findings" is never the steady state and pinning the
            # desk red on their mere existence is alert fatigue. The actionable
            # signal is whether the operator has REVIEWED the LATEST snapshot
            # (2026-07-02): red only when a FRESH audit has high findings the
            # operator has not yet triaged (ig_reviewed_asof < snapshot asof);
            # green once triaged. A new audit advances the asof -> re-review.
            _reviewed = state.load("ig_reviewed_asof", "") or ""
            _snap_asof = _ig.get("asof") or "" if isinstance(_ig, dict) else ""
            _unreviewed = bool(_high) and _snap_asof > _reviewed
            if not (_findings or _ig_asof):
                # VACUOUS PASS (IG #1): the IG is FORBIDDEN from concluding
                # "all clear", so an empty snapshot never means a clean audit —
                # it means the auditor produced nothing.
                add_undet("IG findings",
                          "0 findings and no snapshot asof — the gap-discovery "
                          "auditor has produced no output to triage")
            else:
                add("IG findings", not _unreviewed,
                    (f"{len(_high)} UN-REVIEWED high finding(s) [{_ig_asof}]: "
                     + "; ".join(str(f.get("gap") or f.get("title") or "?")[:55]
                                 for f in _high[:2])
                     + " — triage + set ig_reviewed_asof") if _unreviewed
                    else (f"{len(_high)} known finding(s) triaged [{_ig_asof}]"
                          if _high else
                          f"no high-severity findings of {len(_findings)} "
                          f"[{_ig_asof}]"))
    except Exception as _e:
        add_errored("IG findings", _e)

    # Stop slippage (fix brief 2026-07-28 #7): realized exit depth vs the
    # DESIGNED distance, split by where in the session it filled — the three
    # buckets have three different fixes and one blended average hides all of
    # them. Reds only on INTRADAY slippage, because that is the only bucket the
    # desk controls: a gap through the level overnight is not a monitor defect,
    # and saying so keeps this line from becoming an unfixable standing red.
    try:
        from firm import learning as _lrn7
        _ss = _lrn7.stop_slippage()
        _n7 = int(_ss.get("n") or 0)
        if not _n7:
            add_undet("stop slippage", "no graded stop/trail exits yet")
        else:
            _ctx = _ss.get("by_exit_context") or {}
            _intra = _ctx.get("intraday") or {}
            _in_n = int(_intra.get("n") or 0)
            _in_avg = _intra.get("avg_slippage_pct")
            _bar = float(getattr(config, "HEALTH_STOP_SLIPPAGE_MAX_PCT", 2.0))
            _parts = ", ".join(
                f"{k} {v['n']}x avg {v['avg_slippage_pct']:+.2f}%"
                for k, v in _ctx.items())
            if _in_n < 3:
                add_undet("stop slippage",
                          f"only {_in_n} intraday stop exit(s) — the buckets "
                          "the desk does NOT control (gaps, extended prints) "
                          "cannot certify the stop engine",
                          f"{_n7} graded: {_parts}", n=_in_n)
            else:
                add("stop slippage", _in_avg is None or _in_avg > -_bar,
                    f"{_n7} graded stop/trail exit(s): {_parts}"
                    + (f" — INTRADAY stops filling {_in_avg:+.2f}% vs design "
                       f"(past the {_bar:.1f}pt bar): the engine, not the tape"
                       if _in_avg is not None and _in_avg <= -_bar else
                       " — intraday fills track their design; the deep exits "
                       "are overnight gaps and extended-hours prints, which no "
                       "monitor cadence can catch")
                    + (f" · worst {_ss['worst'][0]['symbol']} "
                       f"{_ss['worst'][0]['slippage_pct']:+.1f}% "
                       f"({_ss['worst'][0]['context']})" if _ss.get("worst") else ""))
    except Exception as _e:
        add_errored("stop slippage", _e)

    # Registry integrity FIRST — a stale registry poisons nothing and says so.
    try:
        _gaps = _dependency_registry_gaps(checks)
        add("signal dependencies", not _gaps,
            (f"{len(SIGNAL_CONSUMERS)} signal(s) registered, "
             f"{sum(len(s['checks']) for s in SIGNAL_CONSUMERS.values())} "
             "consumer line(s) protected — no gate silently grades a path "
             "built on a flagged signal")
            if not _gaps else
            ("STALE REGISTRY: " + "; ".join(_gaps[:4])
             + " — these names no longer exist among the checks, so those "
               "consumers are UNPROTECTED; a rename silently removed them"))
    except Exception as _e:
        add_errored("signal dependencies", _e)
    # ...then propagate. Runs LAST so it sees every check's own verdict.
    _poisoned = _apply_dependency_poisoning(checks)

    rep = {"asof": datetime.now(timezone.utc).isoformat(),
           "ok": all(c["ok"] for c in checks), "checks": checks,
           # Named separately so a desk that is "green" only because half its
           # checks could not measure anything cannot masquerade as verified.
           "undetermined": sorted(c["name"] for c in checks
                                  if c.get("undetermined")),
           # Forced not-ok by a failing upstream signal, not by their own
           # reading — grouped so the operator sees ONE root, not N symptoms.
           "poisoned": sorted(set(_poisoned))}
    # Age of every failing check, read from the persisted ladder so the
    # dashboard poll path shows the same streak the sweep escalates on.
    _annotate_streaks(rep, state.load("health_streaks", {}) or {})
    return rep


def _escalate_conviction_spread() -> None:
    """Fix brief 2026-07-17 #3: the 'conviction distribution' line failed on
    essentially every cycle in the journal tail as a warn-only red — compressed
    conviction silently neutralizes sizing, the options bar and the acceptance
    bands, and a redline nobody must act on is a redline nobody acts on. Count
    consecutive FAILING RESEARCH CYCLES (keyed by research latest_asof, so the
    30-min sweeps of one cycle can't inflate the streak), reset on any pass,
    and past CONVICTION_SPREAD_ESCALATE_CYCLES file a fix brief demanding the
    already-designed next escalation (debate.py's own ladder: schema-level
    conviction subscores summed mechanically in code). Deduped by a 7-day
    escalated_at latch. The budget deliberately gives the 2026-07-17
    component-sum prompt fix its attribution window before demanding more."""
    report = state.load("health", {}) or {}
    chk = next((c for c in (report.get("checks") or [])
                if c.get("name") == "conviction distribution"), None)
    if chk is None or chk.get("undetermined"):
        return                      # sample too small — the check didn't run
    latest = str((state.load("research", {}) or {}).get("latest_asof") or "")
    cs = state.load("conviction_spread", {}) or {}
    if chk.get("ok"):
        if int(cs.get("streak") or 0):
            state.journal("conviction_spread_recovered",
                          after_cycles=int(cs.get("streak") or 0))
        state.save("conviction_spread",
                   {"streak": 0, "last_asof": latest,
                    "escalated_at": cs.get("escalated_at", "")})
        return
    if not latest or cs.get("last_asof") == latest:
        return                      # same cycle re-measured — not a new failure
    cs = {"streak": int(cs.get("streak") or 0) + 1, "last_asof": latest,
          "escalated_at": cs.get("escalated_at", "")}
    budget = int(getattr(config, "CONVICTION_SPREAD_ESCALATE_CYCLES", 10))
    if (cs["streak"] >= budget
            and _age_hours(cs.get("escalated_at") or "") > 24 * 7):
        try:
            from firm import learning as _lrn
            _lrn._file_fix_requests([{
                "problem": ("conviction spread still compressed after "
                            f"{cs['streak']} consecutive failing cycles — the "
                            "07-17 component-sum prompt fix did not decompress it"),
                "evidence": str(chk.get("detail", ""))[:280],
                "suspected_area": "firm/debate.py _CONVICTION_GUIDANCE / "
                                  "_calibration_note (prompt-level fixes exhausted)",
                "ask_claude": "implement the named next escalation from "
                              "debate.py's conviction ladder: schema-level "
                              "subscores (evidence/asymmetry/timing/"
                              "differentiation as separate JSON fields) summed "
                              "mechanically in code, so conviction variance is "
                              "structural rather than prompted",
            }])
            cs["escalated_at"] = datetime.now(timezone.utc).isoformat()
            state.journal("conviction_spread_escalated", streak=cs["streak"])
        except Exception as e:
            state.journal("conviction_spread_escalation_error",
                          error=str(e)[:120])
    state.save("conviction_spread", cs)


def _probe_benchmark_feed() -> dict:
    """Fetch the benchmark series and persist the outcome — the DIRECT evidence
    the 'benchmark data' health line asserts on (2026-07-25 audit finding 9).

    That line used to infer benchmark health from `mark_missing`, an event that
    only fires for symbols HELD in a sleeve. Nothing has held SPY since the
    parking reservoir moved to RSP, so the counter was pinned at 0 by
    construction and a stone-dead SPY history feed still reported "SPY priced
    cleanly". The report card grades EVERY verdict and trade relative to this
    series, so it must be probed on its own rather than inferred from a
    position-scoped event.

    Runs from run_health_sweep (every HEALTH_INTERVAL_S) and NEVER from
    health_report — that path is recomputed on every dashboard poll and stays
    network-free. period="6mo" deliberately reuses the cache key
    data_engine.spy_return_since already populates, so inside the hourly cache
    bucket this is usually free. Fail-soft: an exception IS the signal and is
    recorded as a failed probe, never raised."""
    bench = str(getattr(config, "BENCHMARK", "SPY"))
    rep: dict = {"asof": datetime.now(timezone.utc).isoformat(), "symbol": bench}
    try:
        from firm import data_engine
        ser = data_engine.fetch_history([bench], period="6mo")[bench].dropna()
        if not len(ser):
            rep.update({"ok": False, "bars": 0, "error": "empty series"})
        else:
            last_day = ser.index[-1].date()
            rep.update({"ok": True, "bars": int(len(ser)),
                        "last_bar": last_day.isoformat(),
                        "last_close": round(float(ser.iloc[-1]), 4),
                        "last_bar_age_days": max(
                            0, (datetime.now(market_hours.ET).date()
                                - last_day).days)})
    except Exception as e:
        rep.update({"ok": False, "bars": 0, "error": str(e)[:120]})
    prev = state.load("benchmark_feed", {}) or {}
    if prev and bool(prev.get("ok")) != bool(rep.get("ok")):
        state.journal("benchmark_feed_status", symbol=bench, ok=rep.get("ok"),
                      error=str(rep.get("error", ""))[:100],
                      last_bar=rep.get("last_bar", ""))
    state.save("benchmark_feed", rep)
    return rep


def run_health_sweep() -> dict:
    # Probe FIRST so the report below reads this sweep's benchmark evidence
    # rather than the previous one's (2026-07-25 audit finding 9).
    try:
        _probe_benchmark_feed()
    except Exception as e:
        state.journal("benchmark_probe_error", error=str(e)[:120])
    report = health_report()
    # ESCALATION LADDER (IG 2026-07-27 #14) — BEFORE the save, so the persisted
    # report carries each failing check's age. 'eod mark-to-close' failed for
    # days with the equity of record on a fallback and the only consequence was
    # another health_warning line every sweep; a red with no ladder is a red
    # nobody acts on. Generic: every check gets a streak, crossing the budget
    # raises ONE latched critical alert. Fail-soft — a broken ladder must never
    # take the health desk down with it.
    try:
        _annotate_streaks(report, _track_health_streaks(report))
    except Exception as e:
        state.journal("health_streak_error", error=str(e)[:120])
    state.save("health", report)
    if not report["ok"]:
        bad = [c["name"] for c in report["checks"] if not c["ok"]]
        state.journal("health_warning", failing=bad)
    # Street Research pickup + claim review (operator 2026-07-16): a report
    # dropped mid-day loads within one 30-min sweep, and due claims get their
    # weekly prove-or-debunk pass. sha-deduped + cost-bounded inside; fail-soft.
    try:
        from firm import street_research
        street_research.maintain()
    except Exception as e:
        state.journal("street_research_sweep_error", error=str(e)[:100])
    # Sector backfill (fleet re-grade action 10b): legacy positions bought
    # before sectors were stamped are INVISIBLE to every sector cap and blind
    # riskbrain's dominant-sector diagnosis — a sector rout misdiagnosed as
    # idiosyncratic can full-halt the firm on the wrong theory. Stamp the few
    # blanks once (network per name, so capped at 4/sweep; permanent).
    try:
        _stamped = 0
        for _slk in config.SLEEVES:
            if _stamped >= 4:
                break
            with state.SLEEVE_LOCK:
                _sv = state.get_sleeve(_slk)
                _dirty = False
                for _psym, _pp in (_sv.get("positions") or {}).items():
                    if (_stamped >= 4 or _pp.get("sector")
                            or _pp.get("sector_unavailable")
                            or _pp.get("cash_equitization")
                            or _pp.get("instrument", "equity") != "equity"):
                        continue
                    try:
                        import yfinance as _yf
                        _sec = (_yf.Ticker(_psym).info or {}).get("sector")
                    except Exception:
                        _sec = None
                    if not _sec:
                        # (2026-09-02 full audit, health-desk-1) A symbol whose
                        # .info carries no sector -- an ETF, most often -- had
                        # no way to be marked, so this un-timed-out network call
                        # repeated for it every sweep, forever, INSIDE
                        # SLEEVE_LOCK. Stamp the miss so the loop moves on. The
                        # structural fix (fetch outside the lock, write inside)
                        # is the follow-up; this stops the bleeding.
                        _pp["sector_unavailable"] = True
                        _dirty = True
                        state.journal("position_sector_unavailable",
                                      sleeve=_slk, symbol=_psym)
                    if _sec:
                        _pp["sector"] = str(_sec)
                        _dirty = True
                        _stamped += 1
                        state.journal("position_sector_backfilled",
                                      sleeve=_slk, symbol=_psym, sector=_sec)
                if _dirty:
                    state.save_sleeve(_slk, _sv)
    except Exception as e:
        state.journal("sector_backfill_error", error=str(e)[:100])
    try:
        _escalate_recon_drift()
    except Exception as e:
        state.journal("recon_escalation_error", error=str(e)[:120])
    try:
        _escalate_ig_findings()
    except Exception as e:
        state.journal("ig_escalation_error", error=str(e)[:120])
    try:
        _escalate_conviction_spread()
    except Exception as e:
        state.journal("conviction_spread_escalation_error", error=str(e)[:120])
    try:
        auto_recover_halts()
    except Exception as e:
        state.journal("auto_recover_error", error=str(e))
    try:
        review_risk_events()
    except Exception as e:
        state.journal("risk_review_error", error=str(e))
    return report


# Tolerance for "this price actually traded today". Shared by the MARKER and the
# AUDITOR below so a corrector and its detector can never disagree about what
# counts as impossible — the failure mode that lets a fixer and a checker each
# think the other is handling it.
MARK_SESSION_TOL = 0.005            # 0.5%


def _session_bars(syms: list[str]) -> dict:
    """{sym: (low, high, close, bar_date)} from the daily bar — the official
    print, fetched once and reused by the marker, the validator and the audit."""
    out: dict = {}
    if not syms:
        return out
    try:
        from firm import data_engine as _de_yf   # leak-safe yf_download
        h = _de_yf.yf_download(sorted(syms), period="5d", progress=False,
                               auto_adjust=False, group_by="ticker")
    except Exception:
        return out
    for sym in sorted(syms):
        try:
            frame = h[sym] if len(syms) > 1 else h
            row = frame.dropna().iloc[-1]
            lo, hi, cl = float(row["Low"]), float(row["High"]), float(row["Close"])
            if lo > 0 and hi > 0 and cl > 0:
                out[sym] = (lo, hi, cl, str(frame.dropna().index[-1])[:10])
        except Exception:
            continue
    return out


def _in_session_range(px: float, lo: float, hi: float) -> bool:
    """Could this price have traded today? Shared by marker and auditor."""
    return lo * (1 - MARK_SESSION_TOL) <= px <= hi * (1 + MARK_SESSION_TOL)


def mark_books_to_close() -> dict:
    """EOD mark-to-close: snap every equity position's display mark to the
    official close before the P&L booking and the nightly reconciliation.

    Root cause of the 2026-07-16 three-sleeve drift: after 16:00 there are no
    live Public quotes, and sleeve_equity's stale-guard (FIX 1, 2026-06-16)
    correctly refuses to let a daily-close FALLBACK overwrite last_price — so
    each symbol's display mark froze at whatever instant its last live quote
    arrived (restart churn left marks scattered across the afternoon), while
    reconcile_display rebuilt against official closes. The books were right;
    the display convention never converged to the close after the bell.

    This is the explicit, TRUSTED re-mark the stale-guard path can't provide:
    the official close at the postmarket wrap is the final authoritative print
    of the day, fresher than any intraday mark it replaces. Routed through
    sleeve_equity's trusted-marks path so last_price/market_value update, the
    stale flags clear, and peak_price ratchets under the poisoning guard.
    Options positions are untouched (their books reconcile via the EOD record).
    Fail-soft: no closes fetched -> no marks touched."""
    from firm import data_engine
    syms: set[str] = set()
    for name in config.SLEEVES:
        s = state.get_sleeve(name)
        for sym, p in (s.get("positions") or {}).items():
            if p.get("instrument") == "equity":
                syms.add(sym)
    if not syms:
        return {"marked": 0}
    # (2026-08-13 operator decision) CLOSE-FEED PRIMACY FLIP: Public.com is
    # now the PRIMARY close source for held names — the feed that was already
    # rescuing the books whenever yfinance failed (mark_rescued_public, incl.
    # the 2026-07-23 outage) — with yfinance the FALLBACK. Event naming keeps
    # every record truthful: primary success journals mark_primary_public,
    # names filled by the yfinance fallback journal mark_fallback_yf, and the
    # 'eod mark-to-close' health check now grades the PUBLIC feed as primary
    # (fallback reliance = degraded). The third-source session-range audit
    # below is deliberately untouched — it catches a print that never traded
    # regardless of which vendor produced it.
    # A MARK-TO-CLOSE MUST USE A CLOSE (operator 2026-08-20).
    # The 2026-08-13 primacy flip made broker.equity_quotes_public the primary
    # source here. That function's own docstring says "Mid pricing": it returns
    # (bid+ask)/2 from the LIVE quote. After the bell that is an after-hours
    # quote, which is not the close and can sit anywhere — including outside the
    # regular session's own high/low. Measured on the 2026-08-19 books: only
    # 2 of 17 held equities were marked at their actual close; MPC was marked
    # 308.78 against a 359.00-365.87 session and a 360.75 close (-14.4%,
    # $155.90 understated on 3 shares) and CORT 110.83 against 118.37-121.11
    # (-6.9%). Firm-wide that put the alpha book $119.48 (-0.51%) below its
    # real-close value, which is exactly the 'display reconciliation' drift
    # (shown -6.63 vs rebuilt -6.15): reconcile_display rebuilds from official
    # closes while the display carried quote mids, so the two conventions could
    # never converge no matter how many times the self-heal ran.
    # The 08-13 decision was about VENDOR reliability for quotes and rescues,
    # and it stands for those. It cannot answer "what was the close" — the
    # broker exposes no close endpoint at all, so the daily bar is the only
    # source that has one. Close primary, quote fallback, and a quote is
    # REFUSED outright when it names a price that never traded.
    _bars = _session_bars(sorted(syms))
    closes: dict = {sym: b[2] for sym, b in _bars.items()}
    if closes:
        state.journal("mark_close_primary", n=len(closes),
                      bar_date=next(iter(_bars.values()))[3])
    _fb_syms = sorted(syms - set(closes))
    _quoted, _refused = {}, []
    if _fb_syms:
        try:
            _q = {sym: px for sym, px
                  in (broker.equity_quotes_public(_fb_syms) or {}).items()
                  if isinstance(px, (int, float)) and px > 0}
        except Exception:
            _q = {}
        for sym, px in _q.items():
            b = _bars.get(sym)
            # No bar at all -> nothing to validate against; take the quote
            # rather than leave the name unmarked (fail-soft, unchanged from
            # the old behaviour for this case).
            if b is None or _in_session_range(px, b[0], b[1]):
                _quoted[sym] = px
            else:
                _refused.append({"symbol": sym, "quote": round(px, 4),
                                 "session_low": round(b[0], 4),
                                 "session_high": round(b[1], 4)})
        if _quoted:
            closes.update(_quoted)
            # A quote standing in for a close IS degradation of this job, even
            # when the number is plausible — grade it as such.
            state.journal("mark_quote_fallback", n=len(_quoted),
                          symbol=",".join(sorted(_quoted)[:12]))
        if _refused:
            state.journal("mark_refused_outside_session", n=len(_refused),
                          detail=_refused[:8])
    if not closes:
        state.journal("eod_mark_to_close", ok=False, error="no closes fetched")
        return {"marked": 0, "error": "no closes fetched"}
    marked, missing = 0, sorted(syms - set(closes))
    for name in config.SLEEVES:
        with state.SLEEVE_LOCK:
            s = state.get_sleeve(name)
            sleeve_marks = {sym: closes[sym]
                            for sym, p in (s.get("positions") or {}).items()
                            if p.get("instrument") == "equity" and closes.get(sym)}
            if not sleeve_marks:
                continue
            broker.sleeve_equity(s, marks=sleeve_marks)
            state.save_sleeve(name, s)
            marked += len(sleeve_marks)
    # AUDIT EVERY MARK (operator 2026-08-20). The sample was sorted(closes)[:8]
    # over the FIRM-WIDE symbol union — 8 of 22 on the 08-19 books. It caught
    # CORT (-6.9%) and EWT (-0.5%) and never looked at MPC (-14.4%, $155.90),
    # the worst mark of the night, because MPC sorts ninth. A sample sized to
    # "catch a systemic feed failure" understated the damage by an order of
    # magnitude and made a book-level error look like a rounding quibble. The
    # bars are already downloaded above, so full coverage costs nothing.
    # STAMP WHAT WE ACTUALLY WROTE (operator 2026-08-21). The display mark is
    # live by design — monitor.py reads pos["last_price"] in five places to
    # evaluate stops, including through the after-hours session, so freezing it
    # would risk blinding a real exit. That means the books ARE repainted after
    # this snap, and any later comparison against official closes is comparing
    # two different things. reconcile_display used to report that as book DRIFT
    # (2026-08-20: alpha shown -6.49 vs rebuilt -6.15 with only 1 of 17 marks
    # still equal to its close, sleeve file rewritten 22 seconds earlier) —
    # a real-looking alarm with no book error behind it. Recording the exact
    # closes we stamped lets the reconciler ask the outcome question directly:
    # is the display still carrying these, or has it moved on? That is a fact
    # about the books, not a guess from the clock.
    try:
        state.save("eod_mark_stamp",
                   {"ts": datetime.now(timezone.utc).isoformat(),
                    "bar_date": (next(iter(_bars.values()))[3] if _bars else None),
                    "marks": {k: round(float(v), 6) for k, v in closes.items()}})
    except Exception:
        pass                      # stamping must never break the mark itself
    _audit = _third_source_mark_audit(closes, bars=_bars)
    state.journal("eod_mark_to_close", ok=True, marked=marked,
                  **({"missing": missing} if missing else {}),
                  **({"third_source": _audit} if _audit else {}))
    return {"marked": marked, "missing": missing, "third_source": _audit}


def _third_source_mark_audit(closes: dict, sample: int = 0,
                             bars: dict | None = None) -> dict | None:
    """Cross-check the equity of record against a source neither leg produced.

    Fix brief 2026-07-28 #10. The nightly mark rides yfinance with a Public
    rescue behind it, and BOTH checks that would notice a bad mark (verdict
    grading, display reconciliation) read the same numbers — so a wrong close
    is self-consistent all the way down. That is not hypothetical here: the
    Public rescue path is exactly what marked PRSU at 39.08 on a session whose
    real low was 51.68, and every downstream surface agreed with it.

    So the third opinion is the DAILY BAR's own high/low, fetched independently
    of the batch that produced `closes`: a close outside its own session range
    is impossible and proves the mark is fabricated, no tolerance argument
    needed. Read-only and best-effort — this reports, it never rewrites a mark.
    Sampled because the point is to catch a systemic feed failure, and a feed
    that is lying is lying about more than one name."""
    try:
        # sample=0 means EVERY mark (2026-08-20). `bars` is reused from the
        # marking pass so full coverage costs no extra download; a caller that
        # has none falls back to fetching them here.
        syms = sorted(closes)
        if sample and int(sample) > 0:
            syms = syms[:int(sample)]
        if not syms:
            return None
        _b = bars if bars is not None else _session_bars(syms)
        checked, bad = 0, []
        for s in syms:
            got = _b.get(s)
            if not got:
                continue
            lo, hi = got[0], got[1]
            try:
                px = float(closes[s])
            except Exception:
                continue
            checked += 1
            if not _in_session_range(px, lo, hi):
                bad.append({"symbol": s, "mark": round(px, 4),
                            "session_low": round(lo, 4), "session_high": round(hi, 4)})
        if not checked:
            return {"checked": 0, "note": "third source unavailable — unverified"}
        out = {"checked": checked, "outside_session_range": bad}
        if bad:
            from firm import alerts as _al
            state.journal("eod_mark_third_source_mismatch", n=len(bad), rows=bad[:5])
            _al.alert("EQUITY OF RECORD MAY BE FABRICATED",
                      f"{len(bad)} of {checked} sampled EOD marks fall OUTSIDE "
                      "their own session high/low — a price that never traded. "
                      f"First: {bad[0]}", severity="critical")
        return out
    except Exception as e:
        return {"checked": 0, "note": f"third-source audit failed: {type(e).__name__}"}


def _escalate_recon_drift() -> None:
    """Persistent display-reconciliation drift ESCALATES (IG 2026-07-03 high,
    ratified): the 07-03 calls_only drift failed the health line every 30-min
    sweep for 6+ hours, each pass re-emitting only a benign health_warning. A
    drift that survives HEALTH_RECON_ESCALATE_H is not display noise — the
    books the operator SEES are not the books the fund KEEPS. Raise ONE
    critical alert per incident (latched in state/recon_drift_incident so the
    30-min sweep never re-alerts; cleared the moment the rebuild goes ok).
    Deliberately NOT an auto-halt: the drift is in the display layer, and a
    guard must not fire trades on a rendering defect (validate-before-harden)
    — the escalation puts a human on it instead."""
    _rd = state.load("reconcile_display", {}) or {}
    _inc = state.load("recon_drift_incident", {}) or {}
    if _rd and _rd.get("ok") is False:
        if not _inc.get("since"):
            _inc = {"since": datetime.now(timezone.utc).isoformat(),
                    "alerted": False}
            state.save("recon_drift_incident", _inc)
        _pers_h = _age_hours(_inc.get("since", ""))
        if (_pers_h >= float(getattr(config, "HEALTH_RECON_ESCALATE_H", 4.0))
                and not _inc.get("alerted")):
            # Re-verify before crying wolf (2026-07-17): the reconciler runs
            # once nightly, so a single failed record otherwise stays red for
            # ~24h and the 4h escalation fires on STALE evidence even after
            # the books converge (e.g. the postmarket mark-to-close landed).
            # One fail-soft re-run at the escalation moment only — the
            # `alerted` latch guarantees this never loops on the 30-min sweep.
            try:
                import subprocess
                import sys as _sys
                subprocess.run(
                    [_sys.executable,
                     str(config.BASE_DIR / "scripts" / "reconcile_display.py")],
                    capture_output=True, text=True, timeout=300,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),)
                _rd = state.load("reconcile_display", {}) or _rd
            except Exception:
                pass                      # re-run unavailable: escalate as before
            if _rd.get("ok"):
                state.save("recon_drift_incident", {})
                state.journal("recon_drift_cleared_on_recheck",
                              hours=round(_pers_h, 1))
                return
            from firm import alerts
            alerts.alert(
                "RECONCILIATION DRIFT PERSISTENT",
                f"display reconciliation failing {_pers_h:.1f}h: "
                + str((_rd.get("drifts") or ["?"])[0])[:140]
                + " — displayed P&L may not match the rebuilt books; "
                "investigate now",
                "critical")
            _inc["alerted"] = True
            state.save("recon_drift_incident", _inc)
            state.journal("recon_drift_escalated", hours=round(_pers_h, 1),
                          drift=str((_rd.get("drifts") or ["?"])[0])[:100])
    elif _inc:
        state.save("recon_drift_incident", {})


def _escalate_ig_findings() -> None:
    """The IG->ratification loop must never silently rot (IG 2026-07-13 critical,
    Item 7): high-severity findings that sit UN-REVIEWED past the triage SLA were
    re-emitting only a benign health_warning every 30 min for >24h with nobody
    triaging. Mirror _escalate_recon_drift: latch ONE critical alert per un-reviewed
    incident (state/ig_triage_incident, so the sweep never re-alerts), cleared the
    moment the operator advances ig_reviewed_asof or the highs clear. Deliberately
    NOT an auto-halt/BUY-block by default — the adversarial IG ALWAYS emits ranked
    gap hypotheses, so auto-gating trades on their mere existence would fire on a
    normal state (validate-before-harden). It puts a human on it; the optional
    IG_TRIAGE_BLOCK_BUYS flag (default off) is there if the desk wants the harder
    stance."""
    _ig = state.load("ig_findings_latest", {}) or {}
    _findings = (_ig.get("findings") or []) if isinstance(_ig, dict) else []
    _high = [f for f in _findings
             if str(f.get("severity", "")).lower() in ("high", "critical")]
    _snap_asof = _ig.get("asof") or "" if isinstance(_ig, dict) else ""
    _reviewed = state.load("ig_reviewed_asof", "") or ""
    _unreviewed = bool(_high) and _snap_asof > _reviewed
    _inc = state.load("ig_triage_incident", {}) or {}
    if _unreviewed:
        if not _inc.get("since"):
            _inc = {"since": datetime.now(timezone.utc).isoformat(),
                    "snapshot": _snap_asof, "alerted": False}
            state.save("ig_triage_incident", _inc)
        # A NEW snapshot restarts the SLA clock (fresh findings deserve their own
        # window) but keeps the desk aware it is still behind.
        elif _inc.get("snapshot") != _snap_asof:
            _inc = {"since": datetime.now(timezone.utc).isoformat(),
                    "snapshot": _snap_asof, "alerted": False}
            state.save("ig_triage_incident", _inc)
        _pers_h = _age_hours(_inc.get("since", ""))
        if (_pers_h >= float(getattr(config, "IG_TRIAGE_ESCALATE_H", 24.0))
                and not _inc.get("alerted")):
            from firm import alerts
            _n_crit = sum(1 for f in _high
                          if str(f.get("severity", "")).lower() == "critical")
            alerts.alert(
                "IG FINDINGS UN-TRIAGED PAST SLA",
                f"{len(_high)} high finding(s) ({_n_crit} critical) un-reviewed "
                f"{_pers_h:.1f}h [{_snap_asof[:10]}]: "
                + str((_high[0].get("gap") or _high[0].get("title") or "?"))[:140]
                + " — read state/FIX_BRIEFS.md, then set ig_reviewed_asof "
                "(POST /api/ig/triage) to close the loop",
                "critical")
            _inc["alerted"] = True
            state.save("ig_triage_incident", _inc)
            state.journal("ig_triage_escalated", hours=round(_pers_h, 1),
                          highs=len(_high), snapshot=_snap_asof[:19])
    elif _inc:
        state.save("ig_triage_incident", {})


def review_risk_events() -> None:
    """Risk-brain follow-through, every health sweep:
      1. Opus CRO second opinion on fresh diagnoses (may ESCALATE a sector
         restriction to a full halt; never silently de-escalates).
      2. Sector restrictions lift when the damaged sector ETF reclaims its
         level (+0.5%), after a 3-day minimum."""
    from firm import alerts, riskbrain
    re_ = state.load("risk_events", {"events": []})
    changed = False
    for ev in re_.get("events", [])[-5:]:
        if ev.get("opus_confirmed") or ev.get("outcome"):
            continue
        verdict = riskbrain.opus_confirm(ev)
        if not verdict:
            continue
        ev["opus_confirmed"] = True
        ev["opus_view"] = verdict
        changed = True
        state.journal("risk_event_confirmed", sleeve=ev["sleeve"],
                      deterministic=ev["classification"],
                      opus=verdict.get("classification"),
                      confidence=verdict.get("confidence"))
        # Escalation only: a "sector" restriction Opus calls market-wide with
        # high confidence becomes a full halt
        if (ev["classification"] == "sector"
                and verdict.get("classification") == "market_wide"
                and (verdict.get("confidence") or 0) >= 70):
            # The benchmark print is fetched OUTSIDE the lock on purpose: a
            # network call under SLEEVE_LOCK starves the 1s options loop into
            # the 180s watchdog restart (batch review 2026-07-01 R3/R4).
            _esc_spy = 0.0
            try:
                from firm import data_engine as _de_esc
                _esc_spy = float(_de_esc.batch_last_prices(
                    [config.BENCHMARK]).get(config.BENCHMARK, 0) or 0)
            except Exception:
                _esc_spy = 0.0
            with state.SLEEVE_LOCK:
                sl = state.get_sleeve(ev["sleeve"])
                if not sl.get("halted"):
                    sl["halted"] = True
                    sl["halt_reason"] = ("KILL SWITCH (market_wide, Opus "
                                         "escalation): " + str(verdict.get("note", ""))[:120])
                    sl["halt_equity"] = broker.sleeve_equity(sl)
                    # halt_ts + halt_spy (2026-07-25 audit finding 3): this path
                    # set halted/halt_reason/halt_equity and NOTHING else, so
                    # the most severe halt in the firm carried no cooling-off
                    # clock — `_age_hours("")` returns 1e9, which sailed through
                    # every `< 72` gate — and no market-recovery reference, so
                    # auto_recover_halts could only ever resume it on the
                    # sleeve's own bounce (impossible for a halted sleeve
                    # sitting in cash). risk.py stamps both on its own halts;
                    # this one now matches.
                    sl["halt_ts"] = datetime.now(timezone.utc).isoformat()
                    sl["halt_spy"] = _esc_spy
                    sl.pop("restricted_sectors", None)
                    state.save_sleeve(ev["sleeve"], sl)
            alerts.alert(f"ESCALATED: {ev['sleeve']} halted",
                         "Opus CRO judged the sector drawdown to be market-wide: "
                         + str(verdict.get("note", ""))[:150])
    if changed:
        state.save("risk_events", re_)

    # Sector-restriction recovery: the damaged ETF must reclaim its level
    for k in config.SLEEVES:
        with state.SLEEVE_LOCK:
            s = state.get_sleeve(k)
            levels = s.get("restricted_levels") or {}
            if not levels or s.get("halted"):
                continue
            # Fail CLOSED on a missing stamp (2026-07-25 audit finding 3):
            # `_age_hours` returns 1e9 when halt_ts is absent/unparseable, so
            # this 72h cooling-off PASSED for exactly the events that never got
            # a stamp. `_ensure_halt_ts` starts the clock at first observation
            # instead of blocking the lift forever.
            _had_ts = bool(s.get("halt_ts"))
            _sec_age = _ensure_halt_ts(k, s)
            if not _had_ts:
                state.save_sleeve(k, s)       # persist the stamp we just minted
            if _sec_age < 72:
                continue
            try:
                from firm import data_engine
                now_px = data_engine.batch_last_prices(list(levels))
            except Exception:
                continue
            recovered = [etf for etf, lvl in levels.items()
                         if lvl and now_px.get(etf, 0) >= lvl * 1.005]
            if len(recovered) == len(levels):
                s.pop("restricted_sectors", None)
                s.pop("restricted_levels", None)
                s["risk_event_active"] = False
                state.save_sleeve(k, s)
                from firm import riskbrain as rb
                rb.record_outcome(k, "sector_recovered", etfs=recovered)
                alerts.alert(f"{k}: sector restriction lifted",
                             f"{', '.join(recovered)} reclaimed pre-event levels — "
                             "all sectors open again.", severity="info")


def _exit_failure_symbol(sleeve: str, s: dict) -> str:
    """Which position could not be sold? (2026-07-25 audit finding 2.)

    monitor._sell_or_escalate stamps the symbol into halt_reason
    ("EXIT FAILURE: {sym} sell failed Nx (...)") and journals
    sleeve_halted_exit_failure with it, but never writes it onto the sleeve —
    so the recovery path had nothing to verify against and wiped every
    position's counter instead. Recovered here in evidence order: the halt
    reason, then the halt journal line, then the position whose sell_fail_count
    is actually at the escalation threshold."""
    r = str(s.get("halt_reason") or "")
    if r.startswith("EXIT FAILURE:"):
        tok = r.split(":", 1)[1].strip().split(" ")[0].strip()
        if tok:
            return tok          # may already be gone from positions — still it
    for e in state.read_journal(1500):        # newest first
        if (e.get("event") == "sleeve_halted_exit_failure"
                and e.get("sleeve") == sleeve and e.get("symbol")):
            return str(e["symbol"])
    try:
        from firm.monitor import SELL_FAIL_HALT as _sfh
    except Exception:
        _sfh = 3
    worst = [(int(p.get("sell_fail_count") or 0), sym)
             for sym, p in (s.get("positions") or {}).items()
             if int(p.get("sell_fail_count") or 0) >= _sfh]
    return max(worst)[1] if worst else ""


def _sell_succeeded_since(sleeve: str, symbol: str, since_iso: str) -> bool:
    """Did a SELL of `symbol` in `sleeve` actually WORK after `since_iso`?

    This is the positive evidence an EXIT FAILURE halt now requires
    (2026-07-25 audit finding 2). Accepts a booked paper/live fill (`order`),
    a live order the broker ACCEPTED (`order_pending` — a rejection at exactly
    that step is what failed in the first place), or a settled live fill."""
    if not symbol or not since_iso:
        return False
    for e in state.read_journal(1500):        # newest first, ts-ordered
        ts = str(e.get("ts") or "")
        if ts and ts < since_iso:
            break
        if (e.get("sleeve") != sleeve or e.get("symbol") != symbol
                or e.get("side") != "sell"):
            continue
        ev = e.get("event")
        if ev in ("order", "order_pending"):
            return True
        if ev == "order_settled" and float(e.get("filled") or 0) > 0:
            return True
    return False


def _halt_clear_budget_ok(sleeve: str) -> bool:
    """At most N auto-clears per sleeve per 24h (2026-07-25 audit finding 2).
    Even with symbol-level evidence, a halt that keeps re-arming is a DEFECT to
    look at, not a loop to run — the old code could oscillate forever at
    SELL_FAIL_HALT=3 without a single line telling the operator."""
    cut = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    log = (state.load("halt_clears", {}) or {}).get(sleeve) or []
    recent = [t for t in log if str(t) >= cut]
    return len(recent) < int(getattr(config, "HEALTH_MAX_HALT_CLEARS_24H", 2))


def _record_halt_clear(sleeve: str) -> None:
    cut = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    log = state.load("halt_clears", {}) or {}
    recent = [t for t in (log.get(sleeve) or []) if str(t) >= cut]
    recent.append(datetime.now(timezone.utc).isoformat())
    log[sleeve] = recent[-10:]
    state.save("halt_clears", log)


def auto_recover_halts() -> None:
    """Halts heal themselves when their CAUSE is verifiably gone:
      EXIT FAILURE  — clears once the SYMBOL that failed has verifiably sold
                      (or left the book) since the halt, within a 24h clear
                      budget
      KILL SWITCH   — clears once equity recovers +5% from the halt point
                      (proof the drawdown stabilized, not a dead-cat tick)
    RECONCILE and STATE CORRUPT halts never auto-clear — those mean the books
    themselves are in question and need human/Claude review first."""
    from firm import alerts, broker
    mode = state.get_settings()["mode"]
    for k in config.SLEEVES:
        with state.SLEEVE_LOCK:
            s = state.get_sleeve(k)
            reason = s.get("halt_reason", "")
            if not s.get("halted"):
                continue
            if reason.startswith("EXIT FAILURE"):
                # POSITIVE EVIDENCE REQUIRED (2026-07-25 audit finding 2). This
                # branch initialised link_ok=True and only ever reassigned it in
                # LIVE mode, so in PAPER the halt cleared on the very next 30-min
                # sweep with nothing verified — and then zeroed sell_fail_count
                # for EVERY position in the sleeve, including the one that could
                # not be sold. With monitor.SELL_FAIL_HALT=3 that is a perpetual
                # oscillator: halt -> auto-clear -> three more failures -> halt,
                # while the stuck position never exits and its stop never fires.
                # The halt now clears only on proof the exit path works for the
                # SYMBOL that failed, and only that symbol's counter resets.
                # monitor.py stamps no halt_ts on an exit-failure halt, and the
                # evidence below is "newer than the halt" — so mint one on first
                # observation (conservative: it can only DELAY a clear by a sweep).
                _had_ts = bool(s.get("halt_ts"))
                _ensure_halt_ts(k, s)
                _fsym = str(s.get("halt_symbol") or ""
                            or _exit_failure_symbol(k, s))
                _dirty = not _had_ts
                if _fsym and s.get("halt_symbol") != _fsym:
                    s["halt_symbol"] = _fsym  # remember WHAT to verify against
                    _dirty = True
                link_ok = True
                if mode == "live":
                    try:
                        broker.live_account()
                    except Exception:
                        link_ok = False
                _since = str(s.get("halt_ts") or "")
                # Gone from the book = the exit completed (or the position was
                # otherwise resolved); a successful sell after the halt = the
                # path is proven. Either is real evidence about THIS symbol.
                _exit_proven = bool(_fsym) and (
                    _fsym not in (s.get("positions") or {})
                    or _sell_succeeded_since(k, _fsym, _since))
                _budget_ok = _halt_clear_budget_ok(k)
                if link_ok and _exit_proven and _budget_ok:
                    s["halted"] = False
                    s["halt_reason"] = ""
                    s.pop("halt_symbol", None)
                    s.pop("halt_block_reason", None)
                    s.pop("halt_ts_backfilled", None)
                    pos = (s.get("positions") or {}).get(_fsym)
                    if pos is not None:
                        pos["sell_fail_count"] = 0   # ONLY the proven symbol
                    state.save_sleeve(k, s)
                    _record_halt_clear(k)
                    state.journal("halt_auto_cleared", sleeve=k,
                                  was="exit_failure", symbol=_fsym,
                                  evidence=("position closed"
                                            if _fsym not in (s.get("positions") or {})
                                            else "sell succeeded since halt"))
                    from firm import riskbrain as rb
                    rb.record_outcome(k, "auto_recovered_exit_failure",
                                      symbol=_fsym)
                    alerts.alert(f"{k} recovered",
                                 f"{_fsym} exited cleanly since the halt — exit "
                                 "failure halt cleared, monitoring resumed.",
                                 severity="info")
                else:
                    # Journal the blocked state ONCE per halt so a sleeve that
                    # stays down is visible instead of silent.
                    _why = ("broker link down" if not link_ok else
                            "auto-clear budget exhausted (24h)" if not _budget_ok
                            else ("no failing symbol recoverable"
                                  if not _fsym else
                                  f"{_fsym} has not sold since the halt"))
                    if s.get("halt_block_reason") != _why:
                        s["halt_block_reason"] = _why
                        _dirty = True
                        state.journal("halt_auto_clear_blocked", sleeve=k,
                                      symbol=_fsym, why=_why)
                    if _dirty:
                        state.save_sleeve(k, s)
            elif reason.startswith("KILL SWITCH"):
                # 1) Cooling-off: minimum 3 days, no instant re-trips. Fails
                #    CLOSED on a missing stamp (2026-07-25 audit finding 3) —
                #    `_age_hours` returned 1e9 for a halt with no halt_ts, which
                #    sailed straight through this gate; the Opus CRO escalation
                #    was exactly such a halt.
                _had_ts = bool(s.get("halt_ts"))
                _ks_age = _ensure_halt_ts(k, s)
                if not _had_ts:
                    state.save_sleeve(k, s)   # persist the stamp we just minted
                if _ks_age < 72:
                    continue
                # 2) Resume on EITHER signal — the sleeve's own bounce, or the
                #    market reclaiming its halt-day level (works even when the
                #    halted sleeve sits in cash and its equity can't move)
                halt_eq = s.get("halt_equity") or 0
                equity = broker.sleeve_equity(s, broker.fetch_marks(s), from_fetch_marks=True)
                sleeve_recovered = bool(halt_eq and equity >= halt_eq * 1.03)
                market_recovered = False
                halt_spy = s.get("halt_spy") or 0
                if halt_spy:
                    try:
                        from firm import data_engine
                        spy_now = data_engine.batch_last_prices(
                            [config.BENCHMARK]).get(config.BENCHMARK, 0)
                        market_recovered = spy_now >= halt_spy * 1.005
                    except Exception:
                        market_recovered = False
                if sleeve_recovered or market_recovered:
                    s["halted"] = False
                    s["halt_reason"] = ""
                    s.pop("halt_equity", None)
                    s.pop("halt_spy", None)
                    # RE-ARM the kill switch from the resume point: peak resets
                    # to current equity so another -kill% from HERE re-trips.
                    # Without this a resumed sleeve could bleed unprotected.
                    s["peak_equity"] = equity
                    # The kill switch measures the RISK basis (parking at cost),
                    # so its peak must re-arm too — leaving the old high here
                    # would re-trip the halt on the very next sweep.
                    s["peak_risk_equity"] = broker.sleeve_risk_equity(s, equity)
                    s["risk_event_active"] = False
                    # 3) Graduated re-entry: half-size positions for 5 days
                    s["reduced_risk_until"] = (
                        datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
                    state.save_sleeve(k, s)
                    why = ("sleeve equity +3%" if sleeve_recovered
                           else "SPY reclaimed halt-day level")
                    state.journal("halt_auto_cleared", sleeve=k, was="kill_switch",
                                  trigger=why, equity=equity)
                    from firm import riskbrain as rb
                    rb.record_outcome(k, "auto_recovered_kill_switch",
                                      trigger=why, equity_at_resume=equity)
                    alerts.alert(f"{k} resumed at half size",
                                 f"Kill switch cleared ({why}) after cooling off. "
                                 "Positions sized at 50% for 5 days, then full.",
                                 severity="info")


# ── Firm-level capital rebalance ──────────────────────────────────────────────

def _rearm_risk_basis(s: dict, delta: float, new_equity: float) -> None:
    """Move a sleeve's RISK BASES with a capital transfer. CAPITAL MOVED IS NOT
    P&L (2026-07-25 audit finding 1 — CRITICAL).

    Cash sits inside broker.sleeve_equity AND broker.sleeve_risk_equity, so an
    internal transfer is arithmetically indistinguishable from a trading loss
    on the receiving/ giving side unless every risk basis moves with it.
    rebalance_firm contained ZERO references to peak_equity, peak_risk_equity
    or day_pnl_anchor. Live numbers at the time: alpha's day_pnl_anchor was
    24,295.68 against a 3.0% day breaker = a $728.87 limit, while this function
    could move up to cash - MIN_CASH_BUFFER = $5,261. Any transfer above the
    limit instantly registered as a day loss: every entry blocked for the rest
    of the day and a `breaker_trips` row written that the learning loop later
    reads back as MARKET behaviour. The drawdown leg has the same shape — a
    donor's peak stays at its pre-transfer high, so the sleeve reads as being
    in a drawdown it never took, and the soft tier (0.7x kill) trips on it.

    Both peaks shift by the same delta and are FLOORED at the sleeve's new
    equity so a transfer can never leave a peak below current equity (a
    negative-drawdown artifact that would ratchet the kill-switch bar). The
    risk peak is floored at the new RISK equity, not the display equity —
    flooring it at the display number would import parking mark-to-market into
    the very basis sleeve_risk_equity exists to exclude, and could trip the
    kill switch outright.

    day_pnl_anchor.equity shifts by delta and is deliberately NOT floored: the
    anchor legitimately sits below equity (a gain) or above it (a loss), and
    clamping it would erase real day P&L from risk.day_pnl and the firm daily
    breaker — the opposite of the bug being fixed. It is only guarded at 0.

    peak_risk_equity is a key added 2026-07-25 and is absent on sleeves not yet
    marked since; it is adjusted ONLY when present. That is safe: risk.mark_equity
    seeds a missing one from peak_equity, which this has already corrected."""
    new_risk = broker.sleeve_risk_equity(s, new_equity)
    for key, floor in (("peak_equity", new_equity),
                       ("peak_risk_equity", new_risk)):
        v = s.get(key)
        if isinstance(v, (int, float)):
            s[key] = round(max(float(v) + delta, floor), 2)
    anc = s.get("day_pnl_anchor")
    if isinstance(anc, dict) and isinstance(anc.get("equity"), (int, float)):
        anc["equity"] = round(max(0.0, float(anc["equity"]) + delta), 2)


def rebalance_firm() -> dict | None:
    """Move free CASH from oversized sleeves to undersized ones when any
    sleeve's share of firm equity leaves the band. Positions are never
    force-sold — only idle cash migrates."""
    firm = state.load("firm", {})
    if _age_hours(firm.get("last_rebalance_check", "")) < config.REBALANCE_CHECK_DAYS * 24:
        return None
    lo, hi = config.REBALANCE_BAND

    with state.SLEEVE_LOCK:
        sleeves_st = {k: state.get_sleeve(k) for k in config.SLEEVES}
        equities = {k: broker.sleeve_equity(s) for k, s in sleeves_st.items()}
        total = sum(equities.values())
        firm["last_rebalance_check"] = datetime.now(timezone.utc).isoformat()
        state.save("firm", firm)
        if total <= 0:
            return None

        shares = {k: eq / total for k, eq in equities.items()}
        if all(lo <= sh <= hi for sh in shares.values()):
            return None

        target = total / len(config.SLEEVES)
        moved: dict[str, float] = {}
        # Queued-entry obligations (2026-07-25 audit finding 1c): a BUY blocked
        # by the closing bell waits in state/pending_open_entries and flushes at
        # the next open against the sleeve's CASH (sleeves.flush_pending_open_
        # entries). `free` counted raw cash, so the rebalance could hand away
        # money already spoken for — currently $1,922 of queued RSP — and turn a
        # saved entry into "insufficient sleeve cash". Reserve it per sleeve.
        obligations: dict[str, float] = {}
        try:
            for _pe in (state.load("pending_open_entries", []) or []):
                _sk = str(_pe.get("sleeve") or "")
                if _sk not in sleeves_st:
                    continue
                obligations[_sk] = obligations.get(_sk, 0.0) + max(
                    0.0, float(_pe.get("quantity") or 0)
                    * float(_pe.get("thesis_price") or 0))
        except (TypeError, ValueError):
            obligations = {}
        donors = sorted((k for k in shares if equities[k] > target),
                        key=lambda k: equities[k], reverse=True)
        # HALTED sleeves are never funded (2026-07-25 audit finding 1b): the old
        # sort was by equity alone, so the SMALLEST sleeve was topped up first
        # even with a kill switch on it — capital parked where no mandate can
        # deploy it while a trading sleeve stayed starved. A halt is temporary
        # and the band check re-runs, so this only defers the transfer.
        takers = sorted((k for k in shares
                         if equities[k] < target
                         and not sleeves_st[k].get("halted")),
                        key=lambda k: equities[k])
        for taker in takers:
            need = target - equities[taker]
            for donor in donors:
                if need <= 0:
                    break
                if equities[donor] <= target:
                    continue   # donor already at/below target — never drain past it
                free = max(0.0, sleeves_st[donor]["cash"]
                           - config.MIN_CASH_BUFFER
                           - obligations.get(donor, 0.0))
                surplus = max(0.0, equities[donor] - target)
                give = round(min(need, free, surplus), 2)
                if give < 100:
                    continue
                sleeves_st[donor]["cash"] -= give
                sleeves_st[taker]["cash"] += give
                # Re-arm BOTH sides' risk bases in the same locked block as the
                # cash move — a transfer must be invisible to the day breaker
                # and the drawdown kill switch (audit finding 1, see
                # _rearm_risk_basis for the numbers that made it critical).
                _rearm_risk_basis(sleeves_st[donor], -give, equities[donor] - give)
                _rearm_risk_basis(sleeves_st[taker], +give, equities[taker] + give)
                equities[donor] -= give
                equities[taker] += give
                need -= give
                moved[f"{donor}->{taker}"] = moved.get(f"{donor}->{taker}", 0) + give
        if moved:
            for k, s in sleeves_st.items():
                state.save_sleeve(k, s)
            state.journal(
                "firm_rebalance", moved=moved,
                shares_before={k: round(v, 3) for k, v in shares.items()},
                # The re-arm is journaled ALONGSIDE the move so a later reader
                # (learning, an audit) can see that the day/drawdown bases were
                # carried with the capital and never mistake the step for P&L.
                risk_basis_rearmed={
                    k: {"peak_equity": sleeves_st[k].get("peak_equity"),
                        "peak_risk_equity": sleeves_st[k].get("peak_risk_equity"),
                        "day_anchor": (sleeves_st[k].get("day_pnl_anchor")
                                       or {}).get("equity")}
                    for k in {p.split("->")[0] for p in moved}
                    | {p.split("->")[1] for p in moved}},
                reserved_for_queued_entries={k: round(v, 2)
                                             for k, v in obligations.items() if v})
            return {"moved": moved}
    return None


# ── Live-account reconciliation ───────────────────────────────────────────────

def reconcile_live() -> dict:
    """Compare internal books vs the real Public.com account. Detect-and-flag
    only — money discrepancies are surfaced, never silently 'fixed'."""
    if state.get_settings()["mode"] != "live":
        return {"drift": False, "summary": "paper mode"}
    try:
        acct = broker.live_account()
    except Exception as e:
        # asof stamped even on failure (2026-07-25 audit finding 4c): the health
        # line now age-gates this record, and a report with no asof read as
        # "never ran" forever.
        rep = {"drift": True, "summary": f"cannot reach broker: {e}",
               "asof": datetime.now(timezone.utc).isoformat()}
        state.save("reconcile", rep)
        return rep

    acct_qty = {p["symbol"]: p["quantity"] for p in acct["positions"]}
    tracked: dict[str, float] = {}
    with state.SLEEVE_LOCK:
        for k in config.SLEEVES:
            for sym, pos in state.get_sleeve(k)["positions"].items():
                tracked[sym] = tracked.get(sym, 0) + pos["quantity"]

    missing = {s: q for s, q in tracked.items()
               if abs(acct_qty.get(s, 0) - q) > 1e-6 and acct_qty.get(s, 0) < q}
    ignore = set(getattr(config, "RECONCILE_IGNORE_SYMBOLS", []))
    foreign = {s: q for s, q in acct_qty.items()
               if s not in tracked and q > 0 and s not in ignore}
    # EXCESS: the broker holds MORE than the books track (2026-07-25 audit
    # finding 4a). The comparison only ever caught `acct_qty < tracked`, so a
    # double-routed live BUY was neither `missing` (the books' quantity is
    # present) nor `foreign` (the symbol IS tracked) — the report said "books
    # match account" while untracked shares ran with NO stop, no exit ladder
    # and no position sizing.
    #
    # Deliberately NOT a halt: halting the sleeve would freeze the mandate
    # without touching the extra shares, and the shared Public.com account
    # means the external scalper can legitimately hold the same ticker (which
    # is exactly why `foreign` is an info-alert and RECONCILE_IGNORE_SYMBOLS
    # exists — it is honored here too). It is drift, alerted and journaled, for
    # the operator to attribute.
    excess = {s: {"tracked": q, "account": acct_qty.get(s, 0),
                  "extra": round(acct_qty.get(s, 0) - q, 6)}
              for s, q in tracked.items()
              if s not in ignore and acct_qty.get(s, 0) - q > 1e-6}

    # MISSING positions are the dangerous direction (books think we own
    # something the broker doesn't) — halt the owning sleeve immediately.
    # FOREIGN positions are the user trading manually in the shared account:
    # acknowledged ONCE (info alert), remembered, never touched, never spam.
    known_manual = set(state.load("manual_positions", {}).get("symbols", []))
    new_manual = [s for s in foreign if s not in known_manual]
    if new_manual:
        mp = state.load("manual_positions", {"symbols": []})
        mp["symbols"] = sorted(set(mp.get("symbols", [])) | set(new_manual))[-200:]
        state.save("manual_positions", mp)
        from firm import alerts
        alerts.alert("Manual positions noted",
                     f"You hold {', '.join(new_manual[:6])} outside the firm's "
                     "books. The firm will never touch them — they only share "
                     "your buying power.", severity="info")
        state.journal("manual_positions_detected", symbols=new_manual)
    drift = bool(missing or excess)
    if excess:
        # Alert ONCE per distinct excess set (the sweep runs every 30 min) —
        # same anti-spam latch shape as the manual-position acknowledgement.
        _xk = ",".join(f"{s}:{d['extra']}" for s, d in sorted(excess.items()))
        if (state.load("reconcile_excess", {}) or {}).get("key") != _xk:
            state.save("reconcile_excess",
                       {"key": _xk,
                        "asof": datetime.now(timezone.utc).isoformat()})
            from firm import alerts as _al_x
            _al_x.alert(
                "RECONCILE: broker holds MORE than the books",
                "; ".join(f"{s}: books {d['tracked']}, account {d['account']} "
                          f"(+{d['extra']})" for s, d in list(excess.items())[:5])
                + " — those extra shares have NO stop and no exit ladder. "
                "Attribute them (double-routed order? manual/scalper buy in the "
                "shared account?) before the next session.")
            state.journal("reconcile_excess_detected", symbols=sorted(excess))
    elif state.load("reconcile_excess", {}):
        state.save("reconcile_excess", {})
    if missing:
        with state.SLEEVE_LOCK:
            for k in config.SLEEVES:
                sl = state.get_sleeve(k)
                bad = [sym for sym in missing if sym in sl["positions"]]
                if bad and not sl.get("halted"):
                    sl["halted"] = True
                    sl["halt_reason"] = (f"RECONCILE: broker does not hold {bad} "
                                         "that the books track — verify fills "
                                         "manually, then clear halt")
                    state.save_sleeve(k, sl)
                    state.journal("sleeve_halted_reconcile", sleeve=k, symbols=bad)
                    from firm import alerts, riskbrain
                    riskbrain.record_halt(k, "reconcile_drift", sl["halt_reason"])
                    alerts.alert(f"RECONCILE DRIFT: {k} halted",
                                 f"Books track {bad} but the broker doesn't hold "
                                 "them. Ask Claude to reconcile this sleeve, "
                                 "then clear the halt.")

    parts = []
    if missing:
        parts.append(f"tracked but short at broker: {list(missing)[:5]}")
    if excess:
        parts.append("broker holds MORE than the books (unstopped shares): "
                     + ", ".join(f"{s} +{d['extra']}"
                                 for s, d in list(excess.items())[:5]))
    if foreign:
        parts.append(f"untracked account positions (manual/scalper?): {list(foreign)[:5]}")
    # NOTE (2026-07-25 audit finding 4b, NOT fixed here): `tracked` is summed by
    # SYMBOL across all four sleeves with no per-sleeve or per-instrument (OSI)
    # split, so two sleeves holding the same ticker net out against one account
    # line and an option contract compares against its underlying's row. Fixing
    # that needs the per-sleeve/instrument account breakdown from broker.py,
    # which is outside this file's ownership.
    rep = {"drift": drift, "summary": "; ".join(parts) or "books match account",
           "asof": datetime.now(timezone.utc).isoformat(),
           "missing": missing, "foreign": foreign, "excess": excess}
    state.save("reconcile", rep)
    if drift:
        state.journal("reconcile_drift", **{k: v for k, v in rep.items() if k != "asof"})
    return rep
