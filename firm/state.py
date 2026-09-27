"""JSON state store + append-only journal for the firm.

Every sleeve owns one state file: state/<sleeve>.json
Shared files: state/settings.json, state/status.json, state/research.json
Journal:      state/journal.jsonl (append-only event log)
"""
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config

_LOCK = threading.Lock()

# Serializes every read-modify-write of sleeve state across the monitor
# threads and the cycle thread (same process). Coarse by design: a missed
# tick is recoverable; a lost trade record is not.
SLEEVE_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Cross-process cycle lock (2026-06-14 audit A13) ──────────────────────────
# A running cycle (headless run_cycle.py OR the in-app cycle thread) holds this
# lock so NO other process writes sleeve state at the same time (a lost write =
# a lost trade, the one unrecoverable corruption). It is PID-AWARE: the holder's
# pid is recorded so the app can tell ITS OWN in-app cycle (same pid — its
# monitors are safe under SLEEVE_LOCK and keep running) apart from a FOREIGN
# headless cycle (different pid — the app's monitors must stand down).
CYCLE_LOCK = config.STATE_DIR / "cycle.lock"
# 15 min: a live cycle heartbeats the lock every 300s, so 3 missed beats = dead.
# Much tighter than the old 3600s so an orphaned lock (a hard-killed headless run
# whose pid is also checked below) self-clears fast instead of blinding monitors
# for an hour (change-audit verify 2026-06-14).
CYCLE_LOCK_STALE_S = 900.0


def _lock_payload() -> dict:
    """The lock record. `born` is the holder process's creation time, which
    makes the identity (pid, born) instead of just pid: a recycled pid then
    reads as a DIFFERENT process and its stale lock is correctly seen as free.
    Absent when psutil is unavailable, and every reader treats a missing `born`
    as "cannot disprove" so older records stay valid."""
    born = 0.0
    try:
        import psutil
        born = float(psutil.Process(os.getpid()).create_time())
    except Exception:
        pass
    return {"pid": os.getpid(), "ts": time.time(), "born": born}


def try_claim_cycle_lock() -> bool:
    """ATOMICALLY claim the cross-process cycle lock. True = we hold it.

    (2026-09-02 full audit; four lenses.) Every caller used to do
    `if cycle_active(): return` and only then write the lock -- a check-then-act
    race whose window is the whole gap between the two. Both processes can pass
    the check, both write, and both then write sleeve state: the "lost write =
    lost trade" corruption CLAUDE.md calls the one unrecoverable failure.

    O_CREAT|O_EXCL makes creation-or-fail one filesystem operation, so exactly
    one racer wins. On collision we consult cycle_lock_holder(): our own pid is
    re-entrant (the in-app cycle refreshes), a live foreign holder loses, and a
    lock the holder logic already considers free (dead pid, recycled pid, or
    past the stale window) is removed so the next O_EXCL attempt can win it --
    still atomically, so two racers cannot both take over."""
    payload = json.dumps(_lock_payload()).encode("utf-8")
    for _ in range(3):
        try:
            fd = os.open(CYCLE_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, payload)
            finally:
                os.close(fd)
            return True
        except FileExistsError:
            holder = cycle_lock_holder()
            if holder == os.getpid():
                write_cycle_lock()          # ours already: refresh and proceed
                return True
            if holder is not None:
                return False                # genuinely held (or fresh+unreadable)
            try:
                CYCLE_LOCK.unlink(missing_ok=True)   # free: drop and re-race
            except OSError:
                return False
        except OSError as e:
            try:
                journal("cycle_lock_claim_error", pid=os.getpid(), error=str(e)[:160])
            except Exception:
                pass
            return False
    return False


def write_cycle_lock() -> None:
    """Claim/refresh the cycle lock for THIS process — ATOMICALLY (2026-09-01).
    write_text truncates, then writes: a reader landing in between (app.py's
    monitor loop reads the lock every 60 s) saw an empty/partial file, which
    cycle_lock_holder treats as 'unparseable but fresh => FOREIGN' — standing
    the app's own monitors down for a pass on every 300 s heartbeat of its OWN
    in-app cycle. Same tmp+replace pattern as state.save."""
    try:
        tmp = CYCLE_LOCK.parent / f"{CYCLE_LOCK.name}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(_lock_payload()), encoding="utf-8")
        for attempt in range(6):
            try:
                tmp.replace(CYCLE_LOCK)
                return
            except PermissionError:
                time.sleep(0.05 * (attempt + 1))
        tmp.replace(CYCLE_LOCK)
    except OSError as e:
        # (2026-09-02 full audit, live-order-path-2) NEVER silent. This is the
        # cross-process single-writer guard — "a lost write is a lost trade".
        # A blanket `except OSError: pass` here meant a lock that could not be
        # claimed looked exactly like one that was: the caller ran its cycle
        # believing it held the lock while every other reader saw no holder, so
        # the app's monitors never stood down and two processes wrote the same
        # sleeve files with no evidence anywhere. save() has differentiated
        # BOOK-vs-EPHEMERAL policy for precisely this reason; the lock had none.
        try:
            journal("cycle_lock_write_failed", pid=os.getpid(), error=str(e)[:160])
            from firm import alerts
            alerts.alert("CYCLE LOCK NOT CLAIMED",
                         f"write_cycle_lock failed for pid {os.getpid()} ({str(e)[:120]}) "
                         "— the single-writer guard is NOT held; a concurrent cycle "
                         "could write sleeve state. Check disk/permissions on "
                         "state/cycle.lock before the next cycle.", "critical")
        except Exception:
            pass


def clear_cycle_lock() -> None:
    try:
        CYCLE_LOCK.unlink(missing_ok=True)
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    """Windows-safe liveness probe — NEVER os.kill() on Windows (it can TERMINATE
    the target). Uses psutil when present; if unavailable, assume alive so a live
    cycle is never wrongly cleared (the mtime stale window remains the fallback)."""
    if pid is None or pid < 0:
        return True
    try:
        import psutil
        return psutil.pid_exists(pid)
    except Exception:
        return True


def cycle_lock_holder() -> int | None:
    """PID holding a LIVE, FRESH cycle lock, or None if absent/stale/dead-pid.
    Returns -1 for a fresh lock in the legacy bare-timestamp format (unknown
    process — fail safe). A recorded pid that is no longer running (a hard-killed
    headless cycle that never reached its finally) is treated as NOT held, so its
    orphan lock can't blind the app's monitors for the stale window."""
    try:
        if not CYCLE_LOCK.exists():
            return None
        if time.time() - CYCLE_LOCK.stat().st_mtime >= CYCLE_LOCK_STALE_S:
            return None
        raw = CYCLE_LOCK.read_text(encoding="utf-8").strip()
        try:
            _rec = json.loads(raw)
            pid = int(_rec.get("pid"))
        except (ValueError, TypeError, AttributeError):
            return -1   # legacy/unparseable but fresh -> treat as foreign-held
        if not _pid_alive(pid):
            return None   # dead-pid orphan -> not actually held
        # (2026-09-02) PID REUSE. A dead cycle's pid can be handed to an
        # unrelated process, and _pid_alive then says "held" about a lock whose
        # real owner is long gone -- blinding the app's monitors until the
        # stale window expires. Compare the recorded birth time: same pid AND
        # same birth is the same process. A record without `born` (written by
        # an older build, or where psutil was unavailable) cannot be disproved,
        # so it keeps the previous pid-only behaviour.
        _born = _rec.get("born")
        if isinstance(_born, (int, float)) and _born > 0:
            try:
                import psutil
                if abs(psutil.Process(pid).create_time() - float(_born)) > 1.0:
                    return None          # pid recycled -> not our holder
            except Exception:
                pass
        return pid
    except OSError:
        return None


def cycle_active(exclude_own: bool = False) -> bool:
    """Is a cycle currently running (cross-process)? With exclude_own=True, the
    caller's OWN cycle (same pid) does not count — used by the app so its in-app
    cycle doesn't make its own monitors stand down."""
    holder = cycle_lock_holder()
    if holder is None:
        return False
    return not (exclude_own and holder == os.getpid())


def _path(name: str) -> Path:
    return config.STATE_DIR / f"{name}.json"


_last_bak: dict[str, float] = {}
_BAK_INTERVAL_S = 60.0


def load(name: str, default: Any = None) -> Any:
    """Load state with corruption recovery. A corrupt sleeve file must NEVER
    silently become an empty default — that would erase the book. On parse
    failure we restore from the .bak written alongside every save.

    A LOCKED READ IS NOT A CORRUPT FILE (2026-07-28 operator investigation).
    save() already retries `tmp.replace(p)` eight times because "os.replace
    fails with WinError 5 if another process holds the target open" — but the
    READ side treated that same sharing violation (a PermissionError, i.e. an
    OSError) as corruption and rolled the live file back to a .bak that is up
    to _BAK_INTERVAL_S old. On heartbeat/status that is noise; on a SLEEVE it
    is a silent 60-second book rollback — the one unrecoverable corruption the
    whole single-writer design exists to prevent. The evidence was in the
    journal: 34 restores distributed 23 heartbeat / 9 status / 2 sleeve, i.e.
    almost exactly the write-FREQUENCY ranking of those files, which is the
    signature of a read/replace race, not of failing disk sectors (a bad disk
    has no reason to prefer the hottest file). state_save_skipped_locked fired
    29 times on this host, proving the violations happen here routinely.
    So: retry a failed READ with save()'s own backoff, and only fall through to
    the backup on a genuine parse failure or an OSError that survives every
    attempt. A read that recovers on retry is journaled as what it is."""
    p = _path(name)
    if not p.exists():
        return {} if default is None else default
    data = _sentinel = object()
    _io_err: OSError | None = None
    for _attempt in range(8):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            break
        except json.JSONDecodeError:
            break                     # real corruption — go to the backup now
        except OSError as e:          # locked/vanishing mid-replace — transient
            _io_err = e
            time.sleep(0.05 * (_attempt + 1))
    if data is _sentinel:
        # Genuine failure — a parse error, or a lock that outlived every retry.
        _why = ("io:" + type(_io_err).__name__) if _io_err else "parse_error"
        bak = p.with_suffix(".bak")
        try:
            data = json.loads(bak.read_text(encoding="utf-8"))
            # ATOMIC restore (2026-07-04): this used to write_text() straight
            # into the live file — any concurrent reader (keepalive, a second
            # process's load) caught a half-written file, "restored" from it,
            # and the corruption cascaded into .bak until both copies were
            # garbage (heartbeat.json went state_corrupt_unrecoverable and the
            # keepalive restarted a healthy app). Same tmp+replace as save().
            rtmp = p.parent / f"{p.name}.{os.getpid()}.rtmp"
            rtmp.write_text(json.dumps(data, indent=2, default=str),
                            encoding="utf-8")
            try:
                rtmp.replace(p)
            except PermissionError:
                time.sleep(0.05)
                rtmp.replace(p)
            journal("state_restored_from_backup", file=name, reason=_why)
        except (OSError, json.JSONDecodeError):
            journal("state_corrupt_unrecoverable", file=name, reason=_why)
            return {} if default is None else default
    elif _io_err is not None:
        # Recovered on retry: NOT corruption, but worth seeing — a rising rate
        # here means writers and readers are colliding more often.
        journal("state_read_retried", file=name,
                error=type(_io_err).__name__)
    # A partial or empty dict on disk must never KeyError a caller that asked
    # for a dict default (2026-06-10: ledger.json == {} crashed the cycle).
    # Shallow merge — the file's own keys always win, missing keys fall back.
    if isinstance(default, dict) and isinstance(data, dict):
        return {**default, **data}
    return data


# Rewritten every sweep — losing ONE write is harmless (the next beat lands in
# seconds); the same set load()/risk.py treat as benign-on-restore.
_EPHEMERAL_STATE = {"heartbeat", "status", "headless_sweep"}

# ── CRITICALITY TIERS (operator 2026-08-19) ─────────────────────────────────
# 'state integrity' graded every non-ephemeral file as "CRITICAL state file(s)
# recovered from backup — check disk/writes", even though its own comment
# defined CRITICAL as "the books: sleeves, ledger, watchlist, lessons,
# research, settings". The implementation was a two-name BLACKLIST, so all ~60
# other state files — health, health_streaks, report_card_*, ig_*, routing_*,
# reopen_path_inert — were graded as money state by default. The check has been
# red since 14:49 today on exactly one event: reopen_path_inert restored from
# its .bak. That is the health desk's own inertness tracker; losing it costs a
# telemetry reset, not a dollar, and it does not mean the disk is failing.
#
# Declaring the tiers HERE (not in health.py) keeps one definition next to the
# reader/writer that owns these names. Ordering matters:
#   BOOK      -> money and decisions. A restore here is a real alarm.
#   EPHEMERAL -> liveness stamps, regenerated in seconds.
#   DERIVED   -> everything the firm RECOMPUTES from the books + journal.
# UNKNOWN NAMES DELIBERATELY FALL THROUGH TO BOOK, never to DERIVED: an
# allow-list is a silent scope limit, and the failure mode we must never ship
# is a NEW book file quietly landing in the benign bucket. Adding a derived
# file is a conscious edit; forgetting to classify one is loud.
_BOOK_STATE = {
    "ledger", "watchlist", "lessons", "research", "settings", "firm",
    "bench_ledger", "equity_history", "dividends_paid", "eod",
    "pending_open_entries", "risk_events", "breaker_trips", "tuning",
    "conviction_anchors", "etf_seats", "rotation_history",
}


def state_tier(name: str) -> str:
    """BOOK | EPHEMERAL | DERIVED for a state file name. Unknown -> BOOK."""
    n = str(name)
    if n in _EPHEMERAL_STATE:
        return "EPHEMERAL"
    if n.startswith("sleeve_") or n in _BOOK_STATE:
        return "BOOK"
    if n in _DERIVED_STATE:
        return "DERIVED"
    return "BOOK"


# Recomputable from the books + journal. Losing one costs a rebuild, not money.
_DERIVED_STATE = {
    "health", "health_streaks", "degraded_health", "standing_defects",
    "reopen_path_inert", "inert_sleeves", "recon_drift_incident",
    "reconcile_display", "report_card_digest", "report_card_stats",
    "edge_health", "held_deterioration", "conviction_spread",
    "seat_switch_scorecard", "routing_history", "routing_report",
    "ig_findings_latest", "ig_reviewed_asof", "ig_briefs_filed",
    "ig_triage_incident", "fix_briefs", "self_test", "alerts",
    "cycle_stats", "cycle_retry", "midday_refresh", "ram_sweep_0915",
    "research_backlog", "research_checkpoint", "research_progress",
    "research_requests", "market_scan", "universe_symbols",
    "tradable_universe", "etf_pool", "sector_regime", "vol_regime",
    "macro_calendar", "benchmark_feed", "earnings_docs_cache",
    "transcript_cache", "fmp_usage", "fmp_blocklist",
}


def save(name: str, data: Any) -> None:
    _skipped_locked = False
    with _LOCK:
        p = _path(name)
        # Throttled backup of the last-known-good version (atomic, so a kill
        # mid-backup can never corrupt the safety net itself)
        now = datetime.now(timezone.utc).timestamp()
        if p.exists() and now - _last_bak.get(name, 0) > _BAK_INTERVAL_S:
            try:
                cur_txt = p.read_text(encoding="utf-8")
                # Never promote garbage into the safety net (2026-07-04): if
                # the live file is corrupt at backup time, copying it forward
                # destroys the last-known-good .bak — the exact double-
                # corruption that made heartbeat.json unrecoverable. A corrupt
                # current file keeps the old .bak; load() restores from it.
                json.loads(cur_txt)
                bk_tmp = p.parent / f"{p.name}.{os.getpid()}.baktmp"
                bk_tmp.write_text(cur_txt, encoding="utf-8")
                bk_tmp.replace(p.with_suffix(".bak"))
                _last_bak[name] = now
            except (OSError, json.JSONDecodeError):
                pass
        # PID-unique tmp: the app, headless cycles, and scheduled tasks are
        # SEPARATE processes — a shared <name>.tmp let two writers interleave
        # into the same scratch file and replace the target with garbage
        # (2026-06-10: heartbeat.json corruption). _LOCK only guards one process.
        tmp = p.parent / f"{p.name}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        # Windows: os.replace fails with WinError 5 if another process holds
        # the target open for a read at that instant (observed 2026-06-10 on
        # heartbeat.json). Brief retry — readers finish in milliseconds.
        for attempt in range(8):
            try:
                tmp.replace(p)
                break
            except PermissionError:
                time.sleep(0.05 * (attempt + 1))
        else:
            try:
                tmp.replace(p)   # final attempt — a real lock surfaces loudly
            except PermissionError:
                # EPHEMERAL files only (2026-07-04): under host-RAM starvation a
                # reader can hold heartbeat.json past every retry, and the raise
                # here crashed the whole monitor sweep iteration (the recurring
                # monitor_error/calls_monitor_error WinError 5, 07-02..07-04).
                # Skipping one heartbeat write is harmless — the next beat lands
                # in seconds. BOOKS STILL RAISE: money state must never fail
                # silently.
                if name not in _EPHEMERAL_STATE:
                    raise
                _skipped_locked = True
                try:
                    tmp.unlink()
                except OSError:
                    pass
    # journal() takes _LOCK too (non-reentrant) — log only after release
    if _skipped_locked:
        journal("state_save_skipped_locked", file=name)


def journal(event: str, **fields: Any) -> None:
    """Best-effort by design: a full disk must never crash a monitor loop
    just because it couldn't write a log line."""
    rec = {"ts": _now(), "event": event, **fields}
    p = config.STATE_DIR / "journal.jsonl"
    try:
        with _LOCK:
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, default=str) + "\n")
            # Rotation via tmp+replace — never truncate the audit trail in place
            if p.stat().st_size > config.JOURNAL_MAX_BYTES:
                lines = p.read_text(encoding="utf-8").strip().splitlines()
                tmp = p.with_suffix(".jtmp")
                tmp.write_text("\n".join(lines[-config.JOURNAL_KEEP_LINES:]) + "\n",
                               encoding="utf-8")
                tmp.replace(p)
    except OSError:
        pass


def read_journal(limit: int = 200) -> list[dict]:
    p = config.STATE_DIR / "journal.jsonl"
    if not p.exists():
        return []
    lines = p.read_text(encoding="utf-8").strip().splitlines()
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return list(reversed(out))


# ── Settings ──────────────────────────────────────────────────────────────────

DEFAULT_SETTINGS = {
    "mode": "paper",                 # "paper" | "live"
    "depth": config.DEFAULT_DEPTH,   # "fast" | "standard" | "full"
    "auto_monitor": True,
}


def get_settings() -> dict:
    s = load("settings", {})
    merged = {**DEFAULT_SETTINGS, **s}
    if merged != s:
        save("settings", merged)
    return merged


def set_settings(**updates: Any) -> dict:
    s = get_settings()
    s.update(updates)
    save("settings", s)
    journal("settings_changed", **updates)
    return s


# ── Sleeve state ──────────────────────────────────────────────────────────────

def default_sleeve_state(sleeve: str) -> dict:
    cfg = config.SLEEVES[sleeve]
    return {
        "sleeve": sleeve,
        # Immutable birth timestamp — benchmark comparisons anchor here.
        # NEVER derive inception from equity_curve[0]: the curve trims at
        # 2000 points and the baseline would silently slide forward
        # (2026-06-11 operator report — caught one day before the trim hit)
        "inception": _now(),
        "cash": cfg["paper_capital"],
        "starting_capital": cfg["paper_capital"],
        "positions": {},        # symbol -> position dict
        "pending_orders": [],   # live orders awaiting fill confirmation
        "equity_curve": [],     # [{ts, equity}]
        "peak_equity": cfg["paper_capital"],
        "day_pnl_anchor": {"date": "", "equity": cfg["paper_capital"]},
        "halted": False,
        "halt_reason": "",
        "last_rotation": "",
        "closed_trades": [],
    }


def get_sleeve(sleeve: str) -> dict:
    s = load(f"sleeve_{sleeve}", None)
    if not s:
        s = default_sleeve_state(sleeve)
        if _path(f"sleeve_{sleeve}").exists():
            # File exists but is unrecoverably corrupt: the real book is
            # UNKNOWN. A fresh default must never quietly start trading —
            # halt it and demand human eyes.
            s["halted"] = True
            s["halt_reason"] = ("STATE CORRUPT: book reset to default after "
                                "unrecoverable file corruption — reconcile "
                                "manually, then clear halt")
            journal("sleeve_corrupt_halted", sleeve=sleeve)
            try:
                from firm import alerts, riskbrain
                riskbrain.record_halt(sleeve, "state_corrupt", s["halt_reason"])
                alerts.alert(f"STATE CORRUPT: {sleeve}",
                             "Book unrecoverable — sleeve halted. Open Claude in "
                             "ai-fund and ask it to reconcile this sleeve against "
                             "the broker/journal, then clear the halt.")
            except Exception:
                pass
        save(f"sleeve_{sleeve}", s)
    s.setdefault("pending_orders", [])
    # Migration: sleeves born before the inception field anchor to their
    # earliest surviving curve point (exact while the curve is untrimmed)
    if not s.get("inception"):
        curve = s.get("equity_curve") or []
        s["inception"] = curve[0]["ts"] if curve else _now()
        save(f"sleeve_{sleeve}", s)
    return s


def save_sleeve(sleeve: str, data: dict) -> None:
    save(f"sleeve_{sleeve}", data)


def set_status(stage: str, detail: str = "", pct: float = 0.0, running: bool = True) -> None:
    save("status", {"ts": _now(), "stage": stage, "detail": detail,
                    "pct": pct, "running": running})


def get_status() -> dict:
    return load("status", {"stage": "idle", "detail": "", "pct": 0, "running": False})
