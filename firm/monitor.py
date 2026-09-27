"""Exit monitor — runs continuously while the app is up.

Equity sleeves: hard stop from entry + trailing stop behind peak.
Calls desk: the proven layered scaled-exit ladder from the market-analysis
scalper, applied to the OPTION CONTRACT price:
  SL -2% | TP1 +5% sell 50% | TP2 +60% sell rest except 1 runner
  TP3 +100% runner rides | trail 5% -> 15% -> 20% behind peak per tier
"""
from datetime import date, datetime, timezone

import config
from firm import broker, risk, state


def _check_equity_sleeve(sleeve: str, mode: str) -> int:
    with state.SLEEVE_LOCK:
        return _check_equity_sleeve_locked(sleeve, mode)


MARK_ANOMALY_PCT = 50.0   # one-sweep move beyond this needs confirmation
SELL_FAIL_HALT = 3        # consecutive failed live exits -> halt the sleeve
MARK_FAIL_ALERT_STREAK = 5  # consecutive unpriced sweeps -> tell the operator
MARK_FAIL_ALERT_MIN_S = 120.0   # ...but the streak must ALSO span this long, so the
                                # ~1s calls loop can't page on a 5-second no-bid
                                # flicker while the 60s equity loop is unaffected
PEAK_MAX_JUMP_PCT = config.PEAK_MAX_JUMP_PCT   # peak-poisoning guard (see config)


# ── Headless exit-protection guard (2026-09-01; fix briefs 87/66/53/72) ──────
# The single-writer rule says a FOREIGN process holding state/cycle.lock makes
# the app's monitors stand down — correct, and it left the book unprotected for
# the whole hold: the headless cycle swept once at its end (never, on the
# no_verdicts path), and 2026-08-31 logged 36 straight regular-hours minutes of
# equity_sweep_skipped. The fix is not to relax the rule but to make the lock-
# holder protect the book itself: same process as the cycle, so its sweeps and
# the cycle's sleeve writes serialize on state.SLEEVE_LOCK exactly as the app's
# own monitor threads do beside an in-app cycle (A13 design). The guard stamps
# state/headless_sweep {pid, ts} on every real sweep so app.py can tell a
# covered minute from a blind one.

def _stamp_headless_sweep(sweeps: int) -> None:
    import os as _os
    import time as _time
    try:
        state.save("headless_sweep", {"pid": _os.getpid(), "ts": _time.time(),
                                      "sweeps": sweeps})
        from firm import health as _hl
        _hl.heartbeat("equity_sweep")      # the coverage beat health.py grades
    except Exception:
        pass


def headless_sweep_fresh(holder_pid: int | None) -> bool:
    """Is the process holding the cycle lock sweeping exits right now?"""
    import time as _time
    if holder_pid is None or holder_pid < 0:
        return False
    try:
        rec = state.load("headless_sweep", {}) or {}
        if int(rec.get("pid") or -1) != int(holder_pid):
            return False
        max_age = (float(config.EQUITY_MONITOR_INTERVAL_S)
                   * float(getattr(config, "HEADLESS_SWEEP_FRESH_MULT", 3.0)))
        return (_time.time() - float(rec.get("ts") or 0.0)) <= max_age
    except Exception:
        return False


class HeadlessExitGuard:
    """Context manager: keep exit protection alive while THIS process holds the
    cycle lock. Sweeps the equity sleeves every EQUITY_MONITOR_INTERVAL_S and
    the options book every CALLS_MONITOR_INTERVAL_S (while real options are
    open) for as long as a session is open; idles otherwise. The first sweep
    runs immediately, so the hand-off gap is seconds, not a cadence. Fail-soft:
    an error in one sweep is journaled (rate-limited) and the loop continues —
    the guard must never take the cycle down, and the cycle must never take
    the guard down (daemon thread, joined on exit)."""

    def __init__(self, label: str = "cycle") -> None:
        import threading as _th
        self.label = label
        self.sweeps = 0
        self.errors = 0
        self._stop = _th.Event()
        self._thread = None

    def __enter__(self):
        import threading as _th
        if not getattr(config, "HEADLESS_EXIT_SWEEP", True):
            return self
        self._thread = _th.Thread(target=self._run, daemon=True,
                                  name=f"headless-exit-{self.label}")
        self._thread.start()
        state.journal("headless_exit_guard_started", label=self.label)
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=30)
        state.journal("headless_exit_guard_stopped", label=self.label,
                      sweeps=self.sweeps, errors=self.errors)
        return False

    def _run(self) -> None:
        import time as _time
        from firm import market_hours
        next_eq = 0.0
        next_calls = 0.0
        last_err_ts = 0.0
        while not self._stop.is_set():
            now = _time.time()
            cadence = float(config.EQUITY_MONITOR_INTERVAL_S)
            try:
                session_open = (market_hours.market_is_open()
                                or market_hours.extended_session_open())
                if session_open and now >= next_eq:
                    if state.get_settings().get("auto_monitor", True):
                        check_all_exits(include_calls=False)
                        self.sweeps += 1
                        _stamp_headless_sweep(self.sweeps)
                    next_eq = now + cadence
                if session_open and now >= next_calls and market_hours.market_is_open():
                    if has_real_options(state.get_sleeve("calls_only")):
                        check_calls_exits()
                        next_calls = now + float(config.CALLS_MONITOR_INTERVAL_S)
                    else:
                        next_calls = now + float(config.CALLS_MONITOR_IDLE_S)
            except Exception as e:
                self.errors += 1
                if now - last_err_ts > 300:
                    last_err_ts = now
                    state.journal("headless_exit_guard_error", label=self.label,
                                  error=str(e)[:160])
            # Tick fast enough to honor a short test cadence, slow enough to
            # cost nothing in production (60 s cadence -> 0.5 s ticks).
            self._stop.wait(max(0.05, min(0.5, cadence / 4.0)))


def _abs_stop_floor() -> float:
    """Effective ABSOLUTE per-position loss floor (fix brief 2026-07-27 #6).

    Mirrors risk.tuned_cap's contract — config default, overridable from
    tuning.json "caps", hard-clamped into config.ABS_STOP_BOUNDS — without
    routing through tuned_cap itself, which only knows CAP_/MOMENTUM_LANE_/
    RATCHET_BOUNDS and would silently return the static base for this key. A
    lever whose actuator is disconnected mints false evidence of
    self-improvement (the momentum-lane lesson), so the read is wired here and
    the WRITE side stays gated in learning._apply_cap_tuning until that
    whitelist adds ABS_STOP_BOUNDS.

    0.0 disables the floor (config removed / unreadable) — fail-soft to the
    pre-existing vol-aware stops, never to a wider stop.
    """
    base = float(getattr(config, "ABS_STOP_FLOOR_PCT", 0.0) or 0.0)
    bounds = getattr(config, "ABS_STOP_BOUNDS", {}).get("ABS_STOP_FLOOR_PCT")
    if not bounds:
        return base
    try:
        v = state.load("tuning", {}).get("caps", {}).get("ABS_STOP_FLOOR_PCT")
        if isinstance(v, (int, float)):
            return min(bounds[1], max(bounds[0], float(v)))
    except Exception:
        pass
    return base


def _stop_depth_audit(sleeve: str, sym: str, pos: dict, *, entry: float,
                      exit_px: float, peak: float, reason: str,
                      intended_hard: float | None, intended_trail: float | None,
                      floor: float, prev_mark: float, live_ext: bool) -> None:
    """INTENDED-VS-REALIZED stop depth, written on every routed stop exit
    (fix brief 2026-07-27 #6).

    The review could see that stops executed at -28.4%/-29.2% against a
    ~2.5-sigma design worth roughly -8% to -13%, but had no per-trade record of
    WHICH part of the chain produced the slippage: a stop distance stamped too
    wide at entry, a gap through the level between two sweeps, or a monitor that
    was not looking. Each of those has a different fix, so each needs its own
    measurable. This line carries all three:

      * DESIGN  — intended_stop_pct / intended_stop_px as stamped at entry, plus
        the vol_ann and vol regime that produced them.
      * SLIPPAGE — gap_pct, realized minus intended. NEGATIVE means the exit was
        deeper than designed; this is the number the review tunes against.
      * CADENCE — the sweep interval, the seconds actually elapsed since this
        position's previous sweep (a blackout shows up here as a number far
        larger than the interval), and the one-sweep move that carried it
        through the level.

    Purely observational: it never changes an exit decision. Fail-soft, because
    an audit line must never be able to block a stop from being recorded.
    """
    try:
        from firm import market_hours
        now_s = datetime.now(timezone.utc).timestamp()
        prev_sweep = pos.get("_last_sweep_ts")
        realized = (exit_px / entry - 1) * 100 if entry else 0.0
        from_peak = (exit_px / peak - 1) * 100 if peak else 0.0
        vol_ann = pos.get("vol_ann")
        is_trail = reason.startswith(("trailing stop", "profit ratchet"))
        # Intended depth is measured against whichever leg fired: a trailing
        # exit is designed as a give-back from the PEAK, so grading it against
        # the entry-referenced hard stop would report false slippage.
        intended = intended_trail if is_trail else intended_hard
        if reason.startswith("hard stop (absolute floor)"):
            intended = floor
        realized_vs = from_peak if is_trail else realized
        state.journal(
            "stop_depth_audit", sleeve=sleeve, symbol=sym, reason=reason,
            exit_leg=("trail" if is_trail else "hard"),
            intended_stop_pct=(round(float(intended), 2)
                               if isinstance(intended, (int, float)) else None),
            # The stop DISTANCE stamped at entry by sleeves._vol_stops, always
            # reported even when the absolute floor is what actually fired —
            # this pair IS the sigma-design evidence brief #6 asks for, and it
            # would otherwise be erased in exactly the cases that motivated the
            # brief (a vol stop so wide the floor had to bind).
            stamped_stop_pct=(round(float(intended_hard), 2)
                              if isinstance(intended_hard, (int, float)) else None),
            stamped_trail_pct=(round(float(intended_trail), 2)
                               if isinstance(intended_trail, (int, float)) else None),
            intended_stop_px=(round(entry * (1 - float(intended) / 100), 4)
                              if isinstance(intended, (int, float)) and entry
                              and not is_trail else None),
            entry_px=round(entry, 4), exit_px=round(exit_px, 4),
            peak_px=round(peak, 4) if peak else None,
            realized_pct=round(realized, 2),
            realized_from_peak_pct=round(from_peak, 2),
            # negative = executed DEEPER than the design allowed
            gap_pct=(round(realized_vs + float(intended), 2)
                     if isinstance(intended, (int, float)) else None),
            abs_floor_pct=round(floor, 2),
            vol_ann=vol_ann, vol_bucket=config.vol_bucket(vol_ann),
            # cadence context — the "did the monitor get a look?" evidence
            sweep_interval_s=getattr(config, "EQUITY_MONITOR_INTERVAL_S", None),
            since_prev_sweep_s=(round(now_s - float(prev_sweep), 1)
                                if isinstance(prev_sweep, (int, float)) else None),
            prev_mark=round(prev_mark, 4) if prev_mark else None,
            one_sweep_move_pct=(round((exit_px / prev_mark - 1) * 100, 2)
                                if prev_mark else None),
            market_open=market_hours.market_is_open(),
            extended_session=market_hours.extended_session_open(),
            # NAME the session (operator 2026-08-21): 'extended_session: true'
            # is ambiguous between PRE-MARKET and AFTER HOURS, and a sweep
            # record that cannot say which side of the close it ran on cannot
            # be used to explain a gap afterwards.
            session_phase=market_hours.session_phase(),
            stop_on_live_extended_quote=bool(live_ext),
        )
    except Exception as e:
        state.journal("stop_depth_audit_failed", sleeve=sleeve, symbol=sym,
                      error=str(e)[:100])


def _pricing_matters_now() -> bool:
    """Pricing only PROTECTS the book while the book can trade — regular or
    extended hours. After the close (and before 8:30 pre-market) an unpriced
    position is harmless: the official close is the mark and no stop could
    route anyway. Counting failures then cried wolf nightly (2026-06-12:
    'CANNOT PRICE EEM' fired at 5:16 PM on a closed market)."""
    try:
        from firm import market_hours
        return market_hours.market_is_open() or market_hours.extended_session_open()
    except Exception:
        return True   # unknown -> fail safe (still alert)


def _mark_fail(pos: dict, sleeve: str, sym: str) -> None:
    """A held position the firm cannot price WHILE THE MARKET IS OPEN is a
    position it cannot PROTECT. Transient quote failures happen constantly
    (throttles, blips) — the fallback chain and the next sweep absorb those
    silently. But FIVE consecutive in-hours sweeps with no price from ANY
    source is not a blip (halt? delisting? symbol change?) — the operator
    gets told, once, loudly (operator ask 2026-06-12). Out of hours, an
    unpriced name is expected and never counts."""
    if not _pricing_matters_now():
        return
    pos["mark_fail_streak"] = pos.get("mark_fail_streak", 0) + 1
    # A STREAK ALONE IS NOT A DURATION (2026-07-25), but ONLY on the fast loop.
    # The equity sweep runs every 60s, so 5 consecutive failures already mean
    # ~5 minutes — a sane "this is not a blip" bar, and the one the regression
    # suite asserts. The OPTIONS sweep runs every ~1s, so the identical streak
    # means FIVE SECONDS. That became live-reachable the moment option marks
    # started returning no-mark on a missing bid (2026-07-25 audit fix): a
    # momentary no-bid flicker on a thin contract would page the operator almost
    # instantly. So the extra duration bar is scoped to OPTION positions — the
    # ones on the 1s loop — and the equity path keeps pure streak semantics.
    _elapsed_ok = True
    if pos.get("instrument") == "option":
        _now_s = datetime.now(timezone.utc).timestamp()
        pos.setdefault("mark_fail_first_ts", _now_s)
        _elapsed_ok = (_now_s - float(pos.get("mark_fail_first_ts") or _now_s)
                       >= MARK_FAIL_ALERT_MIN_S)
    if (pos["mark_fail_streak"] >= MARK_FAIL_ALERT_STREAK
            and _elapsed_ok
            and not pos.get("mark_fail_alerted")):
        pos["mark_fail_alerted"] = True
        state.journal("position_unpriceable", sleeve=sleeve, symbol=sym,
                      consecutive_sweeps=pos["mark_fail_streak"])
        from firm import alerts
        # Self-diagnose the SHAPE of the outage (2026-07-17: a process-wide
        # yfinance failure fired this alert on IWM with 'check for a halt/
        # delisting' — the wrong diagnosis for a vendor-wide outage; the
        # operator was sent chasing a symbol problem that wasn't one).
        _others = [s for s in broker.recent_unpriced() if s != sym]
        if _others:
            _diag = (f"{len(_others)} other position(s) are unpriced right "
                     f"now too ({', '.join(_others[:6])}"
                     f"{', …' if len(_others) > 6 else ''}) — this is a "
                     "VENDOR-WIDE data outage (rate limiting / feed down), "
                     "not a problem with this name. The session self-reset "
                     "and Public last-resort quotes retry every sweep.")
        else:
            _diag = ("Only this name is failing while the rest of the book "
                     "prices — check for a halt/delisting/symbol change.")
        alerts.alert(f"CANNOT PRICE {sym}",
                     f"{sleeve} position unpriced {pos['mark_fail_streak']} "
                     "sweeps in a row — every data source is failing for "
                     "this name. Stops cannot protect it until pricing "
                     f"returns. {_diag}")


def _mark_ok(pos: dict, sym: str = "") -> None:
    if pos.get("mark_fail_streak"):
        pos["mark_fail_streak"] = 0
        pos.pop("mark_fail_alerted", None)
        pos.pop("mark_fail_first_ts", None)   # restart the duration clock too
        # the name prices cleanly again -> clear its now-stale CANNOT PRICE / STALE
        # MARK alert so the feed SELF-HEALS instead of showing a resolved outage as a
        # permanent critical (2026-06-22 operator: 30 fixed-outage alerts still red).
        if sym:
            from firm import alerts
            alerts.resolve(sym)


def _stale_skip(pos: dict, sleeve: str, sym: str) -> None:
    """The only available mark is a daily-close FALLBACK (live quote down) — a
    price we cannot trust enough to FIRE A STOP on. Protective exits are ALWAYS
    skipped on a stale mark (the caller's `continue`); this only decides whether
    to ALERT. A stale mark is EXPECTED in the extended/pre-market session (the
    fund fetches live Public quotes only in REGULAR hours), so alerting then
    floods the operator every premarket morning (operator caught 14 STALE-MARK
    criticals at 8:34am 2026-06-15). Only a stale mark during REGULAR hours is a
    real outage worth an alert (Public genuinely down while it should be up)."""
    from firm import market_hours
    if not market_hours.market_is_open():
        return                              # extended/pre-market staleness is normal — no alert
    pos["mark_fail_streak"] = pos.get("mark_fail_streak", 0) + 1
    if (pos["mark_fail_streak"] >= MARK_FAIL_ALERT_STREAK
            and not pos.get("mark_fail_alerted")):
        pos["mark_fail_alerted"] = True
        state.journal("position_mark_stale", sleeve=sleeve, symbol=sym,
                      consecutive_sweeps=pos["mark_fail_streak"])
        from firm import alerts
        alerts.alert(f"STALE MARK {sym}",
                     f"{sleeve}: only a stale daily-close mark for "
                     f"{pos['mark_fail_streak']} sweeps during regular hours (live "
                     "quote down) — stops are NOT firing on it to avoid a false "
                     "trigger on a price that isn't live. Check the quote feed.")


def _maybe_split_adjust(pos: dict, px: float, sleeve: str, sym: str) -> bool:
    """A PERSISTENT halving isn't always a crash — a 2:1 split halves the
    print while the investor's wealth is unchanged. Before exit logic may
    act on a confirmed >50% dislocation, verify against the vendor's split
    calendar and restate the position in place (2026-06-10 absence audit:
    without this, the second sweep CONFIRMED the split as a real -50% and
    the hard stop fired on a phantom loss)."""
    last = pos.get("last_price") or pos.get("entry_price") or 0
    if not last or px <= 0:
        return False
    try:
        import pandas as pd
        import yfinance as yf
        splits = yf.Ticker(sym).splits
        if splits is None or not len(splits):
            return False
        cutoff = pd.Timestamp.now(tz=getattr(splits.index, "tz", None) or "UTC")
        recent = splits[splits.index >= cutoff - pd.Timedelta(days=7)]
        if not len(recent):
            return False
        factor = float(recent.iloc[-1])     # 2.0 = 2:1 split; 0.1 = 1:10 reverse
        if factor <= 0:
            return False
        expected = last / factor
        if abs(px - expected) / expected > 0.15:
            return False                    # price defies the split math — real move
        pos["quantity"] = round(pos["quantity"] * factor, 6)
        pos["entry_price"] = round(pos["entry_price"] / factor, 6)
        pos["peak_price"] = round((pos.get("peak_price") or last) / factor, 6)
        pos["last_price"] = px
        pos["cost"] = round(pos["entry_price"] * pos["quantity"], 2)
        state.journal("split_adjusted", sleeve=sleeve, symbol=sym, factor=factor,
                      new_qty=pos["quantity"], new_entry=pos["entry_price"])
        return True
    except Exception:
        return False


def _confirm_mark(pos: dict, px: float, sleeve: str, sym: str) -> bool:
    """Reject one-tick price dislocations (splits, bad prints). A mark that
    moved >50% from the last known mark must persist a second sweep before
    any exit logic may act on it; a persistent move is then checked against
    the split calendar before it may trigger stops."""
    last = pos.get("last_price") or pos.get("entry_price") or 0
    if last and abs(px / last - 1) * 100 > MARK_ANOMALY_PCT:
        if pos.get("anomaly_count", 0) < 1:
            pos["anomaly_count"] = pos.get("anomaly_count", 0) + 1
            state.journal("mark_anomaly", sleeve=sleeve, symbol=sym,
                          last=last, new=px, action="awaiting confirmation")
            return False
        if pos.get("instrument") == "equity" and _maybe_split_adjust(pos, px, sleeve, sym):
            pos["anomaly_count"] = 0
            return False    # restated — exits act on clean marks next sweep
    pos["anomaly_count"] = 0
    return True


def _mark_corroborated(pos: dict, px: float, sleeve: str, sym: str,
                       reason: str) -> bool:
    """Second opinion before a protective exit may ROUTE on this mark.

    THE DOWNWARD TWIN OF THE PEAK GUARD (operator 2026-07-28, PRSU). The book
    already refuses to let a single high tick advance the trailing peak
    (PEAK_MAX_JUMP_PCT) — the direction that costs nothing. Nothing guarded the
    direction that costs money. `_confirm_mark` only wakes at a 50% dislocation
    because it was built to catch SPLITS; every phantom that actually hurt this
    fund was far smaller and sailed straight through it: INNV's bad print was
    6.6% below the day's true low, which was enough to cross a 12% stop while
    the real price was only 7% down. PRSU twice, STRZ once, INNV once —
    -$1,516 realized on prices that never traded, 47% of the fund's lifetime
    loss, all of it from marks no second source would have confirmed.

    Cheap by construction: this runs only when a stop has ALREADY triggered,
    which is rare. Fails toward NOT trading on an uncorroborated outlier, with
    STOP_UNCONFIRMED_MAX_SWEEPS as the release valve so a genuine breakdown
    during a data outage still exits — a phantom tick does not persist, a real
    crash does."""
    from firm import alerts, data_engine
    tol = float(getattr(config, "STOP_MARK_CONFIRM_TOLERANCE_PCT", 3.0))
    ref = 0.0
    try:                                  # independent of the Public quote path
        ref = float(data_engine.last_price(sym) or 0.0)
    except Exception:
        ref = 0.0
    if ref > 0 and px > 0 and abs(px / ref - 1) * 100 <= tol:
        pos.pop("stop_unconfirmed_sweeps", None)
        return True
    n = int(pos.get("stop_unconfirmed_sweeps") or 0) + 1
    pos["stop_unconfirmed_sweeps"] = n
    # UNAVAILABLE is not DISAGREEMENT (2026-09-01). During a process-wide
    # yfinance outage (08-31: 22 positions unpriceable, yf_batch_failed
    # consecutive=783) `ref` reads 0 and every triggered stop was held the
    # full cap — a real breakdown waiting 3 minutes for a witness that could
    # not show up. The Public quote here already passed the two-sided/spread
    # sanitizer, so with no witness the hold is the SHORTER cap: still never
    # the first tick (the one-tick phantom class dies on a single held sweep).
    _cap_dis = int(getattr(config, "STOP_UNCONFIRMED_MAX_SWEEPS", 3))
    _cap_noref = int(getattr(config, "STOP_UNCONFIRMED_MAX_SWEEPS_NO_REF", 2))
    # The missing-witness hold never EXCEEDS the disagreement hold: an absent
    # second opinion is weaker evidence against the mark than a contradicting
    # one, so it can never justify a longer wait.
    cap = _cap_dis if ref > 0 else min(_cap_noref, _cap_dis)
    if n >= cap:
        state.journal("stop_routed_unconfirmed", sleeve=sleeve, symbol=sym,
                      mark=round(px, 4), independent=round(ref, 4) or None,
                      sweeps=n, reason=reason,
                      note=("the condition persisted across sweeps, so this is "
                            "a real move rather than a one-tick artifact"))
        pos.pop("stop_unconfirmed_sweeps", None)
        return True
    state.journal("stop_aborted_mark_unconfirmed", sleeve=sleeve, symbol=sym,
                  mark=round(px, 4), independent=round(ref, 4) or None,
                  disagreement_pct=(round((px / ref - 1) * 100, 2)
                                    if ref > 0 else None),
                  tolerance_pct=tol, sweep=n, of=cap, reason=reason)
    if n == 1:
        alerts.alert(
            f"STOP HELD ON UNCONFIRMED MARK — {sym}",
            f"{sleeve}: '{reason}' triggered at {px:.2f} but the independent "
            f"price reads {ref:.2f}" if ref > 0 else
            f"{sleeve}: '{reason}' triggered at {px:.2f} but no independent "
            "price could be read",
            severity="warning")
    return False


def _sell_or_escalate(mode: str, s: dict, sym: str, pos: dict, px: float,
                      meta: dict, qty: float | None = None) -> bool:
    """Execute an exit; escalate to a sleeve halt after repeated live failures
    (expired API key etc. must not silently retry while a price free-falls)."""
    res = broker.execute(mode, s, symbol=sym, side="sell",
                         quantity=qty if qty is not None else pos["quantity"],
                         price=px, instrument=pos["instrument"], meta=meta)
    if res.get("success"):
        if sym in s["positions"]:
            s["positions"][sym]["sell_fail_count"] = 0
        return True
    err = res.get("error") or ""
    if "already pending" in err:
        return False
    if "market closed" in err:
        return False   # the hours gate working is not a failure — no escalation
    pos["sell_fail_count"] = pos.get("sell_fail_count", 0) + 1
    state.journal("exit_failed", sleeve=s["sleeve"], symbol=sym,
                  error=res.get("error"), fails=pos["sell_fail_count"])
    if pos["sell_fail_count"] >= SELL_FAIL_HALT and not s.get("halted"):
        s["halted"] = True
        s["halt_reason"] = (f"EXIT FAILURE: {sym} sell failed "
                            f"{pos['sell_fail_count']}x ({res.get('error')}) — "
                            "check broker connection/API key")
        state.journal("sleeve_halted_exit_failure", sleeve=s["sleeve"], symbol=sym)
        from firm import alerts, riskbrain
        riskbrain.record_halt(s["sleeve"], "exit_failure", s["halt_reason"])
        alerts.alert(f"EXIT FAILURE: {s['sleeve']} halted",
                     f"{sym} exit failed {pos['sell_fail_count']}x: "
                     f"{res.get('error')}. Auto-retries every sweep; halt "
                     "auto-clears when the broker link recovers.")
    return False


def _core_syms(sleeve: str) -> set:
    """Symbols in `sleeve` that are STRATEGIC CORE / mechanical slots, exempt from
    per-name stops (a stop-sell here is rebought by the next rebalance)."""
    from firm import allocation as _alloc_mon
    if sleeve == "core_blend":
        return set(_alloc_mon.core_weights())
    return set(config.SLEEVES.get(sleeve, {}).get("core_weights") or {})


def _propagate_stop(origin: str, sym: str, px: float, reason: str, mode: str) -> int:
    """A stop is a risk verdict on the NAME, not on one sleeve's copy of it.

    Operator 2026-07-25: alpha stopped OSCR (-12.1%) and CON (-16.7% trail) while
    core_blend kept holding both (77sh + 71sh = $4,415, 17.9% of core equity)
    because core's satellite stops are wider (15/18 vs alpha's 12/15). TXN had
    been duplicated the same way for 26 days. From the operator's seat the firm
    announced a stop-out and the position was still there, which read as the stop
    never being recorded.

    Holding a name elsewhere also directly CONTRADICTS the firm's own bench: the
    origin sleeve files the name as stop_out and blocks re-entry until price
    reclaims the stop +3%, while a peer sleeve quietly keeps the exposure on.
    Single-name risk is firm-wide (MAX_FIRM_NAME_PCT), so the exit must be too.

    Scope is deliberately narrow: only the two DISCRETIONARY equity sleeves, and
    never a peer's strategic-core/mechanical slot or its parking (those are exempt
    from per-name stops by design — the sell-low/rebuy-high churn rule). The
    mechanical etf_rotation sleeve is never overridden.
    """
    if not getattr(config, "PROPAGATE_STOPS_ACROSS_SLEEVES", True):
        return 0
    done = 0
    for peer in ("alpha_stocks", "core_blend"):
        if peer == origin:
            continue
        ps = state.get_sleeve(peer)
        pos = (ps.get("positions") or {}).get(sym)
        if not pos or pos.get("instrument") != "equity":
            continue
        if pos.get("exit_pending") or pos.get("cash_equitization"):
            continue
        if sym in _core_syms(peer):
            continue                      # peer holds it as a core slot — exempt
        entry = pos.get("entry_price") or px
        peer_reason = f"stop propagated from {origin} ({reason})"
        if _sell_or_escalate(mode, ps, sym, pos, px,
                             {"reason": peer_reason,
                              "urgent": reason.startswith("hard stop")}):
            done += 1
            state.journal("stop_propagated", origin=origin, peer=peer, symbol=sym,
                          reason=reason, entry=round(entry, 4), exit=round(px, 4))
            # Deliberately NO add_stop_out here: the bench is keyed by TICKER and
            # the ORIGIN sleeve already benched this name a moment ago with the
            # real stop level and thesis. A second call would overwrite that row
            # wholesale, so stop_out.sleeve/entry/exit would record the peer's
            # copy instead of the sleeve that actually triggered — corrupting the
            # re-entry attribution the learning loop grades stop-outs on.
        # Re-mark the peer so its equity/kill-switch reflect the exit immediately
        _eq = broker.sleeve_equity(ps)
        risk.mark_equity(peer, ps, _eq, broker.sleeve_risk_equity(ps, _eq))
        state.save_sleeve(peer, ps)
    return done


def _check_equity_sleeve_locked(sleeve: str, mode: str) -> int:
    cfg = config.SLEEVES[sleeve]
    s = state.get_sleeve(sleeve)
    if mode == "live" and s.get("pending_orders"):
        broker.settle_pending(s)
    if not s["positions"]:
        risk.mark_equity(sleeve, s, broker.sleeve_equity(s))
        state.save_sleeve(sleeve, s)
        return 0
    marks = broker.fetch_marks(s)
    closes = 0
    # Strategic core ETFs are exempt from per-name stops: a stop-sell would be
    # rebought by the next cycle's core rebalance (sell-low/rebuy-high churn).
    # The core's protection is the kill-switch diagnosis, not position stops.
    core = _core_syms(sleeve)
    for sym in list(s["positions"].keys()):
        pos = s["positions"][sym]
        # Equitization parking is benchmark exposure, not a stock pick —
        # stop-selling it would be rebought next cycle (same churn logic
        # that exempts the strategic core)
        if (pos["instrument"] != "equity" or pos.get("exit_pending")
                or sym in core or pos.get("cash_equitization")):
            continue
        px = marks.get(sym, 0)
        _prev_mark = float(pos.get("last_price") or 0.0)   # last sweep's trusted mark
        _live_ext = False                                  # stop judged on an extended quote?
        if px <= 0:
            _mark_fail(pos, sleeve, sym)
            continue
        if pos.get("_mark_stale"):
            # EXTENDED-SESSION RESCUE (2026-07-26). A stale flag means the book
            # mark is a daily close, which is NOT trustworthy enough to fire a
            # stop — but if the broker attached a genuine live two-sided quote
            # for this name (`_live_px`, set only in the extended session, see
            # broker.fetch_marks), that quote IS trustworthy and is exactly what
            # the stop should be judged on. Without this the extended session had
            # no stop protection at all and PRSU ran to -29% against a -12% stop.
            # The book/display mark deliberately stays the close; only the STOP
            # DECISION uses the live quote.
            _live = pos.get("_live_px")
            if isinstance(_live, (int, float)) and _live > 0:
                px = float(_live)
                _live_ext = True
                state.journal("stop_check_on_live_extended_quote", sleeve=sleeve,
                              symbol=sym, live_px=round(px, 4),
                              book_mark=round(marks.get(sym, 0), 4))
            else:
                # In-hours but only a stale daily-close mark, or no live quote —
                # don't trust it enough to fire a stop. Skip protective exits;
                # equity display still uses it.
                _stale_skip(pos, sleeve, sym)
                continue
        _mark_ok(pos, sym)
        if not _confirm_mark(pos, px, sleeve, sym):
            continue
        entry = pos["entry_price"]
        # Guard the trailing-stop peak against a single anomalous high tick
        # (2026-07-02: a bad print under the 50% mark-confirm threshold stuck as
        # the peak — a sticky max — and fired a FALSE -18% trailing stop on a
        # name only ~-7% off its TRUE high). A one-sweep jump > PEAK_MAX_JUMP_PCT
        # above the prior peak does NOT advance the live peak; the daily bar
        # reconciliation (reconcile_peaks) sets the true high from real OHLC.
        _prior = pos.get("peak_price")
        if _prior and px > _prior * (1 + PEAK_MAX_JUMP_PCT / 100):
            state.journal("peak_spike_ignored", sleeve=sleeve, symbol=sym,
                          prior_peak=round(_prior, 2), spike_px=round(px, 2))
            peak = _prior
        else:
            peak = max(_prior or px, px)
        pos["peak_price"] = peak
        chg_from_entry = (px / entry - 1) * 100
        chg_from_peak = (px / peak - 1) * 100
        reason = ""
        # Volatility-aware per-position stops (set at entry), sleeve defaults
        # for positions that predate the feature
        hard = pos.get("stop_pct") or cfg.get("hard_stop_pct")
        trail = pos.get("trail_stop_pct") or cfg.get("trail_pct")
        # PROFIT RATCHET (2026-07-26, operator: "set this fund up to be
        # mathematically profitable"). A trail expressed as a fixed % OF PRICE is
        # the wrong instrument once a position has real profit: on a +49% winner
        # an 18% trail surrenders 27 percentage points. Measured over every
        # winner this fund has closed, it captured just 35% of the peak gain and
        # handed back 65% —
        #     MU   peak +36.2% -> exited +15.5%      AMAT peak +49.4% -> +22.5%
        #     AMAT peak +32.0% -> exited +11.3%      AMD  peak +22.7% ->  +3.3%
        # — which is why avg win (+13.1%) and avg loss (-12.4%) are nearly
        # symmetric, and a 22% hit rate on a ~1:1 payoff is arithmetically a
        # guaranteed bleed (breakeven needs 3.5:1).
        # So give-back is capped as a fraction of the peak GAIN, not of price,
        # and only once the position has meaningful profit — below that floor the
        # existing wide vol-aware trail still governs, so small positions keep
        # their noise tolerance and are not shaken out early. Whichever triggers
        # first wins. Modelled on the four closed winners this lifts capture from
        # 35% to ~65% (avg win +13.1% -> +22.8%).
        # Tuned-value read (2026-07-26): these two knobs joined RATCHET_BOUNDS,
        # so the live values come through risk.tuned_cap — the review can move
        # them inside the rails and the actuator is actually connected (the
        # momentum-lane lesson: a clamp that accepts a key nobody reads mints
        # false evidence of self-improvement).
        _rat_min = risk.tuned_cap("PROFIT_RATCHET_MIN_GAIN_PCT")
        _rat_give = risk.tuned_cap("PROFIT_RATCHET_GIVEBACK_PCT")
        _peak_gain = (peak / entry - 1) * 100 if entry else 0.0
        _ratchet_floor = _peak_gain * (1 - _rat_give / 100)
        # ABSOLUTE FLOOR (fix brief 2026-07-27 #6). Evaluated AFTER the
        # per-position stop, never before: a tighter vol-aware stop must keep
        # firing at its own level with its own label, so this branch can only be
        # reached when `hard` is wider than the floor (or missing entirely). It
        # is therefore a CEILING on the worst case — it can make an exit earlier,
        # never later, and it loosens nothing. Independent of the sigma math by
        # design: the whole point is that it binds when the sigma estimate is
        # wrong (a stale/understated small-cap vol) or when price gapped past a
        # correctly-computed level.
        _floor = _abs_stop_floor()
        if hard and chg_from_entry <= -hard:
            reason = f"hard stop {chg_from_entry:.1f}%"
        elif _floor and chg_from_entry <= -_floor:
            reason = (f"hard stop (absolute floor) {chg_from_entry:.1f}% "
                      f"(floor {_floor:.1f}%)")
        elif (_peak_gain >= _rat_min and chg_from_entry > 0
                and chg_from_entry <= _ratchet_floor):
            # Still GREEN — this books a winner, it is not a loss stop. Guarded on
            # chg_from_entry > 0 so a name that round-trips through the entry is
            # handled by the hard stop below, never exited "in profit" at a loss.
            reason = (f"profit ratchet: +{chg_from_entry:.1f}% held of a "
                      f"+{_peak_gain:.1f}% peak (give-back cap {_rat_give:.0f}%)")
        # Trail arms once the position has EVER been green (peak > entry) —
        # gating on "currently green" (2026-06-10 audit) let a big winner
        # round-trip all the way to the hard stop with no exit in between
        elif trail and peak > entry and chg_from_peak <= -trail:
            reason = f"trailing stop {chg_from_peak:.1f}% from peak"
        # A triggered stop gets a second opinion before it may route (see
        # _mark_corroborated). Deliberately placed AFTER `reason` is computed so
        # the cost is one extra price read on the rare sweeps where a stop
        # actually fires, not on every sweep of every position.
        if not reason:
            # No stop is asking this sweep: a held-then-recovered phantom must
            # not leave a stale unconfirmed count that shortens the NEXT hold
            # (2026-09-01 audit: the counter was reset only on agreement or at
            # the cap).
            pos.pop("stop_unconfirmed_sweeps", None)
        if reason and not _mark_corroborated(pos, px, sleeve, sym, reason):
            reason = ""
        if reason and _sell_or_escalate(mode, s, sym, pos, px,
                                        {"reason": reason,
                                         "urgent": reason.startswith("hard stop")}):
            closes += 1
            pos.pop("stop_unrouted_logged", None)
            # Measure this exit against the level it was DESIGNED to fire at
            # (fix brief 2026-07-27 #6). Written for EVERY routed stop exit —
            # hard, absolute floor, ratchet and trail — because the slippage
            # question ("did the stop execute where it was supposed to?") is
            # identical for all four legs and a sample restricted to the ones
            # that already look bad cannot measure the distribution.
            _stop_depth_audit(sleeve, sym, pos, entry=entry, exit_px=px,
                              peak=peak, reason=reason, intended_hard=hard,
                              intended_trail=trail, floor=_floor,
                              prev_mark=_prev_mark, live_ext=_live_ext)
            # A stop is a RISK exit, not a thesis verdict: bench the name with
            # its thesis so the committee re-judges it once price recovers.
            # (Rotation sleeve excluded — its re-entry is mechanical anyway.)
            if sleeve in ("alpha_stocks", "core_blend"):
                from firm import watchlist
                watchlist.add_stop_out(sym, sleeve, entry, px, reason,
                                       thesis=pos.get("thesis", ""),
                                       conviction=pos.get("conviction"))
                # The same name held in the OTHER discretionary sleeve exits too
                closes += _propagate_stop(sleeve, sym, px, reason, mode)
        elif reason:
            # TRIGGERED BUT NOT ROUTED (operator 2026-07-25). _sell_or_escalate
            # returns False WITHOUT journaling on "already pending" and "market
            # closed", so a stop that genuinely fired left no evidence anywhere:
            # no journal line, no bench entry, no ledger row. The operator's only
            # signal was the position still sitting there. Record the trigger so
            # a coverage gap is visible; deduped by reason so a market-closed
            # weekend does not write the same line every 60s.
            # Dedupe on the reason FAMILY, not the formatted string: the reason
            # embeds a live percentage ("hard stop -12.3%"), so a drifting mark
            # changes the text every sweep and would re-journal the same stuck
            # stop all weekend — the flood this dedupe exists to prevent.
            _fam = reason.split("-")[0].strip() or reason
            if pos.get("stop_unrouted_logged") != _fam:
                pos["stop_unrouted_logged"] = _fam
                state.journal("stop_triggered_unrouted", sleeve=sleeve, symbol=sym,
                              reason=reason, price=round(px, 4))
        # Cadence witness for the stop-depth audit (fix brief 2026-07-27 #6):
        # stamped only where a position was actually EVALUATED against its stop,
        # so "seconds since the previous sweep" measures monitor coverage of
        # THIS name. A gap far larger than EQUITY_MONITOR_INTERVAL_S in the
        # audit line is a blackout, not a wide stop — the two look identical in
        # the exit price alone, which is why the review could not separate them.
        pos["_last_sweep_ts"] = datetime.now(timezone.utc).timestamp()
    equity = broker.sleeve_equity(s, marks, from_fetch_marks=True)
    risk.mark_equity(sleeve, s, equity, broker.sleeve_risk_equity(s, equity))
    state.save_sleeve(sleeve, s)
    return closes


def _check_calls_sleeve(mode: str) -> int:
    with state.SLEEVE_LOCK:
        return _check_calls_sleeve_locked(mode)


def _min_hold_session_passed(pos: dict) -> bool:
    """Has a NEW trading session begun since this option was opened?
    (fix brief 2026-07-27 #7 — minimum holding period.)

    The routine underlying-referenced stop is suppressed inside the entry
    session. Both HPE calls died 47 and 19 minutes after entry: a thesis the
    committee formed that morning cannot be falsified by the underlying's next
    half hour, and an invalidation stop that fires that fast is measuring
    intraday noise, not the thesis. The CATASTROPHE premium backstop is never
    suppressed — a contract that has lost 40% of its premium is a broken trade
    whatever the clock says — and neither are the TP ladder, the trail or the
    expiry guard.

    Fails toward PROTECTION: an unparseable/absent entry timestamp returns True
    (the stop stays live), because a risk system's unknown state must not
    silently disable a stop.
    """
    try:
        from firm import market_hours
        opened = pos.get("opened_at") or ""
        if not opened:
            return True
        dt = datetime.fromisoformat(opened.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now_et = datetime.now(timezone.utc).astimezone(market_hours.ET)
        # A LATER trading day, not merely a later calendar day: entering on a
        # Friday must not have the stop arm on Saturday's date rollover into a
        # session that does not exist.
        return (now_et.date() > dt.astimezone(market_hours.ET).date()
                and market_hours.is_trading_day())
    except Exception:
        return True


def _underlying_breached(pos: dict, und_px: float) -> bool:
    """Has the underlying traded THROUGH the level that invalidates the thesis?
    Direction-aware: a long CALL is invalidated below its level, a long PUT
    above (the desk holds both since 2026-07-07)."""
    lvl = pos.get("underlying_stop_px")
    if not isinstance(lvl, (int, float)) or lvl <= 0 or und_px <= 0:
        return False
    if pos.get("underlying_stop_dir") == "above":
        return und_px >= float(lvl)
    return und_px <= float(lvl)


def _check_calls_sleeve_locked(mode: str) -> int:
    cfg = config.SLEEVES["calls_only"]
    s = state.get_sleeve("calls_only")
    if mode == "live" and s.get("pending_orders"):
        broker.settle_pending(s)
    if not s["positions"]:
        risk.mark_equity("calls_only", s, broker.sleeve_equity(s))
        state.save_sleeve("calls_only", s)
        return 0
    marks = broker.fetch_marks(s)
    closes = 0
    today = datetime.now(timezone.utc).date()
    # UNDERLYING marks for the invalidation stop (fix brief 2026-07-27 #7), one
    # TTL-throttled batch for the whole book rather than a quote per position.
    # Fetched ONLY for contracts that are both armed and past their minimum
    # holding session, so a book with no armed positions adds zero API traffic
    # to the 1s loop. Failure is silent and safe: an empty dict means the
    # underlying check simply does not run this sweep and the premium
    # catastrophe backstop still covers every position.
    _und_syms = [p.get("underlying") for p in s["positions"].values()
                 if p.get("instrument") == "option" and not p.get("cash_equitization")
                 and p.get("underlying") and p.get("underlying_stop_px")
                 and _min_hold_session_passed(p)]
    _und_marks = broker.underlying_marks(_und_syms) if _und_syms else {}
    for osi in list(s["positions"].keys()):
        pos = s["positions"][osi]
        if pos.get("exit_pending"):
            continue
        # The BIL cash-equitization parking is an EQUITY position in the calls
        # book (goal audit fix 3) — option ladders/expiry/stops don't apply to
        # it, exactly like alpha's SPY parking is exempt from per-name stops.
        # It exits only by funding a premium buy (_free_cash_from_equitization).
        if pos.get("instrument") != "option" or pos.get("cash_equitization"):
            continue
        px = marks.get(osi, 0)

        # Expiry guard: never hold a long option into its final day.
        # An unparseable expiration is treated as EXPIRED (force write-off),
        # never as "immortal" — a phantom contract must not haunt the books.
        exp = pos.get("expiration")
        if exp:
            try:
                dte = (date.fromisoformat(exp) - today).days
            except (ValueError, TypeError):
                dte = 0
            if dte <= 1:
                exit_px = px if px > 0 else float(pos.get("last_price") or 0)
                if _sell_or_escalate(mode, s, osi, pos, max(exit_px, 0.0),
                                     {"open_close": "close", "force": True,
                                      "reason": f"expiry exit ({dte} DTE)"}):
                    closes += 1
                continue

        if px <= 0:
            _mark_fail(pos, "calls_only", osi)
            continue
        _mark_ok(pos, osi)
        if not _confirm_mark(pos, px, "calls_only", osi):
            continue
        entry = pos["entry_price"]
        # NO peak-spike guard here, deliberately (tried and reverted 2026-07-25).
        # The equity path refuses a one-sweep peak advance beyond
        # PEAK_MAX_JUMP_PCT (25%) as a bad print, but that constant is calibrated
        # for SHARES. A 25%+ one-sweep move in an option PREMIUM is ordinary
        # leverage, not a bad tick, so the same guard froze the peak at entry
        # while a contract ran +30% and the trail could then never fire (caught
        # by test_firm "runner trail-stops out"). Options also have no
        # reconcile_peaks repair path (vendors serve no reliable per-contract
        # OHLC), so a wrongly-blocked option peak would be permanent — strictly
        # worse than the poisoning it aims to prevent. Bad prints on this book
        # are handled upstream by _confirm_mark's 50% two-sweep gate instead.
        peak = max(pos.get("peak_price", px), px)
        pos["peak_price"] = peak
        gain = (px / entry - 1) * 100
        off_peak = (px / peak - 1) * 100
        # Per-position ladder (Opus-designed at entry); config values as fallback
        ladder = pos.get("ladder", {})
        sl = ladder.get("sl_pct", ladder.get("sl", cfg["hard_sl_pct"]))
        # SL must clear the bid/ask spread even on the config-fallback path
        # (positions predating Opus ladders): a -2% stop inside a 10% spread
        # fires on pure quote noise (2026-06-10 audit)
        spr = pos.get("spread_pct")
        if isinstance(spr, (int, float)) and spr > 0:
            sl = max(sl, min(15.0, spr / 2 + 1.5))
        # PREMIUM SL BECOMES THE CATASTROPHE BACKSTOP (fix brief 2026-07-27 #7).
        # A percentage-of-premium stop is the wrong instrument for a thesis
        # exit: premium moves on spread width, IV and delta, so -15% is
        # reachable on quote noise minutes after entry with the thesis intact
        # (both HPE calls, dead at 47 and 19 minutes). The ROUTINE exit is now
        # the underlying trading through underlying_stop_px; the premium level
        # is widened to the point where the trade is broken regardless of the
        # underlying (IV crush, delta collapse, or a gap through the level while
        # no quote was available).
        #
        # STRICTLY CONDITIONAL on an armed underlying stop. A position with no
        # stamp — a row predating this change, or an entry whose vol/quote
        # lookup failed — keeps its ORIGINAL tighter premium SL, because
        # widening a stop with nothing to replace it removes protection outright.
        _und_armed = bool(pos.get("underlying_stop_px"))
        if _und_armed:
            sl = max(sl, float(getattr(config, "OPT_CATASTROPHE_SL_PCT", 40.0)))
        tp1 = ladder.get("tp1_pct", ladder.get("tp1", cfg["tp1_pct"]))
        tp1_sell = ladder.get("tp1_sell_pct", cfg["tp1_sell_pct"])
        tp2 = ladder.get("tp2_pct", ladder.get("tp2", cfg["tp2_pct"]))
        tp3 = ladder.get("tp3_pct", ladder.get("tp3", cfg["tp3_pct"]))
        trail1 = ladder.get("trail1", cfg["trail_after_tp1"])
        trail2 = ladder.get("trail2", cfg["trail_after_tp2"])
        trail3 = ladder.get("trail3", cfg["trail_after_tp3"])
        # PRE-TP1 TRAIL (operator 2026-07-25: "make sure max drawdown is really
        # based on the call, not universal"). trail_pct was None until TP1 fired,
        # so a call could run to one tick under TP1, round-trip the whole way
        # down and exit only at the hard SL from ENTRY — a peak-to-trough
        # drawdown of sl+tp1 (31.5% on the HPE ladder) with no exit in between.
        # That is the SAME defect the equity path fixed in the 2026-06-10 audit
        # (see _check_equity_sleeve_locked) and the options book never got it.
        # Width = max(trail1, sl), never TIGHTER than the hard stop: the SL is
        # the risk budget from entry, so before any profit is banked the trail
        # must not be stricter than it. This CANNOT exit a position earlier than
        # today — when peak == entry, off_peak == gain and the trail trigger is
        # identical to the SL trigger; it only binds once the trade has been
        # green (peak > entry), which is exactly the exposure being fixed.
        _eff_trail = pos.get("trail_pct") or max(trail1, sl)
        sell_qty, reason, close_meta = 0, "", {"open_close": "close"}
        # TIER LATCHING (2026-07-25 audit, live-only defect). tp1_sold/tp2_sold
        # used to be set at the TOP of their branch, ~20 lines before the sell was
        # attempted, and the sell's return value was never checked against them.
        # A live rejection — "market closed" or "already pending", both of which
        # _sell_or_escalate swallows and returns False for — therefore left the
        # tier marked DONE forever: `elif not pos.get("tp1_sold")` never matched
        # again, the planned 50% scale-out never executed for the life of the
        # trade, and the position sat on the TIGHTER post-TP1 trail having banked
        # nothing. Paper never exposed it because paper sells always route.
        # Tiers that actually SELL now latch only on a routed order (via _latch);
        # tiers that sell nothing (single-contract runner, TP3) latch immediately,
        # since there is no order whose success could gate them.
        _latch: dict = {}

        _und_px = _und_marks.get(pos.get("underlying") or "", 0.0)
        if gain <= -sl:
            sell_qty = pos["quantity"]
            # The word "stop" is load-bearing, not cosmetic: learning._exit_family
            # buckets on it, and a "catastrophe SL" spelling would fall through
            # every family test into `other` — the exact mis-bucketing the
            # 2026-07-25 audit fixed, which fed the exit strategist bad evidence.
            reason = (f"catastrophe stop {gain:.1f}% premium" if _und_armed
                      else f"hard SL {gain:.1f}%")
            # Live: price THROUGH the quote — a stop in a falling option must
            # not dangle at mid for 150s while the premium evaporates
            close_meta["urgent"] = True
        elif (_und_armed and _und_px > 0 and gain < tp1
                and _underlying_breached(pos, _und_px)
                and _min_hold_session_passed(pos)):
            # ROUTINE INVALIDATION EXIT (fix brief 2026-07-27 #7): the thing the
            # thesis was actually about — the UNDERLYING — has traded through
            # the level set at entry (entry price -/+ ~1.5 daily sigma). Placed
            # directly after the catastrophe backstop and before the TP tiers so
            # the ladder, trail and ratchet are untouched. `gain < tp1` makes
            # that literal rather than merely usual: a breached underlying and a
            # contract trading at TP1 are contradictory in real quotes (the
            # option moves with its underlying, put or call), but the two prices
            # come from different feeds at different freshness — a thin, stale
            # option mark alongside a live underlying quote could show both at
            # once, and in that disagreement the profit-taking ladder wins.
            sell_qty = pos["quantity"]
            reason = (f"underlying stop {pos.get('underlying')} @ {_und_px:.2f} "
                      f"through {float(pos['underlying_stop_px']):.2f} "
                      f"(premium {gain:.1f}%)")
            close_meta["urgent"] = True
            state.journal("underlying_stop_fired", symbol=osi,
                          underlying=pos.get("underlying"),
                          underlying_px=round(_und_px, 4),
                          stop_px=round(float(pos["underlying_stop_px"]), 4),
                          entry_px=pos.get("underlying_entry_px"),
                          direction=pos.get("underlying_stop_dir"),
                          sigma_mult=pos.get("underlying_stop_sigma"),
                          vol_ann=pos.get("underlying_vol_ann"),
                          premium_pct=round(gain, 2))
        elif not pos.get("tp1_sold") and gain >= tp1:
            if pos["quantity"] > 1:
                sell_qty = max(1, int(pos["quantity"] * tp1_sell / 100))
                if pos["quantity"] - sell_qty < 1:
                    sell_qty = int(pos["quantity"]) - 1
                reason = f"TP1 +{gain:.0f}% sell {tp1_sell}%"
                _latch = {"tp1_sold": True, "trail_pct": trail1}
            else:
                # single contract IS the runner — nothing to scale out, just trail
                pos["tp1_sold"] = True
                pos["trail_pct"] = trail1
        elif pos.get("tp1_sold") and not pos.get("tp2_sold") and gain >= tp2:
            # TP2 only after TP1 — a gap past both tiers takes them in order
            keep = 1 if cfg["always_keep_runner"] else 0
            sell_qty = max(0, int(pos["quantity"]) - keep)
            if sell_qty >= 1:
                reason = f"TP2 +{gain:.0f}% sell all but runner"
                _latch = {"tp2_sold": True, "trail_pct": trail2}
            else:
                pos["tp2_sold"] = True
                pos["trail_pct"] = trail2
        elif pos.get("tp2_sold") and not pos.get("tp3_hit") and gain >= tp3:
            pos["tp3_hit"] = True
            pos["trail_pct"] = trail3
        elif _eff_trail and peak > entry and off_peak <= -_eff_trail:
            sell_qty, reason = pos["quantity"], f"trail {off_peak:.1f}% from peak"

        if sell_qty >= 1:
            if _sell_or_escalate(mode, s, osi, pos, px,
                                 {**close_meta, "reason": reason},
                                 qty=sell_qty):
                closes += 1
                # Re-read: a full close removes the position from the book, and a
                # partial leaves a NEW dict — latching onto the stale local would
                # be written nowhere.
                _live = s["positions"].get(osi)
                if _live is not None and _latch:
                    _live.update(_latch)
            elif _latch:
                # Order did not route: the tier is NOT consumed and this sweep's
                # decision is retried next tick. Journal it so a tier that keeps
                # failing to route is visible instead of silently deferring.
                state.journal("option_tier_unrouted", symbol=osi, reason=reason,
                              tier=next(iter(_latch)), price=round(px, 4))
    equity = broker.sleeve_equity(s, marks, from_fetch_marks=True)
    risk.mark_equity("calls_only", s, equity, broker.sleeve_risk_equity(s, equity))
    state.save_sleeve("calls_only", s)
    return closes


def check_calls_exits() -> int:
    """Fast-path sweep of ONLY the calls sleeve (called every ~1s by the app)."""
    mode = state.get_settings()["mode"]
    closes = _check_calls_sleeve(mode)
    if closes:
        state.journal("monitor_closes", calls_only=closes)
        # A calls-only close day previously waited for cycle end for its
        # ledger row (batch review R12) — harvest here too (rare: only on an
        # actual close, never on the 1s ticks).
        _harvest_closes()
    return closes


def reconcile_peaks() -> int:
    """Correct every equity position's trailing-stop peak against the TRUE high
    since entry from real daily bars — a bad intraday tick can poison the
    tick-accumulated peak (a sticky max) and fire a FALSE trailing stop
    (2026-07-02: core sold AMAT on a phantom -18% when the true pullback was
    ~-7%). yfinance daily highs are the authoritative high-water-mark. Runs at
    postmarket + startup; batched network OUTSIDE the sleeve lock, correction
    inside. LOWERS a poisoned peak (only past a 2% band — a bad tick needs the
    tolerance) and RAISES a peak the live sweep never saw (2026-08-28 outage,
    see below), never below entry or the last mark. Returns #corrected."""
    try:
        import pandas as pd
        from firm import data_engine as _de_pk   # owns the leak-safe yf_download
    except Exception:
        return 0
    # EVERY sleeve that can hold an equity row, not just the two discretionary
    # ones (2026-08-28). etf_rotation and the calls sleeve's equity rows carry
    # the same tick-accumulated peak and were silently exempt from every
    # reconciliation this function has ever run — the mechanical sleeve is the
    # one nobody watches, so a poisoned or stale peak there survives longest.
    # The instrument/cash_equitization filter below already excludes the option
    # rows and the BIL parking, so adding the sleeves adds no new row types.
    _SCAN = ("alpha_stocks", "core_blend", "etf_rotation", "calls_only")
    want: dict[str, str] = {}     # symbol -> earliest opened_at date across sleeves
    for sl in _SCAN:
        for sym, p in (state.get_sleeve(sl).get("positions") or {}).items():
            if p.get("instrument") == "equity" and not p.get("cash_equitization"):
                d = (p.get("opened_at") or "")[:10]
                if d and (sym not in want or d < want[sym]):
                    want[sym] = d
    if not want:
        return 0
    try:                          # ONE batched fetch, outside any lock (R3 lesson)
        data = _de_pk.yf_download(list(want), start=min(want.values()),
                                  auto_adjust=False, progress=False,
                                  group_by="ticker")
    except Exception:
        return 0

    def _high_since(sym: str, since: str):
        try:
            df = data[sym] if len(want) > 1 else data
            h = df["High"].dropna()
            h = h[h.index >= pd.Timestamp(since)] if since else h
            return float(h.max()) if len(h) else None
        except Exception:
            return None

    fixed = 0
    for sl in _SCAN:
        with state.SLEEVE_LOCK:
            s = state.get_sleeve(sl)
            changed = False
            for sym, p in s["positions"].items():
                if p.get("instrument") != "equity" or p.get("cash_equitization"):
                    continue
                th = _high_since(sym, (p.get("opened_at") or "")[:10])
                if th is None:
                    continue
                floor = max(p.get("entry_price") or 0, p.get("last_price") or 0)
                true_peak = round(max(floor, th), 6)
                old = p.get("peak_price")
                if old and true_peak < old * 0.98:    # peak was poisoned too high
                    state.journal("peak_reconciled", sleeve=sl, symbol=sym,
                                  old_peak=round(old, 2), new_peak=true_peak)
                    p["peak_price"] = true_peak
                    changed = True
                    fixed += 1
                elif old and true_peak > old:         # peak was never SEEN at all
                    # RAISE (2026-08-28). The live sweep can only advance a peak
                    # from a mark it actually observes (`peak = max(_prior, px)`),
                    # so a high made while this process was DOWN is lost forever
                    # and the trail stays anchored to a stale, too-low peak. The
                    # host was powered off 2026-08-23 05:50 -> 2026-08-27 21:42
                    # (flat battery, event 6008): seven positions came back with
                    # trailing stops several % too loose — ETON made 66.37 on
                    # 08-24 while the book still believed 64.34, i.e. a 16.6%
                    # trail sitting ~2 points below the level the mandate
                    # specifies. A daily bar is the authoritative high-water mark
                    # in BOTH directions, so a true high ABOVE the stored peak is
                    # a missed high, not noise, and needs no 2% band — the band
                    # on the lower branch exists to tolerate a bad tick, and a
                    # bad tick cannot make the peak too LOW.
                    state.journal("peak_raised", sleeve=sl, symbol=sym,
                                  old_peak=round(old, 2), new_peak=true_peak)
                    p["peak_price"] = true_peak
                    changed = True
                    fixed += 1
            if changed:
                state.save_sleeve(sl, s)
    return fixed


def has_real_options(s: dict) -> bool:
    """True iff the calls book holds an actual OPTION position — the BIL
    cash-equitization parking is an equity row and must not read as 'options
    open' (batch review 2026-07-01 R4: gating the 1s hot loop / 180s options
    watchdog / health check on bare `positions` would have hammered Public
    quotes and armed a dead-man switch over a T-bill)."""
    return any(p.get("instrument") == "option" and not p.get("cash_equitization")
               for p in (s.get("positions") or {}).values())


def check_all_exits(include_calls: bool = True) -> dict:
    mode = state.get_settings()["mode"]
    # Queued-for-the-open entries flush FIRST (operator 2026-07-16): a BUY the
    # committee decided after the bell executes at the next open through the
    # drift/risk/cash gates. Fail-soft — exits below must always run.
    try:
        from firm import sleeves as _slv
        _slv.flush_pending_open_entries(mode)
    except Exception as e:
        state.journal("open_queue_flush_error", error=str(e)[:100])
    out = {
        "alpha_stocks": _check_equity_sleeve("alpha_stocks", mode),
        "core_blend": _check_equity_sleeve("core_blend", mode),
        "etf_rotation": _check_equity_sleeve("etf_rotation", mode),
    }
    if include_calls:
        out["calls_only"] = _check_calls_sleeve(mode)
    total = sum(out.values())
    if total:
        state.journal("monitor_closes", **out)
        _harvest_closes()
    return out


def _harvest_closes() -> None:
    """Harvest closes into the durable ledger NOW, not only at cycle end
    (goal audit fix 6): sleeve closed_trades cap at 300 and were wiped once
    (2026-07-01) — a stop-out that sat only in the sleeve file until the next
    cycle was one corruption away from never being learned from.
    record_outcomes is idempotent by trade_id. SPY is prefetched OUTSIDE the
    lock (batch review R3/R4: the lazy fetch inside the lock starved the 1s
    options loop into the watchdog); the cycle-end caller takes the same lock
    so the ledger read-modify-write is genuinely serialized."""
    try:
        from firm import learning
        spy = learning.prefetch_spy_series()      # network, lock-free
        with state.SLEEVE_LOCK:
            learning.record_outcomes(spy_ser=spy)
    except Exception:
        state.journal("sweep_record_outcomes_failed")
