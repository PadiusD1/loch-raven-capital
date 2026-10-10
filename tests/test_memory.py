"""Institutional Memory regression suite (hackUMBC 2026).

Runs fully offline against the bundled archive in data/memory, in a
throwaway state sandbox. No Claude calls, no market data.

    py -3.11 tests\\test_memory.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import config  # noqa: E402

config.STATE_DIR = Path(tempfile.mkdtemp(prefix="lochraven_memtest_"))

from firm import memory_archive as archive  # noqa: E402
from firm import memory_eval as mem_eval  # noqa: E402
from firm import memory_finetune as finetune  # noqa: E402
from firm import memory_guard as guard  # noqa: E402
from firm import memory_rag as rag  # noqa: E402
from firm import memory_review as review  # noqa: E402

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


print("\n1. Archive")
memos = archive.load_memos()
stats = archive.stats(memos)
disk = json.loads((archive.MEMORY_DIR / "dataset_stats.json").read_text(encoding="utf-8"))
check("archive loads every memo", len(memos) == disk["n_memos"], f"{len(memos)} vs {disk['n_memos']}")
check("ids are unique", len({m.id for m in memos}) == len(memos))
check("memos sorted by date", all(memos[i].asof <= memos[i + 1].asof for i in range(len(memos) - 1)))
check("live stats agree with the build stats",
      stats["directional"]["n"] == disk["directional_20d"]["overall"]["n"]
      and abs(stats["directional"]["hit_rate"] - disk["directional_20d"]["overall"]["hit_rate"]) < 1e-3)
scored = [m for m in memos if m.excess_20d is not None]
check("an outcome is only knowable after its decision", all(m.known_at_20d > m.asof for m in scored))
check("labels follow the scoring rule", all(
    (m.label == "right") == (m.score_20d is not None and m.score_20d > 0)
    for m in scored if m.direction != 0))
check("HOLD calls are never graded right or wrong", all(m.label in ("neutral", "pending") for m in memos if m.direction == 0))

print("\n2. Point in time retrieval")
idx = rag.get_index()
probe = next(m for m in reversed(memos) if m.ticker == "MU" and m.direction != 0)
hits = idx.search(probe.text(), asof=probe.asof, ticker=probe.ticker, k=8)
check("retrieval returns precedents", len(hits) > 0)
check("no precedent is dated on or after the decision", all(h.memo.asof < probe.asof for h in hits))
check("an outcome is shown only when it was knowable",
      all((not h.outcome_visible) or h.memo.known_at_20d < probe.asof for h in hits))
graded = idx.search(probe.text(), asof=probe.asof, ticker=probe.ticker, k=8, require_outcome=True)
check("outcome mode keeps only graded, knowable precedents",
      all(h.memo.outcome_known_before(probe.asof) for h in graded))
check("the name's own history ranks among the top precedents",
      any(h.memo.ticker == "MU" for h in hits[:3]))
first_day = memos[0].asof
check("a decision on day one has no precedents", idx.search("anything at all", asof=first_day, k=5) == [])
self_hits = idx.search(probe.text(), k=3)
check("an exact memo retrieves itself first when the date filter is off", self_hits and self_hits[0].memo.id == probe.id)
per = {}
for h in idx.search("momentum leader overbought pullback", k=12, per_ticker=2):
    per[h.memo.ticker] = per.get(h.memo.ticker, 0) + 1
check("diversity cap limits any one ticker", max(per.values()) <= 3, str(per))

print("\n3. Precedent block and signal")
block, bhits, sig = rag.precedent_block(probe.text(), ticker="MU", asof=probe.asof, direction=probe.direction, k=5)
check("block labels precedents P1 to Pk", all(f"[P{i}]" in block for i in range(1, len(bhits) + 1)))
check("block never states an outcome that was not knowable",
      all(("not yet knowable" in line) for line in block.splitlines()
          for i, h in enumerate(bhits, 1) if line.startswith(f"[P{i}]") and not h.outcome_visible))
check("signal counts only knowable outcomes", sig["n"] == sum(1 for h in bhits if h.outcome_visible and h.memo.direction == probe.direction and h.memo.excess_20d is not None))
fake = [rag.Hit(memo=m, score=1.0, bm25_rank=1, dense_rank=1, outcome_visible=True)
        for m in scored[:4]]
buy = rag.precedent_signal(fake, 1)
sell = rag.precedent_signal(fake, -1)
check("signals count the selected same-direction precedent cohort", buy["n"] == sum(1 for h in fake if h.memo.direction == 1) and sell["n"] == sum(1 for h in fake if h.memo.direction == -1))

print("\n3b. Firm playbook")
kidx = rag.get_knowledge_index()
check("playbook loads chapters and lessons", len(kidx.passages) >= 40, str(len(kidx.passages)))
check("every passage names its source", all(p.source for p in kidx.passages))
top = kidx.search("terminal value perpetuity growth WACC discount rate", k=2)
check("a DCF question retrieves the DCF chapter", top and "DCF" in top[0][0].chapter, top[0][0].chapter if top else "none")
top = kidx.search("conviction band 60 to 69 half clip sizing", k=3)
check("a sizing question retrieves the firm lessons", any(p.chapter.startswith("Firm lessons") for p, _ in top))
kb, ps = rag.knowledge_block("price to earnings misleads for cyclicals at peak earnings", k=3)
check("playbook block labels passages K1 to Kk", all(f"[K{i}]" in kb for i in range(1, len(ps) + 1)) and ps)

print("\n4. Guardrails")
c_ok = guard.check_citations("Momentum faded after [P1] and [P3].", 3)
c_bad = guard.check_citations("As [P9] shows.", 3)
check("real citations pass", c_ok["status"] == "pass" and c_ok["cited"] == [1, 3])
check("a citation to an unseen precedent fails", c_bad["status"] == "fail" and c_bad["invalid"] == ["P9"])
c_k = guard.check_citations("Terminal value dominates [K1], as in [P2].", 3, 2)
c_kbad = guard.check_citations("Per the playbook [K4].", 3, 2)
check("playbook citations pass when real", c_k["status"] == "pass" and c_k["kcited"] == [1])
check("a citation to an unseen playbook passage fails", c_kbad["status"] == "fail" and c_kbad["invalid"] == ["K4"])
n_ok = guard.check_numbers("Revenue grew 52.2% at a 0.19% margin.", "revenue growing 52.2% with a 0.19% operating margin")
n_bad = guard.check_numbers("Revenue grew 61.4% this year.", "revenue growing 52.2%")
check("figures found in the evidence pass", n_ok["status"] == "pass")
check("an invented figure is flagged", n_bad["status"] == "warn" and "61.4%" in n_bad["ungrounded"])
check("dates and small counts are not treated as claims",
      guard.check_numbers("Over 20 trading days in 2026.", "")["status"] == "pass")
small = guard.check_numbers("Margins expanded 18% with a price target of $22.", "no matching figures")
check("small percentages and dollar figures are still claims", small["status"] == "warn"
      and "18%" in small["ungrounded"] and "$22" in small["ungrounded"])
check("policy rejects an unknown rating", guard.check_policy({"rating": "MOON", "conviction": 50})["status"] == "fail")
check("policy rejects an oversized position",
      guard.check_policy({"rating": "BUY", "conviction": 80, "target_weight_pct": 40})["status"] == "fail")
check("policy accepts a sane verdict", guard.check_policy({"rating": "HOLD", "conviction": 45})["status"] == "pass")
pit = guard.check_point_in_time(hits, probe.asof)
check("point in time check passes on real retrieval", pit["status"] == "pass")
future = [rag.Hit(memo=memos[-1], score=1.0, bm25_rank=1, dense_rank=1, outcome_visible=True)]
check("point in time check catches a future precedent", guard.check_point_in_time(future, memos[0].asof)["status"] == "fail")

print("\n5. Audit chain")
apath = config.STATE_DIR / "audit_test.jsonl"
for i in range(3):
    guard.audit_append({"kind": "test", "i": i}, apath)
check("an untouched chain verifies", guard.audit_verify(apath) == {"ok": True, "records": 3})
lines = apath.read_text(encoding="utf-8").splitlines()
row = json.loads(lines[1])
row["i"] = 99
lines[1] = json.dumps(row, sort_keys=True, separators=(",", ":"))
apath.write_text("\n".join(lines) + "\n", encoding="utf-8")
check("an edited record breaks the chain", not guard.audit_verify(apath)["ok"])

print("\n6. Fine tuning datasets")
built = finetune.build()
card = built["card"]
ds = built["datasets"]
check("no training row was graded after the split", card["leakage_rows"] == [])
check("no row sits in both train and test", card["train_test_overlap"] == [])
check("SFT keeps only calls the market proved right",
      all(r["metadata"]["label"] == "right" for r in ds["sft_train"] + ds["sft_test"]))
check("SFT rows clear the minimum score",
      all(abs(r["metadata"]["score_20d"]) >= finetune.MIN_ABS_SCORE for r in ds["sft_train"]))
check("KTO labels match the outcome", all(r["label"] == (r["metadata"]["label"] == "right") for r in ds["kto_train"]))
check("test rows were decided on or after the split", all(r["metadata"]["asof"] >= card["split_date"] for r in ds["kto_test"]))
check("SFT completions are valid JSON verdicts",
      all(json.loads(r["messages"][2]["content"])["rating"] for r in ds["sft_train"][:25]))
check("RAFT training never contains lessons authored after its split",
      all("2026-09-18" not in r["prompt"] and "2026-09-26" not in r["prompt"] for r in ds["kto_train"]))
rafts = [r for r in ds["kto_train"] if "[P1]" in r["prompt"]]
check("RAFT precedents never leak an outcome known after the decision", all(
    "not yet knowable" in line or line.split(" ")[1] < r["metadata"]["asof"]
    for r in rafts for line in r["prompt"].splitlines() if line.startswith("[P")))
out = finetune.export(out_dir=config.STATE_DIR / "ft")
check("export writes every file and a data card",
      all((config.STATE_DIR / "ft" / f"{k}.jsonl").is_file() for k in ds)
      and (config.STATE_DIR / "ft" / "DATA_CARD.md").is_file())

print("\n7. Walk forward evaluation")
rep = mem_eval.walk_forward(k=6)
check("every graded directional call is evaluated", rep["committee_alone"]["n"] == stats["directional"]["n"])
check("buckets partition the calls",
      rep["memory_kept"]["n"] + rep["memory_vetoed"]["n"] + rep["no_memory_yet"]["n"] == rep["committee_alone"]["n"])
check("rows carry the memory decision", all(r["memory"] in ("keep", "veto", "no memory yet") for r in rep["rows"]))
check("headline is computed from the numbers", "headline" not in rep or str(rep["memory_kept"]["n"]) in rep["headline"])

print("\n8. Review pipeline, replay and guardrails")
tickers = review.replay_tickers()
if tickers:
    r = review.run(tickers[0], mode="replay")
    check("a recorded review replays offline", r["ok"] and r["verdict"]["rating"] in guard.VALID_RATINGS)
    check("replayed review re-runs every guardrail", len(r["guardrails"]["checks"]) == 5)
    check("replayed review is appended to the audit chain", guard.audit_verify()["ok"])
else:
    print("  SKIP  no recorded reviews in data/memory/demo")
missing = review.run("ZZZZ", mode="replay")
check("an unknown name fails soft with no verdict", missing["ok"] is False)

print("\n9. HTTP routes")
from fastapi.testclient import TestClient  # noqa: E402

import memory_console  # noqa: E402

client = TestClient(memory_console.app)
check("stats route", client.get("/api/memory/stats").status_code == 200)
check("search route", client.get("/api/memory/search", params={"q": "memory chip pricing"}).json()["hits"] != [])
check("ticker route", client.get("/api/memory/ticker/MU").status_code == 200)
check("bad ticker is rejected", client.post("/api/memory/review", json={"ticker": "../etc", "mode": "replay"}).status_code == 400)
check("bad date is rejected", client.post("/api/memory/review", json={"ticker": "MU", "asof": "yesterday"}).status_code == 400)
check("console page is served", client.get("/memory").status_code == 200)
check("fine tuning preview route", client.get("/api/memory/finetune").status_code == 200)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
