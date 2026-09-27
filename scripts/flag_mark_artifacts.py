"""One-time repair: flag closed trades whose fill price never traded.

Operator 2026-07-28 (PRSU). `broker.equity_quotes_public` could return a raw
one-sided BID as a mark; the monitor compared that bid to the stop and sold.
Four equity exits booked at prices outside the day's real high/low range,
together -$1,516 — 47% of every dollar the fund has realized as a loss.

The rows STAY in the ledger: deleting them would erase the evidence that the
bug happened. They get `mark_artifact` plus the range that disproves the fill,
and `learning._is_committee_trade` / `stop_slippage` then keep them out of
every evidence table so no threshold is ever tuned on a price that did not
exist.

Idempotent. Re-verifies against live market data on every run, so a row is
only flagged while the data still disproves it. Honors the single-writer rule.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from firm import state  # noqa: E402

TOL = 0.005            # allow a half-percent for vendor rounding on the bar


def main(apply: bool = False) -> int:
    holder = state.cycle_lock_holder()
    if holder is not None:
        print(f"REFUSING: a cycle holds the state lock (pid {holder}). "
              "Stop app.py / wait for the cycle, then re-run.")
        return 2

    import yfinance as yf
    ledger = state.load("ledger", {}) or {}
    trades = ledger.get("trades") or []
    # Equities only — an option's premium is not comparable to its underlying's
    # bar, and comparing them manufactures a 96% "impossible fill" every time.
    eq = [t for t in trades
          if t.get("closed_at") and t.get("exit")
          and len(str(t.get("symbol", ""))) < 16]
    if not eq:
        print("no closed equity trades")
        return 0

    syms = sorted({t["symbol"] for t in eq})
    hist = yf.download(syms, start="2026-06-01", end="2026-12-31", progress=False,
                       auto_adjust=False, group_by="ticker")

    flagged, cleared, unverifiable = [], [], []
    for t in eq:
        sym, day, px = t["symbol"], str(t["closed_at"])[:10], float(t["exit"])
        try:
            frame = hist[sym] if len(syms) > 1 else hist
            row = frame.loc[day]
            lo, hi = float(row["Low"]), float(row["High"])
        except Exception:
            if t.get("mark_artifact"):
                unverifiable.append(sym)   # keep the flag; say we could not re-check
            continue
        impossible = not (lo * (1 - TOL) <= px <= hi * (1 + TOL))
        if impossible and not t.get("mark_artifact"):
            t["mark_artifact"] = True
            t["mark_artifact_evidence"] = {
                "exit_price": round(px, 4), "real_low": round(lo, 4),
                "real_high": round(hi, 4), "session": day,
                "pct_outside": round((px / lo - 1) * 100 if px < lo
                                     else (px / hi - 1) * 100, 2),
                "cause": ("one-sided-bid mark from equity_quotes_public "
                          "(fixed 2026-07-28)"),
            }
            flagged.append((sym, day, px, lo, hi, t.get("pnl") or 0))
        elif not impossible and t.get("mark_artifact"):
            t.pop("mark_artifact", None)          # data was revised — un-flag
            t.pop("mark_artifact_evidence", None)
            cleared.append((sym, day))

    print(f"{'sym':7s} {'session':11s} {'filled':>9s} {'real low':>9s} "
          f"{'real high':>9s} {'P&L':>10s}")
    for f in flagged:
        print(f"{f[0]:7s} {f[1]:11s} {f[2]:>9.2f} {f[3]:>9.2f} {f[4]:>9.2f} "
              f"{f[5]:>10,.2f}")
    print(f"\nflagged {len(flagged)} · un-flagged {len(cleared)} · "
          f"un-recheckable {len(unverifiable)}")
    print(f"P&L quarantined from the evidence tables: "
          f"${sum(f[5] for f in flagged):,.2f}")

    if not apply:
        print("\nDRY RUN — re-run with --apply to write.")
        return 0
    if flagged or cleared:
        state.save("ledger", ledger)
        state.journal("mark_artifacts_flagged", n=len(flagged),
                      unflagged=len(cleared),
                      symbols=[f[0] for f in flagged],
                      pnl=round(sum(f[5] for f in flagged), 2))
        print("written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(apply="--apply" in sys.argv))
