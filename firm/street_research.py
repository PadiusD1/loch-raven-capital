"""Street Research desk — institutional reports feed the committee.

Operator 2026-07-16: "Goldman came out with a 30+ page report on AI — these
reports help the fund make the best decisions and stay informed." The scan
screen sees prices; the news tape sees headlines; NOBODY on the floor was
reading the street's actual research. This desk closes that gap:

  1. The operator drops reports (PDF / .txt / .md) into research_docs/inbox/.
  2. Each NEW file (sha-tracked, ingested once) is distilled by Opus into a
     compact brief that PRESERVES the report's own numbers — no fabrication,
     only claims present in the document (prompt-completeness doctrine: the
     grounding channel is the attached source text).
  3. digest_context() combines the newest briefs into a bounded packet section
     that rides every candidate packet — the fundamental/sentiment/macro desks
     see it in their views and the judge sees everything.
  4. Reports expire after STREET_RESEARCH_TTL_DAYS so a stale March thesis
     never masquerades as the current tape.

Fail-soft everywhere: a corrupt PDF, a missing extractor, or an Opus outage
journals and moves on — the cycle must never die on a dropped file.
"""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import config
from firm import state

_DISTILL_PROMPT = """You are the STREET RESEARCH desk of an institutional \
asset manager (US-listed equities and ETFs; single-name and sector decisions). \
Below is the extracted text of a research report the operator dropped for the \
investment committee. Distill it for the research floor.

STRICT GROUNDING: use ONLY claims, numbers, and names present in the document \
text. If the document does not state something, do not invent it. Preserve the \
report's own figures (price targets, growth rates, TAM numbers) verbatim.

CREDIBILITY DISCIPLINE (operator 2026-07-16): a prestigious letterhead is NOT \
evidence — a Goldman report earns exactly as much scrutiny as anyone else's. \
Judge each claim on its own checkability. Some claims are decade-scale \
structural truths, some are worth two weeks, some are wrong on their face. \
Classify each thesis by HORIZON and give the CONCRETE observable that would \
prove or debunk it, so the desk can retire claims as reality arrives.

Respond with ONLY this JSON:
{"relevance": {"is_relevant": <bool — can this inform US equity/ETF/sector
               decisions at all?>, "why": "<one line>"},
 "source": "<publisher, e.g. Goldman Sachs>", "title": "<report title>",
 "published": "<date if stated, else null>",
 "scope": "<one line: what the report covers>",
 "key_theses": [{"claim": "<one bullet carrying the report's own numbers>",
                 "horizon": "structural|cyclical|tactical",
                 "checkable": "<the specific print/price/event that would
                               prove or debunk this claim>",
                 "confidence": "high|medium|low — YOUR read of the claim's
                                support IN the document, not the brand"}],
 "sector_implications": {"favored": ["..."], "pressured": ["..."]},
 "named_tickers": [{"ticker": "XYZ", "stance": "positive|negative|neutral",
                    "why": "<one line>"}],
 "risks_and_contrarian_points": ["<2-5 bullets>"]}

(6-12 key_theses. horizon guide: tactical = playable weeks, cyclical = this
quarter-to-year regime, structural = multi-year secular claim.)

DOCUMENT TEXT:
"""

_REVIEW_PROMPT = """You are the STREET RESEARCH desk reviewing the SHELF — \
previously distilled report claims — against what has actually happened since. \
Today is {today}. You have NO web access and your training may predate these \
claims — your MEMORY of "what happened" is NOT evidence (fleet re-grade \
2026-07-16: an evidence-free "supported" mints permanent source credibility; \
a confabulated "debunked" destroys a good source). The ONLY admissible \
evidence is the OBSERVED DATA block below: real price moves of the report's \
named tickers since ingestion and macro releases that actually landed. For \
each claim, judge its CURRENT status:

  intact    — the observed data neither proves nor debunks it (the DEFAULT)
  supported — the observed data CONFIRMS it (quote the datum)
  debunked  — the observed data CONTRADICTS it (quote the datum)
  expired   — its window passed without resolution; no longer actionable

Be adversarial: a prestigious source earns no benefit of the doubt — and a \
claim the observed data cannot reach stays intact until its horizon expires.

OBSERVED DATA (the only admissible evidence):
{ground_truth}

Respond with ONLY this JSON:
{"reviews": [{"idx": <claim index as given>, "status":
  "intact|supported|debunked|expired", "evidence": "<one line, concrete>",
  "resolved_from": "<REQUIRED for supported/debunked: quote the exact line \
from OBSERVED DATA that resolves the claim>"}]}

CLAIMS:
{claims}
"""


def _ground_truth(d: dict, theses: list, live_idx: list) -> tuple[str, set]:
    """OBSERVABLE ground truth for the claim review (fleet re-grade action 6):
    the review brain is a no-tools print-mode model — asked for 'what has
    occurred since', it confabulates by construction. Attach what the fund
    can OBSERVE in-process: each named ticker's price move since ingestion
    (data_engine closes) and any macro release a claim references that has
    actually landed (macro_calendar). Returns (text_block, anchor_tokens) —
    the caller refuses any supported/debunked whose resolved_from quotes
    none of the anchors."""
    anchors: set[str] = set()
    lines: list[str] = []
    b = d.get("brief") or {}
    added = str(d.get("added", ""))[:10]
    try:
        since = datetime.fromisoformat(added).date()
    except (ValueError, TypeError):
        return "", anchors
    tickers = sorted({str(t.get("ticker", "")).upper()
                      for t in (b.get("named_tickers") or [])
                      if isinstance(t, dict) and t.get("ticker")})[:12]
    if tickers:
        try:
            import pandas as pd
            from firm import data_engine
            closes = data_engine.fetch_history(tickers, period="6mo")
            for t in tickers:
                if t not in getattr(closes, "columns", []):
                    continue
                seg = closes[t].dropna()
                seg = seg[seg.index >= pd.Timestamp(since)]
                if len(seg) >= 2:
                    chg = (float(seg.iloc[-1]) / float(seg.iloc[0]) - 1) * 100
                    lines.append(f"{t}: {chg:+.1f}% since {added} "
                                 f"(now {float(seg.iloc[-1]):.2f})")
                    anchors.add(t)
        except Exception as e:
            state.journal("street_ground_truth_prices_failed",
                          file=d.get("file"), error=str(e)[:80])
    try:
        from firm import macro_calendar
        today = datetime.now(timezone.utc).date()
        for i in live_idx:
            txt = (f"{theses[i].get('claim', '')} "
                   f"{theses[i].get('checkable', '')}")
            landed = macro_calendar.mentioned_landed(txt, since, today)
            if landed:
                lines.append(f"claim {i}: a macro release it references "
                             f"landed {landed.isoformat()}")
                anchors.add(landed.isoformat())
    except Exception:
        pass
    return "\n".join(lines), anchors


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()[:24]


def _extract_text(path: Path) -> str | None:
    """Report text, capped. None = unreadable (journaled by the caller)."""
    cap = int(getattr(config, "STREET_RESEARCH_MAX_CHARS", 45_000))
    suffix = path.suffix.lower()
    try:
        if suffix in (".txt", ".md"):
            return path.read_text(encoding="utf-8", errors="ignore")[:cap]
        if suffix == ".pdf":
            try:
                from pypdf import PdfReader
            except ImportError:
                state.journal("street_research_no_pdf_extractor", file=path.name)
                return None
            out: list[str] = []
            n = 0
            for page in PdfReader(str(path)).pages:
                t = page.extract_text() or ""
                out.append(t)
                n += len(t)
                if n >= cap:
                    break
            return "\n".join(out)[:cap]
    except Exception as e:
        state.journal("street_research_extract_failed", file=path.name,
                      error=str(e)[:100])
    return None


def _age_days(iso: str) -> float:
    try:
        return (datetime.now(timezone.utc)
                - datetime.fromisoformat(iso)).total_seconds() / 86400
    except (ValueError, TypeError):
        return 1e9


def _norm_thesis(t) -> dict:
    """Claim shape tolerance: legacy briefs stored bare strings; the credibility
    engine stores dicts. Everything downstream sees ONE shape."""
    if isinstance(t, dict):
        return {"claim": str(t.get("claim", ""))[:400],
                "horizon": t.get("horizon") if t.get("horizon") in
                ("structural", "cyclical", "tactical") else "cyclical",
                "checkable": str(t.get("checkable", ""))[:200],
                "confidence": t.get("confidence", "medium"),
                "status": t.get("status", "intact"),
                "resolved_evidence": t.get("resolved_evidence", "")}
    return {"claim": str(t)[:400], "horizon": "cyclical", "checkable": "",
            "confidence": "medium", "status": "intact", "resolved_evidence": ""}


def _claim_alive(t: dict, doc_added: str) -> bool:
    """A claim leaves the digest when the review loop resolves it (debunked =
    wrong, expired = window passed) or its horizon TTL runs out. 'supported'
    stays — a CONFIRMED thesis is the most valuable kind until its TTL."""
    if t.get("status") in ("debunked", "expired"):
        return False
    ttls = getattr(config, "STREET_RESEARCH_TTL_BY_HORIZON",
                   {"tactical": 14, "cyclical": 45, "structural": 180})
    return _age_days(doc_added) <= float(ttls.get(t.get("horizon"), 45))


def _prune(docs: list[dict]) -> list[dict]:
    """Doc-level shelf rules: an irrelevant doc never rides (kept only as a
    sha-dedupe tombstone); a doc with ZERO live claims drops off; hard cap on
    the newest N. The per-claim lifecycle happens in _claim_alive/review."""
    cap = int(getattr(config, "STREET_RESEARCH_MAX_DOCS", 8))
    live = []
    for d in docs:
        if d.get("irrelevant"):
            live.append(d)                 # tombstone: cheap, blocks re-distill
            continue
        theses = [_norm_thesis(t)
                  for t in ((d.get("brief") or {}).get("key_theses") or [])]
        if any(_claim_alive(t, d.get("added", "")) for t in theses):
            live.append(d)
        else:
            state.journal("street_research_doc_retired", file=d.get("file"),
                          note="no live claims remain (resolved/expired)")
    keep = [d for d in live if not d.get("irrelevant")]
    keep.sort(key=lambda d: d.get("added", ""), reverse=True)
    stones = [d for d in live if d.get("irrelevant")][-20:]
    return keep[:cap] + stones


def scan_and_distill(max_new: int | None = None) -> int:
    """Ingest new reports from the inbox. Returns how many were distilled.
    Bounded per call (Opus cost); the rest are picked up next sweep. Safe to
    call from the cycle AND the 30-min health sweep — sha dedupe makes it
    idempotent, and it holds no sleeve lock (never touches money state)."""
    from firm import brain
    inbox = Path(getattr(config, "STREET_RESEARCH_DIR",
                         config.BASE_DIR / "research_docs" / "inbox"))
    try:
        inbox.mkdir(parents=True, exist_ok=True)
    except OSError:
        return 0
    store = state.load("street_research", {}) or {}
    docs = [d for d in (store.get("docs") or []) if isinstance(d, dict)]
    seen = {d.get("sha") for d in docs}
    cap_new = max_new if max_new is not None else int(
        getattr(config, "STREET_RESEARCH_MAX_NEW_PER_SCAN", 2))
    new_files = []
    for p in sorted(inbox.iterdir()):
        if not p.is_file() or p.suffix.lower() not in (".pdf", ".txt", ".md"):
            continue
        try:
            sha = _sha(p)
        except OSError:
            continue
        if sha not in seen:
            new_files.append((p, sha))
    if not new_files:
        # No new documents — still keep the shelf fresh (expiry, cap).
        pruned = _prune(docs)
        if len(pruned) != len(docs):
            store["docs"] = pruned          # preserve source_track etc.
            state.save("street_research", store)
        return 0
    if not brain.available():
        state.journal("street_research_deferred",
                      pending=[p.name for p, _ in new_files[:6]],
                      reason="claude CLI unavailable — retry next sweep")
        return 0
    ingested = 0
    for p, sha in new_files[:cap_new]:
        text = _extract_text(p)
        if not text or len(text.strip()) < 200:
            state.journal("street_research_unreadable", file=p.name)
            continue
        brief = brain.ask_json(_DISTILL_PROMPT + text, retries=1)
        if not isinstance(brief, dict) or not brief.get("key_theses"):
            state.journal("street_research_distill_failed", file=p.name)
            continue
        # Relevance gate (operator 2026-07-16): a report that cannot inform a
        # US equity/ETF/sector decision never rides a packet — it becomes a
        # sha tombstone (never re-distilled) and the operator is told why.
        _rel = brief.get("relevance") or {}
        if _rel.get("is_relevant") is False:
            docs.append({"sha": sha, "file": p.name, "irrelevant": True,
                         "added": datetime.now(timezone.utc).isoformat(),
                         "why": str(_rel.get("why", ""))[:200]})
            state.journal("street_research_irrelevant", file=p.name,
                          why=str(_rel.get("why", ""))[:150])
            try:
                from firm import alerts
                alerts.alert("STREET RESEARCH SKIPPED (not relevant)",
                             f"{p.name}: {str(_rel.get('why', ''))[:120]}",
                             "info")
            except Exception:
                pass
            continue
        brief["key_theses"] = [_norm_thesis(t)
                               for t in (brief.get("key_theses") or [])][:12]
        docs.append({"sha": sha, "file": p.name,
                     "added": datetime.now(timezone.utc).isoformat(),
                     "brief": brief})
        ingested += 1
        state.journal("street_research_ingested", file=p.name,
                      source=str(brief.get("source", ""))[:60],
                      title=str(brief.get("title", ""))[:100])
        try:
            from firm import alerts
            alerts.alert("STREET RESEARCH LOADED",
                         f"{brief.get('source', '?')}: "
                         f"{str(brief.get('title', p.name))[:90]} — the "
                         "committee sees it from the next cycle on.", "info")
        except Exception:
            pass
    store["docs"] = _prune(docs)
    state.save("street_research", store)
    if len(new_files) > cap_new:
        state.journal("street_research_backlog",
                      remaining=len(new_files) - cap_new,
                      note="picked up on the next sweep (cost bound)")
    return ingested


def review_claims(max_docs: int = 2) -> int:
    """The claim VERIFICATION loop (operator 2026-07-16: "some are completely
    false and can be debunked... once proven false or true, remove"): every
    REVIEW_EVERY_D days each doc's live claims go back to Opus with their own
    checkables — intact / supported / debunked / expired. Debunked+expired
    claims leave the digest immediately; every resolution updates the SOURCE
    TRACK RECORD, so a staple name earns trust from its resolution history,
    not its letterhead. Self-gating on cadence (0 Opus calls when nothing is
    due) and bounded per call. Returns docs reviewed."""
    from firm import brain
    if not brain.available():
        return 0
    store = state.load("street_research", {}) or {}
    docs = [d for d in (store.get("docs") or []) if isinstance(d, dict)]
    every = float(getattr(config, "STREET_RESEARCH_REVIEW_EVERY_D", 7))
    due = [d for d in docs if not d.get("irrelevant")
           and _age_days(d.get("last_reviewed") or d.get("added", "")) >= every
           and any(_claim_alive(_norm_thesis(t), d.get("added", ""))
                   for t in ((d.get("brief") or {}).get("key_theses") or []))]
    if not due:
        return 0
    track = store.get("source_track") or {}
    reviewed = 0
    for d in due[:max_docs]:
        b = d.get("brief") or {}
        theses = [_norm_thesis(t) for t in (b.get("key_theses") or [])]
        live_idx = [i for i, t in enumerate(theses)
                    if _claim_alive(t, d.get("added", ""))]
        claims_txt = json.dumps(
            [{"idx": i, "claim": theses[i]["claim"],
              "horizon": theses[i]["horizon"],
              "checkable": theses[i]["checkable"],
              "report_date": d.get("added", "")[:10]} for i in live_idx],
            indent=1)
        gt_txt, gt_anchors = _ground_truth(d, theses, live_idx)
        res = brain.ask_json(
            _REVIEW_PROMPT.replace("{today}",
                                   datetime.now(timezone.utc).date().isoformat())
                          .replace("{ground_truth}",
                                   gt_txt or "(none available this pass — "
                                             "no claim may be supported or "
                                             "debunked)")
                          .replace("{claims}", claims_txt), retries=1)
        d["last_reviewed"] = datetime.now(timezone.utc).isoformat()
        reviewed += 1
        if not isinstance(res, dict):
            state.journal("street_research_review_failed", file=d.get("file"))
            continue
        src = str(b.get("source") or "?")[:60]
        tr = track.setdefault(src, {"supported": 0, "debunked": 0, "expired": 0})
        changed = []
        for r in (res.get("reviews") or []):
            i = r.get("idx")
            st = str(r.get("status", "")).lower()
            if not isinstance(i, int) or i not in live_idx \
                    or st not in ("intact", "supported", "debunked", "expired"):
                continue
            # GROUNDING GUARD (fleet re-grade action 6): supported/debunked
            # MINT or DESTROY source credibility — they are only admissible
            # when resolved FROM the attached observed data. resolved_from
            # must quote an anchor that is actually IN the data block (a
            # named ticker or a landed release date); a length check alone
            # let confabulated evidence through by construction.
            if st in ("supported", "debunked"):
                _rf = str(r.get("resolved_from", "")).strip().upper()
                _anchored = bool(_rf) and any(a in _rf for a in gt_anchors)
                if (not _anchored
                        or len(str(r.get("evidence", "")).strip()) < 20):
                    state.journal("street_review_ungrounded",
                                  file=d.get("file"), idx=i,
                                  claimed_status=st,
                                  note="resolved_from quotes no observed "
                                       "datum — kept intact")
                    continue
            if st != theses[i]["status"]:
                theses[i]["status"] = st
                theses[i]["resolved_evidence"] = str(r.get("evidence", ""))[:200]
                changed.append(f"{st}: {theses[i]['claim'][:60]}")
                if st in tr:
                    tr[st] += 1
                if st == "debunked":
                    try:
                        from firm import alerts
                        alerts.alert(
                            "STREET CLAIM DEBUNKED",
                            f"{src}: \"{theses[i]['claim'][:90]}\" — "
                            f"{theses[i]['resolved_evidence'][:100]}. Weigh "
                            "this source's remaining claims with caution.",
                            "info")
                    except Exception:
                        pass
        b["key_theses"] = theses
        d["brief"] = b
        if changed:
            state.journal("street_research_claims_resolved",
                          file=d.get("file"), source=src, changes=changed[:6])
    store["docs"] = _prune(docs)
    store["source_track"] = track
    state.save("street_research", store)
    return reviewed


def maintain() -> None:
    """One call for the cycle/health-sweep hooks: ingest anything new, then
    run any due claim reviews. Both halves self-gate and fail soft."""
    scan_and_distill()
    review_claims()


def digest_context() -> dict | None:
    """The packet-ready digest: newest briefs, LIVE claims only (intact +
    supported — a debunked or expired claim never reaches a desk again), with
    each source's resolution track record attached so desks weigh the record,
    not the letterhead. None when the shelf is empty (the packet key is then
    simply absent — desks are told nothing rather than shown an empty section
    to confabulate around)."""
    store = state.load("street_research", {}) or {}
    docs = _prune([d for d in (store.get("docs") or []) if isinstance(d, dict)])
    track = store.get("source_track") or {}
    reports = []
    for d in docs:
        if d.get("irrelevant"):
            continue
        b = d.get("brief") or {}
        theses = [_norm_thesis(t) for t in (b.get("key_theses") or [])]
        live = []
        for t in theses:
            if not _claim_alive(t, d.get("added", "")):
                continue
            line = {"claim": t["claim"], "horizon": t["horizon"],
                    "confidence": t["confidence"]}
            if t["status"] == "supported":
                line["confirmed_since_publication"] = t["resolved_evidence"]
            live.append(line)
        if not live:
            continue
        src = str(b.get("source") or "?")[:60]
        tr = track.get(src) or {}
        rec = {"source": src, "title": b.get("title"),
               "published": b.get("published"),
               "age_days": round(_age_days(d.get("added", "")), 1),
               "scope": b.get("scope"),
               "key_theses": live[:12],
               "sector_implications": b.get("sector_implications") or {},
               "named_tickers": (b.get("named_tickers") or [])[:15],
               "risks_and_contrarian_points":
                   (b.get("risks_and_contrarian_points") or [])[:5]}
        if any(tr.values()):
            rec["source_track_record"] = (
                f"{tr.get('supported', 0)} claim(s) later confirmed, "
                f"{tr.get('debunked', 0)} debunked, {tr.get('expired', 0)} "
                "expired unresolved — weigh THIS record, not the brand name.")
        reports.append(rec)
    if not reports:
        return None
    return {"note": ("Institutional street research the operator loaded — ONE "
                     "informed voice, not gospel. A prestigious letterhead is "
                     "not evidence: weigh each claim's own support and the "
                     "source's resolution track record. Numbers come from the "
                     "reports themselves; debunked/expired claims have already "
                     "been removed."),
            "reports": reports}
