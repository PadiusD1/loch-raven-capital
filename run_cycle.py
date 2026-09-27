"""CLI entry — run one full firm cycle headless (for scheduled tasks).

  py -3.11 run_cycle.py                 # research + trade per current settings
  py -3.11 run_cycle.py --monitor-only  # exit sweep only
  py -3.11 run_cycle.py --depth full    # override research depth this run
"""
import argparse
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import config
from firm import cycle, monitor, state

# Cycle lock now lives in firm.state (PID-aware, shared with the in-app cycle so
# the two never write sleeve state at once — 2026-06-14 audit A13).
CYCLE_LOCK = state.CYCLE_LOCK
LOCK_STALE_S = state.CYCLE_LOCK_STALE_S


def _age_hours(iso: str) -> float:
    try:
        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat(iso)).total_seconds() / 3600
        return 1e9 if age < 0 else age
    except (ValueError, TypeError):
        return 1e9


def premarket() -> None:
    """8:30 AM: refresh the full-market scan with overnight data so the 9:45
    cycle trades on fresh information. Scans only — never trades."""
    from zoneinfo import ZoneInfo as _ZI
    from firm import universe
    _ET = _ZI("America/New_York")
    scan = state.load("market_scan", {})
    # SAME ET DAY, not "<12h" (operator caught 2026-06-15): at 8:30am a Sunday-
    # night scan was only ~9.8h old so the 12h gate skipped the refresh, and the
    # 9:45 cycle then judged a STALE candidate universe. The daily scan must
    # actually run each trading morning, so refresh unless a scan already ran TODAY.
    try:
        scan_et = datetime.fromisoformat(scan.get("asof", "")).astimezone(_ET).date()
    except (ValueError, TypeError):
        scan_et = None
    _refreshed = scan_et != datetime.now(_ET).date()
    if _refreshed:
        print("refreshing full-market scan with overnight data...")
        scan = universe.scan_market(force_refresh=True)
    else:
        print("market scan already fresh (ran today)")
    top = scan.get("candidates_flat", [])
    # refreshed flag makes a silent stale-serve self-evident in the journal rather
    # than only inferable by cross-file count-diffing (change-audit verify 2026-06-15)
    state.journal("premarket_scan", tradable=scan.get("symbols_tradable"),
                  tier_candidates=top, refreshed=_refreshed)
    # The scan sets status "screening/running" but resetting it is normally
    # the cycle's job — standalone, we must clean up our own flag or the
    # health desk janitors it 6 min later with a message that reads like a
    # malfunction (operator asked about it 2026-06-12)
    state.set_status("idle", "premarket scan complete", 0, running=False)
    # Tradable-universe refresh hook (2026-08-13 IG audit): universe_health()
    # points the operator at "premarket refresh builds it", but this premarket
    # path only refreshed the universe when the held re-underwrite ran a full
    # headless underwrite_held() below — the delegated/skipped branches never
    # invoked tradable.refresh() and the snapshot could only age. Same
    # fail-soft hook as cycle.underwrite_held; refresh() self-gates on its 24h
    # TTL so double-invocation is a no-op. Placed AFTER set_status so a hung
    # API call can never leave the status flag stuck.
    try:
        from firm import tradable
        tradable.refresh()
    except Exception:
        state.journal("tradable_refresh_hook_error")
    print(f"premarket ready: {scan.get('symbols_tradable')} tradable, "
          f"tier candidates: {', '.join(top)}")
    # Refresh the IG adversarial self-audit if stale (operator 2026-07-05). The
    # postmarket path already does this, but across a long holiday weekend NO
    # postmarket runs for days, so the snapshot rots and the health desk's
    # 'IG findings' line goes stale-red. Refreshing here at the 8:30 premarket
    # means the FIRST trading morning (including a Monday catch-up after a
    # weekend) clears a stale audit before the open rather than pinning it red
    # until 4:30 PM. Fail-soft, internally gated once/~20h, and writes only the
    # ig_findings/fix-brief keys (never sleeve state) — single-writer-safe even
    # when the app is running, exactly like the postmarket call.
    try:
        import importlib
        _ig = importlib.import_module("scripts.ig_audit")
        if _ig.refresh_if_stale():
            print("IG audit refreshed (premarket)")
    except Exception:
        print("IG audit refresh failed (non-fatal)")
    # HELD-BOOK CYCLE (operator 2026-07-16): money at risk gets its own
    # complete committee cycle NOW — research AND action (sells route
    # premarket; entries queue for the open) — so the 9:45 cycle spends its
    # whole budget on a wider NEW-name intake. Sectioned stalest-first per
    # run. Delegated to the app when it's up (one process, one research
    # floor); headless under the cycle lock otherwise.
    if _app_running():
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:8420/api/underwrite-held", method="POST")
            with urllib.request.urlopen(req, timeout=10) as r:
                print(f"held re-underwrite delegated to app: HTTP {r.status}")
        except Exception as e:
            print(f"held re-underwrite delegation failed: {e}")
            # (2026-08-13 IG audit) print-only refusal: a scheduled premarket
            # run logs to a console nobody reads, so repeated delegation
            # failures starved the held-book cycle with zero journal trace.
            state.journal("premarket_delegation_refused", error=str(e)[:150])
    elif state.cycle_active():
        print("a cycle holds the lock — held re-underwrite skipped this pass "
              "(the cycle re-judges held names itself)")
        # (2026-08-13 IG audit) same silent-starvation fix as above — a skip
        # every morning is invisible unless it reaches the journal.
        state.journal("premarket_skipped_cycle_active")
    else:
        import threading as _th
        _stop = _th.Event()

        def _refresh_lock() -> None:
            while not _stop.wait(300):
                state.write_cycle_lock()

        try:
            state.write_cycle_lock()
            _hb = _th.Thread(target=_refresh_lock, daemon=True)
            _hb.start()
            try:
                with monitor.HeadlessExitGuard("premarket_underwrite"):
                    res = cycle.underwrite_held()
            finally:
                _stop.set()
                _hb.join(timeout=5)
            print(f"held re-underwrite: {len(res.get('underwritten', []))} "
                  f"re-judged, backlog {len(res.get('backlog', []))}")
        finally:
            state.clear_cycle_lock()


def postmarket() -> None:
    """4:30 PM: final exit sweep after the close (catches post-earnings moves
    4:05-4:30), then book the official end-of-day P&L per sleeve."""
    from firm import broker, health, risk
    if _app_running():
        try:
            req = urllib.request.Request("http://127.0.0.1:8420/api/monitor-now",
                                         method="POST")
            with urllib.request.urlopen(req, timeout=120):
                print("EOD exit sweep delegated to running app")
        except Exception as e:
            print(f"sweep delegation failed: {e}")
    elif state.cycle_active():
        # A cycle holds the lock (rare at 4:30, but possible mid-restart): its
        # process owns sleeve writes — do NOT sweep here too (A13 dual-writer).
        print("a cycle holds the lock — skipping headless EOD sweep (dual-writer guard)")
    else:
        # Sweep writes sleeve state — hold the lock (re-grade audit 2026-07-16)
        try:
            state.write_cycle_lock()
            monitor.check_all_exits()
            health.run_health_sweep()
        finally:
            state.clear_cycle_lock()
        print("EOD exit sweep done headless")

    # Dividend accrual: ex-dates credit paper cash daily at the wrap — a
    # live account receives these; paper books must not quietly lag it.
    # Single-writer safety: when the app is up, IT books the credits.
    if _app_running():
        try:
            req = urllib.request.Request("http://127.0.0.1:8420/api/dividends",
                                         method="POST")
            with urllib.request.urlopen(req, timeout=120) as r:
                print(f"dividend accrual delegated to app: HTTP {r.status}")
        except Exception as e:
            print(f"dividend delegation failed (non-fatal): {e}")
    elif state.cycle_active():
        print("a cycle holds the lock — skipping headless dividend credit (dual-writer guard)")
    else:
        from firm import dividends
        try:
            nd = dividends.credit_dividends()
            if nd:
                print(f"dividends credited: {nd}")
        except Exception as e:
            print(f"dividend accrual failed (non-fatal): {e}")

    # EOD mark-to-close BEFORE booking: after 16:00 the stale-guard (broker
    # FIX 1) rightly refuses fallback overwrites, so display marks freeze at
    # each symbol's last LIVE quote — on 2026-07-16 restart churn left three
    # sleeves booked at mid-afternoon prints while reconcile_display rebuilt
    # against official closes (guaranteed nightly drift alarm). Snap every
    # equity mark to the official close so the EOD booking, the dashboard,
    # and the reconciler share one convention. Single-writer: app books it.
    if _app_running():
        try:
            req = urllib.request.Request("http://127.0.0.1:8420/api/mark-close",
                                         method="POST")
            with urllib.request.urlopen(req, timeout=120) as r:
                print(f"EOD mark-to-close delegated to app: HTTP {r.status}")
        except Exception as e:
            print(f"mark-to-close delegation failed (non-fatal): {e}")
    elif state.cycle_active():
        print("a cycle holds the lock — skipping headless mark-to-close (dual-writer guard)")
    else:
        try:
            state.write_cycle_lock()
            try:
                print(f"EOD mark-to-close: {health.mark_books_to_close()}")
            finally:
                state.clear_cycle_lock()
        except Exception as e:
            print(f"mark-to-close failed (non-fatal): {e}")

    # Display reconciliation runs IMMEDIATELY after the mark-to-close
    # (operator 2026-08-21). It compares a LIVE display surface against a
    # STATIC rebuild from official closes, so it is only defined while the
    # display still carries those closes. It used to run at the END of the
    # wrap, ~4 minutes later, and the after-hours monitor repaints
    # continuously (it must — monitor.py reads the same last_price field in
    # five places to evaluate stops), so by then the two sides were different
    # quantities and the difference read as book drift. Reconciling here, in
    # the one window where the comparison means anything, is the fix; the
    # repaint precondition inside the script is the backstop that makes a late
    # run say "cannot grade" instead of inventing drift.
    try:
        import subprocess
        import sys as _sys
        rc = subprocess.run([_sys.executable,
                             str(config.BASE_DIR / "scripts" / "reconcile_display.py")],
                            capture_output=True, text=True, timeout=300)
        print((rc.stdout or "").strip())
    except Exception as e:
        print(f"display reconciliation failed to run (non-fatal): {e}")

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
                  firm_day_pnl=round(total_day, 2), sleeves=summary)
    # Durable equity history (goal audit fix 6) — survives sleeve-file wipes
    from firm import health as _health
    _health.record_equity_history(summary, total)
    # Exit-quality telemetry (purpose review #7): grade matured exits on what
    # the name did AFTER we sold — the evidence base for ever tuning the trail
    try:
        from firm import learning as _lrn
        _lrn.backfill_post_exit_excess()
    except Exception:
        print("post-exit backfill failed (non-fatal)")
    # Refresh the IG adversarial self-audit daily (2026-07-02): the weekly-only
    # cadence left a stale snapshot pinning the health desk red for days on
    # findings that were already fixed. Fail-soft, once/~20h, off-hours so the
    # Opus latency never matters; writes only the ig_findings / fix-brief keys.
    try:
        import importlib
        _ig = importlib.import_module("scripts.ig_audit")
        if _ig.refresh_if_stale():
            print("IG audit refreshed")
    except Exception:
        print("IG audit refresh failed (non-fatal)")
    # Mark today booked so the health loop's EOD catch-up doesn't double-book
    from datetime import datetime as _dt

    from zoneinfo import ZoneInfo as _ZI
    state.save("eod", {"date": _dt.now(_ZI("America/New_York")).strftime("%Y-%m-%d")})
    print(f"EOD booked: firm ${total:,.2f} ({total_day:+,.2f} today)")



def _port_listening(port: int = 8420) -> bool:
    """Is SOMETHING accepting connections on the app port? (connect-only probe)"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2.0):
            return True
    except OSError:
        return False


def _app_running() -> bool:
    """Is the dashboard app ALIVE (so the cycle must be delegated to it)?

    (2026-09-01, fix brief 87) This was ONE 3 s probe of /api/overview — an
    endpoint that prices every sleeve and fetches SPY over the network. Under
    host RAM pressure, a vendor outage (2026-08-31: yf_batch_failed
    consecutive=783 all morning) or the yfinance handle leak, it timed out
    while the app was alive; the cycle ran HEADLESS and the app's monitors
    stood down for the whole hold. Now: the state-free /api/ping, several
    attempts with gaps (a busy app gets ~30 s of patience), and ANY HTTP
    answer — even a 404 from a pre-upgrade app — counts as alive, because an
    answer proves the server loop is turning. A port that accepts connections
    but never answers is a ZOMBIE: journaled and alerted (keepalive owns the
    relaunch) and reported NOT running, so the cycle still happens — headless,
    under its own HeadlessExitGuard, never unprotected."""
    attempts = int(getattr(config, "APP_PROBE_ATTEMPTS", 4))
    timeout = float(getattr(config, "APP_PROBE_TIMEOUT_S", 5.0))
    gap = float(getattr(config, "APP_PROBE_GAP_S", 5.0))
    last = ""
    for i in range(max(1, attempts)):
        try:
            with urllib.request.urlopen("http://127.0.0.1:8420/api/ping",
                                        timeout=timeout):
                return True
        except urllib.error.HTTPError:
            return True            # it ANSWERED (older app without /api/ping)
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:80]}"
        if i + 1 < attempts:
            time.sleep(gap)
    if _port_listening():
        state.journal("app_unresponsive_zombie", attempts=attempts, error=last)
        try:
            from firm import alerts
            alerts.alert("APP UNRESPONSIVE (zombie)",
                         f"port 8420 accepts connections but /api/ping did not "
                         f"answer in {attempts} tries ({last}); this run proceeds "
                         "headless under its own exit guard — keepalive owns the "
                         "relaunch", "critical")
        except Exception:
            pass
    return False


def _delegate_cycle() -> bool:
    """POST /api/cycle with retries. True = the app owns the cycle (accepted, OR
    refused because one is already running); False = delegation is broken and
    the caller should run headless. Print-only failure was the 2026-08-13 IG
    defect class: a 9:45 cycle that silently did not happen."""
    attempts = int(getattr(config, "CYCLE_DELEGATION_ATTEMPTS", 3))
    last = ""
    for i in range(max(1, attempts)):
        try:
            req = urllib.request.Request("http://127.0.0.1:8420/api/cycle", method="POST")
            with urllib.request.urlopen(req, timeout=10) as r:
                print(f"cycle delegated to running dashboard app: HTTP {r.status}")
                return True
        except urllib.error.HTTPError as e:
            if e.code == 409:
                print("app refused the cycle: one is already running")
                state.journal("cycle_delegation_refused", http=409)
                return True
            last = f"HTTP {e.code}"
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:80]}"
        if i + 1 < attempts:
            time.sleep(5)
    print(f"could not delegate cycle to app after {attempts} tries: {last}")
    state.journal("cycle_delegation_failed", attempts=attempts, error=last)
    return False


def _headless_cycle_running() -> bool:
    """Any cycle active (a headless run OR the app's in-app cycle, which now also
    holds the PID-aware lock) — used to stand down monitor-only/postmarket writes."""
    return state.cycle_active()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--monitor-only", action="store_true")
    ap.add_argument("--premarket", action="store_true")
    ap.add_argument("--postmarket", action="store_true")
    ap.add_argument("--depth", choices=["fast", "standard", "full"])
    args = ap.parse_args()

    # Pre-run RAM cleanup (operator 2026-07-17): the cycle locks worker count
    # and depth ONCE at pool construction, so RAM freed mid-cycle never helps
    # the running cycle. The dedicated 8:25/9:40 "LochRaven RAM Cleaner" task
    # covers logged-in sessions (Interactive logon — the non-elevated ceiling);
    # this in-process pass covers the S4U/logged-out path at the exact run
    # moment, before delegation or sizing. Fail-soft: a cleaner error must
    # never cost a scan or a cycle.
    if args.premarket or not (args.postmarket or args.monitor_only):
        try:
            import subprocess
            import sys as _sys
            rc = subprocess.run(
                [_sys.executable, str(config.BASE_DIR / "scripts" / "ram_cleaner.py")],
                capture_output=True, text=True, timeout=180)
            print((rc.stdout or "").strip())
        except Exception as e:
            print(f"pre-run RAM cleaner failed (non-fatal): {e}")

    if args.premarket:
        premarket()
        return
    if args.postmarket:
        postmarket()
        return

    if args.monitor_only:
        # DEAD-MAN (2026-09-01 machine audit): under host starvation this 15-min
        # poller wedged for 38+ min in imports/probes, two instances piled up
        # (Task Scheduler 0x800710E0 "previous instance still running"), and a
        # sweep wedged INSIDE check_all_exits would hold state/cycle.lock with
        # no heartbeat for the whole 15-min stale window while the app's
        # monitors stood down. A sweep is a two-minute job; past 10 minutes
        # this instance is a zombie — release the lock and exit hard.
        import os as _os
        import threading as _th

        def _dead_man() -> None:
            try:
                state.journal("monitor_only_dead_man", seconds=600)
                state.clear_cycle_lock()
            finally:
                _os._exit(3)
        _dm = _th.Timer(600, _dead_man)
        _dm.daemon = True
        _dm.start()
        if _app_running():
            print("dashboard app is running — its built-in monitors cover exits, skipping")
            return
        if _headless_cycle_running():
            print("a headless cycle is in progress — it sweeps exits itself, skipping")
            return
        # Sweeps WRITE sleeve state (re-grade audit 2026-07-16): hold the lock
        # so a keepalive-relaunched app can't write mid-sweep (dual writer).
        try:
            state.write_cycle_lock()
            closes = monitor.check_all_exits()
        finally:
            state.clear_cycle_lock()
        print(f"monitor sweep complete: {closes}")
        return

    if args.depth:
        state.set_settings(depth=args.depth)

    # If the dashboard app is up, run the cycle INSIDE it so all state writes
    # share one process (and one lock). Two writers = lost trades.
    if _app_running():
        if _delegate_cycle():
            return
        # The app answers /api/ping but would not take the cycle: fall through
        # to the headless path (2026-09-01). It holds the lock, so the app's
        # monitors stand down exactly as before — and HeadlessExitGuard keeps
        # the book protected meanwhile. A 409 (cycle already running) is a
        # refusal, not a failure, and returned above.

    # (2026-09-02 full audit) ATOMIC claim. The old shape asked
    # `_headless_cycle_running()` and only then wrote the lock, so two headless
    # runs could both pass the question and both proceed to write sleeve state.
    if not state.try_claim_cycle_lock():
        print("another cycle holds the state lock, skipping")
        import os as _os_claim
        state.journal("cycle_claim_refused", pid=_os_claim.getpid(),
                      holder=state.cycle_lock_holder())
        return
    try:
        # Heartbeat the lock while the cycle runs: a full-depth cycle can
        # exceed the stale window, and a stale-looking lock lets the app's
        # monitor threads write sleeve state mid-cycle (dual writers)
        import threading
        _stop = threading.Event()

        def _refresh_lock() -> None:
            while not _stop.wait(300):
                state.write_cycle_lock()

        _hb_t = threading.Thread(target=_refresh_lock, daemon=True)
        _hb_t.start()
        try:
            # Exit protection stays LIVE for the whole hold (2026-09-01): the
            # app's monitors stand down for our lock, so we sweep ourselves.
            with monitor.HeadlessExitGuard("cycle"):
                result = cycle.run_cycle()
        finally:
            # Join the heartbeat before the outer finally clears the lock, so a
            # late refresh can't re-create an orphan (change-audit verify 2026-06-14)
            _stop.set()
            _hb_t.join(timeout=5)
        print(f"cycle result: {result}")
    finally:
        state.clear_cycle_lock()


if __name__ == "__main__":
    main()
