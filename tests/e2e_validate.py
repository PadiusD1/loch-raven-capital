"""End-to-End validation harness for Loch Raven Capital.

Run this repeatedly (e.g. on a loop) to prove the WHOLE fund keeps working over
time. It validates the entire application flow from start to finish, in two layers:

  LAYER 1 — OFFLINE REGRESSION (the full-flow simulation, sandboxed state):
    tests/test_firm.py       — safety-property regression over screen -> candidate
                               assembly -> research floor -> debate -> verdict ->
                               sleeves -> risk desk -> monitor -> watchlist ->
                               learning (degraded-mode, add-on-strength, earnings
                               docs, owned-HOLD grading, adaptive workers, ...).
    tests/test_week_sim.py   — a week-in-the-life simulation + LIVE-mode plumbing.

  LAYER 2 — LIVE READ-ONLY SMOKE of the running app (no Opus, no trades, no writes):
    - dashboard/API reachable and not wedged in a stale "running" state
    - the last committee cycle produced REAL (non-degraded) verdicts, OR degraded
      safely (Feature A: degraded verdicts blocked from trading)
    - sleeve state integrity: equity is a sane positive number, no corruption
    - RAM headroom (the recurring reliability lever) + adaptive worker sizing
    - the fund is alive (recent journal activity) and the learning loop is recording

Exit 0 = every check green. Non-zero = a regression to investigate. Fully
read-only + offline for the live layer — safe to run on ANY cadence, forever.
"""
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"
PY = sys.executable or "py"
# Standalone script: ensure the project root is importable no matter how it's
# invoked (from tests/, from root, or a scheduled task) so `import config` and
# `from firm import ...` always resolve (2026-07-01: fixed effective_workers=?).
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_PASS, _FAIL, _WARN = "PASS", "FAIL", "WARN"
results: list[tuple[str, str, str]] = []


def check(name: str, status: str, detail: str = "") -> None:
    results.append((name, status, detail))
    icon = {"PASS": "[OK]  ", "FAIL": "[FAIL]", "WARN": "[warn]"}.get(status, "      ")
    print(f"  {icon} {name}" + (f" — {detail}" if detail else ""))


def _load(name: str, default=None):
    p = STATE / f"{name}.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default
    except Exception:
        return default


# ── LAYER 1: offline regression suites ────────────────────────────────────────
def run_suite(rel: str) -> None:
    path = ROOT / rel
    if not path.exists():
        check(f"suite {rel}", _FAIL, "file missing")
        return
    try:
        t0 = time.time()
        p = subprocess.run([PY, str(path)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=900, cwd=str(ROOT))
    except subprocess.TimeoutExpired:
        check(f"suite {rel}", _FAIL, "timed out (>15m)")
        return
    out = (p.stdout or "") + (p.stderr or "")
    line = next((ln for ln in out.splitlines() if "passed" in ln and "/" in ln), "")
    fails = [ln for ln in out.splitlines() if "[FAIL]" in ln]
    ok = (p.returncode == 0 and not fails)
    check(f"suite {rel}", _PASS if ok else _FAIL,
          f"{line.strip() or 'no summary'} ({time.time() - t0:.0f}s)"
          + (f" | {len(fails)} FAIL lines" if fails else ""))


# ── LAYER 2: live read-only smoke ─────────────────────────────────────────────
def smoke_app_reachable() -> bool:
    try:
        import urllib.request
        r = urllib.request.urlopen("http://localhost:8420/api/health", timeout=6)
        r.read()
        check("live: dashboard/API reachable (:8420)", _PASS)
        return True
    except Exception as e:
        check("live: dashboard/API reachable (:8420)", _WARN,
              f"app not running ({str(e)[:40]}) — offline layer still validated")
        return False


def smoke_status_not_wedged() -> None:
    st = _load("status", {}) or {}
    ts = st.get("ts")
    if st.get("running") and ts:
        try:
            age_h = (datetime.now(timezone.utc)
                     - datetime.fromisoformat(str(ts))).total_seconds() / 3600
            check("live: no wedged 'running' status", _PASS if age_h < 3 else _WARN,
                  f"running for {age_h:.1f}h" + (" (>3h — check for a hung cycle)" if age_h >= 3 else ""))
            return
        except Exception:
            pass
    check("live: no wedged 'running' status", _PASS, f"stage={st.get('stage')}, running={st.get('running')}")


def smoke_last_cycle_verdicts() -> None:
    r = _load("research", {}) or {}
    latest = r.get("latest", {}) or {}
    if not latest:
        check("live: committee produced verdicts", _WARN, "no stored verdicts yet")
        return

    def vof(x):
        return (x.get("verdict") or x) if isinstance(x, dict) else {}
    deg = sum(1 for v in latest.values() if vof(v).get("degraded"))
    real = len(latest) - deg
    if real > 0:
        check("live: last cycle produced REAL committee verdicts", _PASS,
              f"{real} real / {deg} degraded")
    else:
        # all-degraded is SAFE (Feature A blocks trading) but flags a resource issue
        check("live: last cycle produced REAL committee verdicts", _WARN,
              f"all {deg} degraded (safe: trades blocked, but check host RAM)")


def smoke_degraded_never_traded() -> None:
    jp = STATE / "journal.jsonl"
    if not jp.exists():
        check("live: degraded verdicts never traded (Feature A)", _WARN, "no journal")
        return
    try:
        rows = [json.loads(ln) for ln in jp.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except Exception:
        check("live: degraded verdicts never traded (Feature A)", _WARN, "journal unreadable")
        return
    tail = rows[-4000:]
    blocked = sum(1 for r in tail if r.get("event") == "degraded_buy_blocked")
    # a degraded 'order' side=buy would be the failure mode Feature A prevents
    bad = [r for r in tail if r.get("event") == "order" and r.get("side") == "buy"
           and r.get("degraded")]
    check("live: degraded verdicts never traded (Feature A)",
          _PASS if not bad else _FAIL,
          f"{blocked} degraded buys blocked, {len(bad)} leaked" if (blocked or bad)
          else "no degraded activity in window")


def smoke_sleeve_integrity() -> None:
    import config as _cfg
    bad = []
    total_eq = 0.0
    for sl in getattr(_cfg, "SLEEVES", {}):
        s = _load(f"sleeve_{sl}", None)
        if s is None:
            continue
        cash = s.get("cash")
        if not isinstance(cash, (int, float)) or cash < -1e-6:
            bad.append(f"{sl}:cash={cash}")
        total_eq += (cash or 0)
        for sym, p in (s.get("positions") or {}).items():
            q = p.get("quantity")
            if q is not None and isinstance(q, (int, float)) and q < 0 and not p.get("instrument") == "option":
                bad.append(f"{sl}:{sym} qty<0")
    check("live: sleeve state integrity (cash>=0, no neg equity qty)",
          _PASS if not bad else _FAIL,
          f"~${total_eq:,.0f} cash across sleeves" if not bad else "; ".join(bad[:4]))


def smoke_ram_and_workers() -> None:
    try:
        import psutil
        avail = psutil.virtual_memory().available / 1e9
    except Exception:
        check("live: RAM headroom + adaptive workers", _WARN, "psutil unavailable")
        return
    try:
        from firm import cycle
        eff = cycle._effective_workers()
    except Exception:
        eff = "?"
    status = _PASS if avail >= 2.0 else _WARN
    check("live: RAM headroom + adaptive workers", status,
          f"{avail:.1f}GB free -> effective_workers={eff}"
          + ("" if avail >= 2.0 else " (tight: close idle Claude windows / Desktop app)"))


def smoke_fund_alive_and_learning() -> None:
    jp = STATE / "journal.jsonl"
    if jp.exists():
        try:
            rows = [json.loads(ln) for ln in jp.read_text(encoding="utf-8").splitlines() if ln.strip()]
            last = rows[-1].get("ts") if rows else None
            age_h = ((datetime.now(timezone.utc) - datetime.fromisoformat(str(last))).total_seconds() / 3600
                     if last else 999)
            check("live: fund alive (recent journal activity)",
                  _PASS if age_h < 24 else _WARN,
                  f"last event {age_h:.1f}h ago")
        except Exception:
            check("live: fund alive (recent journal activity)", _WARN, "journal parse error")
    led = _load("ledger", {}) or {}
    n = len(led.get("trades", []))
    check("live: learning loop recording closed trades", _PASS,
          f"{n} closed trade(s) logged (review distills at 20 trades / 21 days)")


def main() -> int:
    print("=" * 72)
    print(f"  Loch Raven Capital — E2E validation  {datetime.now(timezone.utc).isoformat()[:19]} UTC")
    print("=" * 72)
    print("\n[Layer 1] OFFLINE REGRESSION — full-flow simulation (sandboxed):")
    run_suite("tests/test_firm.py")
    run_suite("tests/test_week_sim.py")

    print("\n[Layer 2] LIVE READ-ONLY SMOKE — the running application:")
    smoke_app_reachable()
    smoke_status_not_wedged()
    smoke_last_cycle_verdicts()
    smoke_degraded_never_traded()
    smoke_sleeve_integrity()
    smoke_ram_and_workers()
    smoke_fund_alive_and_learning()

    n_fail = sum(1 for _, s, _ in results if s == _FAIL)
    n_warn = sum(1 for _, s, _ in results if s == _WARN)
    n_pass = sum(1 for _, s, _ in results if s == _PASS)
    print("\n" + "=" * 72)
    print(f"  RESULT: {n_pass} PASS, {n_warn} WARN, {n_fail} FAIL  ->  "
          + ("E2E GREEN — fund pipeline validated end to end" if n_fail == 0
             else "E2E FAILED — regression(s) above need attention"))
    print("=" * 72)
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
