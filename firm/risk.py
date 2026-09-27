"""Risk desk — circuit breakers, drawdown kill switch, sizing caps.

Runs before every entry and continuously during monitoring. The risk desk
can refuse any trade and can halt a sleeve outright.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import config
from firm import state

_ET = ZoneInfo("America/New_York")


def _today() -> str:
    """Trading-day boundary is EASTERN midnight, not UTC. UTC would reset the
    day-P&L anchor at 8 pm ET — wiping the day's number to $0 every evening
    while the user is looking at it."""
    return datetime.now(_ET).strftime("%Y-%m-%d")


def _age_h(iso: str | None) -> float:
    """Hours since an ISO timestamp; unparseable -> very old (fails safe for
    'did this happen recently?' checks)."""
    try:
        return (datetime.now(timezone.utc)
                - datetime.fromisoformat(str(iso))).total_seconds() / 3600
    except (ValueError, TypeError):
        return 1e9


def mark_equity(sleeve: str, s: dict, equity: float,
                risk_equity: float | None = None) -> dict:
    """Update equity curve, peak, day anchor; trip breakers if needed.
    Curve points are throttled to one per 30s so high-frequency monitor
    ticks (the 1s calls loop) don't bloat state files.

    `equity` is the FULL sleeve value and remains the basis for the curve, the
    day anchor and every display/report — those must keep showing the real book.
    `risk_equity` (broker.sleeve_risk_equity: parking held at cost) is the basis
    for the DRAWDOWN KILL SWITCH only. Omit it and behaviour is identical to
    before, which is what every sleeve without parking sees anyway."""
    now = datetime.now(timezone.utc)
    curve = s["equity_curve"]
    append = True
    if curve:
        try:
            last_ts = datetime.fromisoformat(curve[-1]["ts"])
            append = (now - last_ts).total_seconds() >= 30
        except (ValueError, KeyError):
            pass
    if append:
        curve.append({"ts": now.isoformat(), "equity": round(equity, 2)})
    s["equity_curve"] = curve[-2000:]
    s["peak_equity"] = max(s.get("peak_equity", equity), equity)

    anchor = s.get("day_pnl_anchor", {})
    from firm import market_hours as _mh
    if anchor.get("date") != _today() and _mh.is_trading_day():
        # Day P&L counts from the PRIOR CLOSE (last mark of the previous
        # day), like every brokerage — anchoring to the first mark of the
        # new day let the overnight gap vanish from "today" entirely, so
        # day-1 -0.92% and day-2 "+1.92%" could never compound to the real
        # since-inception number (2026-06-11 operator reconciliation).
        # TRADING DAYS ONLY (2026-07-04 operator): rolling the anchor on a
        # weekend/holiday minted a phantom "day" whose P&L was pure data-source
        # drift ('Day +$8' on the Jul-4 Saturday, '+$229' on the Jul-3 holiday).
        # The prior close must also come from the last TRADING day's final
        # mark — a drift point stamped during the closed weekend would
        # otherwise become Monday's baseline and eat the real overnight gap.
        s["day_pnl_anchor"] = {"date": _today(),
                               "equity": _prior_close_equity(s, equity)}

    # Drawdown kill switch — per-sleeve threshold so a long-horizon equity
    # book survives normal corrections while the calls desk stays tight
    kill_pct = config.SLEEVES.get(sleeve, {}).get("kill_dd_pct", config.DRAWDOWN_KILL_PCT)
    # Measured on the RISK basis (parking at cost), not the display basis, so a
    # move in the parked ETF can neither halt the sleeve nor ratchet the bar it
    # is judged against. peak_equity above stays the display peak.
    r_eq = equity if risk_equity is None else risk_equity
    if "peak_risk_equity" not in s:
        # Seed by INHERITING the display peak: a sleeve already mid-drawdown must
        # not get its kill-switch clock reset to zero by this upgrade landing.
        # KNOWN CONSERVATISM (2026-07-25): where the display peak was itself built
        # by parking marks, this imports drift the risk basis exists to exclude —
        # calls_only inherits 25,456.93, a peak set 2026-07-10 with zero options
        # open, so it boots ~3% into its own kill budget. The true historical risk
        # peak is not reconstructable, the error is in the PROTECTIVE direction
        # (it halts sooner, never later), it still leaves more than one max-size
        # option loss of headroom, and it self-corrects on the first new risk-
        # equity high. Seeding at r_eq instead would zero a genuine in-progress
        # drawdown for every other sleeve, which is the worse failure.
        s["peak_risk_equity"] = max(r_eq, s.get("peak_equity", r_eq))
    s["peak_risk_equity"] = max(s["peak_risk_equity"], r_eq)
    dd_pct = (r_eq / s["peak_risk_equity"] - 1) * 100 if s["peak_risk_equity"] else 0

    # SOFT TIER (70% of the kill threshold): keep trading, but at half size.
    # Hysteresis: engages at -0.7x, releases at -0.5x — no flapping.
    if not s.get("soft_risk") and dd_pct <= -0.7 * kill_pct:
        s["soft_risk"] = True
        state.journal("soft_risk_on", sleeve=sleeve, drawdown_pct=round(dd_pct, 2))
    elif s.get("soft_risk") and dd_pct > -0.5 * kill_pct:
        s["soft_risk"] = False
        state.journal("soft_risk_off", sleeve=sleeve, drawdown_pct=round(dd_pct, 2))

    # Release a finished risk event once the drawdown has clearly healed
    if s.get("risk_event_active") and dd_pct > -0.5 * kill_pct and not s.get("halted"):
        s["risk_event_active"] = False
        s.pop("restricted_sectors", None)
        s.pop("restricted_levels", None)
        from firm import riskbrain
        riskbrain.record_outcome(sleeve, "recovered", drawdown_at_end=round(dd_pct, 2))

    # IMPLAUSIBLE DRAWDOWN = RE-PRICE BEFORE YOU HALT (fix brief 2026-07-28
    # #10). On 2026-07-01 alpha_stocks tripped a full halt on a -99.3%
    # drawdown while SPY was -1.03%, with no sector hit — and the reviewing
    # model itself wrote "not a plausible real equity loss in a flat tape;
    # treat it as a probable data/valuation artifact". It halted anyway and
    # idled the sleeve on bad data. That was the same family as the PRSU
    # phantom fills found 07-28 (a one-sided bid marking a book), which is
    # why this validates rather than trusts: an equity book cannot lose most
    # of its value while the index is flat, so a reading that says otherwise
    # is a claim about the DATA first and the market second. Re-fetch, re-price
    # from the positions, and halt only if the number reproduces.
    if (dd_pct <= -float(getattr(config, "IMPLAUSIBLE_DD_PCT", 40.0))
            and not s.get("halted") and not s.get("risk_event_active")):
        from firm import alerts, broker
        _re_dd = dd_pct
        try:
            _marks = broker.fetch_marks(s)
            _eq = broker.sleeve_equity(s, _marks, from_fetch_marks=True)
            # SAME BASIS as the first read (2026-09-01 audit): dd_pct above is
            # RISK-basis (parking at cost / peak_risk_equity); re-pricing on the
            # DISPLAY basis (peak_equity, parking marked to market) compared two
            # different quantities, so the parking's own move could suppress a
            # REAL halt or confirm a phantom one.
            _peak = float(s.get("peak_risk_equity") or 0) or None
            if _peak and _eq > 0:
                _re_dd = (broker.sleeve_risk_equity(s, _eq) / _peak - 1) * 100
        except Exception:
            _re_dd = dd_pct          # cannot re-price -> fall through and halt
        if _re_dd > dd_pct + 5.0:
            # The re-price disagrees materially: the first reading was a mark
            # artifact, not a loss. Say so LOUDLY and do not halt — an idled
            # sleeve on bad data costs real opportunity and hides the real bug.
            state.journal("drawdown_repriced_no_halt", sleeve=sleeve,
                          first_read_pct=round(dd_pct, 2),
                          repriced_pct=round(_re_dd, 2),
                          note="halt suppressed — marks disagreed on re-fetch")
            alerts.alert(
                f"DATA INTEGRITY: {sleeve} drawdown did not reproduce",
                f"first read {dd_pct:.1f}%, re-priced {_re_dd:.1f}% — the halt "
                "was SUPPRESSED and the marks are suspect. Check the quote "
                "feed before trusting this sleeve's equity.", severity="critical")
            dd_pct = _re_dd

    # HARD TIER: diagnose first, then respond proportionally
    if dd_pct <= -kill_pct and not s.get("halted") and not s.get("risk_event_active"):
        from firm import alerts, riskbrain
        event = riskbrain.diagnose(sleeve, s, dd_pct)
        s["risk_event_active"] = True
        s["halt_ts"] = datetime.now(timezone.utc).isoformat()

        if event["classification"] == "sector" and event["restricted_sectors"]:
            # Sector rout, broad market fine: amputate, don't euthanize.
            # New buys blocked ONLY in the damaged sectors; everything else
            # trades at half size. No full halt.
            s["restricted_sectors"] = event["restricted_sectors"]
            s["restricted_levels"] = event["restricted_levels"]
            s["reduced_risk_until"] = (
                datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
            riskbrain.record_event(event, "sector_restriction")
            alerts.alert(
                f"SECTOR DRAWDOWN: {sleeve} restricted",
                f"-{abs(dd_pct):.1f}% driven by {', '.join(event['restricted_sectors'])} "
                f"(SPY only {event['spy_ret_1m']:+.1f}% 1m). New buys blocked in "
                "those sectors only; others at half size. Lifts when the sector "
                "ETF reclaims its level.")
        else:
            # Market-wide or idiosyncratic: full halt with diagnosis attached
            s["halted"] = True
            s["halt_reason"] = (f"KILL SWITCH ({event['classification']}): "
                                f"drawdown {dd_pct:.1f}% breached -{kill_pct}%")
            s["halt_equity"] = equity
            s["halt_spy"] = riskbrain.market_snapshot().get("prices", {}).get(
                config.BENCHMARK, 0) if not event.get("spy_ret_1m") else None
            try:
                from firm import data_engine
                s["halt_spy"] = data_engine.batch_last_prices(
                    [config.BENCHMARK]).get(config.BENCHMARK, 0)
            except Exception:
                s["halt_spy"] = s.get("halt_spy") or 0
            riskbrain.record_event(event, "full_halt")
            why = ("broad market event" if event["classification"] == "market_wide"
                   else "picks failing while market holds — strategy review needed")
            alerts.alert(
                f"KILL SWITCH: {sleeve} halted ({event['classification']})",
                f"Drawdown {dd_pct:.1f}%: {why}. Cooling off 3+ days, then "
                "auto-resumes at HALF size on sleeve +3% or SPY reclaiming "
                "its halt-day level.")
        state.journal("kill_switch", sleeve=sleeve, drawdown_pct=round(dd_pct, 2),
                      classification=event["classification"])
    return s


def risk_scale(s: dict) -> float:
    """Half-size positions while: the soft tier is engaged (drawdown past 70%
    of the kill threshold), or inside a post-event reduced-risk window."""
    if s.get("soft_risk"):
        return 0.5
    until = s.get("reduced_risk_until", "")
    if not until:
        return 1.0
    try:
        if datetime.now(timezone.utc) < datetime.fromisoformat(until):
            return 0.5
    except (ValueError, TypeError):
        pass
    return 1.0


def sector_allowed(s: dict, sector: str) -> bool:
    """During a sector-restriction event, new buys in the damaged sectors are
    blocked; everything else keeps trading."""
    return not sector or sector not in (s.get("restricted_sectors") or [])


def _prior_close_equity(s: dict, fallback: float) -> float:
    """Equity at the last mark of the most recent PRIOR TRADING day — the
    brokerage 'day P&L counts from prior close' baseline. Skips weekend/holiday
    drift points so a closed-day mark never becomes the new baseline. Returns
    `fallback` (current equity) when the curve has no qualifying prior-day mark."""
    from firm import market_hours as _mh
    for pt in reversed(s.get("equity_curve") or []):
        try:
            pt_dt_et = datetime.fromisoformat(pt["ts"]).astimezone(_ET)
            if (pt_dt_et.strftime("%Y-%m-%d") < _today()
                    and _mh.is_trading_day(pt_dt_et)):
                return pt["equity"]
        except (ValueError, KeyError, TypeError):
            break
    return fallback


def day_pnl(s: dict, equity: float) -> float:
    anchor = s.get("day_pnl_anchor", {})
    # DISPLAY-CORRECT across a day boundary (operator 2026-07-07): the stored
    # anchor only rolls inside mark_equity (a cycle / monitor sweep). Before the
    # FIRST sweep of a new trading day — pre-market, or right after a reboot while
    # the monitor idles market-closed — the anchor is still YESTERDAY's, so
    # `equity - anchor.equity` showed yesterday's WHOLE gain as "today's". When the
    # anchor is stale on a new trading day, count from the PRIOR CLOSE instead
    # (pure read; the stored anchor still rolls properly on the next real sweep).
    from firm import market_hours as _mh
    if (anchor.get("date") and anchor.get("date") != _today()
            and _mh.is_trading_day()):
        return equity - _prior_close_equity(s, equity)
    return equity - anchor.get("equity", equity)


def firm_daily_breaker() -> tuple[bool, str]:
    """FIRM-WIDE daily loss breaker (2026-06-14 audit, finding A1). The per-sleeve
    breakers each gate only their OWN day loss, so on a correlated red day all four
    sleeves can run to their own limits at once (~4.5%+ of total firm equity in new
    entries alone) with NO aggregate brake. This caps the firm's combined day P&L:
    once the whole book is down FIRM_DAILY_LOSS_BREAKER_PCT (vol-widened) of firm
    day-start equity, NO sleeve may open new entries (exits/stops always work).
    Fails OPEN only on a genuine compute error (never block trading on a glitch),
    journaled. Cheap, internal-state only."""
    try:
        from firm import broker, riskbrain
        firm_day, firm_anchor = 0.0, 0.0
        today = _today()
        for k in config.SLEEVES:
            sv = state.get_sleeve(k)
            eq = broker.sleeve_equity(sv)
            anc = sv.get("day_pnl_anchor", {})
            # DATE-AWARE (change-audit verify 2026-06-14): a sleeve's anchor is only
            # rolled to today inside mark_equity(), which in the cycle runs AFTER
            # entries_allowed()->firm_daily_breaker(). On the first cycle of a new
            # day, other sleeves' anchors are still YESTERDAY's — counting them would
            # measure the overnight gap, not today, and falsely block (or miss). A
            # not-yet-marked-today sleeve contributes its equity to the denominator
            # but ZERO to today's P&L until it is actually marked.
            if anc.get("date") == today:
                base = anc.get("equity") or eq
                firm_day += eq - base
                firm_anchor += base
            else:
                firm_anchor += eq
        if firm_anchor <= 0:
            return True, ""
        limit = firm_anchor * tuned_cap("FIRM_DAILY_LOSS_BREAKER_PCT") / 100 * riskbrain.vol_multiplier()
        if firm_day <= -limit:
            return False, (f"FIRM daily breaker: combined day P&L {firm_day:+,.0f} "
                           f"<= -${limit:,.0f} ({tuned_cap('FIRM_DAILY_LOSS_BREAKER_PCT')}% "
                           "of firm day-start equity). All new entries paused firm-wide; "
                           "exits/stops still active. Re-opens if the book recovers intraday.")
    except Exception as e:
        state.journal("firm_breaker_check_failed_open", error=str(e)[:120])
    return True, ""


_exit_gap_journal_ts = 0.0


def exit_protection_gap(sleeve: str | None = None) -> str:
    """Reason NEW risk must not be taken right now, or "" (2026-09-01; fix
    briefs 28/72: 'block new entries while exit protection is down').

    In-session, the `equity_sweep` heartbeat is written ONLY by a real exit
    sweep (app.py's monitor loop, or a headless HeadlessExitGuard). If no
    sweep has run for EXIT_PROTECTION_MAX_SWEEP_AGE_S while a session is open,
    stops cannot route, and a fresh position would be born unprotected — the
    2026-07-24 and 08-17 blackouts both produced stops that fired 13-19% late.
    Exits are never gated by this (they ARE the protection). Off-session it is
    silent: entries then queue for the open, where the sweep runs. Fail-open on
    an unreadable heartbeat — a missing file is not evidence of a blackout."""
    global _exit_gap_journal_ts
    if not getattr(config, "EXIT_PROTECTION_ENTRY_GATE", True):
        return ""
    try:
        from firm import market_hours
        if not (market_hours.market_is_open() or market_hours.extended_session_open()):
            return ""
        hb = state.load("heartbeat", {}) or {}
        # (2026-09-02 full audit, exit-protection-e2e-2) The options desk has
        # its own 1 s loop and its own exits; equity_sweep says NOTHING about
        # it. The puts gate became reachable 2026-09-01, so the desk is about
        # to open real leveraged positions for the first time — refuse to open
        # one while its monitor thread is not beating. `calls_monitor` is
        # written at the TOP of app.py's loop, before the has_real_options
        # branch, so it beats even with a flat desk: requiring it cannot
        # deadlock the first open. NOTE it is a LIVENESS beat, not a coverage
        # beat like equity_sweep — a beating thread that is standing down for a
        # foreign cycle lock still beats. This is the floor, not the ceiling; a
        # true options-coverage beat is the follow-up.
        if sleeve == "calls_only":
            _cts = str(hb.get("calls_monitor") or "")
            if _cts:
                _cage = (datetime.now(timezone.utc)
                         - datetime.fromisoformat(_cts)).total_seconds()
                _cmax = float(getattr(config, "OPTIONS_MONITOR_STALE_S", 180.0))
                if _cage > _cmax:
                    import time as _t
                    global _exit_gap_journal_ts
                    if _t.time() - _exit_gap_journal_ts > 300:
                        _exit_gap_journal_ts = _t.time()
                        state.journal("entries_blocked_exit_protection_down",
                                      sleeve="calls_only", monitor="calls",
                                      beat_age_s=round(_cage), bar_s=round(_cmax))
                    return (f"options monitor is not beating — last beat "
                            f"{_cage / 60:.0f} min ago (bar {_cmax / 60:.0f}); no "
                            "new leveraged position while its exits are unwatched")
        ts = str(hb.get("equity_sweep") or "")
        if not ts:
            return ""
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
        max_age = float(getattr(config, "EXIT_PROTECTION_MAX_SWEEP_AGE_S", 600.0))
        if age > max_age:
            import time as _t
            if _t.time() - _exit_gap_journal_ts > 300:
                _exit_gap_journal_ts = _t.time()
                state.journal("entries_blocked_exit_protection_down",
                              sweep_age_s=round(age), bar_s=round(max_age))
            return (f"exit protection is DOWN — last exit sweep {age / 60:.0f} min "
                    f"ago (bar {max_age / 60:.0f}); no new risk while stops cannot "
                    "route")
    except Exception:
        return ""
    return ""


def entries_allowed(sleeve: str, s: dict, equity: float) -> tuple[bool, str]:
    if s.get("halted"):
        return False, s.get("halt_reason", "sleeve halted")
    _gap = exit_protection_gap(sleeve)
    if _gap:
        return False, _gap
    # IG triage hard stance (2026-07-16, wiring the lever health.py's escalator
    # promised): when the flag is ON and high IG findings have sat un-triaged
    # past the SLA (incident latched+alerted), new entries pause until the
    # operator triages (POST /api/ig/triage). Default OFF — exits always run.
    if getattr(config, "IG_TRIAGE_BLOCK_BUYS", False):
        _inc = state.load("ig_triage_incident", {}) or {}
        if _inc.get("alerted"):
            return False, ("IG findings un-triaged past SLA — triage via "
                           "POST /api/ig/triage (IG_TRIAGE_BLOCK_BUYS is on)")
    firm_ok, firm_why = firm_daily_breaker()   # aggregate brake before the per-sleeve one
    if not firm_ok:
        return False, firm_why
    anchor_eq = s.get("day_pnl_anchor", {}).get("equity", equity) or equity
    pct = config.SLEEVES.get(sleeve, {}).get("day_breaker_pct",
                                             config.DAILY_LOSS_BREAKER_PCT)
    from firm import riskbrain
    mult = riskbrain.vol_multiplier()   # fast-market days widen the breaker
    limit = anchor_eq * pct / 100 * mult
    day = day_pnl(s, equity)
    if day <= -limit:
        # PHANTOM-TRIP guard (2026-07-01 goal audit, J2 — deliberately NARROW):
        # a corrupted book can imply a physically impossible day P&L (the HI0-
        # HI6 test-fixture overwrite implied -$18,000 vs an $840 limit, -72% in
        # a day) and the trip then poisons breaker_trips, which feeds the
        # learning review. PAPER mode only, and ONLY when BOTH hold: the implied
        # single-day move exceeds 50% of day-start equity AND a state-corruption
        # signal fired in the last 24h. Entries stay BLOCKED (fail-safe, same as
        # a real trip) — but the trip is not RECORDED as market behavior, and
        # the operator is told to reconcile the books instead of reading a
        # crash. A genuine -50% day without corruption events still trips
        # normally (both legit 06-23 trips were ~-3-5%, far below this bar).
        if (state.get_settings().get("mode") == "paper"
                and anchor_eq > 0 and abs(day) > 0.5 * anchor_eq):
            # Deep scan (2000 lines ≈ 2 days at current volume — a morning
            # corruption must still be visible in the afternoon; batch review
            # R13) and EXCLUDE benign heartbeat/status restores (R14 — those
            # recover ephemeral files, not books, and must not suppress
            # recording a real crash trip).
            _corr = [e for e in state.read_journal(2000)
                     if _age_h(e.get("ts")) <= 24.0
                     and (e.get("event") == "sleeve_corrupt_halted"
                          or (e.get("event") == "state_restored_from_backup"
                              and e.get("file") not in ("heartbeat", "status")))]
            if _corr:
                state.journal("breaker_phantom_suspect", sleeve=sleeve,
                              day_pnl=round(day, 2), limit=round(limit, 2),
                              corruption_events=len(_corr))
                try:
                    from firm import alerts
                    alerts.alert("BREAKER PHANTOM-TRIP SUSPECT",
                                 f"{sleeve} implies a {day / anchor_eq * 100:+.0f}% "
                                 "single-day move WITH state-corruption events in the "
                                 "last 24h — books likely corrupt, not a crash. Entries "
                                 "blocked; reconcile the sleeve, then trading resumes.")
                except Exception:
                    pass
                return False, ("suspected PHANTOM day P&L (state corruption in "
                               "session) — reconcile books; entries blocked, trip "
                               "not recorded")
        # Learnable: count one trip per sleeve per day so the review can see
        # whether the breakers fire too often (thresholds wrong) or never
        trips = state.load("breaker_trips", {})
        today = _today()
        if sleeve not in trips.get(today, {}):
            # AUDIT 2026-07-16: etf_rotation runs THROUGH this breaker by
            # design (ROTATION_IGNORES_LOSS_BREAKER) — its trips gate nothing
            # and were polluting the learning review's breaker telemetry as if
            # they were real halts. Tag them so every consumer can tell a
            # binding trip from an advisory one.
            _advisory = (sleeve == "etf_rotation"
                         and getattr(config, "ROTATION_IGNORES_LOSS_BREAKER", True))
            trips.setdefault(today, {})[sleeve] = {
                "day_pnl": round(day, 2), "limit": round(limit, 2), "mult": mult,
                "advisory": _advisory}
            for old in sorted(trips)[:-14]:
                trips.pop(old, None)
            state.save("breaker_trips", trips)
            state.journal("breaker_tripped", sleeve=sleeve,
                          day_pnl=round(day, 2), limit=round(limit, 2),
                          advisory=_advisory)
        return False, (f"daily circuit breaker: day P&L {day:+,.0f} <= "
                       f"-{pct}%×{mult} of day-start equity (-${limit:,.0f}). "
                       "Re-opens automatically if equity recovers intraday.")
    return True, ""


def check_position_size(sleeve: str, equity: float, cost: float,
                        max_position_pct: float) -> tuple[bool, str]:
    if cost <= 0:
        return False, "non-positive cost"
    pct = cost / equity * 100 if equity else 100
    if pct > max_position_pct + 0.5:
        return False, f"position {pct:.1f}% exceeds cap {max_position_pct}%"
    return True, ""


def check_correlation(s: dict, sym: str, threshold: float | None = None) -> tuple[bool, str]:
    """True diversification check: eight names in different sectors can still
    be ONE bet (all high-beta momentum). Block an entry whose 6-month return
    correlation with the existing book averages above the threshold.
    Strategic core ETFs are excluded from the basis — every stock correlates
    with VOO/QQQ; the check is about the SATELLITE book being one bet."""
    thr = threshold if threshold is not None else tuned_cap("MAX_BOOK_CORRELATION")
    from firm import allocation as _alloc_rk
    core = (set(_alloc_rk.core_weights()) if s.get("sleeve") == "core_blend"
            else set(config.SLEEVES.get(s.get("sleeve", ""), {}).get("core_weights") or {}))
    held = [x for x, p in s["positions"].items()
            if p["instrument"] == "equity" and x not in core
            and not p.get("cash_equitization")]   # parked benchmark isn't a bet
    if len(held) < 2:
        return True, ""
    try:
        from firm import data_engine
        closes = data_engine.fetch_history(sorted(set(held + [sym])), period="6mo")
        rets = closes.pct_change().dropna()
        if sym not in rets.columns:
            return True, ""
        cors = [float(rets[sym].corr(rets[h])) for h in held if h in rets.columns]
        cors = [c for c in cors if c == c]
        if not cors:
            return True, ""
        avg = sum(cors) / len(cors)
        if avg > thr:
            return False, (f"avg return correlation {avg:.2f} with the book > "
                           f"{thr} — different tickers, same bet")
    except Exception as e:
        # Fail-open is the right call (a data hiccup must not block trading),
        # but a SILENT fail-open turns the check into theater — journal it
        state.journal("correlation_check_failed_open", symbol=sym,
                      error=str(e)[:120])
        return True, ""
    return True, ""


def tuned_cap(name: str) -> float:
    """Runtime-effective risk cap. The Opus performance review may re-tune
    caps (state/tuning.json "caps") but ONLY inside config.CAP_BOUNDS — the
    learning loop adapts the firm's risk posture, it never breaks the firm.
    No tuning on file = the config default."""
    base = getattr(config, name)
    # MOMENTUM_LANE_BOUNDS keys are first-class tunables (fleet re-grade
    # action 4: the clamp accepted them and the journal recorded caps_tuned,
    # but this lookup returned the static base — the lever's actuator was
    # disconnected and the loop minted FALSE evidence of self-improvement).
    bounds = (config.CAP_BOUNDS.get(name)
              or getattr(config, "MOMENTUM_LANE_BOUNDS", {}).get(name)
              or getattr(config, "RATCHET_BOUNDS", {}).get(name)
              or getattr(config, "QUANT_COMPRESS_BOUNDS", {}).get(name))
    if not bounds:
        return base
    v = state.load("tuning", {}).get("caps", {}).get(name)
    if isinstance(v, (int, float)):
        return min(bounds[1], max(bounds[0], float(v)))
    return base


def firm_symbol_exposure_pct(symbol: str) -> float:
    """Look-through: one ticker's share of firm equity counting EVERY holding
    — parking, seats, rotation, satellites, options by premium (fleet
    re-grade action 2). Powers the parking ceiling (_equitize_idle_cash) and
    the seat-flip convergence check (allocation.review_seats). Fail-soft 0.0
    — both consumers treat an unreadable book as 'no ceiling evidence' and
    journal their own decision."""
    try:
        total, firm_eq = 0.0, 0.0
        for k in config.SLEEVES:
            sv = state.get_sleeve(k)
            firm_eq += sv.get("cash", 0)
            for sym, p in sv.get("positions", {}).items():
                val = p.get("market_value") or p.get("cost") or 0
                firm_eq += val
                if (p.get("underlying") or sym) == symbol:
                    total += val
        return total / firm_eq * 100 if firm_eq > 0 else 0.0
    except Exception as e:
        state.journal("firm_exposure_read_failed", symbol=symbol,
                      error=str(e)[:80])
        return 0.0


def _is_diversified_etf(symbol: str) -> bool:
    """Broad funds the firm knows as ETFs — single-name concentration risk
    does not apply to a 60-stock sector fund the way it does to one ticker."""
    known = (set(config.ETF_UNIVERSE) | set(config.ROTATION_UNIVERSE)
             | {config.CASH_ETF, config.BENCHMARK})
    for cfg_ in config.SLEEVES.values():
        known |= set(cfg_.get("core_weights") or {})
    try:
        from firm import allocation as _alloc_rk2
        known |= set(_alloc_rk2.core_weights())
        known.add(_alloc_rk2.parking_symbol("alpha_stocks"))
    except Exception:
        pass
    return symbol in known


def check_firm_name_cap(symbol: str, new_cost: float) -> tuple[bool, str]:
    """Sleeve caps see one book at a time — alpha shares + a core satellite
    + a calls position can quietly stack ONE underlying to 3x any single
    sleeve's limit (2026-06-10 absence audit). Cap the FIRM's aggregate
    exposure per underlying: 10% for single names, 25% for diversified ETFs
    (rotation's validated 33%-of-sleeve slugs are exposure, not stacking).
    Fails open on data errors, journaled."""
    try:
        cap = (tuned_cap("MAX_FIRM_ETF_PCT") if _is_diversified_etf(symbol)
               else tuned_cap("MAX_FIRM_NAME_PCT"))
        total, firm_eq = new_cost, 0.0
        for k in config.SLEEVES:
            sv = state.get_sleeve(k)
            firm_eq += sv.get("cash", 0)
            for sym, p in sv.get("positions", {}).items():
                val = p.get("market_value") or p.get("cost") or 0
                firm_eq += val
                # Parking COUNTS in the numerator (fleet re-grade action 2:
                # ~40% of firm equity sat in one SPY-clone ticker that this
                # cap never saw, while the same momentum engine could seat
                # rotation + core into the identical instrument — up to ~50%
                # of firm in one ticker, invisible). Mechanical parking /
                # seat legs don't run this gate, so counting it here only
                # prices the convergence for CONVICTION entries — exactly
                # the exposure a look-through cap exists to see.
                if (p.get("underlying") or sym) == symbol:
                    total += val
        if firm_eq > 0 and total / firm_eq * 100 > cap:
            return False, (f"firm-wide {symbol} exposure would be "
                           f"{total / firm_eq * 100:.0f}% > {cap}% cap")
    except Exception as e:
        # FAIL CLOSED (2026-06-14 audit, A11): this is pure internal-state math —
        # no network — so an exception means a CORRUPT sleeve, not a data hiccup.
        # Blocking a trade when the book is unreadable is the safe choice; the old
        # fail-open disabled the cap in exactly the (corrupt-state) crisis it's for.
        state.journal("firm_cap_check_failed_closed", symbol=symbol, error=str(e)[:120])
        return False, f"firm name-cap check error (fail-closed): {str(e)[:60]}"
    return True, ""


def check_firm_sector_cap(symbol: str, sector: str, new_cost: float) -> tuple[bool, str]:
    """FIRM-WIDE sector exposure cap (2026-06-14 audit, A4). Per-sleeve sector caps
    let alpha + core satellites + calls each hold the same sector under their own
    limit while the WHOLE firm is one giant sector bet (a tech/AI unwind then hits
    all sleeves at once — the correlated drawdown the kill switch assumes won't span
    the firm). Caps aggregate per-GICS-sector value across all sleeves vs firm
    equity. Fails CLOSED (internal math). No-op if positions lack sector tags."""
    if not sector:
        return True, ""
    try:
        cap = tuned_cap("MAX_FIRM_SECTOR_PCT")
        exposure, firm_eq = new_cost, 0.0
        for k in config.SLEEVES:
            sv = state.get_sleeve(k)
            firm_eq += sv.get("cash", 0)
            for sym, p in sv.get("positions", {}).items():
                val = p.get("market_value") or p.get("cost") or 0
                firm_eq += val
                if p.get("cash_equitization"):
                    continue
                if p.get("sector") == sector and (p.get("underlying") or sym) != symbol:
                    exposure += val
        if firm_eq > 0 and exposure / firm_eq * 100 > cap:
            return False, (f"firm-wide {sector} exposure would be "
                           f"{exposure / firm_eq * 100:.0f}% > {cap}% cap")
    except Exception as e:
        state.journal("firm_sector_check_failed_closed", sector=sector, error=str(e)[:120])
        return False, f"firm sector-cap check error (fail-closed): {str(e)[:60]}"
    return True, ""


def check_sector_cap(positions: dict, sectors: dict[str, str],
                     new_symbol: str, new_cost: float, equity: float) -> tuple[bool, str]:
    sector = sectors.get(new_symbol)
    if not sector or not equity:
        return True, ""
    exposure = new_cost
    for sym, pos in positions.items():
        # AUDIT 2026-07-16 F5: the cycle's sectors dict only covers THIS
        # cycle's candidates — since the 9:45/8:30 split, held names are not
        # in it, so the cap went blind to the existing book. Fall back to the
        # sector stamped on the position at buy time.
        if (sectors.get(sym) or pos.get("sector")) == sector:
            exposure += pos.get("market_value", pos.get("cost", 0))
    pct = exposure / equity * 100
    cap = tuned_cap("MAX_SECTOR_PCT")
    if pct > cap:
        return False, f"sector {sector} would be {pct:.0f}% > {cap}% cap"
    return True, ""


def clear_halt(sleeve: str) -> None:
    with state.SLEEVE_LOCK:
        s = state.get_sleeve(sleeve)
        s["halted"] = False
        s["halt_reason"] = ""
        state.save_sleeve(sleeve, s)
    state.journal("halt_cleared", sleeve=sleeve)
