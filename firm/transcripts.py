"""Earnings-call transcript — the literal call, fail-soft.

Primary path (operator authorized the RAM cost 2026-06-25): render
stockanalysis.com's transcript page with a headless browser (Scrapling) and
extract the prepared-remarks + Q&A body. The body is JavaScript-rendered, so a
plain HTTP fetch returns nothing — a browser is required. This is bounded by the
caller: only for a name that just reported, fetched ONCE per ticker per day
(cached in data_engine), short-lived, and size-capped. Set
config.TRANSCRIPT_HEADLESS=False to disable if host RAM is tight.

Fallback path: an optional keyed provider (config.TRANSCRIPT_API, e.g.
earningscalls.dev via RapidAPI), tried only if the headless scrape yields nothing.

Everything is fail-soft — a transcript fetch must never break a cycle; on any
error it returns {} and the committee relies on the SEC 8-K + news.
"""
import json
import re
import urllib.request

import config

_BR = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}


def _latest_slug(ticker: str) -> str | None:
    """The newest transcript slug for a ticker, from the (static) list page."""
    try:
        url = f"https://stockanalysis.com/stocks/{ticker.lower()}/transcripts/"
        page = urllib.request.urlopen(
            urllib.request.Request(url, headers=_BR), timeout=15
        ).read().decode("utf-8", "replace")
    except Exception:
        return None
    slugs = re.findall(
        rf"/stocks/{ticker.lower()}/transcripts/(\d+-q[1-4]-20\d\d[a-z\-]*)/",
        page, re.I)
    if not slugs:
        return None

    def _key(s: str) -> tuple:
        m = re.search(r"q([1-4])-(20\d\d)", s, re.I)
        return (int(m.group(2)), int(m.group(1))) if m else (0, 0)

    return sorted(set(slugs), key=_key, reverse=True)[0]


def _stockanalysis(ticker: str) -> dict:
    """Headless-render the latest stockanalysis.com transcript -> excerpt, or {}."""
    if not getattr(config, "TRANSCRIPT_HEADLESS", True):
        return {}
    # RAM gate (operator 2026-06-29): a headless Chromium render needs ~0.4-0.7 GB.
    # The fund's research floor already runs many concurrent Opus subprocesses;
    # launching the browser when memory is tight starves the cycle (it stalled at
    # 0/N) AND the render fails anyway. Honor "risk the RAM for the transcript" ONLY
    # when there is RAM to risk; otherwise skip — the SEC 8-K + 10-Q carry the
    # management commentary with no browser.
    try:
        import psutil
        if psutil.virtual_memory().available / 1e9 < float(
                getattr(config, "TRANSCRIPT_MIN_RAM_GB", 2.5)):
            return {}
    except Exception:
        pass
    slug = _latest_slug(ticker)
    if not slug:
        return {}
    url = f"https://stockanalysis.com/stocks/{ticker.lower()}/transcripts/{slug}/"
    try:
        from scrapling.fetchers import DynamicFetcher
        page = DynamicFetcher.fetch(url, headless=True, network_idle=True,
                                    timeout=90000)
        txt = page.get_all_text() if hasattr(page, "get_all_text") else (
            getattr(page, "html_content", "") or "")
    except Exception:
        return {}
    i = txt.lower().find("prepared remarks")
    body = re.sub(r"\s+", " ", (txt[i:] if i >= 0 else txt)).strip()
    if len(body) < 500:                      # render failed / paywall / empty
        return {}
    cap = int(getattr(config, "TRANSCRIPT_EXCERPT_CHARS", 6000))
    return {"source": "stockanalysis.com", "url": url, "slug": slug,
            "excerpt": body[:cap]}


def _keyed(ticker: str) -> dict:
    """Optional keyed provider fallback (config.TRANSCRIPT_API)."""
    key = (getattr(config, "TRANSCRIPT_API", "") or "").strip()
    if not key:
        return {}
    host = getattr(config, "TRANSCRIPT_API_HOST", "earnings-calls-api.p.rapidapi.com")
    try:
        req = urllib.request.Request(
            f"https://{host}/transcript?ticker={ticker}",
            headers={"X-RapidAPI-Key": key, "X-RapidAPI-Host": host})
        d = json.loads(urllib.request.urlopen(req, timeout=20).read())
    except Exception:
        return {}
    if not isinstance(d, dict):
        return {}
    text = d.get("transcript") or d.get("content") or d.get("text") or ""
    if isinstance(text, list):
        text = " ".join(str(x) for x in text)
    text = str(text).strip()
    if not text:
        return {}
    cap = int(getattr(config, "TRANSCRIPT_EXCERPT_CHARS", 6000))
    return {"source": host, "quarter": d.get("quarter") or d.get("period"),
            "excerpt": text[:cap]}


def latest(ticker: str, within_days: int = 8) -> dict:
    """Latest call-transcript excerpt for a name that just reported, or {}.

    Headless stockanalysis.com scrape first (free), keyed provider as fallback.
    Caller gates on a fresh print and caches the result, so the browser launches
    at most once per ticker per day."""
    return _stockanalysis(ticker) or _keyed(ticker)
