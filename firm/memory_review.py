"""Memory grounded committee review. Ask the firm what its own history says about a name.

Built at hackUMBC 2026 (T. Rowe Price NextGen Finance track).

One call gathers the name's latest committee file, the most similar precedents
(point in time, with outcomes only where knowable) and the firm's distilled
lessons, asks Claude for a verdict that cites every precedent it relies on,
then runs the deterministic guardrails and appends the whole exchange to the
hash chained audit log.

Replay mode serves a recorded Claude answer for the same prompt so a live demo
never depends on conference wifi. The guardrails always run fresh.
"""
from __future__ import annotations

import json
from datetime import date

from firm import memory_archive as archive
from firm import memory_guard as guard
from firm import memory_rag as rag

VERDICT_KEYS = ("rating", "conviction", "thesis", "bear_risk", "watch_trigger", "memory_note")


def _lessons_excerpt(limit: int = 1800) -> str:
    text = archive.lessons_text()
    if not text:
        return ""
    return "FIRM LESSONS (distilled from closed trades).\n" + text[:limit] + "\n\n"


def build_prompt(ticker: str, asof: str | None = None, k: int = 6) -> dict:
    t = (ticker or "").upper().strip()
    asof = asof or date.today().isoformat()
    history = [m for m in archive.for_ticker(t) if m.asof < asof]
    latest = history[-1] if history else None
    query = latest.text() if latest else t
    direction = latest.direction if latest else 0
    block, hits, signal = rag.precedent_block(query, ticker=t, asof=asof, direction=direction, k=k)
    current = ("No prior committee file on this name." if latest is None else
               f"LATEST COMMITTEE FILE ({latest.asof}). Rating {latest.rating}, conviction "
               f"{latest.conviction}. Thesis. {latest.thesis}\nBiggest risk named. {latest.bear_risk}\n"
               f"What would change the view. {latest.watch_trigger}")
    prompt = (
        _lessons_excerpt()
        + block
        + f"TICKER {t}. Review date {asof}.\n{current}\n\n"
        "You are the portfolio manager re-reviewing this name with the firm's memory in front of you. "
        "Decide whether the precedents strengthen, weaken or do not change the latest view. Use only "
        "the facts above. Do not invent figures. Cite precedents inline as [P#] wherever they inform "
        "the thesis or the risk. HOLD is a legitimate verdict.\n"
        "End your reply with exactly one JSON code block with keys rating (STRONG_BUY, BUY, HOLD, SELL "
        "or STRONG_SELL), conviction (0 to 100), thesis (at most 90 words, with [P#] citations), "
        "bear_risk (one sentence), watch_trigger (one sentence), memory_note (one sentence on what the "
        "memory changed)."
    )
    evidence = "\n".join([block, current, archive.lessons_text()[:1800]])
    return {"ticker": t, "asof": asof, "prompt": prompt, "hits": hits, "signal": signal,
            "latest": latest, "evidence": evidence}


def _clean_verdict(raw: dict | None) -> dict | None:
    if not isinstance(raw, dict):
        return None
    v = {k: raw.get(k) for k in VERDICT_KEYS}
    v["rating"] = str(v.get("rating") or "").upper().replace(" ", "_")
    try:
        v["conviction"] = int(round(float(v.get("conviction"))))
    except (TypeError, ValueError):
        v["conviction"] = None
    return v


def _replay(ticker: str) -> dict | None:
    p = archive.DEMO_DIR / f"{ticker.upper()}.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return None


def replay_tickers() -> list[str]:
    if not archive.DEMO_DIR.is_dir():
        return []
    return sorted(p.stem for p in archive.DEMO_DIR.glob("*.json"))


def run(ticker: str, asof: str | None = None, mode: str = "replay", record: bool = False) -> dict:
    """mode is replay, live or auto (replay when a recording exists, else live)."""
    ctx = build_prompt(ticker, asof)
    source = None
    verdict = None
    recorded = _replay(ctx["ticker"]) if mode in ("replay", "auto") else None
    if recorded and (asof is None or recorded.get("asof") == ctx["asof"]):
        ctx = build_prompt(ctx["ticker"], recorded.get("asof"))
        verdict = _clean_verdict(recorded.get("verdict"))
        source = f"recorded Claude answer from {recorded.get('recorded_at', 'earlier')}"
    elif mode in ("live", "auto"):
        from firm import brain
        verdict = _clean_verdict(brain.ask_json(ctx["prompt"]))
        source = "live Claude call"
        if verdict and record:
            archive.DEMO_DIR.mkdir(parents=True, exist_ok=True)
            (archive.DEMO_DIR / f"{ctx['ticker']}.json").write_text(json.dumps({
                "ticker": ctx["ticker"], "asof": ctx["asof"], "verdict": verdict,
                "recorded_at": date.today().isoformat(), "prompt_chars": len(ctx["prompt"])},
                indent=1), encoding="utf-8")
    if verdict is None:
        return {"ok": False, "ticker": ctx["ticker"], "asof": ctx["asof"],
                "error": "no verdict. Use live mode with the claude CLI signed in, or pick a recorded ticker.",
                "precedents": [h.public() for h in ctx["hits"]], "signal": ctx["signal"]}
    review = guard.review(verdict, ctx["hits"], ctx["evidence"], ctx["asof"])
    entry = guard.audit_append({
        "kind": "memory_review", "ticker": ctx["ticker"], "asof": ctx["asof"], "source": source,
        "precedent_ids": [h.memo.id for h in ctx["hits"]], "verdict": verdict,
        "guardrails": review["status"]})
    return {"ok": True, "ticker": ctx["ticker"], "asof": ctx["asof"], "source": source,
            "verdict": verdict, "guardrails": review, "signal": ctx["signal"],
            "precedents": [h.public() for h in ctx["hits"]],
            "latest": ctx["latest"].public() if ctx["latest"] else None,
            "audit": {"hash": entry["hash"], "prev": entry["prev"]},
            "prompt_chars": len(ctx["prompt"])}
