#!/usr/bin/env python3
"""Build the Institutional Memory archive from the Loch Raven investment committee log.

Reads the committee research history (read only), keeps one record per unique
committee decision, scores every decision against real adjusted closes from
yfinance, and writes into --out:

  memos.jsonl          one committee memo per line, with its measured outcome
  prices.csv           adjusted closes for every memo ticker plus SPY (offline use)
  lessons.md           the firm's distilled lessons, verbatim, with their evidence
  dataset_stats.json   counts, hit rates and conviction buckets

Usage:
  py -3.11 scripts/build_memory_dataset.py
  py -3.11 scripts/build_memory_dataset.py --source PATH --lessons PATH --out data/memory
  py -3.11 scripts/build_memory_dataset.py --offline   # reuse <out>/prices.csv, no network
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from bisect import bisect_left, bisect_right
from collections import Counter
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = REPO_ROOT / "state"

ET = ZoneInfo("America/New_York")
MARKET_CLOSE = dtime(16, 0)
SESSION_FINAL = dtime(16, 30)  # a same-day bar before this is a live quote, not a close
PRICE_START = "2026-05-26"
HORIZONS = (5, 20)
CHUNK_SIZE = 40
BENCHMARK = "SPY"
SOURCE_TAG = "loch-raven-committee"
DIRECTION = {"STRONG_BUY": 1, "BUY": 1, "HOLD": 0, "SELL": -1, "STRONG_SELL": -1}
TEXT_FIELDS = ("thesis", "debate_summary", "bear_risk", "watch_trigger", "what_changed")
BUCKETS = (("lt_60", None, 59), ("60_69", 60, 69), ("70_79", 70, 79), ("80_plus", 80, None))

# Redaction rules applied to every string that leaves this script.
REDACTIONS = (
    ("email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[redacted-email]"),
    ("path", re.compile(r"[A-Za-z]:\\\S+|\\\\\S+"), "[redacted-path]"),
    ("path", re.compile(r"(?<![\w.])(?:~|/(?:Users|home|srv|mnt|tmp|var|opt|etc|root))/\S+"), "[redacted-path]"),
    ("api_key", re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}"
                           r"|xox[abpr]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{30,})"), "[redacted-key]"),
    ("api_key", re.compile(r"\b(?=[A-Za-z0-9_]*\d)(?=[A-Za-z0-9_]*[A-Za-z])[A-Za-z0-9_]{32,}\b"), "[redacted-key]"),
    ("account_number", re.compile(r"(?<![\d.,$])\d{8,}(?![\d.,])"), "[redacted-number]"),
)


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build the Institutional Memory archive.")
    p.add_argument("--source", type=Path, default=STATE_DIR / "research.json",
                   help="committee research.json (read only)")
    p.add_argument("--lessons", type=Path, default=STATE_DIR / "lessons.json",
                   help="firm lessons.json (read only)")
    p.add_argument("--universe", type=Path, default=STATE_DIR / "universe_symbols.json",
                   help="optional universe_symbols.json (ETF list, sector map if present)")
    p.add_argument("--tradable", type=Path, default=STATE_DIR / "tradable_universe.json",
                   help="optional tradable_universe.json (sector map if present)")
    p.add_argument("--out", type=Path, default=REPO_ROOT / "data" / "memory", help="output data directory")
    p.add_argument("--start", default=PRICE_START, help="first price date (YYYY-MM-DD)")
    p.add_argument("--offline", action="store_true",
                   help="read closes from <out>/prices.csv instead of downloading")
    return p.parse_args(argv)


# ---------------------------------------------------------------- loading

def load_json(path: Path, required: bool = True):
    path = Path(path).resolve()
    if not path.is_file():
        if required:
            raise SystemExit(f"missing input file: {path}")
        log(f"optional input not found, skipping: {path.name}")
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def clean_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value).strip()


def to_int(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if value == value and abs(value) != float("inf") else None


def norm_rating(value) -> str:
    return re.sub(r"[\s-]+", "_", clean_text(value).upper())


def parse_ts(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def extract_decisions(research: dict):
    """Earliest snapshot wins for each (ticker, rating, conviction, thesis[:300])."""
    snaps = sorted(research.get("history") or [], key=lambda s: s.get("asof") or "")
    seen, counts = {}, Counter()
    for snap in snaps:
        ts = snap.get("asof")
        for ticker, v in (snap.get("verdicts") or {}).items():
            counts["source_entries"] += 1
            if not ts or not isinstance(v, dict):
                counts["excluded_malformed"] += 1
                continue
            thesis = clean_text(v.get("thesis"))
            if not thesis:
                counts["excluded_empty_thesis"] += 1
                continue
            if v.get("degraded") is True or thesis.startswith("Degraded mode"):
                counts["excluded_degraded"] += 1
                continue
            rating, conviction = norm_rating(v.get("rating")), to_int(v.get("conviction"))
            key = (ticker.strip().upper(), rating, conviction, thesis[:300])
            if key in seen:
                counts["duplicates_collapsed"] += 1
                continue
            seen[key] = (key[0], ts, v, rating, conviction, thesis)
    return list(seen.values()), counts


def find_sector_map(*docs) -> dict:
    """Use a ticker->sector map if a universe file carries one; none do today."""
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        for key, val in doc.items():
            if "sector" in key.lower() and isinstance(val, dict):
                return {str(k).upper(): v.strip() for k, v in val.items() if isinstance(v, str) and v.strip()}
    return {}


def build_memo(decision, latest: dict, etfs: set, sectors: dict) -> dict:
    ticker, ts, v, rating, conviction, thesis = decision
    rec = latest.get(ticker)
    kind = rec.get("kind") if isinstance(rec, dict) else None
    if kind not in ("stock", "etf"):
        kind = "etf" if ticker in etfs else "stock"
    memo = {
        "id": None,
        "ticker": ticker,
        "kind": kind,
        "sector": sectors.get(ticker),
        "asof": parse_ts(ts).astimezone(ET).date().isoformat(),
        "asof_ts": ts,
        "rating": rating,
        "conviction": conviction,
        "horizon": clean_text(v.get("horizon")) or None,
    }
    memo.update({f: clean_text(v.get(f)) for f in TEXT_FIELDS})
    memo["thesis"] = thesis
    memo.update({"source": SOURCE_TAG, "outcome": None})
    return memo


def assign_ids(memos: list) -> list:
    ordered = sorted(memos, key=lambda m: (m["asof"], m["ticker"], m["asof_ts"]))
    per_day = Counter()
    out = []
    for m in ordered:
        per_day[(m["asof"], m["ticker"])] += 1
        n = per_day[(m["asof"], m["ticker"])]
        base = f"LR-{m['asof']}-{m['ticker']}"
        out.append({**m, "id": base if n == 1 else f"{base}-{n}"})
    return out


# ---------------------------------------------------------------- prices

def _reset_yf_session() -> None:
    """Drop yfinance's cached session so a retry gets a fresh cookie and crumb."""
    try:
        import yfinance.data as yfd
        if hasattr(yfd, "SingletonMeta") and hasattr(yfd, "YfData"):
            yfd.SingletonMeta._instances.pop(yfd.YfData, None)
    except Exception as exc:  # internals move between versions; a retry still happens
        log(f"yfinance session reset skipped: {exc!r}")


def _download_batch(batch: list, start: str, end: str) -> dict:
    import pandas as pd
    import yfinance as yf
    ysyms = [t.replace(".", "-").replace("/", "-") for t in batch]
    try:
        df = yf.download(ysyms, start=start, end=end, auto_adjust=True,
                         progress=False, threads=True, group_by="column")
    except Exception as exc:
        log(f"  batch of {len(batch)} failed: {exc!r}")
        return {}
    if df is None or df.empty:
        return {}
    if isinstance(df.columns, pd.MultiIndex):
        close = df["Close"]
    else:
        close = df[["Close"]].rename(columns={"Close": ysyms[0]})
    got = {}
    for ticker, ysym in zip(batch, ysyms):
        if ysym not in close.columns:
            continue
        series = close[ysym].dropna()
        if not series.empty:
            got[ticker] = {ix.strftime("%Y-%m-%d"): round(float(c), 4) for ix, c in series.items()}
    return got


def download_prices(tickers: list, start: str, end: str):
    prices, failed = {}, []
    for i in range(0, len(tickers), CHUNK_SIZE):
        batch = tickers[i:i + CHUNK_SIZE]
        got = _download_batch(batch, start, end)
        prices.update(got)
        failed += [t for t in batch if t not in got]
        log(f"  prices chunk {i // CHUNK_SIZE + 1}: {len(got)}/{len(batch)}")
        time.sleep(1.0)
    if failed:
        log(f"  retrying {len(failed)} failed tickers once: {' '.join(failed)}")
        _reset_yf_session()
        time.sleep(3.0)
        for i in range(0, len(failed), CHUNK_SIZE):
            prices.update(_download_batch(failed[i:i + CHUNK_SIZE], start, end))
        failed = [t for t in failed if t not in prices]
    return prices, failed


def read_prices_csv(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit(f"--offline needs an existing {path}")
    prices = {}
    with open(path, encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            prices.setdefault(row["ticker"], {})[row["date"]] = round(float(row["close"]), 4)
    return prices


def drop_unfinished_session(prices: dict, now_et: datetime) -> dict:
    today = now_et.date().isoformat()
    if now_et.time() >= SESSION_FINAL:
        return prices
    return {t: {d: c for d, c in s.items() if d != today} for t, s in prices.items()}


# ---------------------------------------------------------------- outcomes

def entry_index(asof_ts: str, tdays: list):
    """Decision-day close if stamped before 16:00 ET on a trading day, else next session."""
    et = parse_ts(asof_ts).astimezone(ET)
    day = et.date().isoformat()
    i = bisect_left(tdays, day)
    if i < len(tdays) and tdays[i] == day and et.time() < MARKET_CLOSE:
        return i
    j = bisect_right(tdays, day)
    return j if j < len(tdays) else None


def pct(a: float, b: float) -> float:
    return round((b / a - 1.0) * 100.0, 2) + 0.0


def compute_outcome(memo: dict, prices: dict, tdays: list) -> dict:
    direction = DIRECTION.get(memo["rating"], 0)
    out = {"status": "pending", "entry_date": None, "entry_close": None, "direction": direction}
    for h in HORIZONS:
        out.update({f"ret_{h}d": None, f"spy_{h}d": None, f"excess_{h}d": None, f"known_at_{h}d": None})
    out.update({"score_20d": None, "label": "pending"})
    closes = prices.get(memo["ticker"])
    if not closes:
        return {**out, "status": "no_price"}
    ei = entry_index(memo["asof_ts"], tdays)
    if ei is None:
        return out
    entry_date = tdays[ei]
    entry_close = closes.get(entry_date)
    if entry_close is None:
        return {**out, "status": "no_price", "entry_date": entry_date}
    out.update({"entry_date": entry_date, "entry_close": entry_close})
    spy = prices[BENCHMARK]
    for h in HORIZONS:
        xi = ei + h
        if xi >= len(tdays) or tdays[xi] not in closes:
            continue
        exit_date = tdays[xi]
        ret, spy_ret = pct(entry_close, closes[exit_date]), pct(spy[entry_date], spy[exit_date])
        out.update({f"ret_{h}d": ret, f"spy_{h}d": spy_ret,
                    f"excess_{h}d": round(ret - spy_ret, 2) + 0.0, f"known_at_{h}d": exit_date})
    excess = out["excess_20d"]
    if excess is None:
        out["status"] = "pending" if ei + 20 >= len(tdays) else "no_price"
        return out
    score = None if direction == 0 else round(direction * excess, 2) + 0.0
    label = "neutral" if direction == 0 else ("right" if score > 0 else "wrong")
    return {**out, "status": "scored", "score_20d": score, "label": label}


# ---------------------------------------------------------------- sanitize

def redact(text: str, findings: Counter, examples: list, where: str) -> str:
    for kind, rx, repl in REDACTIONS:
        for m in rx.finditer(text):
            findings[kind] += 1
            if len(examples) < 20:
                examples.append(f"{where}: {kind} {m.group(0)[:40]!r}")
        text = rx.sub(repl, text)
    return text


def sanitize_memos(memos: list):
    findings, examples, clean = Counter(), [], []
    for m in memos:
        fields = {f: redact(m[f], findings, examples, f"{m['id']}.{f}") for f in TEXT_FIELDS}
        if m["horizon"]:
            fields["horizon"] = redact(m["horizon"], findings, examples, f"{m['id']}.horizon")
        clean.append({**m, **fields})
    return clean, findings, examples


# ---------------------------------------------------------------- outputs

def write_atomic(path: Path, writer) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        writer(fh)
    os.replace(tmp, path)


LINE_BREAKERS = str.maketrans({" ": "\\u2028", " ": "\\u2029", "\x85": "\\u0085"})


def write_memos(path: Path, memos: list) -> None:
    def w(fh):
        for m in memos:  # escape Unicode line breaks so every memo stays on one physical line
            fh.write(json.dumps(m, ensure_ascii=False, separators=(",", ":")).translate(LINE_BREAKERS) + "\n")
    write_atomic(path, w)


def write_prices(path: Path, prices: dict, tickers: list, tdays: list) -> int:
    rows = [(d, t, prices[t][d]) for d in tdays for t in tickers if t in prices and d in prices[t]]

    def w(fh):
        cw = csv.writer(fh, lineterminator="\n")
        cw.writerow(["date", "ticker", "close"])
        cw.writerows((d, t, f"{c:.4f}") for d, t, c in rows)
    write_atomic(path, w)
    return len(rows)


def render_lessons(doc: dict) -> str:
    findings, examples = Counter(), []
    lessons = doc.get("lessons") or []
    distilled = clean_text(doc.get("asof"))[:10]
    lines = ["# Firm Memory, Distilled Lessons", ""]
    lines += [f"The committee distilled these {len(lessons)} lessons from its own graded record on {distilled}.",
              "Each lesson and its evidence are reproduced verbatim from the firm's lessons file.", ""]
    for i, item in enumerate(lessons, 1):
        text = redact(clean_text(item.get("lesson")), findings, examples, f"lesson {i}")
        evidence = redact(clean_text(item.get("evidence")), findings, examples, f"lesson {i} evidence")
        lines += [f"{i}. {text}", "", f"   Evidence: {evidence}", "",
                  f"   Distilled: {clean_text(item.get('added'))[:10]}", ""]
    meta = [("Distilled at", clean_text(doc.get("asof"))),
            ("Verdicts reviewed as of", clean_text(doc.get("verdict_reviewed_asof"))),
            ("Reviewed through", clean_text(doc.get("reviewed_through")))]
    lines += ["## Distillation record", ""] + [f"- {k}: {v}" for k, v in meta if v]
    for key in ("cap_tuning_applied", "stop_tuning_applied", "bench_tuning_applied"):
        if doc.get(key):
            lines.append(f"- {key}: {json.dumps(doc[key], separators=(', ', ': '))}")
    if findings:
        log(f"lessons redactions: {dict(findings)} {examples}")
    return "\n".join(lines).rstrip() + "\n"


def summarize(rows: list) -> dict:
    n = len(rows)
    if not n:
        return {"n": 0, "hit_rate": None, "mean_score_20d": None}
    scores = [m["outcome"]["score_20d"] for m in rows]
    return {"n": n, "hit_rate": round(sum(s > 0 for s in scores) / n, 4),
            "mean_score_20d": round(sum(scores) / n, 2)}


def build_stats(memos: list, counts: Counter, tdays: list, failed: list) -> dict:
    status = Counter(m["outcome"]["status"] for m in memos)
    directional = [m for m in memos if m["outcome"]["direction"] != 0 and m["outcome"]["score_20d"] is not None]
    by_bucket = {}
    for name, lo, hi in BUCKETS:
        rows = [m for m in directional if m["conviction"] is not None
                and (lo is None or m["conviction"] >= lo) and (hi is None or m["conviction"] <= hi)]
        by_bucket[name] = summarize(rows)
    return {
        "n_memos": len(memos),
        "n_tickers": len({m["ticker"] for m in memos}),
        "date_range": {"start": memos[0]["asof"], "end": memos[-1]["asof"]} if memos else None,
        "counts_by_rating": dict(sorted(Counter(m["rating"] for m in memos).items())),
        "counts_by_label": dict(sorted(Counter(m["outcome"]["label"] for m in memos).items())),
        "n_scored_20d": sum(m["outcome"]["excess_20d"] is not None for m in memos),
        "n_pending": status["pending"],
        "n_no_price": status["no_price"],
        "directional_20d": {"overall": summarize(directional), "by_conviction": by_bucket},
        "prices_through": tdays[-1] if tdays else None,
        "build": {**dict(sorted(counts.items())), "tickers_without_prices": sorted(failed)},
    }


# ---------------------------------------------------------------- main

def main(argv=None) -> int:
    args = parse_args(argv)
    started = time.perf_counter()
    out_dir = args.out.resolve()
    research = load_json(args.source)
    lessons_doc = load_json(args.lessons)
    universe = load_json(args.universe, required=False)
    tradable = load_json(args.tradable, required=False)

    decisions, counts = extract_decisions(research)
    etfs = set(universe.get("etfs") or [])
    sectors = find_sector_map(universe, tradable)
    latest = research.get("latest") or {}
    memos = assign_ids([build_memo(d, latest, etfs, sectors) for d in decisions])
    unknown = sorted({m["rating"] for m in memos} - set(DIRECTION))
    if unknown:
        log(f"warning: unknown ratings scored as direction 0: {unknown}")
    log(f"decisions: {len(memos)} kept from {counts['source_entries']} entries {dict(counts)}")

    tickers = sorted({m["ticker"] for m in memos} | {BENCHMARK})
    now_et = datetime.now(ET)
    if args.offline:
        prices = read_prices_csv(out_dir / "prices.csv")
    else:
        end = (now_et.date() + timedelta(days=1)).isoformat()
        log(f"downloading {len(tickers)} tickers {args.start}..{end} in chunks of {CHUNK_SIZE}")
        prices, _ = download_prices(tickers, args.start, end)
    prices = drop_unfinished_session(prices, now_et)
    prices = {t: {d: c for d, c in s.items() if d >= args.start} for t, s in prices.items() if t in tickers}
    if not prices.get(BENCHMARK):
        raise SystemExit(f"no {BENCHMARK} prices; cannot build a trading calendar")
    tdays = sorted(prices[BENCHMARK])
    failed = [t for t in tickers if not prices.get(t)]

    memos = [{**m, "outcome": compute_outcome(m, prices, tdays)} for m in memos]
    memos, findings, examples = sanitize_memos(memos)
    log(f"sanitize: {sum(findings.values())} redactions {dict(findings)}" + (f" {examples}" if examples else ""))

    write_memos(out_dir / "memos.jsonl", memos)
    n_rows = write_prices(out_dir / "prices.csv", prices, tickers, tdays)
    write_atomic(out_dir / "lessons.md", lambda fh: fh.write(render_lessons(lessons_doc)))
    stats = build_stats(memos, counts, tdays, failed)
    write_atomic(out_dir / "dataset_stats.json", lambda fh: fh.write(json.dumps(stats, indent=2) + "\n"))

    log(f"wrote {len(memos)} memos, {n_rows} price rows ({len(tdays)} sessions, "
        f"{tdays[0]}..{tdays[-1]}), no prices for {failed or 'none'}")
    log(f"done in {time.perf_counter() - started:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
