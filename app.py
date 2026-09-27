"""Loch Raven Capital — localhost dashboard + firm runtime.

  py -3.11 app.py            ->  http://localhost:8420

Background threads:
  - exit monitor sweep every 60s (paper marks / live ladder enforcement)
Endpoints drive the dashboard; POST /api/cycle launches a full firm cycle
in a worker thread so the UI streams progress live.
"""
import os
import threading
import time
import traceback
from datetime import datetime, timezone

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

import config
from firm import alerts as alerts_mod
from firm import backtest as bt
from firm import broker, cycle, health, learning, monitor, risk, state

app = FastAPI(title="Loch Raven Capital")

# A previous process may have died mid-cycle; reset stale running status —
# but NOT while a headless cycle legitimately holds the cycle lock.
_boot_lock = config.STATE_DIR / "cycle.lock"
try:
    _headless_now = _boot_lock.exists() and (time.time() - _boot_lock.stat().st_mtime) < 3600
except OSError:
    _headless_now = False
if state.get_status().get("running") and not _headless_now:
    state.set_status("idle", "Recovered from interrupted cycle", 0, running=False)

_cycle_thread: threading.Thread | None = None
_backtest_cache: dict[str, dict] = {}


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.get("/")
def dashboard():
    return FileResponse(config.DASHBOARD_DIR / "index.html")


@app.get("/api/ping")
def ping():
    """State-free liveness probe for run_cycle.py (2026-09-01, fix brief 87).

    /api/overview was the probe, and it is NOT a liveness check: it prices
    every sleeve and fetches SPY over the network, so under host RAM pressure
    or a vendor outage it exceeded run_cycle's 3 s timeout while this process
    was alive — the scheduled cycle then ran HEADLESS and the monitors here
    stood down for the whole hold. This endpoint touches no state file and no
    network; if it does not answer, the process is genuinely wedged."""
    return {"ok": True, "pid": os.getpid(), "ts": time.time()}


# ── Overview / sleeves ────────────────────────────────────────────────────────

def _spy_return_since(start_iso: str, base_px: float | None = None,
                      now_px: float | None = None) -> float | None:
    """SPY % return since a moment — exact 1:1 when the sleeve carries the
    actual inception print (bench_inception_px); date-convention fallback."""
    from firm import data_engine
    return data_engine.spy_return_since(start_iso, base_px, now_px)


def _spy_now_px() -> float:
    """THE canonical 'SPY right now' for one overview response. Every SPY
    line on screen (TODAY and SINCE alike) must price the benchmark at the
    same instant — 2026-06-12 operator catch: TODAY used the live quote
    while SINCE used a cached daily series ending at yesterday's close, and
    the two numbers contradicted each other on the same card. Live quote
    during market hours; 0 after the bell (callers fall back to closes)."""
    try:
        from firm import market_hours
        if market_hours.market_is_open():
            return float(broker.equity_quotes_public(
                [config.BENCHMARK]).get(config.BENCHMARK, 0) or 0)
    except Exception:
        pass
    return 0.0


def _ensure_bench_inception_px(s: dict, name: str) -> float | None:
    """Stamp the ACTUAL SPY print at sleeve inception, once. For sleeves born
    while the app runs this is the live quote at birth; legacy sleeves were
    backfilled with the recovered intraday print (737.31 @ 2026-06-09 14:59 ET)."""
    px = s.get("bench_inception_px")
    if isinstance(px, (int, float)) and px > 0:
        return float(px)
    try:
        # A LIVE quote is only the inception print for a sleeve born RIGHT
        # NOW — stamping it onto a days-old sleeve would forge the baseline
        born = datetime.fromisoformat(s.get("inception", ""))
        if (datetime.now(timezone.utc) - born).total_seconds() > 600:
            return None
        live = broker.equity_quotes_public([config.BENCHMARK]).get(config.BENCHMARK, 0)
        if live > 0:
            with state.SLEEVE_LOCK:
                s2 = state.get_sleeve(name)
                s2["bench_inception_px"] = round(float(live), 4)
                state.save_sleeve(name, s2)
            s["bench_inception_px"] = round(float(live), 4)
            return float(live)
    except Exception:
        pass
    return None


def _spy_day_pct(spy_now: float = 0.0) -> float | None:
    """SPY's move TODAY: the pinned live quote vs the prior close (after
    hours: last close vs prior close). Sleeve day P&L needs its benchmark
    twin or a +1.9% day reads as genius on a +1.7% tape. spy_now comes from
    _spy_now_px() so TODAY and SINCE price the same instant."""
    try:
        from firm import data_engine
        ser = data_engine.fetch_history([config.BENCHMARK],
                                        period="1mo")[config.BENCHMARK].dropna()
        if len(ser) < 2:
            return None
        import pandas as pd
        today = pd.Timestamp.now().normalize()
        if pd.Timestamp(ser.index[-1]).normalize() >= today:
            base, now_px = float(ser.iloc[-2]), (spy_now or float(ser.iloc[-1]))
        else:
            base, now_px = float(ser.iloc[-1]), (spy_now or float(ser.iloc[-1]))
        return (now_px / base - 1) * 100 if base > 0 else None
    except Exception:
        return None


@app.get("/api/overview")
def overview():
    settings = state.get_settings()
    # Weekend/holiday flag: the dashboard must say "market closed" instead of
    # rendering a Day/TODAY number — a closed day has no day P&L, and the
    # drift it showed read as a phantom trade ('Day +$8', operator 2026-07-04).
    try:
        from firm import market_hours as _mh_ov
        trading_day = _mh_ov.is_trading_day()
    except Exception:
        trading_day = True                    # fail open: show the numbers
    out = {"settings": settings, "status": state.get_status(), "sleeves": {},
           "trading_day": trading_day}
    total_equity, total_start = 0.0, 0.0
    firm_start_ts, firm_base_px = None, None
    spy_now = _spy_now_px()          # one instant, every SPY line
    spy_day = _spy_day_pct(spy_now)
    for name, cfg in config.SLEEVES.items():
        s = state.get_sleeve(name)
        equity = broker.sleeve_equity(s)  # cached marks; monitor refreshes live
        day = risk.day_pnl(s, equity)
        closed = s.get("closed_trades", [])
        # Win rate grades DECISIONS only (operator 2026-07-20: "these changes
        # don't count") — parking equitization + ETF seat migrations are
        # plumbing; the 07-20 reservoir round-trips booked 8 rows in one day
        # and skewed the success rate. The trade LIST still shows every row;
        # only the grade excludes them (same predicate as the ledger).
        graded = [t for t in closed if not learning.is_mechanical_trade(t)]
        wins = [t for t in graded if t["pnl"] > 0]
        ret_pct = (equity / s["starting_capital"] - 1) * 100
        # IMMUTABLE inception — never equity_curve[0], which trims at 2000
        # points and would silently slide the benchmark baseline forward
        inception = s.get("inception") or ""
        base_px = _ensure_bench_inception_px(s, name)
        spy_ret = (_spy_return_since(inception, base_px, spy_now or None)
                   if inception else None)
        if inception and (firm_start_ts is None or inception < firm_start_ts):
            firm_start_ts = inception
            firm_base_px = base_px
        anchor_eq = s.get("day_pnl_anchor", {}).get("equity") or equity
        day_pct = (day / anchor_eq * 100) if anchor_eq else 0.0
        # FIX 3 (2026-06-16): when positions are priced off a stale daily-close
        # fallback (pre-market / after-hours, no live quote), the "day" number is
        # not a real intraday move — surface that so the dashboard can show it as
        # provisional rather than implying a confident gain/loss.
        marks_stale = any(p.get("_mark_stale") for p in s["positions"].values())
        out["sleeves"][name] = {
            "label": cfg["label"], "desc": cfg["desc"],
            "instruments": cfg["instruments"],
            "equity": equity, "cash": round(s["cash"], 2),
            "starting_capital": s["starting_capital"],
            "total_return_pct": round(ret_pct, 2),
            "inception": inception[:10],
            "spy_return_pct": round(spy_ret, 2) if spy_ret is not None else None,
            "vs_spy_pct": round(ret_pct - spy_ret, 2) if spy_ret is not None else None,
            "day_pnl": round(day, 2),
            "day_pct": round(day_pct, 2),
            "spy_day_pct": round(spy_day, 2) if spy_day is not None else None,
            "day_vs_spy_pct": (round(day_pct - spy_day, 2)
                               if spy_day is not None else None),
            "positions": list(s["positions"].values()),
            "halted": s.get("halted", False),
            "halt_reason": s.get("halt_reason", ""),
            "equity_curve": s["equity_curve"][-300:],
            "closed_trades": closed[-12:][::-1],
            "win_rate": round(len(wins) / len(graded) * 100, 1) if graded else None,
            "trades_closed": len(graded),
            "marks_stale": marks_stale,
        }
        total_equity += equity
        total_start += s["starting_capital"]
    firm_ret = (total_equity / total_start - 1) * 100 if total_start else 0
    firm_spy = (_spy_return_since(firm_start_ts, firm_base_px, spy_now or None)
                if firm_start_ts else None)
    firm_day = sum(v["day_pnl"] for v in out["sleeves"].values())
    firm_anchor = total_equity - firm_day
    firm_day_pct = (firm_day / firm_anchor * 100) if firm_anchor else 0.0
    out["firm"] = {
        "total_equity": round(total_equity, 2),
        "total_return_pct": round(firm_ret, 2),
        "inception": (firm_start_ts or "")[:10],
        "spy_return_pct": round(firm_spy, 2) if firm_spy is not None else None,
        "vs_spy_pct": round(firm_ret - firm_spy, 2) if firm_spy is not None else None,
        "day_pct": round(firm_day_pct, 2),
        "spy_day_pct": round(spy_day, 2) if spy_day is not None else None,
        "day_vs_spy_pct": (round(firm_day_pct - spy_day, 2)
                           if spy_day is not None else None),
        "marks_stale": any(v.get("marks_stale") for v in out["sleeves"].values()),
        "trading_day": trading_day,
    }
    return out


@app.get("/api/research")
def research():
    return state.load("research", {"latest": {}, "history": []})


@app.get("/api/journal")
def journal():
    return {"events": state.read_journal(120)}


@app.get("/api/account")
def account():
    """Live Public.com account snapshot (works in both modes)."""
    try:
        return {"success": True, **broker.live_account()}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── Actions ───────────────────────────────────────────────────────────────────

@app.post("/api/cycle")
def start_cycle():
    global _cycle_thread
    if _cycle_thread and _cycle_thread.is_alive():
        return JSONResponse({"success": False, "error": "cycle already running"}, 409)
    if _headless_cycle_active():
        return JSONResponse({"success": False, "error": "a headless cycle holds "
                             "the state lock — wait for it to finish"}, 409)
    state.set_status("starting", "Firm cycle launching", 1)
    _cycle_thread = threading.Thread(target=_run_cycle_locked, daemon=True)
    _cycle_thread.start()
    return {"success": True}


class SettingsUpdate(BaseModel):
    mode: str | None = None
    depth: str | None = None
    auto_monitor: bool | None = None


@app.post("/api/settings")
def update_settings(upd: SettingsUpdate):
    changes = {k: v for k, v in upd.model_dump().items() if v is not None}
    if changes.get("mode") not in (None, "paper", "live"):
        return JSONResponse({"success": False, "error": "mode must be paper|live"}, 400)
    if changes.get("depth") not in (None, "fast", "standard", "full"):
        return JSONResponse({"success": False, "error": "bad depth"}, 400)

    new_mode = changes.get("mode")
    if new_mode and new_mode != state.get_settings()["mode"]:
        # A cycle captured the OLD mode at start — flipping mid-cycle would
        # let it book trades in the wrong book minutes after the switch
        if (_cycle_thread and _cycle_thread.is_alive()) or state.get_status().get("running"):
            return JSONResponse({"success": False,
                                 "error": "mode switch refused: a firm cycle is "
                                          "running — wait for it to finish"}, 409)
        # MODE-FLIP GUARD: positions opened in one mode must never be managed
        # by orders in the other (a live sell of a paper position would trade
        # real shares — possibly the scalper's, same account). Books must be
        # flat before the switch.
        with state.SLEEVE_LOCK:
            open_stuff = {k: len(state.get_sleeve(k)["positions"])
                          + len(state.get_sleeve(k).get("pending_orders", []))
                          for k in config.SLEEVES}
            # Queued-for-the-open intents count as open business (AUDIT
            # 2026-07-16 #6): a flat-books flip with intents on disk would
            # route yesterday's paper decisions as REAL orders at 9:30.
            open_stuff["open_queue"] = len(state.load("pending_open_entries", []) or [])
            if any(open_stuff.values()):
                state.journal("mode_flip_refused", target=new_mode, open=open_stuff)
                return JSONResponse(
                    {"success": False,
                     "error": "mode switch refused: close all positions first "
                              f"(open: { {k: v for k, v in open_stuff.items() if v} }). "
                              "Books must be flat so paper and live never mix."}, 409)
            if new_mode == "live":
                # Seed sleeves from REAL buying power so live sizing is never
                # computed against fictional paper capital
                try:
                    acct = broker.live_account()
                    bp = acct["cash_buying_power"]
                except Exception as e:
                    return JSONResponse({"success": False,
                                         "error": f"cannot reach broker to seed live capital: {e}"}, 502)
                if bp < 100:
                    return JSONResponse({"success": False,
                                         "error": f"live buying power is ${bp:,.2f} — fund the "
                                                  "account before switching to live"}, 409)
                for k, cfg_s in config.SLEEVES.items():
                    s = state.get_sleeve(k)
                    seed = round(bp * cfg_s["live_pct_of_bp"], 2)
                    s["cash"] = seed
                    s["starting_capital"] = seed
                    s["peak_equity"] = seed
                    s["peak_risk_equity"] = seed   # kill-switch basis reseeds too
                    s["day_pnl_anchor"] = {"date": "", "equity": seed}
                    state.save_sleeve(k, s)
                state.journal("live_capital_seeded", total_bp=bp,
                              per_sleeve={k: round(bp * c["live_pct_of_bp"], 2)
                                          for k, c in config.SLEEVES.items()})
    return {"success": True, "settings": state.set_settings(**changes)}


class ResearchRequestBody(BaseModel):
    symbols: list[str] | str
    note: str | None = None


def _clean_symbols(raw) -> list[str]:
    """Uppercase, dedupe, and validate operator-typed tickers (system boundary:
    never trust free-typed input). Dots/dashes allowed (BRK.B, HTZ-W)."""
    out, seen = [], set()
    for s in (raw if isinstance(raw, list) else [raw]):
        t = str(s or "").upper().strip()
        if (t and t not in seen and len(t) <= 10
                and t.replace(".", "").replace("-", "").isalnum()):
            seen.add(t)
            out.append(t)
    return out


@app.post("/api/research-request")
def add_research_request(req: ResearchRequestBody):
    """Operator-inserted names for the committee (operator 2026-07-16: PYPL
    ripped and HTZ cratered outside the momentum screen's view). Each queued
    name gets a research seat in the NEXT cycle — cooldown overridden, packet
    carries the operator's note. Queue survives restarts; an unpriceable name
    stays queued and alerts rather than being dropped."""
    syms = _clean_symbols(req.symbols)
    if not syms:
        return JSONResponse({"success": False, "error": "no valid symbols"}, 400)
    with state.SLEEVE_LOCK:
        rq = state.load("research_requests", {"queue": []}) or {}
        q = [r for r in (rq.get("queue") or []) if isinstance(r, dict)]
        have = {r.get("ticker") for r in q}
        added = [t for t in syms if t not in have]
        q += [{"ticker": t, "kind": "stock", "note": (req.note or "")[:300],
               "requested_at": datetime.now(timezone.utc).isoformat()}
              for t in added]
        state.save("research_requests", {"queue": q})
    state.journal("operator_request_added", symbols=added,
                  already_queued=[t for t in syms if t not in added])
    return {"success": True, "queued": added, "queue": q}


@app.get("/api/research-requests")
def list_research_requests():
    return state.load("research_requests", {"queue": []}) or {"queue": []}


@app.post("/api/underwrite-held")
def underwrite_held_endpoint():
    """The 8:30 held-book CYCLE (operator 2026-07-16) — research through
    action for held names only (entries queue for the open); shares the
    cycle-thread guard so it can never run beside another cycle."""
    global _cycle_thread
    if _cycle_thread and _cycle_thread.is_alive():
        return JSONResponse({"success": False,
                             "error": "a cycle/underwrite is already running"}, 409)
    if _headless_cycle_active():
        return JSONResponse({"success": False, "error": "a headless cycle holds "
                             "the state lock — it re-judges held names itself"}, 409)

    def _run():
        # A13 (re-grade audit 2026-07-16): the held cycle WRITES SLEEVE STATE
        # (it trades) — it must hold the cross-process cycle lock exactly like
        # _run_cycle_locked, or a headless run_cycle/postmarket process could
        # write concurrently. Heartbeated + always released.
        state.write_cycle_lock()
        _stop = threading.Event()

        def _hb():
            while not _stop.wait(300):
                state.write_cycle_lock()

        _hb_t = threading.Thread(target=_hb, daemon=True)
        _hb_t.start()
        try:
            cycle.underwrite_held()
        except Exception:
            state.journal("held_underwrite_error",
                          trace=traceback.format_exc()[-400:])
        finally:
            _stop.set()
            _hb_t.join(timeout=5)
            state.clear_cycle_lock()

    _cycle_thread = threading.Thread(target=_run, daemon=True)
    _cycle_thread.start()
    return {"success": True, "note": "held re-underwrite launched"}


@app.get("/api/street-research")
def street_research_shelf():
    """The loaded institutional-report shelf (operator 2026-07-16). Drop
    PDF/.txt/.md files into research_docs/inbox/ — the 30-min health sweep or
    the next cycle distills them for the committee."""
    st = state.load("street_research", {}) or {}
    docs = [{"file": d.get("file"), "added": d.get("added"),
             "source": (d.get("brief") or {}).get("source"),
             "title": (d.get("brief") or {}).get("title"),
             "scope": (d.get("brief") or {}).get("scope")}
            for d in (st.get("docs") or []) if isinstance(d, dict)]
    return {"docs": docs, "inbox": str(config.STREET_RESEARCH_DIR)}


@app.post("/api/street-research/scan")
def street_research_scan_now():
    """Operator-triggered immediate ingest of freshly dropped reports."""
    from firm import street_research
    try:
        n = street_research.scan_and_distill()
        return {"success": True, "ingested": n}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)[:200]}, 500)


@app.post("/api/research-request/remove")
def remove_research_request(req: ResearchRequestBody):
    syms = set(_clean_symbols(req.symbols))
    with state.SLEEVE_LOCK:
        rq = state.load("research_requests", {"queue": []}) or {}
        q = [r for r in (rq.get("queue") or [])
             if isinstance(r, dict) and r.get("ticker") not in syms]
        state.save("research_requests", {"queue": q})
    state.journal("operator_request_removed", symbols=sorted(syms))
    return {"success": True, "queue": q}


@app.post("/api/clear-halt/{sleeve}")
def clear_halt(sleeve: str):
    if sleeve not in config.SLEEVES:
        return JSONResponse({"success": False, "error": "unknown sleeve"}, 404)
    risk.clear_halt(sleeve)
    return {"success": True}


@app.post("/api/restart")
def restart_app(force: bool = False):
    """Graceful code reload: journal, exit, and let the 5-min keepalive
    relaunch from disk — the same lifecycle the watchdog uses. Needed since
    S4U scheduling put the process outside interactive kill reach.

    DEFERS during regular market hours unless force=true — a code-reload
    restart briefly pauses the exit monitors, and a non-urgent reload must
    never interrupt live trading (operator 2026-06-23). Crash recovery is
    unaffected (keepalive relaunches a DOWN process directly, not via here)."""
    from firm import market_hours
    if market_hours.code_reload_should_defer(force):
        _ph = market_hours.session_phase()
        _lab = market_hours.session_phase_label()
        state.journal("restart_deferred", phase=_ph,
                      reason=f"{_lab} — code reload waits for the close")
        return {"success": False, "deferred": True, "phase": _ph,
                "note": f"{_lab} — restart deferred to protect live exit "
                        "monitoring; it will run after the 16:00 ET close "
                        "(or POST with ?force=true)"}
    import threading as _t

    def _die():
        time.sleep(0.5)            # let the HTTP response flush
        state.journal("restart_requested", via="api", forced=force)
        os._exit(42)

    _t.Thread(target=_die, daemon=True).start()
    return {"success": True, "note": "restarting — keepalive relaunches within 5 min"}


@app.post("/api/dividends")
def credit_dividends_endpoint():
    """Postmarket wrap delegates dividend accrual here when the app is up —
    single-writer safety on the sleeve books."""
    from firm import dividends
    try:
        return {"success": True, "credited": dividends.credit_dividends()}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/mark-close")
def mark_close_endpoint():
    """Postmarket wrap delegates the EOD mark-to-close here when the app is up
    (single-writer safety) — snaps equity display marks to official closes so
    the books, the dashboard, and the nightly reconciliation share one
    convention after the bell (2026-07-16 three-sleeve drift)."""
    if _headless_cycle_active():
        return JSONResponse({"success": False, "error": "a headless cycle holds "
                             "the state lock — retry after it finishes"}, 409)
    from firm import health as _health
    try:
        return {"success": True, **_health.mark_books_to_close()}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/monitor-now")
def monitor_now():
    if _headless_cycle_active():
        return JSONResponse({"success": False, "error": "a headless cycle holds "
                             "the state lock — it sweeps exits itself"}, 409)
    try:
        return {"success": True, "closes": monitor.check_all_exits()}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/watchlist")
def watchlist_endpoint():
    wl = state.load("watchlist", {})
    out = []
    for t, e in wl.items():
        hist = e.get("history", [])
        first_q = hist[0]["quant"] if hist else None
        last_q = hist[-1]["quant"] if hist else None
        out.append({
            "ticker": t, "source": e.get("source", ""),
            "watch_trigger": e.get("watch_trigger", ""),
            "benched_since": e.get("added", ""),
            "quant_first": first_q, "quant_now": last_q,
            # When the score was last VERIFIED — a live 93 prints the same
            # as a frozen 93; only the timestamp proves the bench isn't blind
            "scored_at": hist[-1]["ts"] if hist else None,
            "trend": round((last_q or 0) - (first_q or 0), 1)
            if first_q is not None and last_q is not None else 0,
            "last_verdict": e.get("last_verdict"),
        })
    out.sort(key=lambda x: x["quant_now"] or 0, reverse=True)
    return {"bench": out, "count": len(out)}


@app.get("/api/learning")
def learning_endpoint():
    return {"lessons": state.load("lessons", {}),
            "stats": learning.stats(),
            "bench_tuning": state.load("tuning", {}).get("bench", {})}


@app.get("/api/routing")
def routing_endpoint():
    """Last cycle's cross-sleeve BUY routing: per name, which book(s) it
    qualified for, which placed it, and why any qualified name was not placed
    (operator 2026-07-02 — 'make sure a buy lands in the right sleeves')."""
    from firm import routing
    rep = state.load("routing_report", {}) or {}
    ok, detail = routing.routing_health()
    return {"asof": rep.get("asof"), "rows": rep.get("rows", []),
            "healthy": ok, "summary": detail,
            "open_slots": {sl: routing.open_slots(sl)[0]
                           for sl in ("alpha_stocks", "core_blend", "calls_only")},
            # how each book's acceptance actually performs forward (learning loop)
            "acceptance": learning.acceptance_report()}


@app.get("/api/alerts")
def get_alerts():
    return {"unacked": alerts_mod.unacked()}


@app.post("/api/alerts/ack")
def ack_alerts():
    alerts_mod.ack_all()
    return {"success": True}


@app.post("/api/lessons/prune/{idx}")
def prune_lesson(idx: int):
    """Human governance over FIRM MEMORY: remove a lesson you judge flawed
    before it keeps steering the committee."""
    with state.SLEEVE_LOCK:
        lessons = state.load("lessons", {})
        items = lessons.get("lessons", [])
        if not (0 <= idx < len(items)):
            return JSONResponse({"success": False, "error": "bad index"}, 404)
        removed = items.pop(idx)
        state.save("lessons", lessons)
    state.journal("lesson_pruned_by_human", lesson=removed.get("lesson", "")[:120])
    return {"success": True, "removed": removed.get("lesson")}


@app.post("/api/ig/triage")
def triage_ig_findings():
    """Close the IG->ratification loop (2026-07-13 Item 7): mark the LATEST IG
    snapshot reviewed so its high findings stop redding health every 30 min. The
    findings remain on file as FIX_BRIEFS.md; this records that the operator has
    SEEN and tracked them. A newer audit advances the asof and re-arms review."""
    _ig = state.load("ig_findings_latest", {}) or {}
    asof = _ig.get("asof") if isinstance(_ig, dict) else ""
    if not asof:
        return JSONResponse({"success": False, "error": "no IG snapshot"}, 404)
    state.save("ig_reviewed_asof", asof)
    state.save("ig_triage_incident", {})   # clear any pending SLA escalation
    highs = sum(1 for f in (_ig.get("findings") or [])
                if str(f.get("severity", "")).lower() in ("high", "critical"))
    state.journal("ig_triage_done", snapshot=asof[:19], highs=highs)
    return {"success": True, "reviewed_asof": asof, "high_findings": highs}


@app.post("/api/seats/review")
def seats_review_endpoint():
    """Run the ETF seat review on demand (operator 2026-07-20: the eligibility
    fix must EVICT a mis-seated parking incumbent now, not at the next 8:30).
    Same single-writer contract as every seat flip: allocation takes
    SLEEVE_LOCK per trade inside this process — but SLEEVE_LOCK is a
    threading lock, IN-PROCESS only (audit 2026-07-26 CRITICAL #3): a headless
    run_cycle.py in another PID writes the same sleeve files unprotected. Refuse
    while a headless cycle holds the cross-process lock, same as /api/cycle."""
    if _headless_cycle_active():
        return JSONResponse({"success": False, "error": "a headless cycle holds "
                             "the state lock — retry after it finishes"}, 409)
    from firm import allocation
    out = allocation.review_seats()
    state.journal("seats_review_via_api", result=out)
    return {"success": True, **out}


@app.get("/api/health")
def health_endpoint():
    # Serve the cached sweep, but recompute on demand when it's missing or STALE —
    # the health loop only refreshes every HEALTH_INTERVAL_S (30 min) and not for the
    # first 120s after a restart, so a fresh process (or a just-finished cycle) would
    # otherwise serve a pre-restart snapshot that reads as a real fault (2026-06-17
    # operator saw a stale "last cycle 25.4h ago"). health_report() is READ-ONLY — no
    # alerts / EOD / auto-recover side effects (those stay in the periodic sweep). We
    # persist the fresh read so rapid dashboard polls stay cheap (recompute <= once/5m).
    saved = state.load("health", {})
    stale = True
    try:
        asof = saved.get("asof", "")
        stale = (not asof or (datetime.now(timezone.utc)
                              - datetime.fromisoformat(asof)).total_seconds() > 300)
    except (ValueError, TypeError):
        stale = True
    if not saved or stale:
        saved = health.health_report()
        state.save("health", saved)
    saved["reconcile"] = state.load("reconcile", {})
    return saved


@app.get("/api/backtest/{sleeve}")
def backtest(sleeve: str, refresh: bool = False):
    if sleeve not in config.SLEEVES:
        return JSONResponse({"error": "unknown sleeve"}, 404)
    if sleeve not in _backtest_cache or refresh:
        try:
            _backtest_cache[sleeve] = bt.run_backtest(sleeve)
        except Exception as e:
            return JSONResponse({"error": str(e)}, 500)
    return _backtest_cache[sleeve]


# ── Background exit monitors ──────────────────────────────────────────────────
# Calls desk gets its own high-frequency loop (options are volatile): ~1s ticks
# via Public.com batch quotes while positions are open, slow idle checks when
# flat. Equity sleeves sweep on a relaxed 60s cadence.

_last_calls_hb = 0.0
_last_calls_idle_mark = 0.0


def _headless_cycle_active() -> bool:
    """A FOREIGN-process cycle (headless run_cycle.py) holds state\\cycle.lock —
    while it does, this process must not write sleeve state (cross-process dual-
    writer guard). PID-aware (2026-06-14 audit A13): the app's OWN in-app cycle
    also holds the lock, but exclude_own=True means that does NOT make the app's
    monitors stand down (they're safe under the in-process SLEEVE_LOCK)."""
    return state.cycle_active(exclude_own=True)


def _run_cycle_locked() -> dict:
    """Run a cycle IN-PROCESS while holding the cross-process cycle lock with this
    app's pid, so a scheduled headless run_cycle.py / postmarket / monitor-only
    process sees a cycle is active and stands down (A13: previously the delegated
    in-app cycle held NO lock, so an external process could write sleeve state
    concurrently). The lock is heartbeated and always released."""
    # (2026-09-02 full audit) ATOMIC claim, same reason as run_cycle.py: the
    # caller checked `_headless_cycle_active()` and only then wrote the lock.
    if not state.try_claim_cycle_lock():
        state.journal("cycle_claim_refused", pid=os.getpid(),
                      holder=state.cycle_lock_holder())
        state.set_status("idle", "another cycle holds the state lock", 0,
                         running=False)
        return {"success": False, "error": "cycle lock held by another process"}
    _stop = threading.Event()

    def _hb():
        while not _stop.wait(300):
            state.write_cycle_lock()
    _hb_t = threading.Thread(target=_hb, daemon=True)
    _hb_t.start()
    try:
        return cycle.run_cycle()
    finally:
        # Quiesce the heartbeat BEFORE clearing, else a late write_cycle_lock()
        # could re-create the lock right after we delete it = orphan that blinds
        # monitors for the stale window (change-audit verify 2026-06-14).
        _stop.set()
        _hb_t.join(timeout=5)
        state.clear_cycle_lock()


def _calls_monitor_loop():
    global _last_calls_hb, _last_calls_idle_mark
    while True:
        try:
            # Heartbeat FIRST, unconditionally — the loop being alive-but-
            # skipping (headless cycle, auto_monitor off) must not look like a
            # hang to the watchdog, or it kills a healthy app every 25 min.
            if time.time() - _last_calls_hb > 30:
                health.heartbeat("calls_monitor")
                _last_calls_hb = time.time()
            if not state.get_settings().get("auto_monitor", True):
                time.sleep(5)
                continue
            if _headless_cycle_active():
                time.sleep(10)
                continue
            from firm import market_hours
            if not market_hours.market_is_open():
                # Options only trade 9:30-16:00 — overnight ticks can't exit
                # anything; expiry guards run on the first open-market sweep
                time.sleep(60)
                continue
            s = state.get_sleeve("calls_only")
            # Real OPTIONS only — the BIL parking is an equity row; ticking a
            # T-bill every second would hammer quotes + rewrite sleeve state
            # and arm the 180s options dead-man (batch review 2026-07-01 R4)
            if monitor.has_real_options(s):
                monitor.check_calls_exits()
                time.sleep(config.CALLS_MONITOR_INTERVAL_S)
            else:
                # Idle book still needs MARKS (operator 2026-07-20): with no
                # open option this loop slept and the 60s equity loop excludes
                # calls_only (include_calls=False) — so the sleeve's equity
                # parking was NEVER priced: SOXX parking sat frozen at entry /
                # 0% on the dashboard AND risk.mark_equity fed the breakers a
                # stale sleeve equity. Throttled to the same 60s cadence the
                # other sleeves get; a 1s hammer stays reserved for real
                # options. (Two loops each assuming the other covered it.)
                if (s.get("positions")
                        and time.time() - _last_calls_idle_mark
                        >= config.EQUITY_MONITOR_INTERVAL_S):
                    monitor.check_calls_exits()
                    _last_calls_idle_mark = time.time()
                time.sleep(config.CALLS_MONITOR_IDLE_S)
        except Exception:
            state.journal("calls_monitor_error", trace=traceback.format_exc()[-1000:])
            time.sleep(10)


_EQ_MON_GEN = 0        # bumped by the watchdog when it replaces a hung monitor thread
_LAST_DELEGATED_HOLDER: int | None = None   # last foreign pid whose sweeps covered us


def _equity_monitor_loop():
    global _LAST_DELEGATED_HOLDER
    from firm import market_hours
    my_gen = _EQ_MON_GEN
    while True:
        time.sleep(config.EQUITY_MONITOR_INTERVAL_S)
        if my_gen != _EQ_MON_GEN:
            return   # superseded by a watchdog thread-restart — never double-sweep
        try:
            # Sweep only while a session is open — PRE-MARKET, MARKET HOURS or
            # AFTER HOURS. The boolean below groups the two extended windows on
            # purpose (protective exits may route in both); when this is
            # REPORTED, use market_hours.session_phase() so the reader is told
            # which one, because a pre-market sweep and an after-hours sweep
            # mean different things (operator 2026-08-21).
            # overnight there is nothing to exit INTO, vendors return empty
            # after-hours quotes (mark_missing noise), and the API calls are
            # pure burn. Heartbeat still writes so the watchdog stays calm.
            session_open = (market_hours.market_is_open()
                            or market_hours.extended_session_open())
            _auto = state.get_settings().get("auto_monitor", True)
            _headless = _headless_cycle_active()
            if session_open and _auto and not _headless:
                monitor.check_all_exits(include_calls=False)
                # SWEEP COVERAGE, distinct from thread liveness (operator
                # 2026-07-25). Only the heartbeat below existed, and it was
                # written OUTSIDE this gate, so health.py rendered "last sweep
                # N min ago" from a beat that fires whether or not a sweep ran.
                # It measured that the thread was alive, never that the book was
                # being protected — so any skip-without-death (auto_monitor off,
                # a headless cycle holding the lock) was invisible to both the
                # health desk and the watchdog. Written only on a real sweep.
                health.heartbeat("equity_sweep")
            elif session_open:
                # In-session and NOT sweeping is a genuine protection gap: say so
                # rather than letting the liveness beat imply coverage —
                # UNLESS the foreign lock-holder is sweeping for us (2026-09-01):
                # a headless run_cycle now carries monitor.HeadlessExitGuard and
                # stamps state/headless_sweep on every real sweep. A fresh stamp
                # from the pid that holds the lock means the book IS protected.
                # Journal the hand-off once per holder (not once per minute — the
                # journal budget is what starves the health windows) and count a
                # minute as BLIND only when nobody swept it.
                _holder = state.cycle_lock_holder() if _headless else None
                if _headless and monitor.headless_sweep_fresh(_holder):
                    if _LAST_DELEGATED_HOLDER != _holder:
                        _LAST_DELEGATED_HOLDER = _holder
                        state.journal("equity_sweep_delegated", holder_pid=_holder)
                else:
                    state.journal("equity_sweep_skipped",
                                  auto_monitor=_auto, headless_cycle=_headless)
            health.heartbeat("equity_monitor")   # thread liveness (watchdog input)
        except Exception:
            state.journal("monitor_error", trace=traceback.format_exc()[-1000:])


def _preopen_ram_sweep_loop():
    """PRE-EMPTIVE 9:15 ET RAM sweep on trading days (operator 2026-07-20,
    after the 07-20 boot-into-1.01GB storm): the 8:25/9:40 Windows-task slots
    miss entirely when the PC was off, and the reactive startup sweep only
    fires once things are already starving. This in-app slot runs the same
    scripts/ram_cleaner.py in the 9:15-9:30 window whenever the fund is up,
    so the OPEN starts on a clean box. Once per day (state-latched, so a
    restart inside the window can't double-fire); latch is written BEFORE the
    sweep so a cleaner crash can't loop it; sandboxed suites never spawn it
    (same STATE_DIR guard as cycle._post_cycle_ram_clean)."""
    import subprocess
    import sys as _sys
    from datetime import time as _dtime
    from zoneinfo import ZoneInfo
    from firm import market_hours
    while True:
        try:
            now_et = datetime.now(ZoneInfo("America/New_York"))
            if (market_hours.is_trading_day(now_et)
                    and _dtime(9, 15) <= now_et.time() < _dtime(9, 30)
                    and str(config.STATE_DIR) == str(config.BASE_DIR / "state")):
                latch = state.load("ram_sweep_0915", {}) or {}
                today = now_et.date().isoformat()
                if latch.get("date") != today:
                    state.save("ram_sweep_0915", {"date": today})
                    r = subprocess.run(
                        [_sys.executable,
                         str(config.BASE_DIR / "scripts" / "ram_cleaner.py")],
                        capture_output=True, text=True, timeout=180,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),)
                    state.journal("ram_sweep_0915", rc=r.returncode)
        except Exception:
            try:
                state.journal("ram_sweep_0915_failed",
                              error=traceback.format_exc()[-200:])
            except Exception:
                pass
        time.sleep(60)


def _health_loop():
    """Account-care sweep: self-checks, live reconciliation, capital rebalance,
    and cycle catch-up (a missed 9:45 task — PC off/offline — runs once fresh)."""
    global _cycle_thread
    time.sleep(120)  # let the app settle first
    while True:
        try:
            health.run_health_sweep()
            health.reconcile_live()
            health.rebalance_firm()

            # Catch-up: no research in >26h, on a weekday, nothing running ->
            # the scheduled cycle was missed; run it now that we're online.
            # In live mode only while the market is open (orders would be
            # refused after hours anyway — don't waste the research).
            research = state.load("research", {})
            stale = True
            try:
                from datetime import datetime as _dt
                age_h = (_dt.now(timezone.utc)
                         - _dt.fromisoformat(research.get("latest_asof", ""))
                         ).total_seconds() / 3600
                stale = age_h > 26 or age_h < 0   # negative = clock jumped: refresh
            except (ValueError, TypeError):
                pass
            from firm import market_hours as _mh_cu
            _now_et_cu = datetime.now(_mh_cu.ET)
            # Trading-day-aware in ET, NOT UTC (Sunday-night ET = Monday UTC fired a
            # pointless cycle), AND only in the sensible WINDOW 10:00-16:00 ET: the
            # scheduled 9:45 cycle covers the normal open; a catch-up is the
            # fallback for a MISSED 9:45, so it only makes sense after ~10:00 (the
            # missed-by-now deadline) and before the 16:00 close. This also stops
            # the dead-of-night catch-ups (operator caught a 00:24 Monday run
            # 2026-06-15) and post-close cycles that can't trade today.
            weekday = (_mh_cu._is_trading_day(_now_et_cu)
                       and 10 <= _now_et_cu.hour < 16)
            # Third trigger (2026-06-12: Task Scheduler's network condition
            # silently declined the 9:45 launch right after an outage and
            # never re-fired): a trading weekday with NO completed cycle by
            # 10:00 ET is a missed cycle, full stop — don't wait for the
            # 26h staleness rule to notice tomorrow.
            try:
                from zoneinfo import ZoneInfo as _ZI2
                _now_et = datetime.now(_ZI2("America/New_York"))
                _last_day_et = (datetime.fromisoformat(research.get("latest_asof", ""))
                                .astimezone(_ZI2("America/New_York"))
                                .strftime("%Y-%m-%d")
                                if research.get("latest_asof") else "")
                if (_now_et.weekday() < 5
                        and _now_et.hour * 60 + _now_et.minute >= 10 * 60
                        and _last_day_et != _now_et.strftime("%Y-%m-%d")):
                    stale = True
            except (ValueError, TypeError):
                pass
            # Self-heal a stuck "running" status left by a dead cycle thread
            if (state.get_status().get("running")
                    and (_cycle_thread is None or not _cycle_thread.is_alive())):
                state.set_status("idle", "stale running-status reset by health desk",
                                 0, running=False)
            busy = ((_cycle_thread and _cycle_thread.is_alive())
                    or state.get_status().get("running")
                    or _headless_cycle_active())
            from firm import market_hours
            mode_ok = (state.get_settings()["mode"] == "paper"
                       or market_hours.market_is_open())
            # Two triggers: research >26h stale (PC was off), OR the last
            # cycle attempt produced ZERO verdicts (internet died with the
            # PC on — the cycle "ran" but nothing happened). Either way,
            # retry only once connectivity is actually back.
            retry_flag = state.load("cycle_retry", {}).get("needed", False)
            # A zero-verdict RETRY (the last cycle hit an outage) must fire whenever
            # connectivity returns and the session is still actionable — NOT only in
            # the 10-16 missed-schedule window. Clamping it to 10-16 (the R34 catch-up
            # fix) left a pre-10am or 3:50pm reconnection un-retried that day (change-
            # audit verify 2026-06-15). The retry window is the whole trading day up to
            # the close; the missed-schedule 'stale' branch keeps its tighter window.
            retry_window = _mh_cu._is_trading_day(_now_et_cu) and _now_et_cu.hour < 16
            # Self-heal a stale ERROR banner (operator 2026-07-04): a zero-verdict
            # cycle stamps status stage='error' ("research outage — will retry when
            # CLI returns") and NOTHING rewrites it until the next cycle — over the
            # Jul-4 weekend that banner read as a live fault for 2+ days while the
            # CLI was already healthy and the retry was simply waiting for Monday.
            # When the retry is pending but CANNOT fire (market closed / after
            # 16:00 ET), restate the truth calmly; the retry path below still owns
            # the actual re-run and will stamp its own status when it fires.
            _st_cu = state.get_status()
            if (retry_flag and not retry_window and not _st_cu.get("running")
                    and _st_cu.get("stage") == "error"):
                state.set_status(
                    "idle",
                    "research floor was degraded last cycle — catch-up retry "
                    "queued, fires automatically next trading session",
                    0, running=False)
            # RAM gate on the RETRY path (2026-07-01 goal audit, fix 2): the 06-30
            # zero-verdict cycles were caused by host-RAM starvation, and the retry
            # re-fired on the SAME starved box and re-failed — burning Opus calls to
            # produce more degraded verdicts. A retry only makes sense once the
            # resource that killed the last attempt has recovered. Missed-schedule
            # catch-ups ('stale') are not gated — a PC-was-off catch-up is usually a
            # fresh boot with plenty of RAM, and skipping it entirely would be worse.
            _retry_ram_ok = True
            if retry_flag:
                try:
                    import psutil as _ps_r
                    _avail_r = _ps_r.virtual_memory().available / 1e9
                    _retry_ram_ok = _avail_r >= float(
                        getattr(config, "RETRY_MIN_RAM_GB", 0.0))
                    if not _retry_ram_ok:
                        state.journal("cycle_retry_deferred_ram",
                                      avail_gb=round(_avail_r, 1),
                                      need_gb=float(getattr(config, "RETRY_MIN_RAM_GB", 0.0)))
                except ImportError:
                    pass          # no psutil -> fail open, old behavior
            # A pending retry_flag PROVES a cycle already ran and failed today —
            # in that state the RAM gate applies to EVERY re-fire path, including
            # the 'stale' branch (a zero-verdict cycle never stamps latest_asof,
            # so 'stale' stays true all day and re-fired ungated every 30 min on
            # the starved box — the exact 06-30 quota-burn loop; batch review R1).
            if (not retry_flag or _retry_ram_ok) \
                    and ((stale and weekday) or (retry_flag and retry_window)) \
                    and not busy and mode_ok and health.internet_up():
                state.journal("cycle_catchup",
                              reason=("zero-verdict cycle retry (outage)"
                                      if retry_flag else
                                      "missed scheduled cycle (PC off/offline)"))
                state.set_status("starting", "Catch-up firm cycle (missed schedule)", 1)
                _cycle_thread = threading.Thread(target=_run_cycle_locked, daemon=True)
                _cycle_thread.start()

            # Scan catch-up (operator 2026-07-07): the 8:30 premarket scan can FAIL
            # (an internet outage at 8:30, as on 07-06) or be missed (PC off),
            # leaving a PRIOR-DAY scan stale all day with no recovery until the next
            # morning — the operator saw "scan failed, internet's back, still not
            # updating". Refresh a prior-day scan as soon as we're up + online on a
            # trading day (7:00-16:00 ET), in a background thread so the sweep never
            # blocks; the premarket's same-ET-day gate then skips a redundant 8:30
            # run. Fires at most once/day (scan asof advances to today on success).
            try:
                _scan = state.load("market_scan", {})
                _scan_et = ""
                try:
                    _scan_et = (datetime.fromisoformat(_scan.get("asof", ""))
                                .astimezone(_mh_cu.ET).strftime("%Y-%m-%d"))
                except (ValueError, TypeError):
                    pass
                if (_mh_cu._is_trading_day(_now_et_cu) and 7 <= _now_et_cu.hour < 16
                        and _scan_et != _now_et_cu.strftime("%Y-%m-%d")
                        and not busy and not state.get_status().get("running")
                        and health.internet_up()):
                    from firm import universe as _uni_cu
                    state.journal("scan_catchup", scan_was=_scan_et or "none")
                    threading.Thread(
                        target=lambda: _uni_cu.scan_market(force_refresh=True),
                        daemon=True).start()
            except Exception:
                state.journal("scan_catchup_error",
                              trace=traceback.format_exc()[-500:])

            # Midday full-refresh (operator 2026-07-08): a morning cycle can degrade
            # on a CLI/network blip; the opportunistic retry above usually recovers
            # it, but a GUARANTEED fresh full pass at ~noon (calm mid-session) catches
            # anything still thin AND re-processes the overdue reopens. Fires ONCE per
            # trading day in the 12:00 ET hour, only when the day's research is not
            # already complete-and-healthy (health.midday_refresh_warranted); a
            # healthy morning -> no-op (logged). Reuses the same locked cycle path.
            try:
                _mid_date = (state.load("midday_refresh", {}) or {}).get("date")
                _tdy = _now_et_cu.strftime("%Y-%m-%d")
                _cyc_busy = busy or bool(_cycle_thread and _cycle_thread.is_alive())
                if (_mh_cu._is_trading_day(_now_et_cu) and _now_et_cu.hour == 12
                        and _mid_date != _tdy and not _cyc_busy and mode_ok
                        and health.internet_up()):
                    from firm import brain as _brain_mid
                    _warrant, _why = health.midday_refresh_warranted()
                    state.save("midday_refresh", {"date": _tdy})   # once/day, either way
                    if _warrant and _brain_mid.available():
                        state.journal("midday_refresh", fired=True, reason=_why)
                        state.set_status(
                            "starting", "Midday refresh — calm-session catch-up cycle", 1)
                        _cycle_thread = threading.Thread(target=_run_cycle_locked,
                                                         daemon=True)
                        _cycle_thread.start()
                    else:
                        state.journal("midday_refresh", fired=False,
                                      reason=(_why if not _warrant else "CLI unavailable"))
            except Exception:
                state.journal("midday_refresh_error",
                              trace=traceback.format_exc()[-500:])

            # EOD catch-up: postmarket wrap missed (offline at 4:30) -> book
            # it as soon as we're back: exit sweep ran above; dividends + the
            # official EOD record must not be lost to an outage
            health.eod_catchup()
        except Exception:
            state.journal("health_error", trace=traceback.format_exc()[-1000:])
        time.sleep(config.HEALTH_INTERVAL_S)


_APP_START = time.time()
WATCHDOG_STALE_S = 1200  # 20 min without a successful monitor sweep = stuck


_EQ_THREAD_RESTART_AT: float | None = None   # strike-1 timestamp (see below)


def _watchdog_loop():
    """A network drop can leave a thread hung in a dead socket — it never
    crashes, so try/except can't save it. This watchdog only reads LOCAL
    files (cannot hang) and hard-exits for the keepalive to restart us when:
      - the equity monitor heartbeat goes stale AND an in-process thread
        replacement (strike 1) also failed to restore it (strike 2 — the
        whole process is sick, not just one thread),
      - the calls monitor heartbeat goes stale WHILE option positions exist
        (unmonitored options are the most dangerous kind), or
      - a cycle status claims 'running' for >3h (hung cycle thread would
        otherwise block every future cycle forever).
    Strike 1 exists because a hard exit for one hung thread also killed every
    healthy research cycle (IG CRITICAL 2026-08-13: 21 cycle_starts vs ~1
    completion; 37 lifetime watchdog_restarts, nearly all 'equity monitor
    stale'). Replacing just the thread restores exit protection in seconds
    instead of a full boot; the abandoned thread exits via the generation
    guard if it ever un-hangs."""
    def _age_s(iso: str) -> float:
        try:
            a = (datetime.now(timezone.utc)
                 - datetime.fromisoformat(iso)).total_seconds()
            return 1e9 if a < 0 else a    # clock jumped back = treat as stale
        except (ValueError, TypeError):
            return -1.0                   # unknown — don't act on it
    while True:
        time.sleep(60)
        try:
            up = time.time() - _APP_START
            hb = state.load("heartbeat", {})
            reasons = []
            # Options dead-man's switch — TIGHT (an unmonitored leveraged option
            # is the most dangerous position). Shorter grace + threshold than the
            # equity/cycle checks: ~3 min stale with open options -> restart now,
            # not in 20. Calls heartbeat refreshes ~every 30s, so this won't trip
            # a healthy loop (incl. the 60s closed-market idle).
            if up > config.OPTIONS_MONITOR_GRACE_S:
                calls_age = _age_s(hb.get("calls_monitor", ""))
                if (calls_age > config.OPTIONS_MONITOR_STALE_S
                        and monitor.has_real_options(state.get_sleeve("calls_only"))):
                    reasons.append(
                        f"calls monitor stale {calls_age / 60:.1f}m with open options")
            # Slower checks — the 60s equity loop and a hung cycle thread keep the
            # conservative 20-min grace/threshold.
            if up >= WATCHDOG_STALE_S:
                # FIX 2 (2026-06-16): only treat a stale equity monitor as a FAULT
                # while a session is live. Off-hours the loop idles (nothing to exit
                # into) and a PC sleep freezes the whole process — staleness is then
                # EXPECTED and a restart fixes nothing, it just churns (5 overnight
                # restarts fighting normal off-hours staleness, operator-caught).
                # During a session a stale monitor really would miss exits -> restart.
                from firm import market_hours as _mh_wd
                try:
                    _session_live = (_mh_wd.market_is_open()
                                     or _mh_wd.extended_session_open())
                except Exception:
                    _session_live = True       # unknown -> fail safe to old behavior
                eq_age = _age_s(hb.get("equity_monitor", ""))
                if _session_live and eq_age > WATCHDOG_STALE_S:
                    global _EQ_MON_GEN, _EQ_THREAD_RESTART_AT
                    if (_EQ_THREAD_RESTART_AT is not None
                            and time.time() - _EQ_THREAD_RESTART_AT
                            > WATCHDOG_STALE_S + 120):
                        # Strike 2: the replacement thread also failed to stamp
                        # within its own stale window — process-level sickness.
                        reasons.append(
                            f"equity monitor stale {eq_age / 60:.0f}m"
                            " after thread restart")
                    elif _EQ_THREAD_RESTART_AT is None:
                        # Strike 1: replace the hung thread in-process; the
                        # research cycle (and everything else healthy) lives on.
                        _EQ_MON_GEN += 1
                        threading.Thread(
                            target=_equity_monitor_loop, daemon=True).start()
                        _EQ_THREAD_RESTART_AT = time.time()
                        state.journal("watchdog_thread_restart",
                                      target="equity_monitor",
                                      stale_min=round(eq_age / 60))
                    # else: replacement still inside its grace window — wait.
                elif 0 <= eq_age <= WATCHDOG_STALE_S and _EQ_THREAD_RESTART_AT:
                    _EQ_THREAD_RESTART_AT = None   # healthy again — re-arm strike 1
                # A cycle stuck "running" for >3h is a hung thread regardless of the
                # clock — keep this check always-on (it blocks every future cycle).
                st = state.get_status()
                if st.get("running"):
                    st_age = _age_s(st.get("ts", ""))
                    if st_age > 3 * 3600:
                        reasons.append(f"cycle claims running for {st_age / 3600:.1f}h (hung)")
            if reasons:
                state.journal("watchdog_restart", reasons=reasons)
                os._exit(42)
        except Exception:
            continue


def _start_background_threads() -> None:
    # A fresh process means the previous one is dead — clear any stale "running"
    # status it left mid-scan/cycle. An os._exit restart that interrupts a scan
    # leaves status="screening, running=True" frozen, which reads as a STUCK scan
    # on the dashboard even though the scan actually completed/died (operator
    # caught "screen stuck at chunk 16/19" 2026-06-15). Nothing is running in THIS
    # process yet, so a running flag is stale — UNLESS a genuine FOREIGN headless
    # cycle holds the lock (then its status is legit; leave it).
    try:
        if state.get_status().get("running") and not _headless_cycle_active():
            state.set_status("idle", "reset stale running-status on app start", 0, running=False)
    except Exception:
        pass
    # Correct any poisoned trailing-stop peaks from real bars BEFORE the monitor
    # loops start acting on them (2026-07-02 poisoning fix) — a bad tick stuck as
    # a peak must not fire a false trailing stop on the first sweep after a boot.
    def _reconcile_on_boot():
        try:
            n = monitor.reconcile_peaks()
            if n:
                state.journal("peaks_reconciled_boot", corrected=n)
        except Exception:
            pass
    # Tell the operator when the fund was DOWN across a trading session. On
    # 2026-07-24 this box entered Modern Standby at 08:05 ET and did not wake
    # until 17:50 ET, so exit protection was absent for the whole session (2
    # sweeps vs 462-674 on adjacent days) and OSCR/CON stopped out ~19h late
    # against Friday's close. Nothing anywhere raised an alert: the watchdog
    # only fires while the process is ALIVE, the keepalive cannot run on a
    # sleeping machine, and the health line read a liveness beat as coverage.
    # A missed session is the one gap the operator must always be told about.
    def _report_coverage_gap():
        try:
            health.report_missed_session_coverage()
        except Exception:
            pass
    # PRE-PAY THE PUBLIC.COM COLD START (2026-08-28). broker.get_client()
    # imports public_api_sdk lazily and mints a token on construction, and that
    # first call is expensive: measured on this box 4.4-8.9 s for the SDK import
    # alone plus ~1.0 s for construct+auth, 5.4-10.0 s total, against ~45 ms for
    # every quote afterwards. Whichever caller happens to be FIRST pays it, and
    # the caller that must never pay it is a live exit order — a stop that has
    # already decided to sell should not then sit 10 s inside an import. Paid
    # here, synchronously, BEFORE the monitor threads exist, which also removes
    # the race on broker._client (get_client has no lock, so a 1s calls tick and
    # a warm-up thread could both construct one). Boot cost is bounded and safe:
    # keepalive waits up to 180 s for the app to answer.
    #
    # Same idea as market-analysis/trade_executor.py and discord-signals/
    # run_all.py, but deliberately NOT a call to live_pricing.warm_up(): that
    # warms live_pricing's OWN module-level client, which this process never
    # uses. Warming the wrong object would look green and change nothing.
    #
    # No token keeper here, unlike live_pricing. Public's access token lives
    # 600 s and the SDK re-mints it lazily on the next request, but a bare mint
    # measured 32 ms (a quote that pays one: 80 ms vs 42 ms warm) — ~38 ms once
    # per 10 min does not justify another thread in this process.
    #
    # This is also the credential SMOKE TEST. config._load_public_credentials
    # cannot raise (the offline suites import config on boxes with no secret),
    # so an empty key would otherwise stay silent — and an empty key does not
    # merely break live orders, it disables every equity stop, because the
    # marks fall back to yfinance daily closes and monitor._stale_skip refuses
    # to fire on a stale mark. Exactly that happened when the scalper moved its
    # secret out of trade_executor.py. Say it loudly, once, at startup.
    def _warm_public_client() -> None:
        if not (config.PUBLIC_API_KEY and config.PUBLIC_ACCOUNT_NUMBER):
            msg = ("Public.com credentials MISSING — live quotes are impossible, "
                   "so every equity mark falls back to a yfinance daily close, "
                   "which monitor._stale_skip refuses to stop on. Exit "
                   "protection is DOWN until this is fixed. Check "
                   "market-analysis/.secrets.json or the PUBLIC_API_KEY / "
                   "PUBLIC_ACCOUNT_NUMBER env vars.")
            state.journal("public_credentials_missing", detail=msg)
            try:
                alerts_mod.alert("Public.com credentials missing", msg)
            except Exception:
                pass
            print(f"[warm-up] {msg}")
            return
        t0 = time.time()
        try:
            broker.get_client()
            auth_ms = (time.time() - t0) * 1000
            t1 = time.time()
            # A throwaway quote finishes the TLS handshake and fills the
            # connection pool, so the first REAL quote is not the one paying it.
            n = len(broker.equity_quotes_public(["SPY"]))
            prime_ms = (time.time() - t1) * 1000
            state.journal("public_warm_up", auth_ms=round(auth_ms),
                          prime_ms=round(prime_ms), primed=n)
            print(f"[warm-up] Public client ready: auth {auth_ms:.0f} ms, "
                  f"prime {prime_ms:.0f} ms")
        except Exception as e:
            # Never block a boot on warm-up — the lazy path still works, it is
            # just slow, and the monitor's own fail-soft paths cover the rest.
            state.journal("public_warm_up_failed", error=str(e)[:200])
            print(f"[warm-up] skipped: {e}")
    _warm_public_client()

    # ...and pre-pay the INDEPENDENT price source too (2026-08-28). Every
    # protective exit routes through monitor._mark_corroborated, which asks
    # data_engine.last_price(sym) — a yfinance lookup deliberately independent
    # of the Public quote path — for a second opinion before selling. That call
    # happens INSIDE state.SLEEVE_LOCK, after the stop has already decided, so
    # its cost lands directly on the exit. Measured on this box across three
    # fresh processes: the FIRST last_price of a process costs 1.51-1.62 s
    # (yfinance session setup) and every one after it 150-250 ms. So the first
    # protective exit of every restart was paying ~1.5 s that no later exit
    # pays — and the first exit after a restart is usually the 9:30 opening-gap
    # stop, the exact case where the recorded overshoots are worst (CNC -3.47pp,
    # DVA -3.09pp, both on the session's first sweep). One throwaway lookup here
    # moves that off the exit path. The ~200 ms steady-state cost stays: it is
    # the corroboration itself, which is worth paying (see _mark_corroborated —
    # phantom marks cost this fund $1,516 before that gate existed).
    def _warm_price_corroboration() -> None:
        try:
            from firm import data_engine
            t0 = time.time()
            px = data_engine.last_price("SPY")
            state.journal("corroboration_warm_up",
                          ms=round((time.time() - t0) * 1000), priced=px > 0)
        except Exception as e:
            state.journal("corroboration_warm_up_failed", error=str(e)[:200])
    _warm_price_corroboration()

    threading.Thread(target=_report_coverage_gap, daemon=True).start()
    threading.Thread(target=_reconcile_on_boot, daemon=True).start()
    threading.Thread(target=_calls_monitor_loop, daemon=True).start()
    threading.Thread(target=_equity_monitor_loop, daemon=True).start()
    threading.Thread(target=_health_loop, daemon=True).start()
    threading.Thread(target=_watchdog_loop, daemon=True).start()
    threading.Thread(target=_preopen_ram_sweep_loop, daemon=True).start()


if __name__ == "__main__":
    # Single-instance guard BEFORE any thread starts: a second instance must
    # never run monitor threads (cross-process state races), not even for the
    # seconds it would take uvicorn to fail binding the port.
    import socket
    _probe = socket.socket()
    try:
        _probe.bind(("127.0.0.1", 8420))
        _probe.close()
    except OSError:
        print("port 8420 already in use — another instance is running; exiting")
        raise SystemExit(0)
    # Janitor: PID-unique scratch files from writers killed mid-save are
    # harmless but should not accumulate across months of restarts. Only
    # sweep files >1h old — a headless cycle may be mid-save right now.
    import time as _time
    for _pat in ("*.tmp", "*.baktmp"):
        for _stale in config.STATE_DIR.glob(_pat):
            try:
                if _time.time() - _stale.stat().st_mtime > 3600:
                    _stale.unlink()
            except OSError:
                pass
    _start_background_threads()
    print("Loch Raven Capital -> http://localhost:8420")
    uvicorn.run(app, host="127.0.0.1", port=8420, log_level="warning")
