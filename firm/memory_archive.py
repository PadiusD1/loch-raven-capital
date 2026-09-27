"""Institutional Memory archive. The committee's own decisions, each with its measured outcome.

Built at hackUMBC 2026 (T. Rowe Price NextGen Finance track).

Every Loch Raven committee verdict since June 2026 is one memo in
data/memory/memos.jsonl (built by scripts/build_memory_dataset.py from
state/research.json). Each memo carries what the market did next, 5 and 20
trading days later and relative to SPY, plus the date that outcome became
knowable. That last field is what keeps the whole memory layer honest. A
decision made on day t may only learn from outcomes that were known before t.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path

import config

MEMORY_DIR = Path(getattr(config, "MEMORY_DIR", config.BASE_DIR / "data" / "memory"))
MEMOS_PATH = MEMORY_DIR / "memos.jsonl"
LESSONS_PATH = MEMORY_DIR / "lessons.md"
DEMO_DIR = MEMORY_DIR / "demo"

DIRECTION = {"STRONG_BUY": 1, "BUY": 1, "HOLD": 0, "SELL": -1, "STRONG_SELL": -1}
CONVICTION_BUCKETS = (("under 60", None, 59), ("60 to 69", 60, 69),
                      ("70 to 79", 70, 79), ("80 plus", 80, None))


@dataclass(frozen=True)
class Memo:
    """One committee decision. Treat every field as read only."""
    id: str
    ticker: str
    kind: str
    asof: str                 # decision date, YYYY-MM-DD (US Eastern)
    rating: str
    conviction: int | None
    horizon: str
    thesis: str
    debate_summary: str
    bear_risk: str
    watch_trigger: str
    what_changed: str
    outcome: dict

    @property
    def direction(self) -> int:
        return DIRECTION.get(self.rating, 0)

    @property
    def excess_20d(self) -> float | None:
        v = self.outcome.get("excess_20d")
        return float(v) if isinstance(v, (int, float)) else None

    @property
    def score_20d(self) -> float | None:
        v = self.outcome.get("score_20d")
        return float(v) if isinstance(v, (int, float)) else None

    @property
    def known_at_20d(self) -> str | None:
        return self.outcome.get("known_at_20d") or None

    @property
    def label(self) -> str:
        return str(self.outcome.get("label") or "pending")

    def outcome_known_before(self, asof: str) -> bool:
        """True only when the 20 day result was already on the tape before asof."""
        known = self.known_at_20d
        return bool(known) and self.excess_20d is not None and known < asof

    def text(self) -> str:
        """What the retriever indexes. The ticker is repeated so exact name matches rank."""
        parts = (self.ticker, self.ticker, self.rating, self.thesis, self.debate_summary,
                 self.bear_risk, self.watch_trigger)
        return " \n".join(p for p in parts if p)

    def headline(self, limit: int = 230) -> str:
        """First sentence of the thesis, trimmed for prompts and the console."""
        t = " ".join(self.thesis.split())
        cut = t.find(". ")
        first = t[: cut + 1] if 0 < cut < limit else t
        return first if len(first) <= limit else first[: limit - 3].rstrip() + "..."

    def public(self, with_outcome: bool = True) -> dict:
        d = {"id": self.id, "ticker": self.ticker, "kind": self.kind, "asof": self.asof,
             "rating": self.rating, "conviction": self.conviction, "horizon": self.horizon,
             "headline": self.headline(), "thesis": self.thesis, "bear_risk": self.bear_risk,
             "watch_trigger": self.watch_trigger}
        if with_outcome:
            d["outcome"] = {k: self.outcome.get(k) for k in (
                "entry_date", "entry_close", "ret_20d", "spy_20d", "excess_20d",
                "known_at_20d", "score_20d", "label", "status")}
        return d


_lock = threading.Lock()
_cache: dict = {"path": None, "mtime": None, "memos": ()}


def _to_memo(row: dict) -> Memo:
    conv = row.get("conviction")
    return Memo(
        id=str(row["id"]), ticker=str(row["ticker"]).upper(), kind=str(row.get("kind") or "stock"),
        asof=str(row["asof"]), rating=str(row.get("rating") or "HOLD").upper(),
        conviction=int(conv) if isinstance(conv, (int, float)) else None,
        horizon=str(row.get("horizon") or ""), thesis=str(row.get("thesis") or ""),
        debate_summary=str(row.get("debate_summary") or ""), bear_risk=str(row.get("bear_risk") or ""),
        watch_trigger=str(row.get("watch_trigger") or ""), what_changed=str(row.get("what_changed") or ""),
        outcome=dict(row.get("outcome") or {}),
    )


def load_memos(path: Path | None = None) -> tuple[Memo, ...]:
    """All memos sorted by date then ticker. Cached, reloaded when the file changes."""
    p = Path(path or MEMOS_PATH)
    if not p.is_file():
        return ()
    mtime = p.stat().st_mtime
    with _lock:
        if _cache["path"] == str(p) and _cache["mtime"] == mtime:
            return _cache["memos"]
        memos = []
        with p.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    memos.append(_to_memo(json.loads(line)))
                except (ValueError, KeyError, TypeError):
                    continue          # one bad line never takes the archive down
        memos.sort(key=lambda m: (m.asof, m.ticker, m.id))
        _cache.update(path=str(p), mtime=mtime, memos=tuple(memos))
        return _cache["memos"]


def by_id(memos: tuple[Memo, ...] | None = None) -> dict[str, Memo]:
    return {m.id: m for m in (memos if memos is not None else load_memos())}


def for_ticker(ticker: str, memos: tuple[Memo, ...] | None = None) -> list[Memo]:
    t = (ticker or "").upper()
    return [m for m in (memos if memos is not None else load_memos()) if m.ticker == t]


def _bucket(conv: int | None) -> str | None:
    if conv is None:
        return None
    for name, lo, hi in CONVICTION_BUCKETS:
        if (lo is None or conv >= lo) and (hi is None or conv <= hi):
            return name
    return None


def _rate(rows: list[Memo]) -> dict:
    scores = [m.score_20d for m in rows if m.score_20d is not None]
    if not scores:
        return {"n": 0, "hit_rate": None, "mean_score_20d": None}
    return {"n": len(scores),
            "hit_rate": round(sum(1 for s in scores if s > 0) / len(scores), 4),
            "mean_score_20d": round(sum(scores) / len(scores), 2)}


def stats(memos: tuple[Memo, ...] | None = None) -> dict:
    """Archive facts computed live from the memos, never copied from a stale file."""
    memos = memos if memos is not None else load_memos()
    if not memos:
        return {"n_memos": 0}
    ratings: dict[str, int] = {}
    labels: dict[str, int] = {}
    for m in memos:
        ratings[m.rating] = ratings.get(m.rating, 0) + 1
        labels[m.label] = labels.get(m.label, 0) + 1
    directional = [m for m in memos if m.direction != 0 and m.score_20d is not None]
    buckets = {}
    for name, _lo, _hi in CONVICTION_BUCKETS:
        buckets[name] = _rate([m for m in directional if _bucket(m.conviction) == name])
    return {
        "n_memos": len(memos),
        "n_tickers": len({m.ticker for m in memos}),
        "first_date": memos[0].asof,
        "last_date": memos[-1].asof,
        "ratings": ratings,
        "labels": labels,
        "n_scored_20d": sum(1 for m in memos if m.excess_20d is not None),
        "directional": _rate(directional),
        "directional_by_conviction": buckets,
        "buys": _rate([m for m in directional if m.direction > 0]),
        "sells": _rate([m for m in directional if m.direction < 0]),
    }


def lessons_text() -> str:
    try:
        return LESSONS_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""
