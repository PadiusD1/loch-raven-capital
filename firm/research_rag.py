"""External verified-source research retrieval, isolated from the decision archive.

Uses only collected and provenance-validated research records. Unlike the
existing institutional memory, this store does not contain outcome labels.
"""
from __future__ import annotations

import json
from pathlib import Path

from firm.memory_rag import tokenize
from firm.research_provenance import validate_research

DEFAULT_DIR = Path(__file__).resolve().parents[1] / "data" / "research"


def load_records(root: Path = DEFAULT_DIR) -> list[dict]:
    records = []
    if not root.exists():
        return records
    for path in sorted(root.glob("*.json")):
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
            record = validate_research(obj)
            records.append(record)
        except (ValueError, OSError, TypeError, KeyError):
            continue
    return records


def retrieve(query: str, *, desk: str | None = None, asof: str | None = None,
             k: int = 5, root: Path = DEFAULT_DIR) -> list[dict]:
    terms = set(tokenize(query))
    if not terms:
        return []
    scored = []
    for r in load_records(root):
        if desk and r["desk"] != desk:
            continue
        if asof:
            try:
                validate_research(r, decision_at=asof)
            except ValueError:
                continue
        words = set(tokenize(r["title"] + " " + r["body"]))
        score = len(terms & words) / len(terms)
        if score:
            scored.append((score, r))
    scored.sort(key=lambda pair: (-pair[0], pair[1]["source_url"]))
    return [{"score": round(score, 4), "record": r} for score, r in scored[:max(0, k)]]
