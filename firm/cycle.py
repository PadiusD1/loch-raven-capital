"""The full firm cycle — one complete pass of the investment process.

  1. Quant screen ranks the whole universe (cheap, deterministic)
  2. Top candidates get full data packets (fundamentals + news + technicals)
  3. Research floor: five analyst desks score each candidate (Opus)
  4. Debate room: bull vs bear vs base argue it out (Opus)
  5. PM judge issues binding verdicts (Opus)
  6. Each sleeve trades its mandate through the risk desk
  7. Exit monitor sweeps all positions
"""
import os
import shutil
import subprocess
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import config
from firm import (analysts, broker, data_engine, debate, learning, monitor,
                  risk, riskbrain, sleeves, state, watchlist)


def _research_deadline_min(depth: str, n_candidates: int,
                           workers: int | None = None) -> tuple[float, dict]:
    """Self-tuning research deadline in minutes (pure: state in, number out).

    History lives in state/cycle_stats.json as
        {"<depth>": [[duration_min, n_candidates, workers?], ...]}  (last 8 normal runs)

    The math, kept deliberately simple:
      1. pace      = worst observed WORKER-minutes-per-candidate at this depth
                     (duration x workers / n — normalizing per candidate AND per
                     worker lets 2-worker history fairly size a 1-worker day;
                     2026-07-01 goal audit fix 5: pace learned at 2 workers made
                     a 1-worker day's deadline mathematically guaranteed to fire,
                     dropping held names CAT/CSCO twice)
      2. base      = pace x today's candidate count / today's workers
      3. deadline  = clamp(1.75 x base, floor[depth], cap) — 75% headroom so
                     only a true anomaly (hung CLI) can hit it, never a
                     normal slow day.
    Legacy 2-element rows are treated as workers=RESEARCH_WORKERS (the count
    they were learned at). No history yet -> the per-depth floor.
    Returns (deadline_min, basis) — basis is journaled for the audit trail.
    """
    floor = float(config.RESEARCH_DEADLINE_FLOOR_MIN.get(depth, 50))
    w_today = max(1, int(workers or getattr(config, "RESEARCH_WORKERS", 2)))
    # The cap bounds a HUNG run, not a small pool: a 1-worker day genuinely
    # needs ~2x a 2-worker day's wall time, so the anomaly bar scales with it
    # (batch review R6a: a full 15-20 name slate at 1 worker needs 156-208 min
    # vs the flat 150 cap — deadline was mathematically guaranteed to fire).
    _w_ref = max(1, int(getattr(config, "RESEARCH_WORKERS", 2)))
    # Scale UP only: a fewer-workers-than-config day honestly needs more wall
    # time; a MORE-workers day keeps the configured bound (shrinking it would
    # clamp legitimately-sized deadlines).
    cap = float(config.RESEARCH_DEADLINE_CAP_MIN) * max(1.0, _w_ref / w_today)
    # Legacy 2-element rows were learned at the worker count IN FORCE THEN —
    # 3 until the 2026-06-30 cut (config note) — not today's config value
    # (batch review R7: stamping them w=2 understated pace ~33%).
    w_legacy = max(1, int(getattr(config, "LEGACY_STATS_WORKERS", 3)))
    runs = (state.load("cycle_stats", {}) or {}).get(depth) or []
    triples = []
    for r in runs:
        if isinstance(r, (list, tuple)) and len(r) in (2, 3):
            d, n = float(r[0]), int(r[1])
            w = max(1, int(r[2])) if len(r) == 3 else w_legacy
            if d > 0 and n > 0:
                triples.append((d, n, w))
    if not triples:
        return floor, {"basis": "floor_no_history", "floor_min": floor}
    pace = max(d * w / n for d, n, w in triples)   # worst worker-min per candidate
    base = pace * max(1, n_candidates) / w_today
    deadline = min(max(config.RESEARCH_DEADLINE_MULT * base, floor), cap)
    return deadline, {"basis": "history", "runs": len(triples),
                      "worst_worker_min_per_candidate": round(pace, 2),
                      "workers_today": w_today,
                      "base_min": round(base, 1),
                      "mult": config.RESEARCH_DEADLINE_MULT,
                      "floor_min": floor, "cap_min": cap}


def _record_research_duration(depth: str, minutes: float, n_candidates: int,
                              workers: int | None = None,
                              real_verdicts: int | None = None) -> None:
    """Remember a research phase that completed NORMALLY (no deadline hit).

    Timed-out or empty runs are never recorded — a hung run must not teach
    the deadline to grow. ZERO-real-verdict runs are never recorded either
    (2026-07-01 goal audit fix 5): an all-degraded cycle fails FAST (CLI calls
    time out immediately), so its pace looks anomalously quick — the 06-30
    degraded runs taught a 1.51 min/candidate pace that made the next real
    cycle's deadline unmeetable. Anomalies teach nothing, in either direction.
    Keeps the last 8 [duration_min, n_candidates, workers] rows per depth.
    """
    if minutes <= 0 or n_candidates <= 0:
        return
    # <50% real is still a degraded-dominated run (batch review R10: a
    # 1-real/13-degraded cycle records nearly the same falsely-fast pace as
    # an all-degraded one — degraded calls fail in seconds, real ones take
    # minutes, so the wall time reflects whichever dominated)
    if real_verdicts is not None and real_verdicts < 0.5 * n_candidates:
        state.journal("research_pace_skipped_degraded", depth=depth,
                      minutes=round(minutes, 2), n=n_candidates,
                      real=real_verdicts)
        return
    stats = state.load("cycle_stats", {}) or {}
    runs = list(stats.get(depth) or [])
    runs.append([round(minutes, 2), int(n_candidates),
                 max(1, int(workers or getattr(config, "RESEARCH_WORKERS", 2)))])
    stats[depth] = runs[-config.RESEARCH_STATS_KEEP:]
    state.save("cycle_stats", stats)


def _effective_workers() -> int:
    """Research-floor concurrency scaled to CURRENTLY-available RAM so the fund
    coexists with whatever else is on the box. Each worker fans out to several
    `claude -p` subprocesses; on a RAM-starved box too many at once make every
    Opus call time out into degraded mode (operator 2026-06-30). Collapse to 1
    worker ONLY when free RAM is at/below the scan floor — slower then, but REAL
    verdicts beat fast degraded ones. Never below 1; never above
    config.RESEARCH_WORKERS."""
    base = int(getattr(config, "RESEARCH_WORKERS", 2))
    try:
        import psutil
        avail_gb = psutil.virtual_memory().available / 1e9
    except Exception:
        return base
    # Operator 2026-07-07: keep the FULL worker width until free RAM is at/below
    # the scan-downgrade floor (config.SCAN_DOWNGRADE_RAM_GB, 1.0 GB); only then
    # collapse to a single worker. (Was a continuous (avail-HEADROOM)/PER_WORKER
    # taper that dropped to 1 worker around 2.5-4 GB free — i.e. almost every cycle
    # on this ~2 GB-free box — keeping the fund permanently thinned to 1 worker.)
    floor = float(getattr(config, "SCAN_DOWNGRADE_RAM_GB", 1.0))
    return base if avail_gb > floor else 1


def _fire_janitor() -> None:
    """Trigger the OS level Claude session janitor task. Windows only. On the
    VPS there is no desktop app and no janitor, systemd owns lifetimes."""
    if os.name != "nt" or not shutil.which("schtasks"):
        return
    subprocess.run(["schtasks", "/Run", "/TN",
                    str(getattr(config, "RAM_RECOVERY_TASK", "Claude Session Janitor"))],
                   capture_output=True, timeout=30,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _preflight_ram_recovery(avail_gb: float | None = None,
                            allow_in_sandbox: bool = False) -> None:
    """RAM-starvation self-defense (operator 2026-07-10: "fix the RAM
    starvation"). Twice this week the research floor launched into a starved
    box where every Opus call died to quant-only degraded (07-09 wedge at
    0.7 GB free; 07-10 13/13 + 17/17 failed) — the hog each time was FINISHED
    Claude sessions idling at ~0% CPU. Before sizing the floor, if free RAM
    is below config.RAM_RECOVERY_GB, fire the (RAM-pressure-aware) session
    janitor and give it a moment to reap, then journal before/after so the
    recovery is observable. Fail-soft everywhere: no psutil, no task, no
    permission -> proceed exactly as before (the worker/depth downgrades
    below still bound the damage). The janitor never touches this process
    (python, not a claude session) nor python-spawned research workers.

    SANDBOX GUARD (2026-08-19, operator: "lock raven capital needs to be
    fixed"). This mirrors the guard _post_cycle_ram_clean has carried since
    it was written; this twin never got one, and that asymmetry is what broke
    the regression suite. The suites drive HUNDREDS of simulated cycles under
    a tempdir STATE_DIR, and every one of them fired the host-wide
    `schtasks /Run` janitor and then slept RAM_RECOVERY_WAIT_S (25s) in-process.
    On this box (~1.0 GB free, permanently under the 2.0 GB trigger) that was
    an unconditional 25s per simulated cycle, so tests/test_firm.py stopped
    finishing inside scripts/self_test.py's 900s budget: state/self_test.json
    has read `crashed: ... timed out after 900 seconds` since 2026-08-17 while
    'self-test cadence' stayed GREEN because it grades recency, not result.
    A SIMULATED cycle must never reap the host's real sessions — same reason,
    same rule, both twins. Unit tests that mean to exercise the decision logic
    opt in explicitly with allow_in_sandbox=True."""
    try:
        if (not allow_in_sandbox
                and str(config.STATE_DIR) != str(config.BASE_DIR / "state")):
            return
        import psutil as _ps
        before = (avail_gb if avail_gb is not None
                  else _ps.virtual_memory().available / 1e9)
        if before >= float(getattr(config, "RAM_RECOVERY_GB", 2.0)):
            return
        _fire_janitor()
        import time as _t
        _t.sleep(float(getattr(config, "RAM_RECOVERY_WAIT_S", 25)))
        after = _ps.virtual_memory().available / 1e9
        state.journal("ram_recovery", before_gb=round(before, 2),
                      after_gb=round(after, 2))
    except Exception:
        state.journal("ram_recovery_failed")


def _post_cycle_ram_clean() -> None:
    """Operator 2026-07-17: "after every cycle just ram clean" — the research
    floor's Opus fan-out leaves working sets and stray helpers behind; return
    the box to headroom the moment the cycle ends instead of waiting for the
    next 8:25/9:40 slot. Runs the same scripts/ram_cleaner.py the scheduled
    task uses (fund tree resolved live and protected; kills only verified
    leaks; journals its own before/after). Fail-soft: a cleaner error must
    never taint a completed cycle's result."""
    try:
        # Sandboxed suites repoint STATE_DIR (test_firm / test_week_sim run
        # hundreds of simulated cycles) — a simulated cycle must never spawn
        # host-wide cleanup subprocesses. Real books only.
        if str(config.STATE_DIR) != str(config.BASE_DIR / "state"):
            return
        import subprocess
        import sys as _sys
        subprocess.run(
            [_sys.executable, str(config.BASE_DIR / "scripts" / "ram_cleaner.py")],
            capture_output=True, text=True, timeout=180,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),)
    except Exception:
        state.journal("post_cycle_ram_clean_failed")


def _effective_depth(depth: str) -> str:
    """Downgrade 'full' -> 'standard' when RAM is tight. Full fans each workup
    into ~9 Opus calls (5 desks concurrent + bull/bear/base + judge); on a starved
    box they ALL time out into QUANT-ONLY degraded mode. Standard folds them into
    ~2 sequential calls, so the committee still returns a REAL verdict instead of a
    fake degraded one (operator 2026-06-30). Only ever downgrades full."""
    if depth != "full":
        return depth
    try:
        import psutil
        avail_gb = psutil.virtual_memory().available / 1e9
    except Exception:
        return depth
    # <= (not <) so depth, worker count and the health line all step down at the
    # SAME 1.0 GB boundary (operator 2026-07-07: "1 GB available or less").
    return ("standard"
            if avail_gb <= float(getattr(config, "DEPTH_DOWNGRADE_RAM_GB", 1.0))
            else depth)


# --- Feature B: research-floor stall observability (operator 2026-06-25) --------
# The whole point is to distinguish "still thinking" from "stalled" from
# "quota out" WITHOUT adding Opus calls and WITHOUT killing the cycle (the
# self-tuning deadline already bounds it). A daemon watcher reads a shared
# progress dict that the as_completed loop updates on every completion; because
# it's a separate thread it keeps reporting even while the main loop is parked
# inside as_completed waiting on a hung worker.
_STALL_HEAD = {
    "progressing": "research progressing",
    "stalled": "⚠ research STALLED",
    "quota_out": "⚠ Opus quota exhausted",
    "complete": "research floor complete",
}


def _classify_floor(progress: dict) -> tuple[str, float, float]:
    """Pure read -> (state, seconds_since_last_completion, elapsed_seconds).

    quota_out wins over stalled: when the subscription is dry every call fails
    FAST (so completions keep landing, no stall) but the verdicts are degraded —
    the operator must see the quota cause, not a misleading 'progressing'."""
    from firm import brain
    now = time.monotonic()
    done, total = progress["done"], progress["total"]
    since_last = now - progress["last_ts"]
    elapsed = now - progress["start_ts"]
    if done >= total:
        return "complete", since_last, elapsed
    if brain.quota_recently_hit():
        return "quota_out", since_last, elapsed
    if since_last >= progress["stall_gap_s"]:
        return "stalled", since_last, elapsed
    return "progressing", since_last, elapsed


def _floor_detail(progress: dict, klass: str, since_last: float) -> str:
    done, total = progress["done"], progress["total"]
    head, dl = _STALL_HEAD.get(klass, klass), progress["deadline_min"]
    if klass == "quota_out":
        return (f"{head} — {done}/{total} workups; calls returning quant-only "
                "fallback, so the cycle will pause and retry on quota reset")
    if klass == "stalled":
        return (f"{head} — no workup completed in {since_last / 60:.0f}m "
                f"({done}/{total} done); likely a hung Opus call. The {dl:.0f}m "
                "research deadline will bound the cycle.")
    if klass == "complete":
        return f"{head} — {done}/{total} workups"
    return (f"{head} — {done}/{total} workups, last completion "
            f"{since_last:.0f}s ago (deadline {dl:.0f}m)")


def _run_floor_watch(progress: dict, stop_evt: "threading.Event") -> None:
    """Heartbeat the research floor until stop_evt: classify it, push the verdict
    to the dashboard status + a machine-readable state/research_progress.json,
    journal only on STATE TRANSITIONS, and alert ONCE on a sustained stall."""
    prev = None
    stalled_ticks = 0
    alerted = False
    while not stop_evt.is_set():
        try:
            klass, since_last, elapsed = _classify_floor(progress)
            detail = _floor_detail(progress, klass, since_last)
            pct = 15 + 55 * progress["done"] / max(1, progress["total"])
            state.set_status("research", detail, pct)
            state.save("research_progress", {
                "ts": datetime.now(timezone.utc).isoformat(),
                "state": klass, "detail": detail,
                "done": progress["done"], "total": progress["total"],
                "since_last_s": round(since_last, 1),
                "elapsed_s": round(elapsed, 1),
                "stall_gap_s": round(progress["stall_gap_s"], 1),
                "deadline_min": progress["deadline_min"], "depth": progress["depth"]})
            if klass != prev:
                state.journal("research_floor_state", state=klass,
                              done=progress["done"], total=progress["total"],
                              since_last_s=round(since_last, 1),
                              elapsed_min=round(elapsed / 60, 1))
                prev = klass
            if klass == "stalled":
                stalled_ticks += 1
                if stalled_ticks >= 2 and not alerted:
                    from firm import alerts
                    alerts.alert(
                        "RESEARCH FLOOR STALLED",
                        f"No workup has completed in {since_last / 60:.0f} minutes "
                        f"({progress['done']}/{progress['total']} done). This is a "
                        "stall, not slow progress — likely a hung Opus call. The "
                        "research deadline will bound the cycle; check CLI / "
                        "connectivity if it repeats.", "critical")
                    alerted = True
            else:
                stalled_ticks = 0
        except Exception:
            pass
        stop_evt.wait(config.STALL_TICK_S)


def _position_context(ticker: str) -> dict | None:
    """What the firm ALREADY HOLDS in this name, per sleeve — injected into the
    packet so the judge sees ownership (2026-07-01 purpose review, rec #5a: the
    latest AMD verdict said 'the firm does not hold it' while AMD sat in TWO
    sleeves — the leader-add guidance and KEEP grading were inert on every
    held-but-unbenched name)."""
    out = {}
    try:
        for sl in config.SLEEVES:
            positions = state.get_sleeve(sl).get("positions") or {}
            # Equity sleeves key positions by ticker; the calls sleeve keys them
            # by the OPTION symbol (OSI) and carries the underlying separately —
            # a plain positions.get(ticker) is blind to a call-held underlying,
            # which recreated the exact AMD 'firm does not hold it' failure on
            # the LEVERAGED book (2026-07-02 review). Match either shape.
            p = positions.get(ticker)
            if p is None:
                p = next((q for q in positions.values()
                          if isinstance(q, dict) and q.get("underlying") == ticker
                          and q.get("instrument") == "option"
                          and not q.get("cash_equitization")), None)
            if not p or p.get("cash_equitization"):
                continue
            entry, last = p.get("entry_price") or 0, p.get("last_price") or 0
            days = None
            try:
                days = (datetime.now(timezone.utc)
                        - datetime.fromisoformat(p.get("opened_at", ""))).days
            except (ValueError, TypeError):
                pass
            _row = {
                "quantity": p.get("quantity"), "entry_price": entry,
                "unrealized_pct": round((last / entry - 1) * 100, 1)
                if entry and last else None,
                "days_held": days,
                "trail_stop_pct": p.get("trail_stop_pct"),
            }
            if p.get("instrument") == "option":
                # Name the instrument so the judge reads the exposure correctly:
                # a long call is a leveraged, decaying, expiring bet, not stock.
                _row["instrument"] = "long_call"
                _row["strike"] = p.get("strike")
                _row["expiration"] = p.get("expiration")
            out[sl] = _row
    except Exception:
        return None
    if not out:
        return None
    return {"note": "The firm ALREADY OWNS this name (details per sleeve). "
                    "Judge it as a HOLDING: your HOLD means KEEP, your BUY "
                    "with a higher target weight means ADD ON STRENGTH, your "
                    "SELL exits it.", **out}


def _previous_verdict(ticker: str, px_now=None) -> dict | None:
    """The judge's own MOST RECENT real verdict on this name — injected into
    the packet so re-research is DIFFERENTIAL, not amnesiac (cognition audit
    2026-07-01, #3: the judge re-derived the same AMD thesis ~15 consecutive
    cycles as if for the first time; its 58↔68 conviction wobble was
    regeneration noise the sleeves sized on). Degraded fallbacks are skipped
    (fabricated priors must not anchor a real committee)."""
    try:
        history = state.load("research", {}).get("history", [])
        for h in reversed(history):
            v = (h.get("verdicts") or {}).get(ticker)
            if not v or v.get("degraded"):
                continue
            ctx = (h.get("context") or {}).get(ticker) or {}
            px_then = ctx.get("px")
            out = {"rating": v.get("rating"), "conviction": v.get("conviction"),
                   "date": (h.get("asof") or "")[:10],
                   "thesis_then": str(v.get("thesis") or "").split(". ")[0][:200],
                   "note": "Your own last real verdict on this name. Judge what "
                           "CHANGED since — do not re-derive from scratch, and "
                           "do not silently flip without naming why."}
            if isinstance(px_then, (int, float)) and px_then > 0:
                out["price_then"] = px_then
                if isinstance(px_now, (int, float)) and px_now > 0:
                    out["price_change_pct"] = round((px_now / px_then - 1) * 100, 1)
            return out
    except Exception:
        pass
    return None


def _research_one(ticker: str, kind: str, tech: dict, quant: float, depth: str,
                  watch_note: dict | None = None, regime: dict | None = None,
                  macro: dict | None = None) -> dict:
    packet = data_engine.build_packet(ticker, tech, quant)
    from firm import macro_calendar
    packet["macro_calendar_next_7d"] = macro_calendar.upcoming(7)
    _pc = _position_context(ticker)
    if _pc:
        packet["position_context"] = _pc
    _pv = _previous_verdict(ticker, (packet.get("technicals") or {}).get("price"))
    if _pv:
        packet["previous_verdict"] = _pv
    # Firm-wide macro read — every candidate judged against ONE coherent
    # picture of the tape, volatility, sector leadership, and the forward
    # (and just-passed) Fed/CPI calendar
    # A DOWN feed is not a quiet tape: mark the outage explicitly instead of
    # silently omitting the key, so the macro desk (whose whole mandate is the
    # regime) and the fundamental desk can tell 'no regime read available' from
    # 'neutral regime' — same doctrine the news path already follows
    # (2026-07-02 review). macro/regime come in as {} on a fetch failure.
    if macro:
        packet["macro_brief"] = macro
    elif macro is not None:
        packet["macro_brief"] = {"note": "(MACRO BRIEF UNAVAILABLE this cycle — "
                                 "the source errored; judge WITHOUT a firm-wide "
                                 "regime read and do NOT assume a calm tape.)"}
    # Street research (operator 2026-07-16: "these reports help the fund make
    # the best decisions and stay informed"): institutional reports the
    # operator loaded ride every packet — fundamental/sentiment/macro desks
    # carry it in their views, the judge sees everything. An absent key means
    # an empty shelf: desks are told nothing rather than handed an empty
    # section to confabulate around (same doctrine as the news tape).
    try:
        from firm import street_research
        _sr = street_research.digest_context()
        if _sr:
            packet["street_research"] = _sr
    except Exception:
        pass
    if regime:
        sec = (packet.get("fundamentals") or {}).get("sector") or ""
        packet["sector_regime"] = {
            "market_leading_sectors": regime.get("leading", []),
            "market_lagging_sectors": regime.get("lagging", []),
            "this_stocks_sector": sec,
            "sector_status": ("LEADING" if sec in regime.get("leading", [])
                              else "LAGGING" if sec in regime.get("lagging", [])
                              else "neutral"),
        }
    elif regime is not None:
        packet["sector_regime"] = {"note": "(SECTOR REGIME UNAVAILABLE this cycle "
                                   "— the source errored; do NOT infer sector "
                                   "leadership.)"}
    if watch_note:
        # Provenance must be TRUE: watch_notes is populated both by genuine bench
        # PROMOTIONS and by the sector read-through path (peers, INCLUDING held
        # names, that were never benched). Telling a read-through peer it 'was on
        # the watchlist and was just promoted' is a false frame that contradicts
        # its own adjacent 'why' field (2026-07-02 review) — branch on it.
        if watch_note.get("readthrough"):
            _wnote = ("SECTOR READ-THROUGH: a sector bellwether just reported — "
                      "judge the spillover to THIS name. It was not necessarily "
                      "on the watchlist; weigh whether the read-through applies.")
        elif _pc:
            _wnote = ("The firm already holds this name (see position_context); "
                      "this context note accompanies its scheduled re-review.")
        else:
            _wnote = ("This name was on the firm's WATCHLIST and was just promoted "
                      "because its setup changed — judge whether the wave is "
                      "actually forming or it's still early.")
        packet["watchlist_context"] = {"note": _wnote, **watch_note}
    research = {} if depth == "fast" else analysts.run_research(packet, depth)
    result = debate.run_debate_and_verdict(packet, research, depth)
    return {
        "ticker": ticker, "kind": kind,
        "asof": datetime.now(timezone.utc).isoformat(),
        "packet": packet, "research": research,
        "composite": analysts.composite_score(research) if research else None,
        "cases": result["cases"], "verdict": result["verdict"],
    }


# --- Mid-pass research checkpoint (operator 2026-07-21) ------------------------
# A restart that lands mid-research (watchdog kill, keepalive relaunch, code
# deploy) used to throw away every COMPLETED workup: the 07-21 forced reload
# killed the 9:45 cycle at 8/9 and the catch-up re-ran all nine Opus workups
# from zero. The pass was crash-SAFE (catch-up re-runs) but crash-EXPENSIVE.
# Every completed workup now persists to state/research_checkpoint the moment
# it lands; a NEW pass consumes matching entries (same ET day, age <=
# RESEARCH_CHECKPOINT_MAX_AGE_H, same-or-DEEPER depth, same kind, non-degraded)
# and only researches the remainder. The checkpoint is CLEARED whenever a pass
# concludes normally — a completed pass must never feed a later same-day pass
# (re-judging every cycle is by design; cooldowns, not this cache, govern
# verdict reuse). Entries never checkpoint degraded results (a failed workup
# must be retried fresh, never resumed as if it were research).
_DEPTH_RANK = {"fast": 0, "standard": 1, "full": 2}


def _checkpoint_workup(t: str, result: dict, depth: str) -> None:
    """Persist one COMPLETED workup. Fail-soft: a checkpoint hiccup must never
    take down the research floor it exists to protect."""
    try:
        v = (result or {}).get("verdict") or {}
        if v.get("degraded"):
            return
        ck = state.load("research_checkpoint", {}) or {}
        w = ck.setdefault("workups", {})
        w[t] = {"asof": result.get("asof")
                or datetime.now(timezone.utc).isoformat(),
                "depth": depth, "result": result}
        if len(w) > 40:                      # bound the file; oldest age out
            for k in sorted(w, key=lambda k: w[k].get("asof", ""))[:len(w) - 40]:
                w.pop(k, None)
        state.save("research_checkpoint", ck)
    except Exception:
        pass


def _resume_from_checkpoint(candidates: list, depth: str) -> dict:
    """{ticker: workup_result} for candidates whose interrupted-pass checkpoint
    entry is still trustworthy: same ET trading day, younger than
    RESEARCH_CHECKPOINT_MAX_AGE_H, researched at the SAME OR DEEPER depth than
    this pass (a standard-depth leftover must not silently degrade a full
    cycle), same kind, and non-degraded. Anything else re-researches fresh."""
    out: dict = {}
    try:
        entries = (state.load("research_checkpoint", {}) or {}).get("workups") or {}
        if not entries:
            return out
        from zoneinfo import ZoneInfo
        now = datetime.now(timezone.utc)
        today_et = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        max_h = float(getattr(config, "RESEARCH_CHECKPOINT_MAX_AGE_H", 6.0))
        for t, kind in candidates:
            e = entries.get(t)
            if not isinstance(e, dict):
                continue
            r = e.get("result") or {}
            if (r.get("kind") != kind
                    or ((r.get("verdict") or {}).get("degraded"))):
                continue
            if (_DEPTH_RANK.get(str(e.get("depth")), -1)
                    < _DEPTH_RANK.get(depth, 99)):
                continue
            try:
                ats = datetime.fromisoformat(str(e.get("asof", "")))
                age_h = (now - ats).total_seconds() / 3600
                same_day = (ats.astimezone(ZoneInfo("America/New_York"))
                            .date().isoformat() == today_et)
            except (ValueError, TypeError):
                continue
            if age_h > max_h or not same_day:
                continue
            out[t] = r
        if out:
            state.journal("research_resumed_from_checkpoint",
                          names=sorted(out), n=len(out), depth=depth)
    except Exception:
        return {}
    return out


def _clear_research_checkpoint() -> None:
    try:
        state.save("research_checkpoint", {})
    except Exception:
        pass


CORE_ADVISORY_WEEKDAY = 0     # Monday (ET): strategic core faces the committee
RESEARCH_COOLDOWN_DAYS = 3    # a fresh HOLD/SELL on a NON-held name stands
                              # this long before the committee re-judges it


def _earnings_landed(date_str: str | None, session: str | None,
                     today, after_close: bool = False) -> bool:
    """Has the print ACTUALLY happened by the time this cycle runs? Date-only
    is wrong: an after-close (AMC) report dated today does not exist at the
    9:45 AM cycle (numbers come at 4 PM). Landed = (date in the past) OR
    (date is today AND it was a before-open print, already out this morning).
    Unknown/intraday timing on today's date is treated as NOT yet out — never
    reopen a name assuming numbers that may not exist (operator: date+time)."""
    from datetime import date as _date
    if not date_str:
        return False
    try:
        edd = _date.fromisoformat(date_str)
    except (ValueError, TypeError):
        return False
    if edd < today:
        return True
    if edd == today:
        if session == "BMO":
            return True
        if after_close:      # post-close sweep: an AMC print is now out too
            return session in ("AMC", "intraday")
    return False


def _core_advisory_names(weekday: int | None = None) -> list[str]:
    """Weekly advisory pass (operator-approved 2026-06-12): the strategic
    core (VOO/QQQ/GLD) goes in front of the committee every Monday. The
    verdicts inform the operator, land in the journal and research history,
    and get GRADED by the verdict report card like every other judgment —
    but never trigger trades (core SELLs stay suppressed by design; the
    core's protection remains the mechanical rebalance + kill switch)."""
    from zoneinfo import ZoneInfo
    wd = (datetime.now(ZoneInfo("America/New_York")).weekday()
          if weekday is None else weekday)
    if wd != CORE_ADVISORY_WEEKDAY:
        return []
    names: set[str] = set()
    for cfg in config.SLEEVES.values():
        names |= set(cfg.get("core_weights") or {})
    # Dynamic seats face the committee too (operator 2026-07-16): the CURRENT
    # core slots + parking ETF join the Monday advisory pass, so a seated ETF
    # can actually EARN the SELL that unseats it.
    try:
        from firm import allocation
        names |= set(allocation.core_weights())
        names.add(allocation.parking_symbol("alpha_stocks"))
    except Exception:
        pass
    return sorted(n for n in names if n)


def _is_extended_momentum_hold(v: dict, ctx_t: dict, age_d: int) -> bool:
    """An EXTENDED-momentum HOLD qualifies for the macro-immune cooldown lane.

    The committee's verdict on a parabolic leader is, cycle after cycle, the
    same: "too extended — wait for a pullback / the print." A generic macro
    print (CPI/PCE/jobs) CANNOT change that, so re-judging the name on every
    macro drop is cost without information — and that macro-reopen is exactly
    what defeats the 3-day cooldown daily today. This identifies those names.
    Guards (each closes a reviewed failure mode):
      - HOLD only — a SELL a macro print could flip still reopens normally;
      - momentum factor >= MOMENTUM_LANE_MOM_MIN — the parabolic-leader
        signature, read from context.factors already persisted each cycle in
        the research history ("context"); no new data, RAM-neutral;
      - NOT a degraded-mode verdict — a quant-only "no qualitative research"
        verdict is the least trustworthy in the system; never grant it a
        macro-immune dwell;
      - bounded dwell — past MOMENTUM_LANE_MAX_DWELL_CYCLES (~age in days) the
        name falls through to a normal full re-research. With the default
        (== RESEARCH_COOLDOWN_DAYS) this coincides with the ordinary cooldown
        expiry, so the lane only ever changes momentum re-research from DAILY
        (macro-defeated) to once-per-cooldown-window; set it LOWER than
        RESEARCH_COOLDOWN_DAYS to force these names back on a tighter cadence.
    """
    if v.get("rating") != "HOLD":
        return False
    if v.get("degraded"):
        return False
    mom = (ctx_t.get("factors") or {}).get("momentum")
    # TUNED read (fleet re-grade action 4): this is the lever's sole consumer —
    # reading config statically here left 228 momentum_lane_skip samples
    # feeding a lever that could never move behavior.
    if not (isinstance(mom, (int, float))
            and mom >= risk.tuned_cap("MOMENTUM_LANE_MOM_MIN")):
        return False
    if age_d >= config.MOMENTUM_LANE_MAX_DWELL_CYCLES:
        return False                  # staleness escape -> normal full re-research
    return True


def _has_nonmacro_reopen(t: str) -> bool:
    """True iff the Bench can summon this name back WITHOUT a macro print: a
    pending earnings_date OR a structured technical reopen_trigger
    (pullback/RSI/SMA/reclaim/below) it watches every cycle. If a name's ONLY
    path back was the macro branch, it must NOT be laned — macro-silencing it
    would orphan it. Fail-CLOSED (don't lane) is the safe default here. Read-
    only (no write) so the single-writer invariant is untouched."""
    try:
        e = (state.load("watchlist", {}) or {}).get(t) or {}
    except Exception:
        return False
    if e.get("earnings_date"):
        return True
    # Compound-aware + fail-CLOSED on a macro/earnings `after`-gated technical leg
    # (a name whose only path is behind a not-yet-landed gate must not be laned).
    from firm.watchlist import _nonmacro_reopen_ok
    return _nonmacro_reopen_ok(e.get("reopen_trigger"))


def _backfill_candidates(candidates: list, tier_pool: dict, held: set,
                         tech: dict) -> tuple[list, list, list]:
    """Refill exploratory stock slots the cooldown / momentum-lane vacated with
    the next FRESH name per cap tier — not held, not laned/recently-judged — so
    the committee explores MORE names, never fewer (operator 2026-06-23: "day to
    day exploring more names, don't shorten how many get brought up"). Backfilled
    names ride ON TOP of the held re-underwrite + bench reopens. Returns a NEW
    candidates list plus what was added and any tier left short (pool exhausted).
    Only FRESH (non-held) survivors count toward a tier's explore target, so a
    held name occupying a slot never crowds out exploration."""
    out = list(candidates)
    cur = {t for t, k in out if k == "stock"}
    added: list[str] = []
    short: list[str] = []
    for tier, pool in (tier_pool or {}).items():
        target = config.TIER_SLOTS.get(tier, 0)
        have = sum(1 for t in cur if t in pool and t not in held)
        for t in pool:
            if have >= target:
                break
            if t in cur or t in held or t not in tech:
                continue
            if _cooldown_state(t):            # skip laned/recently-judged -> want FRESH
                continue
            out.append((t, "stock"))
            cur.add(t)
            added.append(t)
            have += 1
        if have < target:
            short.append(f"{tier}:{have}/{target}")
    return out, added, short


def _cooldown_state(t: str) -> dict | None:
    """Does a recent verdict on this name still stand? A HOLD/SELL younger
    than RESEARCH_COOLDOWN_DAYS keeps its cooldown — re-judging the same
    setup daily is cost without information (operator request 2026-06-12).
    The cooldown REOPENS the moment it should: when the earnings the judge
    was waiting for have been released (re-judged that day with fresh news),
    or when the verdict simply ages out. BUY-family verdicts never cool down
    (an actionable rating needs fresh sizing every cycle)."""
    history = state.load("research", {}).get("history", [])
    for h in reversed(history):
        v = (h.get("verdicts") or {}).get(t)
        if not v:
            continue
        if v.get("rating") in ("BUY", "STRONG_BUY"):
            return None
        if v.get("degraded"):
            # A DEGRADED verdict is a quant-only coin flip (graded 50% correct,
            # -6.6% avg forward excess) — it must not block a REAL committee
            # look for 3 days (2026-07-01 purpose review, rec #6iv). 24% of
            # all verdicts were degraded; each imposed a full cooldown.
            return None
        try:
            age_d = (datetime.now(timezone.utc)
                     - datetime.fromisoformat(h["asof"])).days
        except (ValueError, TypeError, KeyError):
            return None
        # THESIS-GATED bench cooldown (operator 2026-07-09: "a name already on
        # the watchlist must not get re-rated when the thesis itself hasn't
        # triggered the re-run"). A benched HOLD with its OWN name-specific
        # re-look path (pending earnings or a non-macro technical trigger —
        # the same _has_nonmacro_reopen the momentum lane guards on) is
        # summoned back by the Bench the moment that thesis completes
        # (watchlist.promotions below, which BYPASSES this cooldown), so the
        # age timer alone must not re-rate it: each timer re-rate burned a
        # full committee run to conclude "still HOLD" AND fed the max-recheck
        # counter that "judged out" PENG right before its +31.7% earnings pop
        # (IG 2026-07-09 #7 overlooked winners). Its own landed earnings
        # still reopens it below; catalyst-only (macro-thesis) names keep the
        # macro reopen. BOUNDED (no unbounded state, house doctrine): past
        # BENCH_GATE_MAX_DWELL_DAYS the name falls through to ONE fresh full
        # look, and the Bench's own age ceiling bounds the seat itself (off
        # the bench -> the normal age-out resumes at the next look).
        _bench_hold = (bool(getattr(config, "BENCH_THESIS_GATE", True))
                       and v.get("rating") == "HOLD"
                       and age_d < float(getattr(config, "BENCH_GATE_MAX_DWELL_DAYS", 21))
                       and _has_nonmacro_reopen(t))
        if age_d >= RESEARCH_COOLDOWN_DAYS and not _bench_hold:
            return None
        today = datetime.now(timezone.utc).date()
        try:
            v_date = datetime.fromisoformat(h["asof"]).date()
        except (ValueError, TypeError):
            v_date = today
        ctx_t = (h.get("context") or {}).get(t) or {}
        ed = ctx_t.get("earnings")
        if ed and _earnings_landed(ed, ctx_t.get("earnings_session"), today):
            return None             # the print is OUT — re-judge with actuals
        # MOMENTUM LANE (operator 2026-06-23): an EXTENDED-momentum HOLD is
        # macro-IMMUNE — a generic CPI/FOMC print can't flip "too extended, wait
        # for a pullback", so it must not drag the name back into the full
        # committee. (The earnings branch above still reopens it on its OWN
        # print; the Bench still reopens it on its OWN pullback/RSI/SMA trigger.)
        # GUARD: only lane a name the Bench can summon back without a macro
        # print, so macro-silencing can never orphan it.
        _lane = (bool(config.MOMENTUM_LANE)
                 and _is_extended_momentum_hold(v, ctx_t, age_d)
                 and _has_nonmacro_reopen(t))
        if not _lane:
            if _bench_hold:
                # Thesis-gated (see above): the Bench watches this name's own
                # trigger every cycle — a generic macro print is not ITS
                # thesis, so it neither ages out nor macro-reopens here. A
                # catalyst-LEG trigger still macro-reopens via the watchlist
                # promotion path, and a landed earnings reopened it above.
                return {"rated": v.get("rating"), "age_days": age_d,
                        "lane": "bench",
                        "re_look": "own bench trigger / earnings"}
            # Any macro print since the verdict reopens it — rate/inflation-
            # sensitive theses must be re-read after the data drops, and "fed
            # numbers aren't only FOMC" (operator 2026-06-14): FOMC/CPI/jobs.
            from firm import macro_calendar
            if macro_calendar.landed_since(v_date, today):
                return None
        return {"rated": v.get("rating"), "age_days": age_d,
                **({"lane": "momentum"} if _lane else {}),
                "re_look": (f"earnings {ed}" if ed else
                            ("own setup trigger / bench reopen" if _lane
                             else f"in {max(RESEARCH_COOLDOWN_DAYS - age_d, 0)}d"))}
    return None


def _last_verdict_date(t: str):
    """The date of the most recent committee verdict on this name (from the
    research history), or None. Used by the sector read-through to stay
    idempotent — a peer already judged AFTER a bellwether's print is not
    force-reopened again, but one that hasn't been (incl. after a degraded
    cycle that produced no real verdict) still fires."""
    history = state.load("research", {}).get("history", [])
    for h in reversed(history):
        if (h.get("verdicts") or {}).get(t):
            try:
                return datetime.fromisoformat(h["asof"]).date()
            except (ValueError, TypeError, KeyError):
                return None
    return None


def _readthrough_result(surprise_pct) -> str:
    if isinstance(surprise_pct, (int, float)):
        if surprise_pct > 1:
            return f"a BEAT (EPS surprise +{surprise_pct:.0f}%)"
        if surprise_pct < -1:
            return f"a MISS (EPS surprise {surprise_pct:.0f}%)"
    return "an in-line print"


def _inject_cohort_readthrough(candidates: list, tech: dict, screen: dict,
                               held: set, watch_notes: dict, today) -> tuple[list, list]:
    """Feature C (operator 2026-06-25): when a cohort BELLWETHER just reported,
    force-reopen its PEERS this cycle (cooldown overridden) with a read-through
    note, so the committee judges them on the sector signal NOW instead of
    waiting for each peer's own print. Cheap (one recent_earnings probe per
    bellwether), idempotent (a peer judged since the print is skipped),
    bounded (READTHROUGH_MAX_PEERS), and fail-soft. Returns (candidates, added).
    """
    if not getattr(config, "EARNINGS_READTHROUGH", False):
        return candidates, []
    in_cands = {c for c, _ in candidates}
    cap = int(getattr(config, "READTHROUGH_MAX_PEERS", 6))
    window = int(getattr(config, "READTHROUGH_WINDOW_DAYS", 4))
    added: list[str] = []
    handled: set[str] = set()
    events: list[dict] = []
    for cohort, spec in (getattr(config, "EARNINGS_COHORTS", {}) or {}).items():
        if len(added) >= cap:
            break
        for B in (spec.get("bellwethers") or []):
            try:
                re_b = data_engine.recent_earnings(B)
            except Exception:
                continue
            # recent_earnings() returns the print date under "reported_on" (+ a
            # precomputed "days_ago"); "date" is the NEXT-earnings key, a
            # DIFFERENT function — using it here silently inerts the feature.
            # (verified live 2026-06-25: recent_earnings('MU') -> surprise_pct
            # 21.25 but no "date" key; the real key is "reported_on".)
            rdate = (re_b or {}).get("reported_on")
            days_ago = (re_b or {}).get("days_ago")
            if not rdate:
                continue
            try:
                rd = datetime.fromisoformat(str(rdate)[:10]).date()
            except (ValueError, TypeError):
                continue
            _age = days_ago if isinstance(days_ago, (int, float)) else (today - rd).days
            if _age > window or rd > today:
                continue                              # not a fresh print
            result = _readthrough_result(re_b.get("surprise_pct"))
            # The peer's desks must judge the read-through on the NUMBERS AND the
            # qualitative call — not just a surprise %. Carry the full EPS detail,
            # the bellwether's SEC 8-K earnings-release excerpt (real figures +
            # management commentary + outlook, via EDGAR), and its news headlines
            # (the market's digest of the call). Operator 2026-06-25: "the actual
            # transcript on top of the numbers" + "read the files like 8k". All
            # fetched ONCE per bellwether (cached), not per peer; fail-soft.
            _eps_r, _eps_e = re_b.get("eps_reported"), re_b.get("eps_estimate")
            _eps_str = (f" (EPS {_eps_r} vs {_eps_e} est)"
                        if isinstance(_eps_r, (int, float))
                        and isinstance(_eps_e, (int, float)) else "")
            try:
                _bell_news = data_engine.fetch_news(B, limit=5) or []
            except Exception:
                _bell_news = []
            try:
                _bell_docs = data_engine.earnings_docs(B, re_b)
            except Exception:
                _bell_docs = {}
            _bell_filings = _bell_docs.get("filings") or []
            _bell_8k = next((f.get("excerpt", "") for f in _bell_filings
                             if f.get("form") == "8-K" and f.get("excerpt")), "")
            _bell_tr = (_bell_docs.get("transcript") or {}).get("excerpt", "")
            peers_hit: list[str] = []
            for P in (spec.get("peers") or []):
                if P == B or P in handled or len(added) >= cap:
                    continue
                lv = _last_verdict_date(P)
                if lv and lv >= rd:
                    continue                          # already judged since the print
                if P not in tech:                     # underwrite it like a promo/held name
                    try:
                        closes = data_engine.fetch_history([P, config.BENCHMARK], period="1y")
                        bench_s = closes[config.BENCHMARK].dropna()
                        tcomp = data_engine.compute_technicals(closes[[P]], bench_s)
                        if P in tcomp:
                            tech[P] = tcomp[P]
                            screen["quant_scores"][P] = data_engine.quant_score(tcomp[P])
                    except Exception:
                        state.journal("readthrough_tech_fetch_failed", ticker=P, bellwether=B)
                if P not in tech:
                    continue
                note = {
                    "ticker": P, "readthrough": True, "cohort": cohort,
                    "bellwether": B, "bellwether_result": result,
                    "bellwether_surprise_pct": re_b.get("surprise_pct"),
                    "bellwether_eps_reported": _eps_r,
                    "bellwether_eps_estimate": _eps_e,
                    "bellwether_session": re_b.get("session"),
                    "bellwether_reported": str(rdate)[:10],
                    "bellwether_headlines": _bell_news,
                    "bellwether_filings": _bell_filings,
                    "bellwether_transcript_excerpt": _bell_tr,
                    "why": (f"SECTOR READ-THROUGH: {B} ({cohort.replace('_', ' ')} "
                            f"bellwether) just reported {result}{_eps_str} on "
                            f"{str(rdate)[:10]}. Judge {P} on the read-through — the "
                            f"actual numbers, {B}'s SEC 8-K commentary/outlook, and "
                            "its headlines — plus current sector sentiment NOW; do "
                            f"not wait for {P}'s own earnings (by then the move may "
                            "be over)."
                            + (f" {B} 8-K: {_bell_8k[:600]}" if _bell_8k else "")
                            + (f" {B} headlines: " + " | ".join(_bell_news[:5])
                               if _bell_news else ""))}
                if P not in in_cands and P not in held:
                    candidates.append((P, "stock"))
                    added.append(P)
                watch_notes[P] = {**(watch_notes.get(P) or {}), **note}
                handled.add(P)
                peers_hit.append(P)
            if peers_hit:
                events.append({"cohort": cohort, "bellwether": B, "result": result,
                               "reported": str(rdate)[:10], "peers": peers_hit})
    if events:
        state.journal("cohort_readthrough", events=events, added=added)
        try:
            from firm import alerts
            _summary = "; ".join(
                f"{e['bellwether']} {e['result'].split('(')[0].strip()} -> "
                f"{', '.join(e['peers'])}" for e in events)
            alerts.alert("SECTOR EARNINGS READ-THROUGH",
                         f"A bellwether print is being read across to peers this "
                         f"cycle: {_summary}. Peers are judged on the sector signal "
                         "now rather than waiting for their own earnings.", "info")
        except Exception:
            pass
    return candidates, added


def _held_for_rereview() -> set[str]:
    """Names the firm currently holds with an active THESIS: alpha + core
    satellite equities, plus the UNDERLYINGS of open calls (a call whose
    thesis breaks bleeds theta until a stop — the ladder manages price,
    not narrative). Strategic core ETFs (rebalanced mechanically), the
    equitization parking, and the rotation sleeve (a mechanically VALIDATED
    momentum strategy — committee meddling would unvalidate it) carry no
    thesis to re-judge."""
    held: set[str] = set()
    from firm import allocation
    for sl in ("alpha_stocks", "core_blend"):
        sv = state.get_sleeve(sl)
        core = (set(allocation.core_weights()) if sl == "core_blend"
                else set(config.SLEEVES[sl].get("core_weights") or {}))
        held |= {sym for sym, p in sv.get("positions", {}).items()
                 if p.get("instrument") == "equity" and sym not in core
                 and not p.get("cash_equitization")}
    sv = state.get_sleeve("calls_only")
    held |= {p.get("underlying") for p in sv.get("positions", {}).values()
             if p.get("instrument") == "option" and p.get("underlying")}
    return held


def _had_news(v: dict) -> bool:
    """Did this verdict's packet carry REAL headlines (not the no-news / feed-
    down sentinels)? Module-level so BOTH history writers (run_cycle and the
    8:30 underwrite_held, F8 2026-07-16) grade news exposure identically."""
    nh = (v.get("packet") or {}).get("news_headlines") or []
    return bool(nh) and not (len(nh) == 1 and (
        "no recent headlines" in nh[0]
        or "NEWS SOURCE UNAVAILABLE" in nh[0]))


def _news_tier(v: dict) -> str:
    """trusted (a ✓-tagged wire/desk headline) | aggregator (only ·
    sources) | blind (none) — fetch_news tags each headline's source."""
    nh = (v.get("packet") or {}).get("news_headlines") or []
    if not _had_news(v):
        return "blind"
    return "trusted" if any(str(h).startswith("[✓") for h in nh) else "aggregator"


def underwrite_held(max_names: int | None = None) -> dict:
    """The 8:30 HELD-BOOK CYCLE (operator 2026-07-16, refined same day: "8:30
    should be its own cycle but only for held names... from research to ACTING
    upon it"). Money at risk gets its own complete committee cycle BEFORE the
    open — research, verdicts, AND the sleeves acting on them: SELLs exit
    (exits always route, even premarket), adds size up, and any entry hitting
    the market-closed gate is QUEUED FOR THE OPEN (flushed by the monitor at
    9:30). The 9:45 cycle does NOT carry any of this — its whole budget goes
    to a WIDER new-name intake (config.TIER_SLOTS 4/4/4).
    SECTIONED (operator: "if the computer was off, don't cram — section by
    section till the backlog is done"): stalest-verdict-first, capped per run;
    the remainder is journaled and drained by the next run/cycle while
    mechanical stops keep protecting every held name. Caller holds the cycle
    lock (headless) or the app's cycle-thread guard — this trades sleeve
    state like any cycle."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    # LIVE-PARITY UNIVERSE first (operator 2026-07-16): refresh Public's
    # actual tradable list (daily TTL) and the full ETF liquidity pool
    # (weekly TTL) so everything downstream — seats, scan, gates — sees
    # exactly what live can trade. Both self-gate and fail soft.
    try:
        from firm import tradable
        tradable.refresh()
    except Exception:
        state.journal("tradable_refresh_hook_error")
    # ETF SEAT REVIEW next (operator 2026-07-16): every 8:30 the parking and
    # core seats defend their chairs against the eligible universe — a
    # challenger past hysteresis takes the seat before the day's trading.
    # Fail-soft: a review error keeps every incumbent.
    try:
        from firm import allocation
        allocation.refresh_etf_pool()
        _seats = allocation.review_seats()
        if _seats.get("parking_switched") or _seats.get("core_switched"):
            state.journal("etf_seats_changed_premarket", **_seats)
    except Exception:
        state.journal("etf_seat_review_hook_error",
                      trace=traceback.format_exc()[-300:])
    held = sorted(_held_for_rereview())
    if not held:
        state.journal("held_underwrite", n=0, note="no held names carry a thesis")
        return {"underwritten": [], "backlog": [], "deadline_hit": False}
    # Count cap removed (operator 2026-07-17: the book grows — every held name
    # gets re-underwritten each morning). 0/None = uncapped; the 50-min
    # premarket deadline below is the only sectioner. max_names still honors an
    # explicit caller/test override.
    cap = int(max_names or getattr(config, "HELD_UNDERWRITE_MAX_PER_RUN", 0) or 0)
    hist = (state.load("research", {}) or {}).get("history", [])
    _last: dict[str, str] = {}
    for h in hist:
        a = h.get("asof", "")
        for tk, v in (h.get("verdicts") or {}).items():
            if not (v or {}).get("degraded") and (tk not in _last or a > _last[tk]):
                _last[tk] = a
    order = sorted(held, key=lambda t: _last.get(t, ""))   # stalest first
    take, backlog = (order, []) if cap <= 0 else (order[:cap], order[cap:])
    state.set_status("research",
                     f"Premarket held re-underwrite: {len(take)} name(s)", 5,
                     running=True)
    try:
        # Same firm-wide context reads the cycle uses — every held name judged
        # against one coherent regime picture (all fail-soft).
        try:
            regime = riskbrain.sector_regime()
        except Exception:
            regime = {}
        try:
            macro = data_engine.macro_brief(regime)
        except Exception:
            macro = {}
        try:
            from firm import street_research
            street_research.maintain()
        except Exception:
            pass
        tech: dict = {}
        try:
            closes = data_engine.fetch_history(take + [config.BENCHMARK],
                                               period="1y")
            bench_s = closes[config.BENCHMARK].dropna()
            tech = data_engine.compute_technicals(
                closes[[c for c in take if c in closes.columns]], bench_s)
        except Exception:
            state.journal("held_underwrite_tech_failed", names=take)
        take = [t for t in take if t in tech]
        if not take:
            state.journal("held_underwrite", n=0,
                          note="no technicals available — retry next run")
            return {"underwritten": [], "backlog": backlog,
                    "deadline_hit": False}
        # PREMARKET QUOTE OVERLAY (re-grade audit 2026-07-16): daily bars end
        # at yesterday's close — an 8:30 committee judging a name that gapped
        # overnight must SEE the gap. broker quotes are pre/post-aware; the
        # overlay rides the packet without touching the completed-bar series.
        try:
            _pm_q = broker.equity_quotes_public(take)
            for _t, _q in (_pm_q or {}).items():
                if _t in tech and isinstance(_q, (int, float)) and _q > 0:
                    tech[_t]["premarket_price"] = round(_q, 4)
                    _ref = tech[_t].get("price")
                    if isinstance(_ref, (int, float)) and _ref > 0:
                        tech[_t]["premarket_gap_pct"] = round(
                            (_q / _ref - 1) * 100, 2)
        except Exception:
            state.journal("held_underwrite_premarket_quotes_failed")
        depth = _effective_depth(state.get_settings().get("depth", "standard"))
        workers = _effective_workers()
        deadline_min, basis = _research_deadline_min(depth, len(take),
                                                     workers=workers)
        # The held cycle must land its verdicts BEFORE the 9:30 bell so the
        # queued fills act at the open (re-grade audit 2026-07-16): cap the
        # deadline at 50 min from an 8:30 start; names cut by the cap stay in
        # the sectioned backlog and drain next run.
        deadline_min = min(deadline_min, 50.0)
        verdicts: dict = {}
        deadline_hit = False
        # Resume an INTERRUPTED pass (operator 2026-07-21) — same contract as
        # the main cycle floor: adopt same-day completed workups, research the
        # remainder.
        _resumed_h = _resume_from_checkpoint([(t, "stock") for t in take], depth)
        verdicts.update(_resumed_h)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_research_one, t, "stock", tech[t],
                                   data_engine.quant_score(tech[t]), depth,
                                   None, regime, macro): t
                       for t in take if t not in _resumed_h}
            try:
                done = 0
                for fut in as_completed(futures, timeout=deadline_min * 60):
                    t = futures[fut]
                    done += 1
                    try:
                        verdicts[t] = fut.result()
                        _checkpoint_workup(t, verdicts[t], depth)
                    except Exception as e:
                        state.journal("held_underwrite_error", ticker=t,
                                      error=str(e)[:150])
                    state.set_status(
                        "research",
                        f"Premarket held re-underwrite {done}/{len(take)} "
                        f"(latest: {t})", 5 + 90 * done / len(take),
                        running=True)
            except TimeoutError:
                deadline_hit = True
                for f in futures:
                    f.cancel()
                missed = sorted(t for f, t in futures.items() if not f.done())
                backlog = sorted(set(backlog) | set(missed))
                state.journal("held_underwrite_deadline_hit",
                              deadline_min=round(deadline_min, 1),
                              missed=missed)
        # Same no-fabricated-verdicts contract as the main cycle (operator
        # 2026-07-16): a degraded held workup is a FAILED workup — the name
        # stays stale (next section retries it; stops protect it), nothing
        # downstream sees a fake rating.
        _deg = sorted(t for t, v in verdicts.items()
                      if (v.get("verdict") or {}).get("degraded"))
        for t in _deg:
            verdicts.pop(t, None)
        if _deg:
            backlog = sorted(set(backlog) | set(_deg))
            state.journal("degraded_workups_backlogged", names=_deg,
                          via="underwrite_held")
        # Pass concluded normally (complete or deadline-cut, both handled) —
        # the checkpoint's job is over; it must not feed a later fresh pass.
        _clear_research_checkpoint()
        if verdicts:
            # Store like a cycle row (tagged) so reuse/_previous_verdict/
            # cooldowns/report-card all read it natively.
            log = state.load("research", {"history": []})
            log.setdefault("history", []).append({
                "asof": datetime.now(timezone.utc).isoformat(),
                "premarket_held": True,
                "verdicts": {k: v["verdict"] for k, v in verdicts.items()},
                # FULL grading context (AUDIT 2026-07-16 F8): the first cut of
                # this row carried px/earnings/owned only — every held-name
                # verdict would have graded 'blind/quiet/factorless', silently
                # corrupting the news-edge and factor channels that steer the
                # judge. Mirror the main cycle's context grain exactly.
                "context": {
                    k: {"px": ((v.get("packet") or {}).get("technicals")
                               or {}).get("price"),
                        "earnings": ((v.get("packet") or {}).get("next_earnings")
                                     or {}).get("date"),
                        "earnings_session": ((v.get("packet") or {})
                                             .get("next_earnings")
                                             or {}).get("session"),
                        "had_news": _had_news(v),
                        "news_tier": _news_tier(v),
                        "factors": (v.get("packet") or {}).get("factor_scores") or {},
                        "macro_news": (((v.get("packet") or {}).get("macro_brief")
                                        or {}).get("geopolitical_news_topics") or []),
                        "n_headlines": len((v.get("packet") or {}).get("news") or []),
                        "fund_source": ((v.get("packet") or {}).get("fundamentals")
                                        or {}).get("source"),
                        "degraded_reason": (v.get("verdict") or {}).get("degraded_reason")
                        or ("degraded" if (v.get("verdict") or {}).get("degraded")
                            else None),
                        "owned": True}
                    for k, v in verdicts.items()},
                "desk_scores": {
                    k: {d: (v["research"].get(d) or {}).get("score")
                        for d in (v.get("research") or {})
                        if isinstance(v["research"].get(d), dict)}
                    for k, v in verdicts.items() if v.get("research")},
            })
            log["history"] = log["history"][-120:]
            state.save("research", log)

            # A COMPLETE CYCLE, NOT JUST RESEARCH (operator 2026-07-16 refined:
            # "8:30 should be its own research cycle from research to ACTING
            # upon it"): the sleeves run on the held verdicts exactly as the
            # 9:45 cycle would — SELLs exit (exits always route, even
            # premarket), adds-on-strength size up, a held-name BUY may open
            # in a sleeve that doesn't hold it yet. Entries hitting the
            # market-closed gate at 8:30 are QUEUED FOR THE OPEN
            # (broker._queue_open_entry -> monitor flush at 9:30) — decided
            # trades never die on the bell. The 9:45 cycle does NOT carry any
            # of this; its whole budget goes to new names.
            mode = state.get_settings().get("mode", "paper")
            sectors = {t: (v["packet"].get("fundamentals") or {}).get("sector")
                       or "" for t, v in verdicts.items()}
            from firm import sleeves
            for label, fn in (
                ("Alpha Stocks (held cycle)",
                 lambda: sleeves.run_alpha_stocks(verdicts, sectors, mode)),
                ("Core Blend (held cycle)",
                 lambda: sleeves.run_core_blend(verdicts, sectors, mode)),
                ("Calls desk (held cycle)",
                 lambda: sleeves.run_calls_only(verdicts, mode)),
            ):
                try:
                    fn()
                except Exception as e:
                    state.journal("sleeve_error", stage=label,
                                  error=str(e)[:200])
            try:
                from firm import routing
                routing.record_routing(verdicts)
            except Exception:
                state.journal("routing_record_error", via="underwrite_held")
            try:
                from firm import monitor
                monitor.check_all_exits()
            except Exception as e:
                state.journal("held_cycle_exit_sweep_error", error=str(e)[:120])
            # watchlist.update is deliberately NOT called here: with no screen
            # scores every benched name's `missing` counter would tick up and
            # three premarket runs would vanish-evict real near-misses. The
            # 9:45 cycle owns the bench; held HOLD stamps heal from the
            # premarket history row via _reconcile_bench_stamps.
        state.journal("held_underwrite",
                      n=len(verdicts), underwritten=sorted(verdicts),
                      backlog=backlog, deadline_hit=deadline_hit,
                      degraded=len(_deg))
        if backlog:
            state.journal("held_underwrite_backlog", names=backlog,
                          cap=cap or None,
                          sectioner="count" if cap > 0 else "50-min deadline",
                          note="sectioned — the next run drains more; "
                               "mechanical stops protect them meanwhile")
        return {"underwritten": sorted(verdicts), "backlog": backlog,
                "deadline_hit": deadline_hit}
    finally:
        state.set_status("idle", "Premarket held re-underwrite complete", 0,
                         running=False)
        _post_cycle_ram_clean()   # operator 2026-07-17: ram clean after every cycle


def _check_earnings_actuals(verdicts: dict, watch_notes: dict) -> None:
    """Operator guarantee (2026-06-14): a name reopened BECAUSE its earnings
    printed must be judged on the ACTUAL numbers. If the vendor didn't have them
    yet, re-attempt the reopen next cycle ONCE (catches vendor lag), then alert
    loudly rather than silently judging the print blind. Fail-soft — a hiccup
    here must never break the cycle."""
    from firm import alerts
    from datetime import date as _date, timedelta as _td
    wl = state.load("watchlist", {})
    changed = False
    for t, v in verdicts.items():
        why = (watch_notes.get(t) or {}).get("why", "")
        if "earnings printed" not in why:          # only earnings-catalyst reopens
            continue
        re = (v.get("packet") or {}).get("recent_earnings") or {}
        got_actuals = re.get("eps_reported") is not None
        e = wl.get(t)
        if got_actuals:
            if e is not None and e.pop("actuals_retry", None) is not None:
                changed = True                     # success — clear any retry flag
            continue
        if e is not None and e.get("actuals_retry", 0) < 1:
            # one-shot retry: rewind the bench clock so _catalyst_landed reopens
            # this name next cycle to re-attempt the fetch (vendor lag is common)
            e["actuals_retry"] = e.get("actuals_retry", 0) + 1
            ed = e.get("earnings_date")
            try:
                if ed:
                    e["last_verdict"] = {**(e.get("last_verdict") or {}),
                                         "ts": (_date.fromisoformat(str(ed)[:10])
                                                - _td(days=1)).isoformat()}
            except (ValueError, TypeError):
                pass
            changed = True
            state.journal("earnings_actuals_retry", ticker=t, earnings=ed)
        else:
            if e is not None and e.pop("actuals_retry", None) is not None:
                changed = True
            state.journal("earnings_actuals_missing", ticker=t,
                          earnings=(e or {}).get("earnings_date"))
            alerts.alert(f"EARNINGS ACTUALS MISSING: {t}",
                         f"{t} was re-judged on its earnings print but the reported "
                         "numbers couldn't be fetched (vendor gap) even after a retry "
                         "— it was judged on price/context alone. Verify the latest "
                         "earnings manually.")
    if changed:
        state.save("watchlist", wl)


def run_cycle() -> dict:
    settings = state.get_settings()
    mode, depth = settings["mode"], settings["depth"]
    _cycle_started_iso = datetime.now(timezone.utc).isoformat()   # routing block window
    state.journal("cycle_start", mode=mode, depth=depth)
    state.set_status("screening", "Quant screen ranking the universe", 5)

    try:
        # Pull the live macro calendar once (FRED -> Forex Factory -> cache),
        # so this cycle's cooldown/catalyst checks see fresh, exact release dates.
        try:
            from firm import macro_calendar
            macro_calendar.refresh()
        except Exception:
            state.journal("macro_refresh_error", trace=traceback.format_exc()[-500:])
        # Refresh the judge's scorecard digest ONCE per cycle (purpose review
        # #1a) — every judge prompt this cycle then reads the cached text (no
        # network in worker threads). Failure degrades to an absent digest.
        try:
            learning.refresh_report_card_digest()
        except Exception:
            state.journal("report_card_digest_error")
        # Reconcile trailing-stop peaks against real daily-bar highs BEFORE the
        # monitor/sleeves act on them (2026-07-02 poisoning fix) — a bad tick
        # that stuck as a peak would otherwise fire a false trailing stop.
        try:
            from firm import monitor as _mon
            _nfix = _mon.reconcile_peaks()
            if _nfix:
                state.journal("peaks_reconciled_cycle", corrected=_nfix)
        except Exception:
            state.journal("peak_reconcile_error")
        screen = data_engine.screen_universe()
        tech = screen["technicals"]
        candidates = ([(t, "stock") for t in screen["stock_candidates"]]
                      + [(t, "etf") for t in screen["etf_candidates"]])

        # Verdict cooldown: a NON-held name judged HOLD/SELL in the last few
        # days keeps its verdict — re-judging the same setup daily is cost
        # without information. Reopens on the earnings the judge waited for
        # (re-read that day with fresh news) or when the verdict ages out.
        held = _held_for_rereview()
        _kept, _cooled = [], []
        for t, kind in candidates:
            if t in held:
                _kept.append((t, kind))
                continue
            cd = _cooldown_state(t)
            (_cooled.append({"ticker": t, **cd}) if cd
             else _kept.append((t, kind)))
        candidates = _kept
        if _cooled:
            _laned = [c["ticker"] for c in _cooled if c.get("lane") == "momentum"]
            state.journal("research_cooldown", skipped=_cooled,
                          momentum_lane=_laned)
            # Per-symbol marker so the forward-return scorecard (learning.
            # blocked_audit) measures what laning each name actually COST —
            # MOMENTUM_LANE_MOM_MIN then tunes on evidence, not eyeball
            # (evidence-over-constants). One sample per laned name per cycle.
            for _sym in _laned:
                state.journal("momentum_lane_skip", symbol=_sym)

        # BACKFILL (operator 2026-06-23: "day to day exploring MORE names, not
        # the same names — don't shorten how many get brought up"). The momentum
        # lane / cooldown just vacated exploratory stock slots; REFILL each from
        # its cap tier's ranked reserve with the next FRESH (not held, not
        # laned/recently-judged) name, so the committee always explores a full
        # slate of NEW names rather than shrinking to the held book. These ride
        # ON TOP of the held re-underwrite + bench reopens added below. Flag OFF
        # = legacy (a cooled slot simply vanishes).
        if getattr(config, "SCAN_BACKFILL", False):
            _tier_pool = (screen.get("scan_info") or {}).get("tier_pool") or {}
            candidates, _added, _short = _backfill_candidates(
                candidates, _tier_pool, held, tech)
            if _added or _short:
                state.journal("scan_backfill", added=_added, short=_short,
                              note="refilled lane/cooldown-vacated slots with fresh names")

        # Monday advisory: the strategic core through the committee's eyes —
        # informational verdicts, graded by the report card, never traded
        advisory = _core_advisory_names()
        for t in advisory:
            if t in tech and all(t != c for c, _ in candidates):
                candidates.append((t, "etf"))

        # Sector regime: which sectors lead/lag the market right now — stamped
        # on every packet so the committee sees the tailwind/headwind
        try:
            regime = riskbrain.sector_regime()
        except Exception:
            regime = {}

        # Street Research desk: pick up any reports the operator dropped since
        # the last look BEFORE the floor convenes, and run any due claim
        # reviews (sha-deduped, cost-bounded, fail-soft — a bad file must
        # never dent the cycle).
        try:
            from firm import street_research
            street_research.maintain()
        except Exception:
            state.journal("street_research_scan_error")

        # One firm-wide macro brief per cycle, shared by every candidate
        try:
            macro = data_engine.macro_brief(regime)
            state.journal("macro_brief", market=(macro.get("market") or {}).get("trend"),
                          vix=(macro.get("volatility") or {}).get("vix"),
                          just_happened=[e["event"][:40]
                                         for e in macro.get("just_happened", [])])
        except Exception:
            macro = {}

        # The Bench: watchlist names whose setup just completed get promoted
        # into this cycle's research alongside the tier candidates. Pass the
        # ACTUAL post-cooldown research set — the raw screen list let a cooled
        # scan candidate fall between both paths (2026-07-10 APPS trap: skipped
        # by promotions as "a candidate", dropped from candidates by cooldown,
        # so a landed macro catalyst never re-judged it and 'catalyst
        # readiness' redded every 30 min with no self-clear).
        promos = watchlist.promotions(screen,
                                      researching={c for c, _ in candidates})
        watch_notes = {p["ticker"]: p for p in promos}
        # A promoted stop-out is usually NOT in the scan's top-300 momentum
        # ranks (it just fell 15%+ — that's why it was benched). Fetch its
        # technicals directly so the promotion actually reaches the committee.
        for p in promos:
            t = p["ticker"]
            if t in tech:
                continue
            try:
                closes = data_engine.fetch_history([t, config.BENCHMARK], period="1y")
                bench_s = closes[config.BENCHMARK].dropna()
                tcomp = data_engine.compute_technicals(closes[[t]], bench_s)
                if t in tcomp:
                    tech[t] = tcomp[t]
                    screen["quant_scores"][t] = data_engine.quant_score(tcomp[t])
            except Exception:
                state.journal("promotion_tech_fetch_failed", ticker=t)
        candidates += [(p["ticker"], "stock") for p in promos
                       if p["ticker"] in tech]

        # Re-underwrite the BOOK (2026-06-10 absence audit): a held name whose
        # momentum decays out of the scan's top ranks would otherwise never be
        # re-judged — nothing between an aging thesis and a mechanical stop.
        # Every held single-name equity gets a DAILY committee verdict; SELLs
        # then exit via the existing _apply_sells wiring.
        # SCHEDULE SPLIT (operator 2026-07-16): that daily review now happens
        # at the 8:30 premarket pass (cycle.underwrite_held) — a held verdict
        # fresher than HELD_VERDICT_REUSE_H is REUSED (merged into this
        # cycle's pool before the sleeves run) instead of burning a workup, so
        # this cycle's research budget goes to NEW names. Premarket missed
        # (computer off)? Stale held names are re-underwritten here SECTION BY
        # SECTION — stalest verdict first, capped per run, remainder journaled
        # and drained by the next run — never crammed into one cycle.
        in_cands = {c for c, _ in candidates}
        _held_unreviewed = []
        _reused_held: dict[str, dict] = {}
        _reuse_h = float(getattr(config, "HELD_VERDICT_REUSE_H", 6.0))
        # Catch-up UNCAPPED too (operator 2026-07-17, superseding the same-day
        # split and the original "don't cram" section cap): when the premarket
        # was missed, EVERY stale held name joins this cycle's pool — held
        # names are money at risk and outrank a narrower new-name intake. The
        # cycle's self-tuning deadline (scales with candidate count and worker
        # count, hard-capped at RESEARCH_DEADLINE_CAP_MIN) is the only bound;
        # names it cuts are retried next cycle under mechanical-stop protection.
        _lastv: dict[str, tuple[str, dict]] = {}
        for _h in (state.load("research", {}) or {}).get("history", []):
            _a = _h.get("asof", "")
            for _tk, _v in (_h.get("verdicts") or {}).items():
                if not (_v or {}).get("degraded") \
                        and (_tk not in _lastv or _a > _lastv[_tk][0]):
                    _lastv[_tk] = (_a, _v)

        def _v_age_h(iso: str) -> float:
            try:
                return (datetime.now(timezone.utc)
                        - datetime.fromisoformat(iso)).total_seconds() / 3600
            except (ValueError, TypeError):
                return 1e9

        _stale_held: list[tuple[float, str]] = []
        for t in sorted(held - in_cands):
            _a, _v = _lastv.get(t, ("", {}))
            if _v and _v_age_h(_a) < _reuse_h:
                _reused_held[t] = {"asof": _a, "verdict": _v}
                continue
            _stale_held.append((_v_age_h(_a), t))
        _stale_held.sort(reverse=True)               # stalest verdict first
        for _, t in _stale_held:
            if t not in tech:
                try:
                    closes = data_engine.fetch_history([t, config.BENCHMARK], period="1y")
                    bench_s = closes[config.BENCHMARK].dropna()
                    tcomp = data_engine.compute_technicals(closes[[t]], bench_s)
                    if t in tcomp:
                        tech[t] = tcomp[t]
                        screen["quant_scores"][t] = data_engine.quant_score(tcomp[t])
                except Exception:
                    state.journal("held_tech_fetch_failed", ticker=t)
            if t in tech:
                candidates.append((t, "stock"))
            else:
                _held_unreviewed.append(t)   # a held name we couldn't re-underwrite this cycle
        if _held_unreviewed:
            # Surface it (was a silent write-only journal line): money at risk that
            # missed its daily re-underwrite. Mechanical stops still protect it; this
            # tells the operator the verdict-driven exit layer skipped it this cycle
            # (change-audit verify 2026-06-15).
            from firm import alerts
            alerts.alert("HELD NAME NOT RE-UNDERWRITTEN",
                         f"{len(_held_unreviewed)} held name(s) got no fresh verdict this "
                         f"cycle (data fetch failed): {', '.join(_held_unreviewed[:8])}. "
                         "Mechanical stops still protect them; will retry next cycle.", "info")

        # Research backlog (2026-07-01 goal audit, fix 5c): names dropped by a
        # PRIOR cycle's research deadline get a seat at THIS cycle's table —
        # before this they were journaled and then silently lost (an unjudged
        # name carries no cooldown, but nothing re-queued it either). One shot:
        # the backlog is consumed on injection; a name dropped again re-queues.
        # Entries are (ticker, kind) pairs (batch review R2 — legacy bare
        # strings read as stock), and injected names go through the SAME
        # cooldown gate as everyone else: the deadline dropped them, but a
        # verdict they earned since (or an active lane) still applies.
        _backlog = []
        for r in (state.load("research_backlog", []) or []):
            if isinstance(r, str):
                _backlog.append((r, "stock"))
            elif isinstance(r, (list, tuple)) and len(r) == 2:
                _backlog.append((str(r[0]), str(r[1])))
        if _backlog:
            in_cands = {c for c, _ in candidates}
            _inj, _uninj = [], []
            for t, k in _backlog:
                if t in in_cands:
                    continue                    # already back on its own merits
                if t not in held and _cooldown_state(t):
                    _uninj.append(f"{t}(cooldown)")
                    continue
                if t not in tech:
                    try:
                        closes = data_engine.fetch_history([t, config.BENCHMARK], period="1y")
                        bench_s = closes[config.BENCHMARK].dropna()
                        tcomp = data_engine.compute_technicals(closes[[t]], bench_s)
                        if t in tcomp:
                            tech[t] = tcomp[t]
                            screen["quant_scores"][t] = data_engine.quant_score(tcomp[t])
                    except Exception:
                        pass
                (_inj.append((t, k)) if t in tech else _uninj.append(t))
            candidates += _inj
            state.save("research_backlog", [])   # consumed — re-queues on a re-drop
            if _inj or _uninj:
                state.journal("research_backlog_injected",
                              injected=[t for t, _ in _inj],
                              dropped=_uninj)

        # OPERATOR RESEARCH REQUESTS (operator 2026-07-16: "PYPL went parabolic
        # up and HTZ parabolic down but the firm didn't detect them — let me
        # insert names to be researched with the next cycle"). Names queued via
        # POST /api/research-request get a committee seat THIS cycle: cooldown
        # is OVERRIDDEN (the operator outranks the cadence gate — the same
        # override the earnings read-through uses), technicals are fetched
        # directly for off-scan names, and the queue is consumed on injection.
        # A fetch-failed name STAYS queued and alerts — never silently lost.
        try:
            _rq = state.load("research_requests", {"queue": []}) or {}
            _queue = [r for r in (_rq.get("queue") or []) if isinstance(r, dict)]
            if _queue:
                _cap = int(getattr(config, "OPERATOR_REQUESTS_PER_CYCLE", 6))
                _take, _rest = _queue[:_cap], _queue[_cap:]
                in_cands = {c for c, _ in candidates}
                _oinj, _ofail = [], []
                for _req in _take:
                    t = str(_req.get("ticker", "")).upper().strip()
                    if not t or t in in_cands:
                        if t:
                            _oinj.append(t)   # already seated on its own merits
                        continue
                    # LIVE PARITY (2026-07-16): an operator-typed name Public
                    # cannot BUY must be told loudly, not researched into an
                    # untradeable conviction. Fails open w/o a universe snapshot.
                    try:
                        from firm import tradable as _trd
                        if not _trd.is_buyable(t):
                            state.journal("operator_request_not_tradable", ticker=t)
                            from firm import alerts
                            alerts.alert("REQUESTED NAME NOT TRADABLE ON PUBLIC",
                                         f"{t} is not buyable on Public — request "
                                         "dropped (research would be unactionable).",
                                         "info")
                            continue
                    except Exception:
                        pass
                    if t not in tech:
                        try:
                            closes = data_engine.fetch_history(
                                [t, config.BENCHMARK], period="1y")
                            bench_s = closes[config.BENCHMARK].dropna()
                            tcomp = data_engine.compute_technicals(closes[[t]], bench_s)
                            if t in tcomp:
                                tech[t] = tcomp[t]
                                screen["quant_scores"][t] = data_engine.quant_score(tcomp[t])
                        except Exception:
                            pass
                    if t in tech:
                        candidates.append((t, _req.get("kind") or "stock"))
                        in_cands.add(t)
                        _oinj.append(t)
                        # The committee should KNOW the operator called for this
                        # look — ride the packet's watchlist_context like a promo.
                        watch_notes.setdefault(t, {
                            "ticker": t, "why": "operator research request",
                            "watch_trigger": (_req.get("note") or
                                              "operator asked for a committee look"),
                            "benched_since": _req.get("requested_at", ""),
                            "last_verdict": None, "stop_out": None})
                    else:
                        _ofail.append(t)
                        _rest.append(_req)    # keep for next cycle — never lost
                state.save("research_requests", {"queue": _rest})
                if _oinj or _ofail:
                    state.journal("operator_requests_injected",
                                  injected=_oinj, retry=_ofail)
                if _ofail:
                    from firm import alerts
                    alerts.alert("OPERATOR REQUEST UNPRICEABLE",
                                 "No data for operator-requested name(s): "
                                 f"{', '.join(_ofail)} — kept queued; will retry "
                                 "next cycle.", "warning")
        except Exception:
            state.journal("operator_requests_error",
                          trace=traceback.format_exc()[-300:])

        if held - in_cands:
            # Also name the holdings deliberately NOT re-judged and why —
            # "GLD doesn't show up in the scan" must answer itself (2026-06-12)
            judged_all = {c for c, _ in candidates}
            exempt: dict[str, str] = {}
            for _t, _rv in _reused_held.items():
                exempt[_t] = (f"handled by the 8:30 held cycle "
                              f"({_v_age_h(_rv['asof']):.1f}h fresh)")
            # (No sectioned backlog anymore — the catch-up is uncapped,
            # operator 2026-07-17; deadline-cut names surface via the
            # sleeve-position sweep below.)
            for sl in ("alpha_stocks", "core_blend"):
                sv = state.get_sleeve(sl)
                from firm import allocation as _alloc
                core_w = (set(_alloc.core_weights()) if sl == "core_blend"
                          else set(config.SLEEVES[sl].get("core_weights") or {}))
                for sym, p in sv.get("positions", {}).items():
                    if sym in judged_all:
                        continue
                    if p.get("cash_equitization"):
                        exempt[sym] = "cash parking — not a thesis"
                    elif sym in core_w:
                        exempt[sym] = "strategic core — mechanical rebalance"
            for sym in state.get_sleeve("etf_rotation").get("positions", {}):
                if sym not in judged_all:
                    exempt.setdefault(sym, "mechanical rotation — rank-ruled")
            state.journal("holdings_rereview", names=sorted(held - in_cands),
                          exempt=exempt)

        # Feature C: a cohort bellwether's just-landed earnings read across to its
        # peers THIS cycle (cooldown overridden), so the committee judges them on
        # the sector signal now rather than waiting for each peer's own print.
        try:
            _today = datetime.now(timezone.utc).date()
            candidates, _rt_added = _inject_cohort_readthrough(
                candidates, tech, screen, held, watch_notes, _today)
        except Exception:
            _rt_added = []
            state.journal("cohort_readthrough_error", trace=traceback.format_exc()[-400:])

        state.journal("screen_done",
                      stocks=screen["stock_candidates"], etfs=screen["etf_candidates"],
                      watchlist_promotions=[p["ticker"] for p in promos],
                      readthrough_peers=_rt_added,
                      market=screen.get("scan_info", {}))

        # UPDATED PRICING (operator 2026-06-24): the daily scan's technicals are from
        # the premarket sweep — stale by the 9:45 cycle, staler intraday, and a
        # throttled/internet-degraded scan can be a FULL DAY stale. Refresh every
        # stock candidate's technicals with a fresh, SMALL (≈9-18 names), internet-
        # resilient batch so the committee judges TODAY's price, not the scan
        # snapshot. A fetch failure keeps the scan tech for that name (fail-soft).
        _cand_syms = sorted({c for c, k in candidates if k == "stock" and c in tech})
        if _cand_syms:
            try:
                _cl = data_engine.fetch_history(_cand_syms + [config.BENCHMARK], period="1y")
                _bs = _cl[config.BENCHMARK].dropna()
                _fresh = data_engine.compute_technicals(
                    _cl[[c for c in _cand_syms if c in _cl.columns]], _bs)
                for _t, _m in _fresh.items():
                    tech[_t] = _m
                    screen["quant_scores"][_t] = data_engine.quant_score(_m)
                state.journal("candidate_prices_refreshed", refreshed=len(_fresh),
                              of=len(_cand_syms))
            except Exception:
                state.journal("candidate_price_refresh_failed", n=len(_cand_syms))

        verdicts: dict = {}
        done = 0
        state.set_status("research", f"Research floor + debate room on "
                         f"{len(candidates)} candidates ({depth} depth)", 15)
        # No context manager (2026-06-10 audit): pool exit JOINS all threads,
        # so one hung Opus call held the ENTIRE cycle hostage — execution
        # slid unpredictably into the afternoon. Past the deadline the cycle
        # proceeds with the verdicts in hand; stragglers' work is dropped.
        _preflight_ram_recovery()      # reap idle sessions BEFORE sizing the floor
        _eff_workers = _effective_workers()
        _eff_depth = _effective_depth(depth)
        try:
            import psutil as _ps
            _avail_gb = round(_ps.virtual_memory().available / 1e9, 1)
        except Exception:
            _avail_gb = None
        state.journal("research_workers", effective=_eff_workers,
                      base=int(getattr(config, "RESEARCH_WORKERS", 2)), avail_gb=_avail_gb,
                      depth=_eff_depth, requested_depth=depth)
        if _eff_depth != depth:
            from firm import alerts
            alerts.alert("RESEARCH DEPTH AUTO-DOWNGRADED",
                         f"RAM is tight ({_avail_gb} GB free) — this cycle runs "
                         f"'{_eff_depth}' instead of '{depth}' so the committee returns "
                         "REAL verdicts instead of timing out into quant-only degraded "
                         "mode. Free RAM (close idle windows) to restore full depth.",
                         "info")
        pool = ThreadPoolExecutor(max_workers=_eff_workers)
        deadline_hit = False
        futures: dict = {}
        research_t0 = time.monotonic()
        _floor_stop = threading.Event()
        _floor_watch: "threading.Thread | None" = None
        try:
            # HELD-FIRST ordering (2026-07-01 goal audit, fix 5a): held names are
            # appended at the candidate TAIL by construction, and a 1-worker pool
            # executes in submission order — so a deadline hit always dropped the
            # held book first (CAT+CSCO dropped 06-24 AND 07-01, both held: money
            # at risk went un-re-underwritten while fresh ideas got judged).
            # Money at risk researches first; discretionary new ideas absorb the cut.
            _ordered = sorted(candidates, key=lambda ck: ck[0] not in held)
            # Resume an INTERRUPTED pass (operator 2026-07-21): completed
            # workups from a same-day pass a restart killed are adopted
            # directly; only the remainder re-researches.
            _resumed = _resume_from_checkpoint(_ordered, _eff_depth)
            verdicts.update(_resumed)
            futures = {
                pool.submit(_research_one, t, kind, tech[t],
                            screen["quant_scores"].get(t, 50), _eff_depth,
                            watch_notes.get(t), regime, macro): t
                for t, kind in _ordered if t in tech and t not in _resumed
            }
            # Deadline tuned from this depth's own history (see helper above),
            # scaled to TODAY's worker count (fix 5b)
            deadline_min, basis = _research_deadline_min(
                _eff_depth, len(futures), workers=_eff_workers)
            state.journal("research_deadline_used", depth=_eff_depth,
                          deadline_min=round(deadline_min, 1),
                          n_candidates=len(futures), **basis)
            # Feature B: a daemon watcher classifies the live floor
            # (progressing / stalled / quota-out). Stall bar = STALL_PACE_MULT x
            # the worst-ever per-candidate pace, floored at STALL_GAP_FLOOR_MIN
            # and held under the deadline so it always fires before the cut.
            # basis emits WORKER-minutes per candidate since the worker-aware
            # deadline (batch review R8: the old key name silently pinned this
            # to the 6.0 fallback) — divide by today's workers for wall-clock
            _worst_pace = (float(basis.get("worst_worker_min_per_candidate") or 6.0)
                           / max(1, _eff_workers))
            _stall_gap_s = min(
                max(config.STALL_PACE_MULT * _worst_pace, config.STALL_GAP_FLOOR_MIN),
                0.7 * deadline_min) * 60.0
            _now_mono = time.monotonic()
            _floor_progress = {
                "done": 0, "total": len(futures), "last_ts": _now_mono,
                "start_ts": _now_mono, "deadline_min": round(deadline_min, 1),
                "stall_gap_s": _stall_gap_s, "depth": depth}
            if config.STALL_WATCH and futures:
                _floor_watch = threading.Thread(
                    target=_run_floor_watch, args=(_floor_progress, _floor_stop),
                    daemon=True, name="floor-watch")
                _floor_watch.start()
            try:
                for fut in as_completed(futures, timeout=deadline_min * 60):
                    t = futures[fut]
                    done += 1
                    try:
                        verdicts[t] = fut.result()
                        _checkpoint_workup(t, verdicts[t], _eff_depth)
                    except Exception as e:
                        state.journal("research_error", ticker=t, error=str(e))
                    _floor_progress["done"] = done
                    _floor_progress["last_ts"] = time.monotonic()
                    state.set_status(
                        "research",
                        f"{done}/{len(futures)} workups complete (latest: {t})",
                        15 + 55 * done / max(1, len(futures)))
            except TimeoutError:
                deadline_hit = True
                missed = sorted(t for f, t in futures.items() if not f.done())
                for f in futures:
                    f.cancel()
                state.journal("research_deadline_hit",
                              deadline_min=round(deadline_min, 1),
                              completed=done, dropped=missed)
                # Re-queue the dropped names for the NEXT cycle (fix 5c) — held
                # names come back via the held re-underwrite anyway; the backlog
                # gives the dropped DISCRETIONARY ideas their lost seat back.
                # Persist (ticker, KIND) pairs (batch review R2): a bare ticker
                # list re-injected everything as "stock", which would have let a
                # dropped ETF (screen tier or Monday-advisory VOO/QQQ/GLD) re-
                # enter as a buyable stock / options underlying.
                try:
                    _ckind = dict(candidates)
                    _old_bl = {r[0]: r[1] for r in
                               (state.load("research_backlog", []) or [])
                               if isinstance(r, (list, tuple)) and len(r) == 2}
                    for _t in missed:
                        _old_bl[_t] = _ckind.get(_t, "stock")
                    # PRIORITISE HELD NAMES (2026-07-25 audit). `missed` is sorted,
                    # so a bare [:20] cut the backlog ALPHABETICALLY: on 2026-07-25,
                    # 31 names were dropped and only 20 saved, losing NWPX, OKTA,
                    # OSCR, PBI, RLJ, SOXL, SOXX, SYRE, TENB, UCTT, UMC — including
                    # HELD positions (OSCR, PBI) and the core seat SOXX. A held name
                    # that missed its verdict is the one the firm most needs to
                    # re-underwrite; the alphabet is not a risk ranking.
                    try:
                        _held_now = set()
                        for _sl in ("alpha_stocks", "core_blend", "etf_rotation"):
                            for _sym, _p in (state.get_sleeve(_sl).get("positions")
                                             or {}).items():
                                if not _p.get("cash_equitization"):
                                    _held_now.add(_sym)
                    except Exception:
                        _held_now = set()
                    _ordered = sorted(_old_bl.items(),
                                      key=lambda kv: (kv[0] not in _held_now, kv[0]))
                    state.save("research_backlog",
                               [[t, k] for t, k in _ordered[:20]])
                except Exception:
                    pass
                # A held position cancelled by the deadline got NO fresh verdict —
                # surface it rather than burying it in the dropped list (a held name
                # shares the deadline budget with discretionary new ideas; change-
                # audit verify 2026-06-15). Mechanical stops still protect it.
                _dropped_held = [t for t in missed if t in held]
                if _dropped_held:
                    from firm import alerts
                    alerts.alert("HELD NAME DROPPED BY RESEARCH DEADLINE",
                                 f"{len(_dropped_held)} held position(s) ran out of research "
                                 f"time this cycle: {', '.join(_dropped_held[:8])} — no fresh "
                                 "verdict. Mechanical stops still protect them.", "info")
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
            # Stop the Feature B watcher and stamp a terminal heartbeat so the
            # dashboard shows the floor's outcome (complete vs deadline-hit).
            _floor_stop.set()
            if _floor_watch is not None:
                _floor_watch.join(timeout=2)
            try:
                state.save("research_progress", {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "state": "deadline_hit" if deadline_hit else "complete",
                    "done": done, "total": len(futures), "depth": depth})
            except Exception:
                pass

        # Self-tuning feedback: a NORMAL completion teaches the observed pace.
        if not deadline_hit and futures:
            _real_n = sum(1 for v in verdicts.values()
                          if not ((v.get("verdict") or {}).get("degraded")))
            _record_research_duration(
                _eff_depth, (time.monotonic() - research_t0) / 60.0, len(futures),
                workers=_eff_workers, real_verdicts=_real_n)
        elif deadline_hit and futures and done >= max(4, len(futures) // 3):
            # The deadline was HIT (operator 2026-07-02: "the stall got bit by
            # time but time did NOT increase"). Previously a timed-out run taught
            # the deadline NOTHING, so it could only ever SHRINK from fast
            # completions — a too-short deadline could never learn it needed MORE
            # time. Distinguish a HANG (dead CLI, no real progress -> must not
            # inflate the deadline) from a PROGRESSING-but-slow run that genuinely
            # needed more time. Only when a real FRACTION finished (>= a third,
            # >= 4) and they were mostly REAL do we record the observed pace over
            # those `done` candidates in the deadline window: pace =
            # deadline_min*workers/done is HIGHER than the deadline assumed, so the
            # NEXT deadline GROWS (clamped by cap_min). A barely-started or
            # degraded-dominated hang is excluded here (done floor) and by
            # _record_research_duration's own real<50% guard.
            _real_done = sum(1 for v in verdicts.values()
                             if not ((v.get("verdict") or {}).get("degraded")))
            _record_research_duration(
                _eff_depth, float(deadline_min), done,
                workers=_eff_workers, real_verdicts=_real_done)
            state.journal("deadline_grew_from_stall", depth=_eff_depth, done=done,
                          total=len(futures), deadline_min=round(deadline_min, 1))

        # ZERO verdicts = the cycle did not really happen (internet outage,
        # CLI down). Stamping research "fresh" here silently defeated the
        # >26h catch-up safety: the trading day was lost with no retry
        # (2026-06-10 operator report). Leave yesterday's research intact,
        # fail loudly, and let the health loop's catch-up re-run the cycle
        # within 30 min of connectivity returning.
        # Partial data failure must reach the OPERATOR, not just the journal:
        # a third of the research floor failing means data/CLI trouble that
        # will repeat tomorrow unless someone looks (operator ask 2026-06-12)
        if futures:
            # A DEGRADED verdict is a failed workup wearing a badge (IG
            # 2026-07-01: this watchdog NEVER fired across two all-degraded
            # days because degraded verdicts still count as "verdicts" — the
            # error count read 0 while the committee produced nothing real).
            # Degraded + missing both count against the floor.
            _deg_n = sum(1 for v in verdicts.values()
                         if ((v.get("verdict") or {}).get("degraded")))
            _err_n = len(futures) - len(verdicts) + _deg_n
            if _err_n and _err_n / len(futures) >= 0.34:
                from firm import alerts
                state.journal("research_degraded", failed=_err_n,
                              degraded=_deg_n, total=len(futures))
                alerts.alert("RESEARCH DEGRADED",
                             f"{_err_n}/{len(futures)} candidate workups "
                             f"failed or fell to quant-only degraded mode this "
                             "cycle (data or CLI trouble) — verdicts are thin; "
                             "the firm retries next cycle but check "
                             "connectivity/CLI/RAM if this repeats")

        # A FABRICATED VERDICT IS NOT A VERDICT (operator 2026-07-16: "fix this
        # type of thing permanently"). The quant-fallback cohort measured 24%
        # correct at -18.9% avg excess vs 66% for real verdicts — worse than a
        # coin flip, unable to trade (buys blocked), yet it flooded history,
        # bench stamps, and the report card. A failed workup is now treated as
        # exactly that: the name goes to research_backlog (one-shot re-queue,
        # existing rails — the next cycle's REAL committee retries it), the
        # failure is journaled, and NOTHING downstream (pool, history, bench,
        # sleeves, routing, report card) ever sees a fabricated rating. An
        # all-degraded outage cycle now yields ZERO verdicts and flows into the
        # existing zero-verdict outage path (no fresh stamp, catch-up retries).
        _deg_names = sorted(t for t, v in verdicts.items()
                            if (v.get("verdict") or {}).get("degraded"))
        if _deg_names:
            _bl = state.load("research_backlog", []) or []
            _have = {(r[0] if isinstance(r, (list, tuple)) else r) for r in _bl}
            _bl += [[t, verdicts[t].get("kind") or "stock"]
                    for t in _deg_names if t not in _have]
            state.save("research_backlog", _bl)
            for t in _deg_names:
                verdicts.pop(t, None)
            state.journal("degraded_workups_backlogged", names=_deg_names,
                          note="fabricated fallback verdicts are NOT verdicts "
                               "— re-queued for a real committee look")

        # Advisory transparency: a verdict on a name owned ONLY by a
        # MECHANICAL sleeve (ETF rotation) or the strategic core is
        # informational — those sleeves size by rule, not by committee, so
        # "the committee said trim XLK" can never move the position. Say so
        # (operator asked why an XLK verdict produced no action, 2026-06-12).
        try:
            rot_held = set(state.get_sleeve("etf_rotation").get("positions", {}))
            core_held: set[str] = set()
            from firm import allocation as _alloc2
            core_held |= set(_alloc2.core_weights())
            advisory = {t: (verdicts[t]["verdict"].get("rating"))
                        for t in verdicts
                        if (t in rot_held or t in core_held)}
            if advisory:
                state.journal("verdict_advisory_only", verdicts=advisory,
                              note="mechanical/core holding — sized by rule, "
                                   "committee verdict is informational + graded, "
                                   "never traded")
        except Exception:
            pass

        # The committee couldn't really think this cycle if it produced NO verdicts
        # (workups failed) OR only DEGRADED quant-only verdicts (Opus quota out / CLI
        # down). Either way treat it as an outage: do NOT stamp fresh (catch-up retries
        # when the brain returns), do NOT trade or mutate the bench on quant-only
        # guesses, and tell the operator the REAL cause. (operator 2026-06-25: the
        # subscription Opus quota ran dry -> every cycle returned all-BUY degraded
        # verdicts, MU's earnings beat never got a real upgrade, and it read as a
        # confusing "degraded" with no actionable signal.)
        _real_verdicts = {t: v for t, v in verdicts.items()
                          if not (v.get("verdict") or {}).get("degraded")}
        if candidates and not _real_verdicts:
            from firm import brain, alerts
            _quota = brain.quota_recently_hit()
            # A 401 login-expiry is distinct from a quota window: it does NOT
            # self-heal on reset and needs an interactive `claude login`. Name it
            # so the operator fixes the RIGHT thing (operator 2026-07-03).
            _auth = (not _quota) and brain.auth_recently_failed()
            _cause = ("opus_quota" if _quota
                      else "cli_auth_expired" if _auth
                      else ("no_verdicts" if not verdicts else "cli_degraded"))
            if _quota:
                _msg = ("Opus subscription QUOTA EXHAUSTED — research paused; the firm "
                        "retries automatically once the quota window resets. No trades "
                        "or bench changes were made on the quant-only fallback.")
                _stat = "Opus quota exhausted — will retry on reset"
            elif _auth:
                _msg = ("Claude CLI LOGIN EXPIRED — the research brain can't "
                        "authenticate (HTTP 401). Run `claude login` in a terminal to "
                        "restore it; the firm retries automatically once auth returns. "
                        "No trades or bench changes were made on the quant-only fallback.")
                _stat = "CLI login expired — run `claude login`"
            else:
                _msg = ("the research floor produced no real committee verdicts "
                        "(CLI/network outage) — running quant-only; NOT acted on. "
                        "Retries when connectivity/CLI returns.")
                _stat = "research outage — will retry when CLI returns"
            state.journal("cycle_no_real_verdicts", cause=_cause,
                          candidates=len(candidates), degraded=len(verdicts))
            alerts.alert("RESEARCH PAUSED — no committee verdicts", _msg,
                         severity="critical")
            state.save("cycle_retry", {"needed": True,
                                       "ts": datetime.now(timezone.utc).isoformat()})
            state.set_status("error", _stat, 0, running=False)
            return {"success": False, "error": _cause}
        # RESEARCH COVERAGE (2026-07-25 audit). Every outage guard in the firm was
        # a ZERO threshold, never a coverage threshold: ONE surviving verdict out
        # of 32 candidates counted as a completed cycle by the freshness stamp, the
        # catch-up scheduler, this retry flag and the health desk. Live 2026-07-25:
        # `research_deadline_hit completed:1 dropped:[31 names, 13 of them HELD]`,
        # `cycle_done verdicts:{"TXN":"HOLD"}` — cycle_retry was cleared and health
        # reported "research cycle ok, last cycle 12.9h ago" four minutes after a
        # RESEARCH FLOOR STALLED critical. 13 held positions worth ~$12k got no
        # fresh verdict and nothing retried. Coverage is now measured explicitly.
        # latest_asof IS still stamped, deliberately: withholding it would make the
        # catch-up scheduler refire unbounded, and a catch-up storm has starved this
        # fund before. The retry flag plus a loud alert carry the signal instead.
        _coverage = (len(_real_verdicts) / len(candidates)) if candidates else 1.0
        _min_cov = float(getattr(config, "MIN_VERDICT_COVERAGE", 0.6))
        _thin = bool(candidates) and _coverage < _min_cov
        state.save("cycle_retry", {"needed": _thin,
                                   "ts": datetime.now(timezone.utc).isoformat(),
                                   "cause": "thin_coverage" if _thin else "",
                                   "coverage": round(_coverage, 3),
                                   "real_verdicts": len(_real_verdicts),
                                   "candidates": len(candidates)})
        if _thin:
            state.journal("research_coverage_thin", coverage=round(_coverage, 3),
                          real_verdicts=len(_real_verdicts),
                          candidates=len(candidates), floor=_min_cov)
            from firm import alerts as _al_cov
            _al_cov.alert(
                f"THIN RESEARCH COVERAGE — {len(_real_verdicts)}/{len(candidates)} verdicts",
                f"Only {_coverage * 100:.0f}% of this cycle's candidates produced a real "
                f"committee verdict (floor {_min_cov * 100:.0f}%). The rest were dropped by "
                f"the research deadline, so any HELD name among them is running on its "
                f"last verdict. A retry is queued. Check the research floor (RAM, CLI "
                f"quota, Opus reachability).",
                severity="critical")

        # News-feed coverage: per-name "no headlines" is normal, but if MOST
        # of the floor comes back empty the feed itself is broken (yfinance
        # outage, schema change) — tell the operator. A held position trading
        # blind to news is exactly what the operator asked to be alerted to.
        # (_had_news/_news_tier hoisted to module level 2026-07-16 — the 8:30
        # held cycle's history row needs the same grading context, F8.)
        with_news = sum(1 for v in verdicts.values() if _had_news(v))
        if len(verdicts) >= 5 and with_news / len(verdicts) < 0.4:
            from firm import alerts
            state.journal("news_feed_degraded", with_news=with_news,
                          total=len(verdicts))
            alerts.alert("NEWS FEED DEGRADED",
                         f"only {with_news}/{len(verdicts)} candidates had "
                         "headlines this cycle — the news source may be down. "
                         "Verdicts are running on price + fundamentals alone; "
                         "check the feed if this persists.")

        # SCHEDULE SPLIT (operator 2026-07-16, refined): the 8:30 held-book
        # cycle is its OWN research-to-trade cycle — this cycle does NOT carry
        # its decisions. Held names with a fresh premarket verdict simply
        # don't appear here (skipped at candidate assembly, exempt-journaled),
        # and their freed workups went to a WIDER new-name intake
        # (config.TIER_SLOTS). No merge, no double-grading, no fake stamps.
        research_log = state.load("research", {"history": []})
        research_log["latest"] = {
            k: {**{kk: vv for kk, vv in v.items() if kk != "packet"},
                # carry the factor lenses out of the (stripped) packet so the
                # dashboard can show momentum/value/quality per name
                "factor_scores": (v.get("packet") or {}).get("factor_scores") or {},
                # DECISION-TIME SCREEN SCORE (2026-07-28). Without this the
                # report card could grade the JUDGE but never the SCREEN, so the
                # question "does a higher screen rank actually predict a better
                # forward return" had no data behind it — and the screen decides
                # which names the judge ever sees. It is also the evidence the
                # review needs to calibrate QUANT_COMPRESS_BOUNDS: the whole
                # point of the log rescale is that leaning into extreme momentum
                # tails is now a bounded, MEASURED choice rather than a constant.
                "quant_screen_score": (v.get("packet") or {}).get("quant_screen_score"),
                "recent_earnings": (v.get("packet") or {}).get("recent_earnings") or {},
                "next_earnings": (v.get("packet") or {}).get("next_earnings") or {}}
            for k, v in verdicts.items()}
        research_log["latest_asof"] = datetime.now(timezone.utc).isoformat()
        research_log.setdefault("history", []).append({
            "asof": research_log["latest_asof"],
            # (AUDIT 2026-07-16 F0: `_persist` was a leftover of the removed
            # premarket-merge design — 3 uses, 0 assignments, would NameError
            # the whole cycle before the sleeves traded. The 8:30 cycle keeps
            # its own history row; this row is simply this cycle's verdicts.)
            "verdicts": {k: v["verdict"] for k, v in verdicts.items()},
            # Judgment context: the price and earnings date the committee SAW
            # — cooldowns key off the earnings date ("wait for the print"
            # reopens ON the print day), audits key off the judged price
            "context": {
                k: {"px": ((v.get("packet") or {}).get("technicals") or {}).get("price"),
                    "earnings": ((v.get("packet") or {}).get("next_earnings") or {}).get("date"),
                    # session (BMO/AMC) so a reopen waits for the ACTUAL print:
                    # an after-close report "today" isn't out at the 9:45 cycle
                    "earnings_session": ((v.get("packet") or {}).get("next_earnings") or {}).get("session"),
                    # whether the committee SAW news on this name — lets the
                    # report card prove if news-backed verdicts beat blind ones
                    "had_news": _had_news(v),
                    # the TRUST TIER of that news (trusted-wire vs aggregator vs
                    # blind) — the report card learns whether RELIABLE sources
                    # predict and aggregator noise doesn't (operator 2026-06-14:
                    # "this source isn't reliable / this news moves / this is a miss")
                    "news_tier": _news_tier(v),
                    # the three factor lenses at judgment time — the report
                    # card correlates each with forward outcome to show which
                    # factor (momentum/value/quality) actually predicts winners
                    "factors": (v.get("packet") or {}).get("factor_scores") or {},
                    # DECISION-TIME SCREEN SCORE — retained on the HISTORY row
                    # (operator 2026-08-19). It was written to research_log
                    # ["latest"] only, and learning.verdict_report_card read it
                    # off the raw VERDICT dict, which never carried it either:
                    # 0 of 1,467 retained verdict rows had the key, so
                    # `screen_predictiveness` returned {} for every card ever
                    # produced. That is the one measurement built to answer
                    # "do the names my SCREEN surfaces actually beat SPY?" —
                    # the question the fund most needs answered, since the
                    # screen decides which names the committee ever sees and
                    # every rating bucket grades NEGATIVE on forward excess
                    # (BUY -2.0, HOLD -4.1, SELL -9.8) while the ORDERING is
                    # correct. A screen feeding a losing universe is invisible
                    # to a card that can only grade the judge. This does not
                    # backfill: it starts the clock.
                    "quant_screen_score": (v.get("packet") or {}).get(
                        "quant_screen_score"),
                    # geopolitical / macro news TOPICS the committee saw when it
                    # judged — recorded so the review can later test whether verdicts
                    # made during live geopolitical-news periods behave differently
                    # (operator 2026-06-15: "add it to the learning curve too")
                    "macro_news": (((v.get("packet") or {}).get("macro_brief") or {})
                                   .get("geopolitical_news_topics") or []),
                    # whether the firm OWNED this name AT JUDGMENT TIME — the report
                    # card grades an owned-name HOLD as "keep the winner" (correct if
                    # it then beats SPY) and must key off ownership-AT-VERDICT, not
                    # CURRENT holdings, or a name bought/sold later mis-grades the old
                    # row (adversarial review 2026-06-29).
                    "owned": k in held,
                    # DATA-COVERAGE telemetry (purpose review #6iii): packets are
                    # stripped from history, so the conditions a verdict was made
                    # under were unrecoverable — the loop could never learn WHICH
                    # data conditions produce bad verdicts. Persist the grain.
                    "n_headlines": len((v.get("packet") or {}).get("news") or []),
                    "fund_source": ((v.get("packet") or {}).get("fundamentals")
                                    or {}).get("source"),
                    "degraded_reason": (v.get("verdict") or {}).get("degraded_reason")
                    or ("degraded" if (v.get("verdict") or {}).get("degraded") else None)}
                for k, v in verdicts.items()
            },
            # Per-desk scores ride along so the performance review can
            # diagnose DESK biases, not just verdict outcomes
            "desk_scores": {
                k: {d: (v["research"].get(d) or {}).get("score")
                    for d in (v.get("research") or {})
                    if isinstance(v["research"].get(d), dict)}
                for k, v in verdicts.items() if v.get("research")
            },
        })
        research_log["history"] = research_log["history"][-120:]
        state.save("research", research_log)
        # Conviction anchor table (fix brief 2026-07-27 #10): roll this cycle's
        # component scores into the p10/p50/p90 anchors the judge sees next
        # cycle — "typical" becomes a measured number instead of a felt one.
        try:
            debate.record_component_anchors(
                {k: v.get("verdict") for k, v in verdicts.items()})
        except Exception:
            state.journal("conviction_anchor_record_failed")
        # Research pass concluded and its verdicts are on disk — the mid-pass
        # checkpoint's job is over (operator 2026-07-21). Clearing here (the
        # LAST moment) means a crash anywhere before this line still resumes.
        _clear_research_checkpoint()

        # OPEN-LOOP ALARM (cognition audit #1b): if this cycle issued verdicts
        # while BOTH feedback channels (lessons + scorecard digest) were empty,
        # the judge flew blind — 357 verdicts happened that way and nothing
        # said so. Journal it loudly so a dead loop can never be silent again.
        try:
            if verdicts and not (learning.lessons_context()
                                 or learning.digest_context()):
                state.journal("judge_flying_blind", verdicts=len(verdicts),
                              note="no lessons + no scorecard digest reached "
                                   "the judge this cycle")
        except Exception:
            pass

        # Fix (2026-07-01): a real BUY must NEVER be silently dropped for a lost
        # asset 'kind'. _research_one stamps kind (cycle.py:238), but a RAM-starved
        # deadline/fallback path can leave a verdict entry with kind unset — and
        # sleeves._ranked_buys' asset-class filter then eats the name with NO
        # journal line (MU, 2026-07-01: alpha_stocks had a free slot + $5,239 cash
        # and never bought it, never explained why). Backfill kind from the
        # authoritative (ticker, kind) candidate map and journal every repair, so
        # the path that nulls it stays visible instead of quietly losing a trade.
        _cand_kind = dict(candidates)
        for _t, _v in verdicts.items():
            if isinstance(_v, dict) and _v.get("kind") not in ("stock", "etf"):
                _k = _cand_kind.get(_t)
                if _k:
                    state.journal("verdict_kind_backfilled", symbol=_t,
                                  was=_v.get("kind"), kind=_k)
                    _v["kind"] = _k

        sectors = {t: (v["packet"].get("fundamentals") or {}).get("sector") or ""
                   for t, v in verdicts.items()}

        # STALE-CYCLE GUARD (2026-07-25 audit). The research floor is bounded only
        # by `as_completed(timeout=deadline_min*60)`, and that timer does not
        # survive a host suspend — nothing compared wall-clock now to the cycle
        # start. Live incident: a cycle began 2026-07-24 17:51 ET, the box slept
        # ~12h, the deadline fired on resume, and the sleeves traded and swept
        # exits at 07:09-07:13 ET on SATURDAY against Friday's closing marks
        # (OSCR "hard stop -12.1%", CON "trailing stop -16.7%"). Decisions made
        # from one session's prices must never execute in another: if the cycle
        # has crossed out of the ET day it started in, skip trading AND the exit
        # sweep and requeue, rather than acting on marks that are now stale.
        # Two independent staleness tests (refined 2026-07-25 before first use: an
        # ET CALENDAR-DATE comparison was too blunt — a Saturday-evening cycle
        # rolling past midnight into Sunday would abort trading even though no
        # market session had intervened and nothing about the prices had changed).
        #   (a) a regular session OPENED between start and now — the decisions were
        #       priced for a different session than the one they'd execute into;
        #   (b) the cycle simply ran absurdly longer than its own research budget,
        #       which is what the live incident looked like (elapsed 796 min against
        #       a 150-min deadline).
        _stale_cycle, _stale_why = False, ""
        try:
            from datetime import timedelta as _td
            from zoneinfo import ZoneInfo as _ZI
            from firm import market_hours as _mh_stale
            _ET = _ZI("America/New_York")
            _started = datetime.fromisoformat(_cycle_started_iso)
            _now = datetime.now(timezone.utc)
            _elapsed_min = (_now - _started).total_seconds() / 60.0
            _max_min = float(getattr(config, "STALE_CYCLE_MAX_MIN", 240.0))
            if _elapsed_min > _max_min:
                _stale_cycle = True
                _stale_why = (f"ran {_elapsed_min:.0f} min, past the {_max_min:.0f} "
                              "min staleness bound")
            else:
                # Did any trading session's 9:30 ET open fall inside the window?
                _d = _started.astimezone(_ET)
                _end = _now.astimezone(_ET)
                while _d.date() <= _end.date():
                    _open_et = _d.replace(hour=9, minute=30, second=0, microsecond=0)
                    if (_started.astimezone(_ET) < _open_et <= _end
                            and _mh_stale.is_trading_day(_open_et)):
                        _stale_cycle = True
                        _stale_why = (f"a regular session opened "
                                      f"({_open_et:%Y-%m-%d %H:%M} ET) mid-cycle")
                        break
                    _d += _td(days=1)
        except Exception:
            _stale_cycle = False        # unknown -> behave as before, never block on a clock bug
        if _stale_cycle:
            state.journal("cycle_stale_skipped_trading",
                          started=_cycle_started_iso,
                          now=datetime.now(timezone.utc).isoformat(),
                          reason=_stale_why or "cycle outlived its session")
            state.save("cycle_retry", {"needed": True,
                                       "ts": datetime.now(timezone.utc).isoformat(),
                                       "cause": "stale_cycle"})
            from firm import alerts as _al_stale
            _al_stale.alert(
                "CYCLE SPANNED SESSIONS — trading skipped",
                f"This cycle started {_cycle_started_iso[:16]}Z and {_stale_why} "
                f"(the usual cause on this host is the PC sleeping mid-cycle). Its "
                f"prices are stale, so the sleeves did NOT trade and the exit sweep "
                f"was skipped rather than book decisions at another session's marks. "
                f"A retry is queued.")
            state.set_status("idle", "cycle spanned sessions — trading skipped", 100,
                             running=False)
            return {"success": False, "error": "stale_cycle", "verdicts": len(verdicts)}

        # Each sleeve is isolated: one sleeve's failure must never block the
        # others or the end-of-cycle exit sweep
        for pct, label, fn in (
            (75, "Alpha Stocks trading", lambda: sleeves.run_alpha_stocks(verdicts, sectors, mode)),
            (82, "Core Blend rebalancing", lambda: sleeves.run_core_blend(verdicts, sectors, mode)),
            (88, "Calls desk selecting contracts", lambda: sleeves.run_calls_only(verdicts, mode)),
            (93, "ETF rotation check", lambda: sleeves.run_etf_rotation(mode)),
        ):
            state.set_status("trading", label, pct)
            try:
                fn()
            except Exception as e:
                state.journal("sleeve_error", stage=label, error=str(e),
                              trace=traceback.format_exc()[-1500:])

        # Seated-ETF SELL verdicts EXECUTE (operator 2026-07-16: "when an ETF
        # becomes a sell, sell it") — a non-degraded committee SELL on the
        # parking or a core seat sells it and seats the next-best eligible,
        # hysteresis bypassed. Rotation holdings stay advisory (validated
        # mechanical strategy). Fail-soft; runs after the sleeves so the
        # advisory journal below reports only what remained advisory.
        try:
            from firm import allocation
            _demoted = allocation.apply_sell_verdicts(verdicts, mode)
            if _demoted:
                state.journal("etf_seats_demoted_by_committee", symbols=_demoted)
        except Exception:
            state.journal("etf_seat_verdict_hook_error",
                          trace=traceback.format_exc()[-300:])

        # Cross-sleeve routing decision (operator 2026-07-02): now that the
        # sleeves have all traded, RECORD which book(s) each BUY name qualified
        # for, which placed it, and why any qualified name was NOT placed —
        # surfacing a name that belonged in a second book (e.g. core) and a
        # silent drop. Pure observability; changes no trades.
        try:
            from firm import routing
            routing.record_routing(verdicts, since_ts=_cycle_started_iso)
        except Exception:
            state.journal("routing_record_error")

        state.set_status("monitor", "Exit monitor sweep", 96)
        monitor.check_all_exits()

        # The Bench refreshes from this cycle's scan + verdicts, then the firm
        # studies its own closed trades (Opus review once enough have closed)
        state.set_status("learning", "Watchlist + firm memory update", 98)
        # Held-position deterioration (2026-07-26): recompute BEFORE the watchlist
        # so the next cycle's judge prompt carries this cycle's streaks. Pure read
        # + one state write; never allowed to break the cycle.
        try:
            _det = learning.held_deterioration()
            state.save("held_deterioration", _det)
            if _det.get("n_flagged"):
                state.journal("held_deterioration",
                              n_flagged=_det["n_flagged"],
                              held_checked=_det.get("held_checked"),
                              names=[r["ticker"] for r in _det["rows"]],
                              worst=_det["rows"][0] if _det["rows"] else None)
        except Exception:
            state.journal("held_deterioration_error")
        watchlist.update(screen, verdicts)
        # A name reopened for its earnings print must be judged on the ACTUAL
        # numbers — verify they arrived, else retry next cycle then alert.
        try:
            _check_earnings_actuals(verdicts, watch_notes)
        except Exception:
            state.journal("earnings_actuals_check_error",
                          trace=traceback.format_exc()[-800:])
        # Same lock + prefetch discipline as the monitor's sweep-harvest (batch
        # review R3): app monitor threads keep running during in-app cycles, so
        # an unlocked read-modify-write here could interleave with the sweep's
        # and drop/duplicate ledger rows. SPY prefetch stays outside the lock.
        _spy_lrn = learning.prefetch_spy_series()
        with state.SLEEVE_LOCK:
            learning.record_outcomes(spy_ser=_spy_lrn)
        learning.run_review()

        state.set_status("idle", "Cycle complete", 100, running=False)
        state.journal("cycle_done", candidates=len(candidates),
                      verdicts={k: v["verdict"].get("rating") for k, v in verdicts.items()})
        return {"success": True, "verdicts": len(verdicts)}
    except Exception as e:
        state.set_status("error", f"{e}", 0, running=False)
        state.journal("cycle_error", error=str(e), trace=traceback.format_exc()[-2000:])
        return {"success": False, "error": str(e)}
    finally:
        _post_cycle_ram_clean()   # operator 2026-07-17: ram clean after every cycle
