"""Walk forward evaluation. Does institutional memory make the committee's calls better?

Built at hackUMBC 2026 (T. Rowe Price NextGen Finance track).

For every directional call (BUY or SELL) whose 20 day outcome is known, the
memory is queried exactly as it would have been on the decision date. Only
memos written before that date are searchable, and only outcomes knowable
before that date count. The memory then either supports the call (similar
past setups moved the way the call predicts) or warns against it.

The comparison is between the committee alone and the committee with a
memory veto. The veto rule and its threshold are fixed in advance
(warn means the aligned precedent average is below zero), so the result is
not tuned to this sample.
"""
from __future__ import annotations

import json
from pathlib import Path

from firm import memory_archive as archive
from firm import memory_rag as rag

REPORT_PATH = archive.MEMORY_DIR / "eval_report.json"


def _summary(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0, "hit_rate": None, "mean_score_20d": None}
    scores = [r["score_20d"] for r in rows]
    return {"n": len(rows), "hit_rate": round(sum(1 for s in scores if s > 0) / len(rows), 4),
            "mean_score_20d": round(sum(scores) / len(rows), 2)}


def walk_forward(k: int = 8, threshold: float = 0.0, min_precedents: int = 3) -> dict:
    memos = archive.load_memos()
    index = rag.get_index()
    rows: list[dict] = []
    for m in memos:
        if m.direction == 0 or m.score_20d is None:
            continue
        hits = index.search(m.text(), asof=m.asof, ticker=m.ticker, k=k,
                            require_outcome=True, exclude_ids={m.id})
        sig = rag.precedent_signal(hits, m.direction)
        if sig["n"] < min_precedents:
            decision = "no memory yet"
        elif sig["aligned"] < threshold:
            decision = "veto"
        else:
            decision = "keep"
        rows.append({"id": m.id, "ticker": m.ticker, "asof": m.asof, "rating": m.rating,
                     "conviction": m.conviction, "score_20d": m.score_20d,
                     "signal": sig["aligned"], "n_precedents": sig["n"], "memory": decision})
    kept = [r for r in rows if r["memory"] == "keep"]
    vetoed = [r for r in rows if r["memory"] == "veto"]
    cold = [r for r in rows if r["memory"] == "no memory yet"]
    with_memory = kept + vetoed
    report = {
        "method": {"k": k, "threshold_aligned_pct": threshold, "min_precedents": min_precedents,
                   "horizon": "20 trading days, excess return versus SPY, direction adjusted",
                   "point_in_time": "memos before the decision date, outcomes knowable before it"},
        "committee_alone": _summary(rows),
        "calls_with_memory": _summary(with_memory),
        "memory_kept": _summary(kept),
        "memory_vetoed": _summary(vetoed),
        "no_memory_yet": _summary(cold),
        "committee_plus_memory": _summary(kept + cold),
        "rows": rows,
    }
    # Plain language headline, computed, never typed by hand.
    alone, km, vt = report["calls_with_memory"], report["memory_kept"], report["memory_vetoed"]
    if alone["n"] and km["n"] and vt["n"]:
        report["headline"] = (
            f"Of {alone['n']} calls where the memory had evidence, the {km['n']} it supported hit "
            f"{km['hit_rate'] * 100:.0f}% with {km['mean_score_20d']:+.1f}% average excess, and the "
            f"{vt['n']} it warned against hit {vt['hit_rate'] * 100:.0f}% with "
            f"{vt['mean_score_20d']:+.1f}% average excess.")
    return report


def cached_report(refresh: bool = False) -> dict:
    p = Path(REPORT_PATH)
    if p.is_file() and not refresh:
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            pass
    report = walk_forward()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, indent=1), encoding="utf-8")
    tmp.replace(p)
    return report


if __name__ == "__main__":
    out = cached_report(refresh=True)
    print(json.dumps({k: v for k, v in out.items() if k != "rows"}, indent=1))
