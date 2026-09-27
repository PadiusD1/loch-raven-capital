"""HTTP routes for the Institutional Memory console. Mounted by app.py and memory_console.py.

Built at hackUMBC 2026 (T. Rowe Price NextGen Finance track).
"""
from __future__ import annotations

import re
import threading

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

import config
from firm import memory_archive as archive
from firm import memory_eval as mem_eval
from firm import memory_finetune as finetune
from firm import memory_guard as guard
from firm import memory_rag as rag
from firm import memory_review as review

router = APIRouter()
_eval_lock = threading.Lock()
_TICKER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9.\-]{0,9}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def warm_up() -> None:
    """Build the index and the evaluation in the background so the first click is instant."""
    def _go():
        try:
            rag.get_index()
            with _eval_lock:
                mem_eval.cached_report()
        except Exception:
            pass
    threading.Thread(target=_go, daemon=True).start()


@router.get("/memory")
def memory_page():
    return FileResponse(config.DASHBOARD_DIR / "memory.html")


@router.get("/api/memory/stats")
def memory_stats():
    s = archive.stats()
    s["replay_tickers"] = review.replay_tickers()
    s["audit"] = guard.audit_verify()
    s["playbook_passages"] = len(rag.get_knowledge_index().passages)
    return s


@router.get("/api/memory/search")
def memory_search(q: str, ticker: str = "", asof: str = "", k: int = 8, outcomes_only: bool = False):
    k = max(1, min(int(k), 25))
    hits = rag.search(q[:2000], asof=asof or None, ticker=ticker or None, k=k,
                      require_outcome=outcomes_only)
    direction = 0
    return {"query": q[:2000], "asof": asof or None, "hits": [h.public() for h in hits],
            "signal": rag.precedent_signal(hits, direction)}


@router.get("/api/memory/ticker/{ticker}")
def memory_ticker(ticker: str):
    rows = archive.for_ticker(ticker)
    if not rows:
        raise HTTPException(404, f"no committee history for {ticker.upper()}")
    return {"ticker": ticker.upper(), "decisions": [m.public() for m in rows]}


class ReviewRequest(BaseModel):
    ticker: str
    asof: str | None = None
    mode: str = "auto"


@router.post("/api/memory/review")
def memory_review(req: ReviewRequest):
    if req.mode not in ("replay", "live", "auto"):
        raise HTTPException(400, "mode must be replay, live or auto")
    if not _TICKER_RE.match(req.ticker.strip()):
        raise HTTPException(400, "ticker must be a symbol such as NVDA or BRK.B")
    if req.asof and not _DATE_RE.match(req.asof):
        raise HTTPException(400, "asof must be YYYY-MM-DD")
    return review.run(req.ticker.strip(), req.asof, req.mode)


@router.get("/api/memory/eval")
def memory_evaluation(refresh: bool = False):
    with _eval_lock:
        rep = mem_eval.cached_report(refresh=refresh)
    out = {k: v for k, v in rep.items() if k != "rows"}
    out["n_rows"] = len(rep.get("rows", []))
    out["archive"] = archive.stats()
    return out


@router.get("/api/memory/finetune")
def memory_finetune_preview():
    return finetune.preview()


@router.post("/api/memory/finetune/export")
def memory_finetune_export():
    return finetune.export()


@router.get("/api/memory/audit")
def memory_audit():
    return guard.audit_verify()


@router.get("/api/memory/lessons")
def memory_lessons():
    return {"text": archive.lessons_text()}


@router.get("/api/memory/playbook")
def memory_playbook(q: str = "", k: int = 4):
    idx = rag.get_knowledge_index()
    if not q:
        chapters: dict[str, int] = {}
        for p in idx.passages:
            chapters[p.chapter] = chapters.get(p.chapter, 0) + 1
        return {"passages": len(idx.passages), "chapters": chapters}
    k = max(1, min(int(k), 10))
    return {"query": q[:2000], "hits": [dict(p.public(), score=round(s, 5))
                                        for p, s in idx.search(q[:2000], k=k)]}
