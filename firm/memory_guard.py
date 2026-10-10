"""Guardrails for memory grounded decisions, plus a tamper evident audit trail.

Built at hackUMBC 2026 (T. Rowe Price NextGen Finance track).

The language model reasons inside the system. It never grades itself. Every
memory grounded verdict passes deterministic checks before a human sees it.

  citations       every [P#] it cites must be a precedent it was actually shown
  numbers         every figure in the thesis must trace to the evidence it was given
  point in time   no precedent from the future, no outcome that was not yet knowable
  policy          ratings, conviction and position size stay inside firm limits
  human sign off  a memory review is a recommendation. It never places an order.

Each review is appended to a hash chained log. Changing any past record breaks
every hash after it, so the trail shows exactly what the model saw and said.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

import config

CITE_RE = re.compile(r"\[P(\d{1,2})\]")
KCITE_RE = re.compile(r"\[K(\d{1,2})\]")
NUM_RE = re.compile(r"(?<![A-Za-z0-9_.])\$?\d[\d,]*(?:\.\d+)?%?")
VALID_RATINGS = frozenset({"STRONG_BUY", "BUY", "HOLD", "SELL", "STRONG_SELL"})
AUDIT_PATH = Path(getattr(config, "STATE_DIR", Path("state"))) / "memory_audit.jsonl"
_audit_lock = threading.Lock()


def _check(name: str, status: str, detail: str, **extra) -> dict:
    return {"name": name, "status": status, "detail": detail, **extra}


def check_citations(text: str, n_precedents: int, n_passages: int = 0, source_ids=()) -> dict:
    cited = sorted({int(x) for x in CITE_RE.findall(text or "")})
    kcited = sorted({int(x) for x in KCITE_RE.findall(text or "")})
    invalid = [f"P{c}" for c in cited if c < 1 or c > n_precedents]
    invalid += [f"K{c}" for c in kcited if c < 1 or c > n_passages]
    rcited = sorted(set(re.findall(r"\[(R[FSMTQ]\d{1,2})\]", text or "")))
    invalid += [c for c in rcited if c not in set(source_ids)]
    if invalid:
        return _check("citations", "fail", f"cites sources it was never shown {invalid}",
                      cited=cited, kcited=kcited, invalid=invalid)
    if not cited and not kcited and not rcited:
        return _check("citations", "warn", "nothing cited, the verdict rests on the live data only",
                      cited=[], kcited=[], invalid=[])
    return _check("citations", "pass",
                  f"{len(cited)} precedents, {len(kcited)} playbook and {len(rcited)} external sources cited, all supplied",
                  cited=cited, kcited=kcited, invalid=[])


def _as_number(tok: str) -> float | None:
    t = tok.replace("$", "").replace(",", "").rstrip("%")
    try:
        return float(t)
    except ValueError:
        return None


def check_numbers(text: str, evidence: str) -> dict:
    """Figures in the model's text that do not appear anywhere in its evidence.

    Small counting words (1 to 31) and years are ignored because they are dates,
    horizons and ordinal references rather than claims about the company.
    """
    ev_values = [_as_number(t) for t in NUM_RE.findall(evidence or "")]
    ev_values = [v for v in ev_values if v is not None]
    claims = []
    for tok in NUM_RE.findall(text or ""):
        v = _as_number(tok)
        if v is None:
            continue
        is_measure = tok.endswith("%") or tok.startswith("$")   # 18% or $22 is always a claim
        if not is_measure and float(v).is_integer() and (0 <= v <= 31 or 1990 <= v <= 2100):
            continue
        claims.append((tok, v))
    ungrounded = []
    for tok, v in claims:
        tol = max(0.011, abs(v) * 0.006)   # rounding to one decimal place still counts as grounded
        if not any(abs(v - e) <= tol for e in ev_values):
            ungrounded.append(tok)
    if not claims:
        return _check("numbers", "pass", "no numeric claims to verify", claims=0, ungrounded=[])
    if ungrounded:
        return _check("numbers", "warn",
                      f"{len(ungrounded)} of {len(claims)} figures not found in the evidence",
                      claims=len(claims), ungrounded=ungrounded[:12])
    return _check("numbers", "pass", f"all {len(claims)} figures trace to the evidence",
                  claims=len(claims), ungrounded=[])


def check_point_in_time(hits: list, asof: str) -> dict:
    future = [h.memo.id for h in hits if h.memo.asof >= asof]
    leaked = [h.memo.id for h in hits if h.outcome_visible and not h.memo.outcome_known_before(asof)]
    if future or leaked:
        return _check("point_in_time", "fail", "look ahead detected", future=future, leaked=leaked)
    hidden = sum(1 for h in hits if not h.outcome_visible)
    return _check("point_in_time", "pass",
                  f"all {len(hits)} precedents predate {asof}, {hidden} outcomes withheld as not yet knowable")


def check_policy(verdict: dict) -> dict:
    rating = str(verdict.get("rating") or "").upper()
    problems = []
    if rating not in VALID_RATINGS:
        problems.append(f"unknown rating {rating or 'missing'}")
    conv = verdict.get("conviction")
    if not isinstance(conv, (int, float)) or not 0 <= conv <= 100:
        problems.append("conviction outside 0 to 100")
    max_w = float(getattr(config, "MEMORY_MAX_TARGET_WEIGHT_PCT", 18.0))
    tw = verdict.get("target_weight_pct")
    if isinstance(tw, (int, float)) and tw > max_w:
        problems.append(f"target weight {tw}% above the {max_w}% single name cap")
    if rating in {"BUY", "STRONG_BUY"} and isinstance(conv, (int, float)) and conv < 50:
        problems.append("a BUY below 50 conviction is not a real conviction call")
    if problems:
        return _check("policy", "fail", ". ".join(problems))
    return _check("policy", "pass", "rating, conviction and size inside firm limits")


def review(verdict: dict, hits: list, evidence: str, asof: str, n_passages: int = 0) -> dict:
    text = " ".join(str(verdict.get(k) or "") for k in ("thesis", "bear_risk", "watch_trigger", "memory_note"))
    # The verdict's own conviction is not a claim about the company.
    own = f" {verdict.get('conviction')} {verdict.get('target_weight_pct') or ''}"
    checks = [
        check_citations(text, len(hits), n_passages),
        check_numbers(text, evidence + own),
        check_point_in_time(hits, asof),
        check_policy(verdict),
        _check("human_sign_off", "pass",
               "recommendation only. No order is placed until an analyst approves it."),
    ]
    status = "fail" if any(c["status"] == "fail" for c in checks) else (
        "warn" if any(c["status"] == "warn" for c in checks) else "pass")
    return {"status": status, "checks": checks}


# Audit trail ---------------------------------------------------------------

def _canonical(obj: dict) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _last_hash(path: Path) -> str:
    if not path.is_file():
        return "0" * 64
    last = ""
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                last = line
    try:
        return json.loads(last)["hash"]
    except (ValueError, KeyError):
        return "0" * 64


def audit_append(record: dict, path: Path | None = None) -> dict:
    """Append one record to the hash chain and return it with its hash."""
    p = Path(path or AUDIT_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    with _audit_lock:
        prev = _last_hash(p)
        body = {"ts": datetime.now(timezone.utc).isoformat(), "prev": prev, **record}
        digest = hashlib.sha256((prev + _canonical(body)).encode("utf-8")).hexdigest()
        row = {**body, "hash": digest}
        with p.open("a", encoding="utf-8") as fh:
            fh.write(_canonical(row) + "\n")
        return row


def audit_verify(path: Path | None = None) -> dict:
    """Recompute the chain. Any edited, reordered or deleted record breaks it."""
    p = Path(path or AUDIT_PATH)
    if not p.is_file():
        return {"ok": True, "records": 0}
    prev = "0" * 64
    n = 0
    with p.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            digest = row.pop("hash", "")
            if row.get("prev") != prev:
                return {"ok": False, "records": n, "broken_at": n + 1, "reason": "chain link mismatch"}
            if hashlib.sha256((prev + _canonical(row)).encode("utf-8")).hexdigest() != digest:
                return {"ok": False, "records": n, "broken_at": n + 1, "reason": "record altered"}
            prev = digest
            n += 1
    return {"ok": True, "records": n}
