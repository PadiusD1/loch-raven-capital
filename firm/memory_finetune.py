"""Fine tuning datasets built only from reasoning the market has already graded.

Built at hackUMBC 2026 (T. Rowe Price NextGen Finance track).

RAG gives a model the firm's knowledge at decision time. Fine tuning changes
the model's default behavior. This module turns the Institutional Memory
archive into three training sets, each split by time so that nothing in the
training set was graded after the first test decision was made.

Prompts use the RAFT recipe (retrieval augmented fine tuning). Each example
carries the firm playbook passages and the point in time precedents the
committee could have retrieved that day, so the tuned model learns to reason
with the firm's methodology and history in front of it.

  sft      supervised examples, only directional calls the market proved RIGHT
  kto      every graded directional call, labeled desirable or undesirable.
           KTO (Kahneman Tversky Optimization) learns from unpaired good and bad
           examples, which is exactly the shape real investment outcomes take.
  rewards  every graded memo with its numeric reward, for a reward model

The same pipeline pointed at a firm's own decades of research decisions is
the path from "the model knows what we know" to "the model judges the way
our best calls were judged".
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from firm import memory_archive as archive

OUT_DIR = archive.MEMORY_DIR / "finetune"
DEFAULT_SPLIT = "2026-08-10"   # roughly balances train and test after the leakage purge
MIN_ABS_SCORE = 2.0

SYSTEM = ("You are the portfolio manager of an institutional investment committee. "
          "Weigh the evidence, name the single biggest risk, and issue a verdict as JSON "
          "with rating, conviction, thesis, bear_risk and watch_trigger.")


def _context(m: archive.Memo) -> str:
    """RAFT context for one example. The playbook passages and the precedents the
    committee could have retrieved ON THAT DATE, so the tuned model learns to use
    the firm's memory rather than memorize it. Precedent outcomes appear only
    where they were knowable before the decision."""
    from firm import memory_rag as rag
    query = f"{m.ticker} {m.debate_summary}"
    kblock, _ = rag.knowledge_block(query, k=2, asof=m.asof)
    pblock, _, _ = rag.precedent_block(query, ticker=m.ticker, asof=m.asof, direction=0, k=3)
    return kblock + pblock


def _prompt(m: archive.Memo, context: str = "") -> str:
    return (context
            + f"Ticker {m.ticker} ({m.kind}) on {m.asof}.\n"
            f"Committee debate summary. {m.debate_summary or 'none recorded'}\n"
            f"Issue the verdict, citing any playbook passage as [K#] and precedent as [P#] you rely on.")


def _completion(m: archive.Memo) -> str:
    return json.dumps({"rating": m.rating, "conviction": m.conviction, "thesis": m.thesis,
                       "bear_risk": m.bear_risk, "watch_trigger": m.watch_trigger}, ensure_ascii=False)


def _meta(m: archive.Memo) -> dict:
    return {"memo_id": m.id, "ticker": m.ticker, "asof": m.asof, "rating": m.rating,
            "conviction": m.conviction, "excess_20d": m.excess_20d, "score_20d": m.score_20d,
            "label": m.label, "known_at_20d": m.known_at_20d}


_built: dict = {}
_built_lock = threading.Lock()


def build(split: str = DEFAULT_SPLIT, min_abs_score: float = MIN_ABS_SCORE,
          with_context: bool = True) -> dict:
    memos = archive.load_memos()
    key = (split, min_abs_score, with_context, id(memos))
    with _built_lock:
        cached = _built.get(key)
    if cached is not None:
        return cached
    result = _build(split, min_abs_score, with_context)
    with _built_lock:
        if len(_built) >= 4:            # keep a few variants, never an unbounded cache
            _built.pop(next(iter(_built)))
        _built[key] = result
    return result


def _build(split: str, min_abs_score: float, with_context: bool) -> dict:
    """Return every dataset in memory. Train rows must be graded BEFORE split.

    with_context builds retrieval augmented fine tuning (RAFT) prompts, where
    each example carries the playbook passages and point in time precedents.
    """
    memos = archive.load_memos()
    graded = [m for m in memos if m.direction != 0 and m.score_20d is not None and m.debate_summary]
    train = [m for m in graded if m.known_at_20d and m.known_at_20d < split]
    test = [m for m in graded if m.asof >= split]
    contexts = {m.id: (_context(m) if with_context else "") for m in train + test}

    def sft_rows(rows):
        return [{"messages": [{"role": "system", "content": SYSTEM},
                              {"role": "user", "content": _prompt(m, contexts[m.id])},
                              {"role": "assistant", "content": _completion(m)}],
                 "metadata": _meta(m)}
                for m in rows if m.label == "right" and abs(m.score_20d) >= min_abs_score]

    def kto_rows(rows):
        return [{"prompt": _prompt(m, contexts[m.id]), "completion": _completion(m),
                 "label": m.label == "right", "metadata": _meta(m)} for m in rows]

    rewards = [{"memo_id": m.id, "ticker": m.ticker, "asof": m.asof, "rating": m.rating,
                "reward": m.score_20d if m.direction else None, "excess_20d": m.excess_20d,
                "text": m.thesis} for m in memos if m.excess_20d is not None]
    datasets = {"sft_train": sft_rows(train), "sft_test": sft_rows(test),
                "kto_train": kto_rows(train), "kto_test": kto_rows(test), "rewards": rewards}
    leak = [r["metadata"]["memo_id"] for r in datasets["kto_train"]
            if not (r["metadata"]["known_at_20d"] and r["metadata"]["known_at_20d"] < split)]
    overlap = ({r["metadata"]["memo_id"] for r in datasets["kto_train"]}
               & {r["metadata"]["memo_id"] for r in datasets["kto_test"]})
    card = {
        "model_promotion_allowed": False,
        "quality_warning": "Historical profitable calls are noisy outcome labels, not verified expert reasoning. Export does not train a model.",
        "split_date": split, "min_abs_score_for_sft": min_abs_score,
        "counts": {k: len(v) for k, v in datasets.items()},
        "kto_train_desirable": sum(1 for r in datasets["kto_train"] if r["label"]),
        "kto_test_desirable": sum(1 for r in datasets["kto_test"] if r["label"]),
        "leakage_rows": leak, "train_test_overlap": sorted(overlap),
        "format": "RAFT, retrieval augmented fine tuning" if with_context else "plain prompts",
        "source": "Loch Raven committee decisions, graded against real prices versus SPY, "
                  "with firm playbook passages and point in time precedents in every prompt",
    }
    return {"datasets": datasets, "card": card}


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(path)


def _data_card_md(card: dict) -> str:
    c = card["counts"]
    return "\n".join([
        "# Loch Raven fine tuning data card",
        "",
        f"Generated {datetime.now(timezone.utc).date().isoformat()} from the Institutional Memory archive.",
        "",
        "## What is inside",
        "",
        f"* {c['sft_train']} supervised training examples and {c['sft_test']} test examples. Only "
        f"directional calls the market proved right by at least {card['min_abs_score_for_sft']} points versus SPY.",
        f"* {c['kto_train']} KTO training examples ({card['kto_train_desirable']} desirable) and "
        f"{c['kto_test']} test examples ({card['kto_test_desirable']} desirable). Every graded call, labeled by outcome.",
        f"* {c['rewards']} reward labels for a reward model.",
        "",
        "## Format",
        "",
        f"{card.get('format', 'RAFT')}. Every prompt carries the firm playbook passages and the point in "
        "time precedents the committee could have retrieved on that date, so a tuned model learns to "
        "use the firm's memory instead of memorizing it.",
        "",
        "## How leakage is prevented",
        "",
        f"Training rows were graded before {card['split_date']}. Test rows were decided on or after it. "
        f"Leaked rows found {len(card['leakage_rows'])}. Rows in both splits {len(card['train_test_overlap'])}.",
        "",
        "## Limits",
        "",
        "One market regime, a few months, paper decisions from one AI committee. The dataset proves the "
        "pipeline, not an edge. A firm archive with decades of graded decisions is where the statistics "
        "become meaningful.",
        "",
    ])


def export(split: str = DEFAULT_SPLIT, out_dir: Path | None = None) -> dict:
    built = build(split)
    out = Path(out_dir or OUT_DIR)
    out.mkdir(parents=True, exist_ok=True)
    files = {}
    for name, rows in built["datasets"].items():
        p = out / f"{name}.jsonl"
        _write_jsonl(p, rows)
        files[name] = str(p.name)
    (out / "DATA_CARD.md").write_text(_data_card_md(built["card"]), encoding="utf-8")
    (out / "card.json").write_text(json.dumps(built["card"], indent=1), encoding="utf-8")
    return {"card": built["card"], "files": files, "dir": str(out)}


def preview(split: str = DEFAULT_SPLIT, n: int = 2) -> dict:
    """Counts come from a fast build without retrieval context (the counts are
    identical), and only the sample rows carry their full RAFT context."""
    built = build(split, with_context=False)
    card = dict(built["card"], format="RAFT, retrieval augmented fine tuning")
    memos = {m.id: m for m in archive.load_memos()}
    samples = {}
    for name in ("sft_train", "kto_train"):
        rows = []
        for r in built["datasets"][name][:n]:
            m = memos[r["metadata"]["memo_id"]]
            if name == "kto_train":
                rows.append({**r, "prompt": _prompt(m, _context(m))})
            else:
                msgs = [dict(x) for x in r["messages"]]
                msgs[1]["content"] = _prompt(m, _context(m))
                rows.append({**r, "messages": msgs})
        samples[name] = rows
    return {"card": card, "samples": samples}
