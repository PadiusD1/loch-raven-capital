"""SEC EDGAR primary-source filings — free, official, no API key.

On an earnings event the committee should read the ACTUAL filing — the 8-K
earnings-release exhibit and the latest 10-Q/10-K — not just the surprise %
(operator 2026-06-25: "make sure on earnings the files are read too like 8k").
EDGAR needs only a descriptive User-Agent and rate-limits to ~10 req/s; filings
are immutable once filed, so results are cached aggressively. Everything here is
fail-soft: any network/parse failure returns [] and the committee falls back to
the numbers + news, exactly as before.
"""
import html as _html
import json
import re
import urllib.request
from datetime import datetime, timezone

import config
from firm import state

_UA = {"User-Agent": getattr(
    config, "SEC_UA", "LochRaven Capital research contact@example.com")}
_TICKER_CIK: dict[str, str] = {}

# Guidance/outlook is the highest-signal part of an earnings filing — bias the
# excerpt window toward it, else fall back to the document head.
_GUIDANCE_KW = ("outlook", "guidance", "we expect", "we anticipate", "full year",
                "next quarter", "fourth quarter", "first quarter", "fiscal 2")


def _get(url: str, timeout: int = 20) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers=_UA)
        return urllib.request.urlopen(req, timeout=timeout).read()
    except Exception:
        return None


def _strip_html(h: str) -> str:
    h = re.sub(r"(?is)<(script|style|head).*?</\1>", " ", h)
    h = re.sub(r"(?s)<[^>]+>", " ", h)
    return re.sub(r"\s+", " ", _html.unescape(h)).strip()


def _excerpt(text: str, cap: int, form: str | None = None) -> str:
    """Highest-signal slice. For a 10-Q/10-K that's the MD&A (results of
    operations / liquidity), not the boilerplate risk factors; for an 8-K the
    guidance/outlook region; else the document head."""
    low = text.lower()
    if form in ("10-Q", "10-K"):
        for kw in ("management's discussion", "results of operations",
                   "liquidity and capital resources"):
            i = low.find(kw)
            if i >= 0:
                return text[i:i + cap].strip()
    for kw in _GUIDANCE_KW:
        i = low.find(kw)
        if i > 300:
            return text[i - 300:i - 300 + cap].strip()
    return text[:cap].strip()


def cik(ticker: str) -> str | None:
    """Ticker -> zero-padded CIK (cached process-wide from SEC's master map)."""
    if not _TICKER_CIK:
        b = _get("https://www.sec.gov/files/company_tickers.json")
        if b:
            try:
                for row in json.loads(b).values():
                    _TICKER_CIK[str(row["ticker"]).upper()] = str(
                        row["cik_str"]).zfill(10)
            except Exception:
                pass
    return _TICKER_CIK.get(ticker.upper())


def _earnings_exhibit_url(base: str) -> str | None:
    """An 8-K's primary doc is usually a cover page; the earnings RELEASE lives in
    an exhibit (ex-99.1). Read the filing index and pick the press-release htm."""
    b = _get(base + "index.json")
    if not b:
        return None
    try:
        items = json.loads(b).get("directory", {}).get("item", [])
    except Exception:
        return None
    names = [it.get("name", "") for it in items]
    # rank: explicit press-release/ex99 names first, then any non-cover .htm
    for pat in (r"press.?release", r"ex.?99", r"ex99", r"earnings"):
        for nm in names:
            if re.search(pat, nm, re.I) and nm.lower().endswith((".htm", ".html")):
                return base + nm
    return None


def recent_filings(ticker: str, forms: tuple = ("8-K", "10-Q", "10-K"),
                   within_days: int = 8, limit: int = 3,
                   excerpt_chars: int = 2500) -> list[dict]:
    """Recent EDGAR filings of the given forms within the window, each with a
    text excerpt (8-K -> its earnings-release exhibit). [] on any failure."""
    c = cik(ticker)
    if not c:
        return []
    b = _get(f"https://data.sec.gov/submissions/CIK{c}.json")
    if not b:
        return []
    try:
        rec = json.loads(b).get("filings", {}).get("recent", {})
    except Exception:
        return []
    f_form = rec.get("form", [])
    f_date = rec.get("filingDate", [])
    f_acc = rec.get("accessionNumber", [])
    f_doc = rec.get("primaryDocument", [])
    today = datetime.now(timezone.utc).date()
    out: list[dict] = []
    for i, form in enumerate(f_form):
        if form not in forms or len(out) >= limit:
            continue
        try:
            fd = datetime.fromisoformat(f_date[i]).date()
        except (ValueError, TypeError, IndexError):
            continue
        if (today - fd).days > within_days:
            continue
        acc = f_acc[i].replace("-", "")
        base = f"https://www.sec.gov/Archives/edgar/data/{int(c)}/{acc}/"
        doc_url = base + (f_doc[i] if i < len(f_doc) else "")
        # 8-K: the numbers/guidance are in the exhibit, not the cover page
        src_url = (_earnings_exhibit_url(base) if form == "8-K" else None) or doc_url
        raw = _get(src_url)
        excerpt = _excerpt(_strip_html(raw.decode("utf-8", "replace")),
                           excerpt_chars, form=form) if raw else ""
        out.append({"form": form, "date": f_date[i], "url": src_url,
                    "excerpt": excerpt})
    return out


def earnings_filings(ticker: str, within_days: int = 8) -> list[dict]:
    """Cached convenience: the 8-K (earnings release) + latest 10-Q/10-K around a
    print. Cache key is the ticker; refreshed once per UTC day (filings are
    immutable, so a same-day repeat is wasteful)."""
    cache = state.load("earnings_docs_cache", {}) or {}
    today = datetime.now(timezone.utc).date().isoformat()
    ent = cache.get(ticker)
    if isinstance(ent, dict) and ent.get("sec_asof") == today:
        return ent.get("filings") or []
    filings = recent_filings(ticker, within_days=within_days)
    ent = {**(ent or {}), "sec_asof": today, "filings": filings}
    cache[ticker] = ent
    # bound the cache so it can't grow unbounded across the whole universe
    if len(cache) > 120:
        cache = dict(list(cache.items())[-120:])
    state.save("earnings_docs_cache", cache)
    return filings
