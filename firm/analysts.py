"""The research floor — five analyst desks, T. Rowe Price style.

Each desk independently scores a candidate from its own lens:
  fundamental — valuation, growth quality, balance sheet
  technical   — momentum, trend structure, relative strength
  sentiment   — news flow, narrative, positioning
  macro       — sector/cycle/rates context
  quant       — factor profile, risk-adjusted setup

Depth modes:
  full     — one Opus call per desk (5 calls)
  standard — one Opus call returning all 5 desk reports
  fast     — folded into the debate call (no separate research call)
If the Claude CLI is unavailable, a deterministic quant fallback keeps the
firm functional in degraded mode (flagged in output).
"""
import json
from concurrent.futures import ThreadPoolExecutor

from firm import brain

DESKS = {
    "fundamental": (
        "You are the FUNDAMENTAL research desk at an institutional asset manager. "
        "Judge valuation vs growth, margin trajectory, balance sheet, moat durability, "
        "and earnings quality. Be skeptical of stories not backed by numbers."),
    "technical": (
        "You are the TECHNICAL & MOMENTUM desk. Judge trend structure, momentum "
        "persistence, relative strength vs SPY, overbought/oversold risk, and "
        "entry timing quality. Respect price; price is the primary evidence."),
    "sentiment": (
        "You are the NEWS & SENTIMENT desk. Judge the headline flow, narrative "
        "direction, catalyst calendar, and whether sentiment is ahead of or behind "
        "fundamentals. Flag crowding and hype risk explicitly."),
    "macro": (
        "You are the MACRO & SECTOR desk. Judge how the current rate, growth, and "
        "sector-rotation regime treats this name. Identify which macro scenario "
        "breaks the thesis."),
    "quant": (
        "You are the QUANT desk. Judge the factor profile (momentum, quality, "
        "volatility, size), risk-adjusted return setup, and drawdown profile. "
        "Numbers over narrative."),
}

_DESK_SCHEMA = (
    '{"score": <int -100..100, negative=bearish>, "conviction": <int 0..10>, '
    '"summary": "<2-3 sentences>", "key_points": ["..."], "risks": ["..."]}')


def _packet_text(packet: dict) -> str:
    return json.dumps(packet, indent=1, default=str)


# Which packet sections each desk sees (2026-06-10 audit): five desks reading
# ONE identical packet produce correlated sampling noise, not independent
# research. Each desk gets only its lens-relevant evidence — disagreement
# between desks then MEANS something. The judge still sees everything.
_DESK_VIEWS = {
    # "earnings_docs" (SEC 8-K/10-Q + transcript) is attached only on a fresh
    # print — the fundamental + sentiment desks read the actual filings then.
    # street_research (2026-07-16): institutional reports the operator loaded —
    # the fundamental/sentiment/macro lenses are exactly where a 30-page street
    # thesis bears; technical/quant stay price-and-factor pure by design.
    "fundamental": ("ticker", "asof", "fundamentals", "quant_screen_score",
                    "next_earnings", "recent_earnings", "earnings_docs",
                    "watchlist_context", "sector_regime", "street_research"),
    "technical": ("ticker", "asof", "technicals", "quant_screen_score",
                  "watchlist_context"),
    # positioning added 2026-07-16 (infodiet audit): the desk's mandate names
    # crowding/positioning — short interest, insider/institution holdings and
    # analyst consensus now arrive as DATA instead of a confabulation invite.
    "sentiment": ("ticker", "asof", "news_headlines", "next_earnings",
                  "recent_earnings", "earnings_docs", "macro_calendar_next_7d",
                  "watchlist_context", "street_research", "positioning"),
    # macro_brief / factor_scores added 2026-07-01 (cognition audit #4a): a
    # desk whose MANDATE names data it cannot see is a confabulation
    # invitation — macro judges "the regime" without the macro brief; quant
    # judges "the factor profile" without factor_scores.
    "macro": ("ticker", "asof", "sector_regime", "macro_calendar_next_7d",
              "fundamentals", "next_earnings", "macro_brief", "street_research"),
    "quant": ("ticker", "asof", "technicals", "quant_screen_score",
              "factor_scores"),
}
# Minimal price context every desk needs to anchor judgments
_ALWAYS_TECH_KEYS = ("price", "ret_1m", "ret_3m")


def _desk_packet(desk: str, packet: dict) -> dict:
    view = {k: packet[k] for k in _DESK_VIEWS.get(desk, ()) if k in packet}
    if "technicals" not in view:
        tech = packet.get("technicals") or {}
        view["price_context"] = {k: tech.get(k) for k in _ALWAYS_TECH_KEYS}
    return view


def _desk_prompt(desk: str, packet: dict) -> str:
    # No FIRM-MEMORY lessons here (2026-06-10 audit): injecting the same
    # prior into all five desks AND the judge applies it seven times over —
    # desks stay evidence-driven; the judge weighs the lessons once.
    return (
        f"{DESKS[desk]}\n\n"
        f"CANDIDATE DATA PACKET (your desk's evidence only):\n"
        f"{_packet_text(_desk_packet(desk, packet))}\n\n"
        f"Respond with ONLY this JSON:\n{_DESK_SCHEMA}"
    )


def _panel_prompt(packet: dict) -> str:
    """Standard-depth panel: one call, five desks — but each desk section
    carries ONLY that desk's evidence slice (cognition audit 2026-07-01, #4a).
    The old version passed the FULL packet with an instruction to
    self-partition — in-context information hiding by instruction reliably
    leaks, so 'independent' desk disagreement was role-play over shared
    evidence and the judge read the correlated agreement as confirmation.
    Physical slicing restores information isolation at zero extra calls; the
    _DESK_VIEWS machinery is the same one the full-depth path uses."""
    sections = []
    for desk, role in DESKS.items():
        sections.append(
            f"=== {desk.upper()} DESK ===\n{role}\n"
            f"THIS DESK'S EVIDENCE (all it may reason from):\n"
            f"{_packet_text(_desk_packet(desk, packet))}")
    return (
        "You are the research floor of an institutional asset manager: five "
        "desks, each with its OWN evidence below. Write each desk's report "
        "using ONLY that desk's evidence section — a desk must not borrow "
        "another desk's data, and desks are encouraged to disagree.\n\n"
        + "\n\n".join(sections) + "\n\n"
        "Respond with ONLY this JSON:\n"
        '{"fundamental": ' + _DESK_SCHEMA + ', "technical": {...}, '
        '"sentiment": {...}, "macro": {...}, "quant": {...}} '
        "(every desk uses the same schema)"
    )


def _fallback_report(packet: dict) -> dict:
    """Deterministic degraded-mode research when the CLI is unavailable."""
    tech = packet.get("technicals", {})
    q = packet.get("quant_screen_score", 50)
    score = int(max(-100, min(100, (q - 50) * 3)))
    base = {"score": score, "conviction": 4,
            "summary": "Degraded mode: deterministic quant screen only (Claude CLI unavailable).",
            "key_points": [f"quant screen {q}", f"6m return {tech.get('ret_6m')}%"],
            "risks": ["No qualitative research performed."]}
    return {d: dict(base) for d in DESKS} | {"degraded": True}


def run_research(packet: dict, depth: str = "standard") -> dict:
    """Return {desk: report} for one candidate."""
    if not brain.available():
        return _fallback_report(packet)

    if depth == "full":
        def one(desk: str):
            return desk, brain.ask_json(_desk_prompt(desk, packet))
        out: dict = {}
        with ThreadPoolExecutor(max_workers=3) as pool:
            for desk, rep in pool.map(lambda d: one(d), DESKS):
                out[desk] = rep or _fallback_report(packet)[desk]
        return out

    # standard: one panel call
    panel = brain.ask_json(_panel_prompt(packet), retries=1)
    if panel and all(k in panel for k in DESKS):
        return {k: panel[k] for k in DESKS}
    return _fallback_report(packet)


def composite_score(research: dict) -> float:
    """Conviction-weighted average desk score, -100..100."""
    num, den = 0.0, 0.0
    for desk in DESKS:
        rep = research.get(desk) or {}
        s = rep.get("score", 0)
        c = max(1, rep.get("conviction", 1))
        if isinstance(s, (int, float)):
            num += s * c
            den += c
    return round(num / den, 1) if den else 0.0
