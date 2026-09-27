"""Does a market-cap floor on entries earn its keep? Graded on the firm's OWN
verdict history, using the firm's OWN forward-excess definition.

WHY (operator 2026-08-19). The 10-year backtest says top-decile momentum is the
best band by a wide margin (+26.66 alpha), but the LIVE fund's momentum-100
entries realized -9.92 with a 12% win rate. The difference is the universe:
scripts/sim_10y.py ranks 58 mega-caps chosen today, while the live screen ranks
~10k names behind only a $1 price and $500k dollar-volume floor. Median market
cap of what the fund actually bought is $2.5B. On the 21 closed trades that
retain a size bucket, sub-$2B names returned -15.70 with a 0% win rate.

n=21 cannot carry a threshold. state/research.json does better: 1,110 context
rows retain factors.size.mkt_cap_b, each pairable with a 21-session forward
SPY-relative return. This script grades cap buckets on THAT sample.

It reuses verdict_report_card's own dedupe discipline so the numbers are
comparable to the card:
  * one verdict per (ticker, calendar day) — the latest wins;
  * one row per ticker per forward window — the OLDEST wins, because a name
    re-judged inside its own 21-session window shares ~95% of its outcome and
    is not an independent observation.
Without both, a frequently re-judged held name is counted many times and a
single outcome masquerades as a trend.

Run:  py -3.11 scripts/cap_floor_study.py
"""
import statistics as st
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import config                              # noqa: E402
from firm import data_engine, state        # noqa: E402

FWD_SESSIONS = 22          # matches learning._fwd_excess ([:22] slice)
MIN_AGE_DAYS = 3
WINDOW_DAYS = 21           # overlapping-window dedupe span


def main() -> None:
    hist = (state.load("research", {}) or {}).get("history", [])
    now = datetime.now(timezone.utc)

    # ── dedupe 1: one verdict per (ticker, day), latest wins ────────────────
    deduped: dict = {}
    for h in hist:
        try:
            age_d = (now - datetime.fromisoformat(h["asof"])).days
        except (KeyError, ValueError, TypeError):
            continue
        if age_d < MIN_AGE_DAYS:
            continue
        d0 = h["asof"][:10]
        ctx = h.get("context") or {}
        for t, v in (h.get("verdicts") or {}).items():
            c = ctx.get(t) or {}
            cap = ((c.get("factors") or {}).get("size") or {}).get("mkt_cap_b")
            if not isinstance(cap, (int, float)) or cap <= 0:
                continue
            deduped[(t, d0)] = (d0, t, float(cap), (v or {}).get("rating"),
                                bool((v or {}).get("degraded")))

    # ── dedupe 2: one row per ticker per forward window, OLDEST wins ────────
    rows = sorted(deduped.values(), key=lambda r: (r[1], r[0]))
    kept: list = []
    last_kept: dict[str, str] = {}
    for d0, t, cap, rating, degraded in rows:
        prev = last_kept.get(t)
        if prev is not None:
            gap = (datetime.fromisoformat(d0) - datetime.fromisoformat(prev)).days
            if gap < WINDOW_DAYS:
                continue
        last_kept[t] = d0
        kept.append((d0, t, cap, rating, degraded))

    tickers = sorted({t for _, t, _, _, _ in kept})
    print(f"{len(deduped)} day-deduped rows -> {len(kept)} window-independent "
          f"observations over {len(tickers)} tickers", flush=True)

    # ── prices ─────────────────────────────────────────────────────────────
    batches = [tickers[i:i + 120] for i in range(0, len(tickers), 120)]
    frames = [data_engine.fetch_history(b + [config.BENCHMARK], period="6mo")
              for b in batches]
    closes = frames[0] if len(frames) == 1 else pd.concat(frames, axis=1)
    closes = closes.loc[:, ~closes.columns.duplicated()]
    spy = closes[config.BENCHMARK].dropna()

    def fwd_excess(t: str, d0: str):
        if t not in closes.columns:
            return None
        s = closes[t].dropna()
        seg = s[s.index >= d0][:FWD_SESSIONS]
        if len(seg) < 2 or float(seg.iloc[0]) <= 0:
            return None
        r = (float(seg.iloc[-1]) / float(seg.iloc[0]) - 1) * 100
        sp = spy[spy.index >= d0][:FWD_SESSIONS]
        if len(sp) >= 2:
            r -= (float(sp.iloc[-1]) / float(sp.iloc[0]) - 1) * 100
        return r

    graded = []
    for d0, t, cap, rating, degraded in kept:
        x = fwd_excess(t, d0)
        if x is not None:
            graded.append((cap, x, rating, degraded))
    print(f"graded {len(graded)} of {len(kept)}\n", flush=True)

    def show(rows_, label):
        if not rows_:
            print(f"  {label:24s} n=  0")
            return
        xs = [x for _, x, _, _ in rows_]
        print(f"  {label:24s} n={len(xs):4d}  mean {st.mean(xs):+7.2f}  "
              f"median {st.median(xs):+7.2f}  win% "
              f"{100 * sum(1 for v in xs if v > 0) / len(xs):3.0f}")

    bands = [(0, 0.3, "< $0.3B  nano"), (0.3, 1, "$0.3-1B  micro"),
             (1, 2, "$1-2B"), (2, 5, "$2-5B"), (5, 10, "$5-10B"),
             (10, 50, "$10-50B  mid"), (50, 200, "$50-200B  large"),
             (200, 1e9, "> $200B  mega")]

    print("=" * 78)
    print("ALL GRADED VERDICTS by market cap (21-session forward SPY-excess)")
    print("=" * 78)
    for lo, hi, lab in bands:
        show([r for r in graded if lo <= r[0] < hi], lab)

    print()
    print("=" * 78)
    print("CUMULATIVE: what a FLOOR would have kept (all verdicts)")
    print("=" * 78)
    for floor in (0, 0.3, 1, 2, 3, 5, 10, 20, 50):
        kept_ = [r for r in graded if r[0] >= floor]
        drop_ = [r for r in graded if r[0] < floor]
        if not kept_:
            continue
        kx = [x for _, x, _, _ in kept_]
        dx = [x for _, x, _, _ in drop_]
        print(f"  floor ${floor:>5.1f}B  keeps n={len(kx):4d} mean {st.mean(kx):+7.2f} "
              f"win% {100 * sum(1 for v in kx if v > 0) / len(kx):3.0f}   |   "
              f"drops n={len(dx):4d} mean "
              f"{(st.mean(dx) if dx else 0):+7.2f}")

    print()
    print("=" * 78)
    print("BUY-FAMILY ONLY (non-degraded) — the population a floor would gate")
    print("=" * 78)
    buys = [r for r in graded if r[2] in ("BUY", "STRONG_BUY") and not r[3]]
    for lo, hi, lab in bands:
        show([r for r in buys if lo <= r[0] < hi], lab)
    print()
    for floor in (0, 1, 2, 3, 5, 10):
        kept_ = [x for c, x, _, _ in buys if c >= floor]
        drop_ = [x for c, x, _, _ in buys if c < floor]
        if not kept_:
            continue
        print(f"  BUY floor ${floor:>4.1f}B  keeps n={len(kept_):3d} mean "
              f"{st.mean(kept_):+7.2f} win% "
              f"{100 * sum(1 for v in kept_ if v > 0) / len(kept_):3.0f}   |   "
              f"drops n={len(drop_):3d} mean "
              f"{(st.mean(drop_) if drop_ else 0):+7.2f}")


if __name__ == "__main__":
    main()
