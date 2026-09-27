"""Exit strategist desk — Opus designs the TP/SL plan per options trade.

Instead of one fixed ladder for every contract, the strategist looks at the
specific contract (strike, DTE, premium, spot), the PM verdict (conviction,
horizon, thesis), and the underlying's volatility/momentum profile, then
designs a custom layered exit: hard SL, TP tiers, scale-out sizes, and
trailing-stop ramp.

Every field is clamped to hard guardrail ranges so a bad model response can
never produce a reckless plan. If the Claude CLI is unavailable or the call
fails, the proven default ladder from config applies.
"""
import json

import config
from firm import brain

# Hard guardrails: (min, max) per field. Opus chooses inside these ranges.
BOUNDS = {
    "sl_pct": (1.0, 15.0),
    "tp1_pct": (3.0, 30.0),
    "tp1_sell_pct": (25, 75),
    "tp2_pct": (20.0, 150.0),
    "tp3_pct": (50.0, 300.0),
    "trail1": (3.0, 15.0),
    "trail2": (5.0, 25.0),
    "trail3": (8.0, 30.0),
}

_EXIT_SCHEMA = (
    '{"sl_pct": <float 1-15, hard stop loss % below option entry>, '
    '"tp1_pct": <float 3-30, first take-profit %>, '
    '"tp1_sell_pct": <int 25-75, % of contracts sold at TP1>, '
    '"tp2_pct": <float 20-150, second tier: sell all but 1 runner>, '
    '"tp3_pct": <float 50-300, runner tier>, '
    '"trail1": <float 3-15, trailing stop % behind peak after TP1>, '
    '"trail2": <float 5-25, trail after TP2>, '
    '"trail3": <float 8-30, trail after TP3>, '
    '"rationale": "<2 sentences on why this ladder fits this trade>"}')


def default_ladder() -> dict:
    cfg = config.SLEEVES["calls_only"]
    return {
        "sl_pct": cfg["hard_sl_pct"], "tp1_pct": cfg["tp1_pct"],
        "tp1_sell_pct": cfg["tp1_sell_pct"], "tp2_pct": cfg["tp2_pct"],
        "tp3_pct": cfg["tp3_pct"], "trail1": cfg["trail_after_tp1"],
        "trail2": cfg["trail_after_tp2"], "trail3": cfg["trail_after_tp3"],
        "rationale": "Default proven ladder (strategist unavailable).",
        "source": "default",
    }


def _clamp(plan: dict) -> dict | None:
    out = {}
    for field, (lo, hi) in BOUNDS.items():
        v = plan.get(field)
        if not isinstance(v, (int, float)):
            return None
        out[field] = round(min(hi, max(lo, float(v))), 1)
    # Ladder must be ordered: tp1 < tp2 < tp3
    if not (out["tp1_pct"] < out["tp2_pct"] < out["tp3_pct"]):
        return None
    out["tp1_sell_pct"] = int(out["tp1_sell_pct"])
    out["rationale"] = str(plan.get("rationale", ""))[:400]
    out["source"] = "opus"
    return out


def _exit_evidence() -> str:
    """The firm's distilled lessons + its OWN realized exit-quality telemetry —
    the exit strategist was a dead-knowledge endpoint until 2026-07-02: the fund
    computes by_exit_reason (avg post-exit SPY-relative excess per trail/stop/
    target exit — did we cut winners early or ride losers?) and distils lessons,
    yet the only prompt that DESIGNS exits saw neither. Fail-open to empty."""
    parts: list[str] = []
    try:
        from firm import learning
        parts.append(learning.lessons_context())
        by_reason = (learning.stats() or {}).get("by_exit_reason") or {}
        rows = []
        for r, b in by_reason.items():
            n = b.get("n") or 0
            if n:
                pe = b.get("avg_post_exit_excess_pct")
                rows.append(f"- {r}: n={n}, avg post-exit SPY-relative excess "
                            f"{pe:+.1f}%" if isinstance(pe, (int, float))
                            else f"- {r}: n={n}")
        if rows:
            parts.append(
                "YOUR REALIZED EXIT QUALITY (how names did AFTER you exited — a "
                "POSITIVE post-exit excess means you exited too EARLY and left "
                "gains on the table; negative means you exited well):\n"
                + "\n".join(rows) + "\n\n")
    except Exception:
        pass
    return "".join(p for p in parts if p)


def plan_option_exits(contract: dict, verdict: dict, technicals: dict) -> dict:
    """Custom exit ladder for one long-option trade (call or put — direction-aware
    2026-07-07). Falls back to the default ladder."""
    if not brain.available():
        return default_ladder()
    ctx = {
        "contract": contract,
        "pm_verdict": {k: verdict.get(k) for k in
                       ("rating", "conviction", "horizon", "thesis", "bear_risk")},
        "underlying_technicals": technicals,
    }
    _dir = ("a long PUT — its premium RISES as the underlying FALLS"
            if str(contract.get("contract_type", "CALL")).upper().startswith("P")
            else "a long CALL — its premium RISES as the underlying RISES")
    prompt = (
        _exit_evidence()
        + "You are the EXIT STRATEGIST at an institutional options desk. "
        f"{_dir}; this position is about to be opened. Design the optimal layered "
        "scaled-exit plan for THIS SPECIFIC trade — all percentages apply to the "
        "OPTION CONTRACT price, not the underlying (the ladder math is identical "
        "for a call or a put).\n\n"
        "Consider: days to expiry (theta pressure -> tighter ladder), the "
        "underlying's volatility (higher vol -> wider stops to avoid noise "
        "stop-outs), PM conviction and horizon (higher conviction -> let "
        "winners run further), premium paid, and the firm's realized exit "
        "quality above (if your trailing exits keep leaving gains on the table, "
        "widen the trail; if stops fire on noise, widen the SL). Structure: "
        "hard SL, TP1 with partial scale-out, TP2 selling all but one runner, "
        "TP3 for the runner, and a trailing-stop ramp that tightens as tiers "
        "are hit.\n\n"
        f"TRADE CONTEXT:\n{json.dumps(ctx, indent=1, default=str)}\n\n"
        f"Respond with ONLY this JSON:\n{_EXIT_SCHEMA}"
    )
    raw = brain.ask_json(prompt, retries=1)
    plan = _clamp(raw) if raw else None
    return plan if plan else default_ladder()
