"""News reliability & speculation filtering (operator 2026-07-07).

WHY THIS EXISTS. The fund's OWN report card measured the news tape as net-NEGATIVE
— verdicts made WITH news underperformed 'blind' ones (blind +0.6% vs trusted
-8.4% fwd excess, IG 2026-07-06). Reacting to headlines has been value-destroying,
yet the only mitigation was a soft "discount the news" note to the judge while the
raw tape — rumor, single-source speculation, aggregator noise — still sat in the
packet dragging conviction down (a plausible driver of the all-HOLD monoculture).

THE MANDATE (operator): stay on top of REAL, market-moving news, but never let
GARBAGE — speculative, unconfirmed, rumor, single-source — be impactful negatively.

WHAT THIS DOES. A cheap, deterministic, no-API gate that runs at packet-build time.
`fetch_news` already stamps each headline with a trust tag and age
("[✓ Reuters, 1d ago] Title" / "[· some blog, 3d ago] Title"). This module scores
each headline for RELIABILITY and partitions a name's headlines into:
  - MATERIAL   — reliable enough to inform a decision (confirmed, trusted-wire, dated)
  - SPECULATIVE — context only; the judge is told these must NEVER move conviction

Reliability combines three signals, none of which needs a model call:
  - SOURCE trust: the '✓' wire/desk tag fetch_news stamps (Reuters/Bloomberg/WSJ/
    AP/CNBC/FT/...) vs the '·' aggregator-or-unknown tag.
  - SPECULATION language: the grammar of a story that has NOT happened yet —
    "reportedly", "sources say", "in talks", "mulls", "rumored", "unconfirmed".
    A headline about a POSSIBILITY is not evidence, from ANY source (a trusted
    wire reporting a rumor is still a rumor).
  - RECENCY: fetch_news already drops >10d items; an 'undated' item is penalized
    (can't tell if it is already priced in).

Nothing here deletes information — SPECULATIVE headlines still travel in the packet
so the judge can SEE the tape; they are just flagged as context so they cannot,
by instruction, move the conviction number. Every threshold is config-tunable and
fully reversible.
"""
from __future__ import annotations

try:
    import config
except Exception:                                    # pragma: no cover - standalone import
    config = None                                    # type: ignore


def _cfg(name: str, default):
    return getattr(config, name, default) if config is not None else default


# The grammar of the UNCONFIRMED: rumor, single-source claim, not-yet-happened.
# High-precision markers only — we must NOT nuke legitimate forward-looking factual
# reporting (the fund still needs to stay on top of real news), so this list favours
# clear rumor/hedge phrases over common words like "may"/"could" that appear in
# ordinary factual headlines ("shares may open lower").
SPECULATION_MARKERS: tuple[str, ...] = (
    "reportedly", "report says", "reports say", "sources say", "source says",
    "people familiar", "person familiar", "familiar with the matter",
    "rumor", "rumour", "rumored", "rumoured", "speculat", "unconfirmed",
    "denies report", "denied report", "in talks", "in advanced talks",
    "weighing", "mulls", "mulling", "eyeing", "is said to", "said to be",
    "explores potential", "exploring a", "considering a", "considering an",
    "potential deal", "potential merger", "potential buyout", "could be looking",
    "may be considering", "reportedly plans", "according to reports", "whispers",
)


def trust_tag(headline: str) -> str:
    """'trusted' | 'aggregator' | 'unknown' from the tag fetch_news stamps."""
    s = (headline or "").lstrip()
    if s.startswith("[✓"):        # '[✓ ...' trusted wire/desk
        return "trusted"
    if s.startswith("[·"):        # '[· ...' aggregator
        return "aggregator"
    return "unknown"                   # untagged (e.g. macro-brief bullet) -> cautious


def is_speculative(headline: str) -> bool:
    """Does the headline carry rumor/unconfirmed/hedge grammar?"""
    low = (headline or "").lower()
    return any(m in low for m in SPECULATION_MARKERS)


def headline_reliability(headline: str) -> float:
    """Reliability in [0, 1].

    Trusted wire = 0.70 base, aggregator = 0.40, unknown = 0.30. Any speculation
    marker MULTIPLIES by 0.35 (an unconfirmed claim is not evidence, from any
    source). An 'undated' item loses 0.10 (can't tell if already priced in)."""
    base = {"trusted": 0.70, "aggregator": 0.40, "unknown": 0.30}[trust_tag(headline)]
    if is_speculative(headline):
        base *= float(_cfg("NEWS_SPECULATION_FACTOR", 0.35))
    if "undated" in (headline or "").lower():
        base -= 0.10
    return max(0.0, min(1.0, base))


def partition(headlines) -> dict:
    """Split a name's tagged headlines into material vs speculative.

    MATERIAL = reliability >= NEWS_RELIABLE_THRESHOLD (default 0.5): in practice a
    dated, non-speculative, trusted-wire item. Everything else — aggregator noise,
    any rumor/hedge item even from a wire, undated promo — is SPECULATIVE (kept as
    context, flagged non-actionable). Order within each bucket is preserved."""
    thr = float(_cfg("NEWS_RELIABLE_THRESHOLD", 0.5))
    material, speculative = [], []
    for h in (headlines or []):
        (material if headline_reliability(h) >= thr else speculative).append(h)
    return {"material": material, "speculative": speculative}


def summarize(headlines) -> dict:
    """Counts + the partition, for packet embedding and telemetry."""
    p = partition(headlines)
    return {"material": p["material"], "speculative": p["speculative"],
            "n_material": len(p["material"]), "n_speculative": len(p["speculative"]),
            "n_total": len(headlines or [])}
