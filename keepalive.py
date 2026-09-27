"""Keepalive — resurrect the dashboard app (and its 1s options monitor) if it
ever dies. Run by the 'LochRaven Keepalive' scheduled task every 5 minutes."""
import os
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

# Dead-man timer (2026-08-13): a keepalive instance that hangs (famine-stretched
# import, wedged urllib probe, stuck verify loop) becomes a ZOMBIE — the task's
# IgnoreNew policy then refuses every future 5-min firing, which left the fund
# dead 65+ minutes with no rescuer able to run (observed 2026-08-12 and -13).
# The task's ExecutionTimeLimit is 72h and needs admin to change; this timer is
# the same protection at the layer we own. The whole job takes ~3 min by design.
_dead_man = threading.Timer(480, lambda: os._exit(3))
_dead_man.daemon = True
_dead_man.start()

BASE = Path(__file__).parent
DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
LOG_MAX_BYTES = 5_000_000
LOG_KEEP_LINES = 2000


def rotate_logs() -> None:
    """Janitor duty: months of scheduled-task output must not eat the disk."""
    for log in (BASE / "logs").glob("*.log"):
        try:
            if log.stat().st_size > LOG_MAX_BYTES:
                lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
                log.write_text("\n".join(lines[-LOG_KEEP_LINES:]) + "\n",
                               encoding="utf-8")
        except OSError:
            continue


def _kill_port_holder(port: int) -> None:
    """Kill whatever is LISTENING on `port` so a fresh app.py can bind. A hung-but-
    listening app holds the port and makes every relaunch crash on a bind conflict;
    this is the missing step that left the fund dark for hours (2026-06-22). Best
    effort, never raises. Runs with this task's privilege — a normally-launched app
    (this same Limited task) is reachable; a stray elevated instance needs a manual
    elevated kill (rare, one-off)."""
    killed = []
    try:
        import psutil
        for c in psutil.net_connections(kind="inet"):
            if (c.laddr and c.laddr.port == port
                    and c.status == psutil.CONN_LISTEN and c.pid):
                try:
                    p = psutil.Process(c.pid)
                    p.terminate()
                    try:
                        p.wait(timeout=5)
                    except psutil.TimeoutExpired:
                        p.kill()
                    killed.append(c.pid)
                except Exception as e:
                    print(f"could not kill pid {c.pid} on :{port}: {e}")
    except Exception:
        # psutil missing / net_connections denied -> netstat + taskkill fallback
        try:
            out = subprocess.check_output(["netstat", "-ano", "-p", "TCP"],
                                          text=True, errors="replace")
            for line in out.splitlines():
                parts = line.split()
                if (len(parts) >= 5 and parts[-1].isdigit()
                        and parts[1].endswith(f":{port}") and "LISTENING" in line):
                    subprocess.run(["taskkill", "/F", "/PID", parts[-1]],
                                   capture_output=True)
                    killed.append(parts[-1])
        except Exception as e:
            print(f"port-{port} kill fallback failed: {e}")
    if killed:
        print(f"freed port {port}: killed {killed}")
        import time as _t
        _t.sleep(2)        # let the OS release the socket before we rebind


def _norm_path(path) -> str:
    """Comparable form of a filesystem path: absolute, forward slashes, and
    case folded on Windows (NTFS is case insensitive and command lines are
    not canonical)."""
    norm = os.path.abspath(str(path)).replace("\\", "/")
    return norm.lower() if os.name == "nt" else norm


def _is_fund_app(proc, target: str) -> bool:
    """Does `proc`'s command line end in THE fund's app.py (BASE / app.py)?
    Only the last argument is inspected and only by resolved path, so another
    service's app.py on a shared VPS is never touched. A relative last
    argument is anchored at the process's own cwd when psutil can read it,
    else at ours."""
    args = [str(a) for a in (proc.info.get("cmdline") or [])]
    if not args:
        return False
    last = args[-1].replace("\\", "/")
    if os.path.basename(last).lower() != "app.py":
        return False
    if not os.path.isabs(last):
        try:
            last = os.path.join(proc.cwd(), last)
        except Exception:
            pass
    return _norm_path(last) == target


def _kill_all_app_instances() -> int:
    """Kill EVERY instance of THE fund's app.py before relaunching, not only
    the port holder.

    2026-09-17 audit found two fund apps alive (175 MB and 22 MB). A starved
    second launch that never bound the port survived every keepalive pass
    because only the listener was ever killed. Review the same day: match by
    resolved path against BASE / app.py only, never by a bare "app.py"
    suffix, so another service's app.py on a shared VPS is never killed."""
    try:
        import psutil
    except ImportError:
        return 0
    target = _norm_path(BASE / "app.py")
    killed = 0
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            if not _is_fund_app(p, target):
                continue
            p.kill()
            p.wait(timeout=5)
            killed += 1
        except (psutil.Error, OSError):
            continue
    return killed


# Monitors heartbeat every ~300s; heartbeats older than this mean the process is
# genuinely HUNG (threads blocked), not merely slow under host RAM pressure.
HEARTBEAT_STALE_MIN = 12.0

# COVERAGE beats are NOT liveness beats (operator 2026-08-19). app.py writes
# `equity_sweep` ONLY inside `if session_open and _auto and not _headless:` —
# it deliberately measures that the book was actually swept, not that the thread
# is alive, and the loop's own comment says so ("Written only on a real sweep").
# `calls_monitor` and `equity_monitor` beat UNCONDITIONALLY and are the real
# liveness inputs. Grading max() across all three would therefore see a
# legitimately quiet overnight/weekend `equity_sweep` as a hang and relaunch the
# app every 5 minutes until the opening bell. Filter coverage beats out of the
# LIVENESS question; health.py's 'exit monitor' and 'session coverage' checks
# are what grade coverage, and they are the right place for it.
COVERAGE_BEATS = {"equity_sweep"}


def _reachable(timeout: float) -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8420/api/overview", timeout=timeout):
            return True
    except Exception:
        return False


def _app_running() -> bool:
    """Is an app.py python process already alive? If so the fund is UP (perhaps
    just slow under host RAM pressure) and keepalive must NOT spawn a duplicate
    writer on top of it (operator 2026-06-23: two app.py thrashing under starvation)."""
    try:
        import psutil
        for p in psutil.process_iter(["name", "cmdline"]):
            try:
                if ("python" in (p.info["name"] or "").lower()
                        and any(str(a).endswith("app.py") for a in (p.info["cmdline"] or []))):
                    return True
            except psutil.Error:
                continue
    except Exception:
        pass
    return False


def _heartbeats_fresh() -> bool:
    """Are ALL declared monitors still beating? Fresh => the process is genuinely
    alive and only its HTTP is slow. A truly hung fund's heartbeats go stale.

    Tolerant read (2026-07-04): heartbeat.json is rewritten every few seconds;
    one mid-write/corrupt read must not condemn a healthy app to a kill+respawn
    (that restart storm corrupted state further). Retry once, then fall back to
    the .bak before concluding the heartbeats are unreadable.

    ONE LIVE THREAD IS NOT A LIVE FUND (operator 2026-08-19). This tested
    `min(ages) < HEARTBEAT_STALE_MIN` — the FRESHEST monitor. Measured live at
    22:14 while the app was hung-but-listening: calls_monitor 1.4 min,
    equity_sweep 34.9 min, equity_monitor 55.2 min. min() read 1.4 and returned
    "fresh", so keepalive took the "slow, NOT respawning" branch and left the
    fund with NO exit protection for nearly an hour — while the beating thread
    was calls_monitor, which had ZERO open option positions and was therefore
    the one monitor whose liveness protected nothing. The in-process watchdog
    could not save it either: watchdog_thread_restart fired for equity_monitor
    at 20:53 (stale 21 min) and again at 21:52 (stale 30 min) and the thread
    went straight back to stale, which is exactly the case an out-of-process
    relaunch exists to handle. This is the mechanism behind 'session coverage'
    reading 60.9% of scheduled session minutes over 10 days and 830 minutes of
    blackout in 7 holes.

    So: grade the WORST monitor, not the best. Every key in heartbeat.json is a
    monitor the app declares it is running; if any one of them has stopped
    beating, the fund is not doing its job and a relaunch is the correct
    recovery. This trades a few more restarts for never again running blind."""
    import json
    import time as _t
    from datetime import datetime, timezone

    def _read(path) -> dict | None:
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    hb_path = BASE / "state" / "heartbeat.json"
    hb = _read(hb_path)
    if hb is None:
        _t.sleep(0.3)                          # a writer finishes in milliseconds
        hb = _read(hb_path)
    if hb is None:
        hb = _read(BASE / "state" / "heartbeat.bak")
    try:
        now = datetime.now(timezone.utc)
        live = {k: v for k, v in (hb or {}).items() if k not in COVERAGE_BEATS}
        # Never let the filter empty the question: an unrecognised heartbeat
        # schema must fall back to grading everything rather than silently
        # certifying the fund alive on zero evidence.
        src = live or (hb or {})
        ages = [(now - datetime.fromisoformat(v)).total_seconds() / 60.0
                for v in src.values()]
        return bool(ages) and max(ages) < HEARTBEAT_STALE_MIN
    except Exception:
        return False


def main() -> None:
    rotate_logs()
    # Retry with a GENEROUS timeout before declaring the fund down — under host RAM
    # pressure the alive fund's HTTP thread can take >5s, and a false "dead" verdict
    # triggers a needless kill+respawn that spawns DUPLICATES (operator 2026-06-23:
    # two app.py instances thrashing under starvation, one clobbered the bench).
    for _i in range(3):
        if _reachable(timeout=15):
            print("app alive")
            return
        if _i < 2:
            import time as _t
            _t.sleep(3)
    # Unreachable after retries. Distinguish ALIVE-BUT-SLOW from TRULY-DOWN so we
    # never spawn a second writer on top of a healthy (just starved) instance: a
    # running app.py whose monitor heartbeats are still fresh is slow, not hung.
    if _app_running() and _heartbeats_fresh():
        print("app unreachable but process alive + heartbeats fresh -> slow, "
              "NOT respawning (avoids a duplicate writer)")
        return
    # Either NO app.py process (fully dead, port free) OR a process whose heartbeats
    # are STALE (genuinely HUNG-BUT-LISTENING — HTTP thread blocked while it holds
    # port 8420; a blind relaunch can't bind, crash-loops, and the zombie keeps the
    # port dark until a human intervenes — 2026-06-22 fund hung 35h). Free the port
    # FIRST, then relaunch — the real recovery path.
    _kill_all_app_instances()
    _kill_port_holder(8420)
    launch_kwargs = {"cwd": str(BASE), "close_fds": True,
                     "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        launch_kwargs["creationflags"] = DETACHED
    else:
        launch_kwargs["start_new_session"] = True
    subprocess.Popen([sys.executable, str(BASE / "app.py")], **launch_kwargs)
    # VERIFY the restart took (2026-06-10 audit): a blind restart that
    # crash-loops (import error, port conflict) looked identical to success.
    # Journal the outcome so the dashboard's journal shows the crash-loop.
    import json
    import time
    from datetime import datetime, timezone
    ok = False
    # ~3 minutes: at COLD BOOT (the AtStartup trigger) everything launches at
    # once and the app can take >60s to bind — a 1-minute window false-flagged
    # a perfectly good boot launch as verified_up=False (2026-06-11 reboot test)
    for _ in range(30):
        time.sleep(6)
        try:
            with urllib.request.urlopen("http://127.0.0.1:8420/api/overview",
                                        timeout=5):
                ok = True
                break
        except Exception:
            continue
    rec = {"ts": datetime.now(timezone.utc).isoformat(),
           "event": "keepalive_restart",
           "verified_up": ok}
    try:
        with open(BASE / "state" / "journal.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass
    print(f"app was down -> restarted (verified_up={ok})")


if __name__ == "__main__":
    main()
