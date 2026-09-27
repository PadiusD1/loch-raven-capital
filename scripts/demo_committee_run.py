"""Run the investment committee, memory on, over a short list of names and publish the verdicts to the cockpit.

  set LOCH_RAVEN_DEMO=1
  py -3.11 scripts/demo_committee_run.py AMD MU DELL TEAM QQQ --depth standard --workers 2

Demo mode only, and never while the app is running against the same state.
Every name goes through the exact research path a firm cycle uses
(firm.cycle._research_one: data packet, research desks, bull, bear and base
debate, PM judge with Institutional Memory). The verdicts are written to
state/research.json the way a cycle writes them, so the unchanged cockpit shows
them in its verdict feed. Nothing is traded.
"""
import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if os.environ.get("LOCH_RAVEN_DEMO", "") != "1":
    raise SystemExit("demo mode only, set LOCH_RAVEN_DEMO=1 first")

import config  # noqa: E402
from firm import cycle, data_engine, riskbrain, state  # noqa: E402


def _kind(t: str) -> str:
    etfs = set(getattr(config, "ETF_UNIVERSE", [])) | set(getattr(config, "ROTATION_UNIVERSE", [])) \
        | set(getattr(config, "CORE_ELIGIBLE", []))
    return "etf" if t in etfs else "stock"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("tickers", nargs="+")
    ap.add_argument("--depth", default="standard", choices=("fast", "standard", "full"))
    ap.add_argument("--workers", type=int, default=2)
    args = ap.parse_args()
    names = [t.upper() for t in args.tickers]
    t0 = time.time()
    state.set_status("research", f"Demo committee run, {len(names)} names, memory on", 10, running=True)
    try:
        regime = riskbrain.sector_regime()
    except Exception:
        regime = {}
    try:
        macro = data_engine.macro_brief(regime)
    except Exception:
        macro = {}
    closes = data_engine.fetch_history(names + [config.BENCHMARK], period="1y")
    bench = closes[config.BENCHMARK].dropna()
    tech = data_engine.compute_technicals(closes[[t for t in names if t in closes.columns]], bench)
    scan_q = (state.load("market_scan", {}) or {}).get("quant_scores") or {}
    todo = [t for t in names if t in tech]
    print("researching", todo, "missing technicals", [t for t in names if t not in tech], flush=True)

    def one(t):
        q = scan_q.get(t)
        if q is None:
            q = data_engine.quant_score(tech[t])
        s = time.time()
        out = cycle._research_one(t, _kind(t), tech[t], q, args.depth, regime=regime, macro=macro)
        v = out["verdict"]
        print(f"{t} {v.get('rating')} {v.get('conviction')} degraded={bool(v.get('degraded'))} "
              f"memory={'memory' in v} in {time.time() - s:.0f}s", flush=True)
        return t, out

    verdicts = {}
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for t, out in pool.map(one, todo):
            verdicts[t] = out

    log = state.load("research", {"history": []})
    log["latest"] = {
        k: {**{kk: vv for kk, vv in v.items() if kk != "packet"},
            "factor_scores": (v.get("packet") or {}).get("factor_scores") or {},
            "quant_screen_score": (v.get("packet") or {}).get("quant_screen_score"),
            "recent_earnings": (v.get("packet") or {}).get("recent_earnings") or {},
            "next_earnings": (v.get("packet") or {}).get("next_earnings") or {}}
        for k, v in verdicts.items()}
    log["latest_asof"] = datetime.now(timezone.utc).isoformat()
    log.setdefault("history", []).append({
        "asof": log["latest_asof"], "source": "demo_committee_run",
        "verdicts": {k: v["verdict"] for k, v in verdicts.items()},
        "context": {k: {"px": ((v.get("packet") or {}).get("technicals") or {}).get("price")}
                    for k, v in verdicts.items()}})
    log["history"] = log["history"][-120:]
    state.save("research", log)
    state.journal("demo_committee_run", names=list(verdicts), depth=args.depth,
                  minutes=round((time.time() - t0) / 60, 1))
    state.set_status("idle", f"Demo committee run complete, {len(verdicts)} verdicts with memory", 100,
                     running=False)
    print(f"done, {len(verdicts)} verdicts in {(time.time() - t0) / 60:.1f} minutes", flush=True)


if __name__ == "__main__":
    main()
