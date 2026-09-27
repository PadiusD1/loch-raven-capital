"""Record live Claude memory reviews so the console can replay them offline.

  py -3.11 scripts/record_memory_demo.py MU CSCO AMD QQQ

Each ticker gets one real, memory grounded Claude review saved to
data/memory/demo/TICKER.json. The console replays the recorded answer for the
same point in time prompt and re-runs every guardrail fresh.
"""
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from firm import memory_rag, memory_review  # noqa: E402


def one(ticker: str) -> str:
    t0 = time.time()
    out = memory_review.run(ticker, mode="live", record=True)
    took = time.time() - t0
    if not out.get("ok"):
        return f"{ticker} failed after {took:.0f}s, {out.get('error')}"
    v = out["verdict"]
    return (f"{ticker} {v['rating']} {v['conviction']} in {took:.0f}s, guardrails "
            f"{out['guardrails']['status']}, {len(out['precedents'])} precedents")


if __name__ == "__main__":
    tickers = [t.upper() for t in sys.argv[1:]] or ["MU", "CSCO", "AMD", "QQQ"]
    memory_rag.get_index()          # build once before the threads start
    with ThreadPoolExecutor(max_workers=len(tickers)) as pool:
        for line in pool.map(one, tickers):
            print(line, flush=True)
