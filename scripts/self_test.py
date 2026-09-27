"""Weekly self-test — both suites (regression + week-in-the-life simulator)
run UNATTENDED so environment drift (vendor API changes, dependency updates,
state-file evolution) is caught within a week, not when it costs money.

Registered as the "LochRaven Self-Test" scheduled task (Sundays 18:00).
Suites are fully sandboxed (tempdir STATE_DIR) — safe beside the live app.
Result is journaled; a failure raises a dashboard alert telling the operator
to open Claude in ai-fund for repair before the next trading session.
"""
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))


def run_suite(name: str) -> tuple[bool, str]:
    try:
        p = subprocess.run([sys.executable, str(BASE / "tests" / name)],
                           capture_output=True, text=True, timeout=900)
        lines = [ln for ln in p.stdout.strip().splitlines() if ln.strip()]
        return p.returncode == 0, (lines[-1] if lines else f"rc={p.returncode}")
    except Exception as e:
        return False, f"crashed: {e}"


def main() -> None:
    ok1, s1 = run_suite("test_firm.py")
    ok2, s2 = run_suite("test_week_sim.py")
    passed = ok1 and ok2
    from firm import state
    state.journal("self_test", passed=passed, firm_suite=s1, week_sim=s2)
    # DURABLE STAMP (operator 2026-07-31, false-red post-mortem). The journal was
    # the ONLY record that this ran, and the health check read it through
    # read_journal(N) — a fixed EVENT-COUNT window. The firm now writes ~320
    # journal events/day, so 1500 events spans ~4.7 days: a WEEKLY event fell out
    # of the window before its own next run, and the cadence check reported
    # "has not run in >8d (or never)" while the task was in fact green (last run
    # 2026-07-26, rc=0, 819/819 + 53/53). Journal rotation at JOURNAL_MAX_BYTES
    # would erase it outright regardless of window size. A dedicated one-key file
    # is O(1) to read, survives both the window and rotation, and cannot be
    # outrun by the firm getting busier.
    state.save("self_test", {
        "ts": datetime.now(timezone.utc).isoformat(), "passed": passed,
        "firm_suite": s1, "week_sim": s2})
    if not passed:
        try:
            from firm import alerts
            alerts.alert("SELF-TEST FAILED",
                         f"regression: {s1} | week-sim: {s2} — open Claude in "
                         "ai-fund and repair before the next session")
        except Exception:
            pass
    print(f"self-test {'PASSED' if passed else 'FAILED'}: "
          f"firm[{s1}] week_sim[{s2}]")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
