"""Macro event calendar — the committee must SEE scheduled macro shocks coming,
and benched names must reopen the day their awaited number prints.

ARCHITECTURE (2026-06-14 re-architecture; operator: "fed numbers aren't only
FOMC; a hardcoded list can't be there, there has to be a better way"):

  Releases live in a REGISTRY (name, impact, FRED id, recurrence rule, the
  keywords a thesis might use). Dates are RESOLVED through tiers, never a
  hand-maintained per-year list:

    Tier 1  FRED API (official, exact, forward-looking)   — if FRED_API_KEY set
    Tier 2  Forex Factory weekly JSON (free, no key)      — keyless exact, near-term
    Tier 3  cached last-good snapshot                      — survives a feed outage
    Tier 4  recurrence rules + verified 2026 seed         — always-on offline floor

  refresh() (called once per cycle) pulls Tier 1/2 and caches the snapshot;
  every read (event_dates/landed_since/imminent_within/future_events/upcoming)
  merges that snapshot OVER the deterministic floor, so the fund NEVER goes
  blind — worst case it runs on rule-approximate dates and health reports the
  live tier. events_mentioned() lets a bench thesis that NAMES a release
  auto-track that release's next date (no manual calendar entry).

Public contract preserved: FOMC (list attr), _safe_date, event_dates,
landed_since, imminent_within, future_events, upcoming.
"""
import json
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone


# ── Registry ──────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Release:
    key: str
    name: str
    impact: str                 # "high" | "medium"
    fred_id: int | None         # FRED release_id (None for FOMC — Fed calendar)
    rule: str                   # recurrence rule tag (see _rule_dates)
    keywords: tuple             # lowercased phrases a thesis might use
    seed_2026: tuple            # verified exact 2026 dates (offline floor anchor)


# Verified 2026 schedules (OMB Principal Federal Economic Indicators + agency
# calendars, cross-checked 2026-06-14). The SEED is a floor anchor, not the
# source of truth — refresh() pulls live dates over it.
RELEASES: tuple = (
    Release("fomc", "FOMC rate decision", "high", None, "anchor",
            ("fomc", "fed ", "federal reserve", "rate decision", "interest rate",
             "rate cut", "rate hike", "dot plot", "powell", "the fed"),
            ("2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29",
             "2026-09-16", "2026-10-28", "2026-12-09")),
    Release("cpi", "CPI (inflation)", "high", 10, "monthly",
            ("cpi", "consumer price", "inflation print", "inflation report", "inflation data"),
            ("2026-01-13", "2026-02-11", "2026-03-11", "2026-04-10", "2026-05-12",
             "2026-06-10", "2026-07-14", "2026-08-12", "2026-09-11", "2026-10-14",
             "2026-11-10", "2026-12-10")),
    Release("pce", "PCE / Personal Income & Outlays", "high", 54, "monthly",
            ("pce", "personal income", "personal spending", "personal outlays",
             "fed's preferred", "core pce"),
            ("2026-01-29", "2026-02-26", "2026-03-27", "2026-04-30", "2026-05-28",
             "2026-06-25", "2026-07-30", "2026-08-26", "2026-09-30", "2026-10-29",
             "2026-11-25", "2026-12-23")),
    Release("nfp", "Nonfarm Payrolls (jobs report)", "high", 50, "first_friday",
            ("nfp", "nonfarm", "non-farm", "payroll", "jobs report", "jobs number",
             "employment situation", "unemployment rate"),
            ("2026-01-09", "2026-02-06", "2026-03-06", "2026-04-03", "2026-05-08",
             "2026-06-05", "2026-07-02", "2026-08-07", "2026-09-04", "2026-10-02",
             "2026-11-06", "2026-12-04")),
    Release("ppi", "PPI (producer prices)", "medium", 46, "monthly",
            ("ppi", "producer price", "wholesale price"),
            ("2026-01-14", "2026-02-12", "2026-03-12", "2026-04-14", "2026-05-13",
             "2026-06-11", "2026-07-15", "2026-08-13", "2026-09-10", "2026-10-15",
             "2026-11-13", "2026-12-15")),
    Release("retail", "Advance Retail Sales", "high", 9, "monthly",
            ("retail sales", "consumer spending", "retail report"),
            ("2026-01-14", "2026-02-10", "2026-03-06", "2026-04-01", "2026-04-21",
             "2026-05-14", "2026-06-17", "2026-07-16", "2026-08-14", "2026-09-16",
             "2026-10-15", "2026-11-17", "2026-12-16")),
    Release("gdp", "GDP (advance estimate)", "high", 53, "quarterly",
            ("gdp", "gross domestic", "growth estimate", "economic growth"),
            ("2026-01-29", "2026-04-30", "2026-07-30", "2026-10-29")),
    Release("ism_mfg", "ISM Manufacturing PMI", "high", None, "first_business_day",
            ("ism manufacturing", "ism mfg", "manufacturing pmi", "factory activity"),
            ("2026-01-02", "2026-02-02", "2026-03-02", "2026-04-01", "2026-05-01",
             "2026-06-01", "2026-07-01", "2026-08-03", "2026-09-01", "2026-10-01",
             "2026-11-02", "2026-12-01")),
    Release("ism_svc", "ISM Services PMI", "high", None, "third_business_day",
            ("ism services", "services pmi", "non-manufacturing", "ism non-manufacturing"),
            ("2026-01-06", "2026-02-04", "2026-03-04", "2026-04-03", "2026-05-05",
             "2026-06-03", "2026-07-07", "2026-08-05", "2026-09-03", "2026-10-05",
             "2026-11-04", "2026-12-03")),
    Release("claims", "Initial Jobless Claims", "medium", 180, "weekly_thursday",
            ("jobless claims", "unemployment claims", "initial claims", "weekly claims"),
            ()),
    Release("jolts", "JOLTS (job openings)", "medium", 192, "monthly_early",
            ("jolts", "job openings", "labor turnover"),
            ("2026-06-02", "2026-06-30")),
    # FOMC MINUTES (IG 2026-07-01: market-moving sibling release missing from
    # the registry) — published exactly 3 weeks after each decision, 2pm ET;
    # regularly repricess the rate path the decision left ambiguous. Fully
    # rule-generated from the FOMC anchor list (one source, covers 2027 too).
    Release("fomc_minutes", "FOMC meeting minutes", "medium", None,
            "fomc_plus_21",
            ("fomc minutes", "fed minutes", "meeting minutes",
             "minutes of the federal"),
            ()),
    # Market-structure event (not a data release, no FRED id): standard monthly
    # options expiration, 3rd Friday. Matters most to the calls_only sleeve
    # (theta/pin/assignment) and to index vol on the Mar/Jun/Sep/Dec
    # quad-witching dates. Fully rule-generated (no seed needed).
    Release("opex", "Options expiration (3rd Fri; quad-witching Mar/Jun/Sep/Dec)",
            "medium", None, "opex",
            ("opex", "options expiration", "quad witching", "quad-witching",
             "triple witching", "expiration", "monthly expiry", "gamma"),
            ()),
    # Sibling releases (IG 2026-07-16 item 10: taxonomy missed plausible
    # siblings that move the same book — the CPI-class miss recurring). All
    # MEDIUM impact deliberately: they inform the packet calendar without
    # joining the high-impact macro-reopen trigger set (a sentiment print must
    # not re-judge the whole bench). No fabricated seed dates — approx rule
    # days only, refined live by the FRED/Forex-Factory tiers.
    Release("umich", "UMich Consumer Sentiment (prelim)", "medium", None,
            "monthly",
            ("consumer sentiment", "umich", "michigan sentiment",
             "sentiment survey", "inflation expectations"),
            ()),
    Release("conf_board", "Conference Board Consumer Confidence", "medium",
            None, "monthly",
            ("consumer confidence", "conference board"),
            ()),
    Release("housing_starts", "Housing Starts & Building Permits", "medium",
            27, "monthly",
            ("housing starts", "building permits", "residential construction",
             "new residential"),
            ()),
    Release("existing_home", "Existing Home Sales", "medium", 291, "monthly",
            ("existing home sales", "home sales", "nar report"),
            ()),
    Release("durables", "Durable Goods Orders", "medium", None, "monthly",
            ("durable goods", "core capital goods", "capex orders",
             "factory orders"),
            ()),
    # IG 2026-07-21 item 10 siblings (same class as the 07-16 batch — high-
    # impact releases the taxonomy missed; same deliberate MEDIUM so a print
    # informs the packet calendar without re-judging the whole bench; no
    # fabricated seeds — rule days only, refined live by the FRED/FF tiers):
    Release("adp", "ADP National Employment Report", "medium", 194,
            "adp_wednesday",
            ("adp", "private payrolls", "adp employment", "adp jobs"),
            ()),
    Release("new_home_sales", "New Home Sales", "medium", 97, "monthly",
            ("new home sales", "new-home sales", "census home sales"),
            ()),
    Release("industrial_production", "Industrial Production & Capacity "
            "Utilization", "medium", 13, "monthly",
            ("industrial production", "capacity utilization", "factory output",
             "g.17"),
            ()),
    Release("trade_balance", "International Trade Balance", "medium", 51,
            "monthly",
            ("trade balance", "trade deficit", "international trade",
             "imports and exports"),
            ()),
    # Beige Book: published ~2 weeks (2 Wednesdays) before each FOMC decision;
    # fully rule-generated from the ONE FOMC anchor list, like the minutes.
    Release("beige_book", "Fed Beige Book", "medium", None, "fomc_minus_14",
            ("beige book", "beigebook", "fed districts", "anecdotal economic"),
            ()),
    # Treasury supply: the quarterly refunding ANNOUNCEMENT (auction sizes /
    # issuance mix, ~first Wednesday of Feb/May/Aug/Nov) — repriced the long
    # end hard in Aug-2023/Nov-2023; rate-sensitive equity books move with it.
    Release("tsy_refunding", "Treasury Quarterly Refunding announcement",
            "medium", None, "quarterly_refunding",
            ("refunding", "treasury auction", "treasury supply", "issuance",
             "auction sizes", "quarterly refunding"),
            ()),
)
_BY_KEY = {r.key: r for r in RELEASES}
# WHY a tracked release carries no fred_id (operator 2026-08-21). Checked live
# against /fred/releases (331 releases): these are genuinely not FRED data
# releases, so a null id here is a FACT about the provider, not an unfinished
# lookup. Ten OTHER releases DID resolve and were wired in the same pass, taking
# provider-grounded coverage from 4/24 to 14/24 — every one confirmed to publish
# forward dates before adoption, because fuzzy name matching had proposed
# "Dallas Fed Energy Survey" for FOMC and would have silently poisoned the
# calendar. The taxonomy check reports FRED-confirmed vs rule-derived using this
# map instead of claiming "24/24 have a forward date" when 20 of those dates
# came from a recurrence rule and no provider had confirmed any of them.
FRED_UNAVAILABLE = {
    "fomc": "Fed calendar, not a FRED data release",
    "fomc_minutes": "Fed calendar, not a FRED data release",
    "beige_book": "Fed publication, not a FRED data release",
    "ism_mfg": "ISM is a private survey — not redistributed by FRED",
    "ism_svc": "ISM is a private survey — not redistributed by FRED",
    "umich": "University of Michigan survey — schedule not on FRED",
    "conf_board": "Conference Board is a private survey",
    "opex": "market structure (3rd-Friday expiry), not a data release",
    "tsy_refunding": "Treasury announcement, not a FRED release",
    "durables": "Census advance durable-goods report is not exposed as a "
                "distinct FRED release with forward dates",
}


def grounding() -> dict:
    """How much of the registry is PROVIDER-confirmed vs derived from a rule.

    The 'macro taxonomy' check used to say "24 release classes tracked; internal
    sibling list covered" and the calendar line said "24/24 tracked release
    classes have a forward date". Both were true and both overstated: the
    taxonomy was diffed against a hard-coded sibling list in this same module
    (so a class absent from BOTH is invisible), and 20 of the 24 forward dates
    were generated by _rule_dates rather than confirmed by any provider. Report
    the split so the reader can tell a confirmed date from an inferred one."""
    tracked = {r.key for r in RELEASES}
    grounded = sorted(r.key for r in RELEASES if r.fred_id)
    ungrounded = sorted(r.key for r in RELEASES if not r.fred_id)
    return {
        "tracked": len(tracked),
        "fred_confirmed": grounded,
        "rule_derived": ungrounded,
        # A null id with no declared reason is an UNFINISHED LOOKUP, and that is
        # the one case here that should fail a check: it means nobody has
        # established whether the provider carries this release at all.
        "undeclared_nulls": sorted(k for k in ungrounded
                                   if k not in FRED_UNAVAILABLE),
        "unavailable_reasons": {k: FRED_UNAVAILABLE[k] for k in ungrounded
                                if k in FRED_UNAVAILABLE},
    }


# Registry releases that are market structure (no FRED/FF feed carries them) and
# so must be merged into the BROAD calendar from the deterministic rule directly.
_MARKET_STRUCTURE = ("opex",)

# ── Taxonomy self-diff (fix brief 2026-07-21 #10) ─────────────────────────────
# The KNOWN market-moving release universe the registry must cover. A key whose
# value is None must exist in RELEASES; a key with a reason string is a
# DOCUMENTED exception (no deterministic schedule exists — named, not silent).
# Extending the universe without adding the Release fails the health check —
# the "missing sibling" class (FOMC-only catalysts, then CPI/PCE/jobs, then
# ADP/new-home/IP/trade/beige-book) becomes a deterministic red on the day the
# sibling is named, instead of an Opus rediscovery every audit.
HIGH_IMPACT_UNIVERSE: dict = {
    "fomc": None, "fomc_minutes": None, "cpi": None, "pce": None, "ppi": None,
    "nfp": None, "adp": None, "jolts": None, "claims": None, "retail": None,
    "gdp": None, "ism_mfg": None, "ism_svc": None, "umich": None,
    "conf_board": None, "housing_starts": None, "existing_home": None,
    "new_home_sales": None, "durables": None, "industrial_production": None,
    "trade_balance": None, "beige_book": None, "tsy_refunding": None,
    "opex": None,
    "fed_testimony": ("semiannual Monetary Policy Report testimony has no "
                      "deterministic schedule (announced ad hoc, ~Feb/Jul); "
                      "covered by the macro news brief's Powell headlines — "
                      "documented exception, not a silent gap"),
}


def taxonomy_gaps() -> dict:
    """Diff the tracked registry against HIGH_IMPACT_UNIVERSE. Consumed by the
    health desk ('macro taxonomy' check) and the IG snapshot. Pure/offline."""
    tracked = {r.key for r in RELEASES}
    return {
        "untracked": sorted(k for k, why in HIGH_IMPACT_UNIVERSE.items()
                            if k not in tracked and why is None),
        "documented_exceptions": {k: why for k, why in
                                  HIGH_IMPACT_UNIVERSE.items()
                                  if k not in tracked and why},
        "tracked_beyond_universe": sorted(
            k for k in tracked if k not in HIGH_IMPACT_UNIVERSE),
    }

# FOMC kept as a module attribute — data_engine.py reads it directly. Derived
# from the registry (+ the published 2027 second-days) so there's ONE source.
FOMC = list(_BY_KEY["fomc"].seed_2026) + [
    "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-16", "2027-07-28",
    "2027-09-22", "2027-11-03", "2027-12-15"]

MACRO_CACHE_HOURS = 18           # one nightly refresh serves every cycle/health read
_CACHE_MAX_AGE_DAYS = 12         # past this the cached snapshot is too stale to trust
_HIGH = "high"


def _safe_date(s):
    try:
        return date.fromisoformat(str(s)[:10])
    except (ValueError, TypeError):
        return None


def _first_friday(year: int, month: int) -> date:
    d = date(year, month, 1)
    return d + timedelta(days=(4 - d.weekday()) % 7)


def _third_friday(year: int, month: int) -> date:
    """Standard US equity/index options expiration = 3rd Friday of the month;
    March/June/Sept/Dec are quad-witching (index futures+options+stock both
    expire) — elevated volume, pin risk, and gamma the calls sleeve must see."""
    return _first_friday(year, month) + timedelta(days=14)


def _nth_business_day(year: int, month: int, n: int) -> date:
    d = date(year, month, 1)
    count = 0
    while True:
        if d.weekday() < 5:
            count += 1
            if count >= n:
                return d
        d += timedelta(days=1)


# ── Tier 4: deterministic rule-based floor (always available, no network) ─────
def _rule_dates(rel: Release, lo: date, hi: date) -> list[date]:
    """Generate a release's dates for [lo, hi] from its seed (exact) + recurrence
    rule (for months beyond the seed). Approximate beyond the seed — refresh()
    refines with exact source dates; health reports when running on rules."""
    out = set()
    seed_months = set()
    for s in rel.seed_2026:                       # exact anchors first
        d = _safe_date(s)
        if d:
            seed_months.add((d.year, d.month))
            if lo <= d <= hi:
                out.add(d)
    if rel.rule == "anchor":                      # FOMC: published list only, no generation
        return sorted(out)
    if rel.rule == "fomc_plus_21":                # minutes: 3 weeks after each decision,
        for s in FOMC:                            # derived from the ONE anchor list
            d = _safe_date(s)
            if d:
                d = d + timedelta(days=21)
                if lo <= d <= hi:
                    out.add(d)
        return sorted(out)
    if rel.rule == "fomc_minus_14":               # Beige Book: ~2 weeks before each
        for s in FOMC:                            # decision, same ONE anchor list
            d = _safe_date(s)
            if d:
                d = d - timedelta(days=14)
                if lo <= d <= hi:
                    out.add(d)
        return sorted(out)
    y, m = lo.year, lo.month
    while date(y, m, 1) <= hi:
        if (y, m) not in seed_months:             # seed is EXACT; only fill UNSEEDED
            d = _gen_monthly(rel, y, m)           # months with the approx rule (no dup)
            if d and lo <= d <= hi:
                out.add(d)
        m, y = (m + 1, y) if m < 12 else (1, y + 1)
    if rel.rule == "weekly_thursday":             # jobless claims — every Thursday
        d = lo + timedelta(days=(3 - lo.weekday()) % 7)
        while d <= hi:
            out.add(d)
            d += timedelta(days=7)
    return sorted(out)


def _gen_monthly(rel: Release, y: int, m: int) -> date | None:
    r = rel.rule
    if r == "first_friday":
        return _first_friday(y, m)
    if r == "first_business_day":
        return _nth_business_day(y, m, 1)
    if r == "third_business_day":
        return _nth_business_day(y, m, 3)
    if r == "adp_wednesday":                      # ADP: the Wednesday before NFP's
        ff = _first_friday(y, m)                  # first Friday (2 days ahead of it)
        return ff - timedelta(days=2) if ff else None
    if r == "monthly":                            # CPI/PPI/PCE/retail — approx by release
        approx = {"cpi": 12, "ppi": 13, "pce": 26, "retail": 15,
                  # IG 2026-07-16 item 10 siblings — typical print days,
                  # refined live by the FRED/FF tiers:
                  "umich": 10, "conf_board": 27, "housing_starts": 17,
                  "existing_home": 21, "durables": 26,
                  # IG 2026-07-21 item 10 siblings:
                  "new_home_sales": 25, "industrial_production": 16,
                  "trade_balance": 5}.get(rel.key, 14)
        return _clamp_day(y, m, approx)
    if r == "monthly_early":                       # JOLTS — ~first week
        return _nth_business_day(y, m, 2)
    if r == "opex":                                # options expiration — 3rd Friday
        return _third_friday(y, m)
    if r == "quarterly":                           # GDP advance — late month after quarter-end
        if m in (1, 4, 7, 10):
            return _clamp_day(y, m, 29)
        return None
    if r == "quarterly_refunding":                 # Treasury refunding announcement —
        if m in (2, 5, 8, 11):                     # first Wednesday of Feb/May/Aug/Nov
            first = date(y, m, 1)
            return first + timedelta(days=(2 - first.weekday()) % 7)
        return None
    return None


def _clamp_day(y: int, m: int, day: int) -> date:
    nxt = date(y + (m // 12), (m % 12) + 1, 1)
    last = (nxt - timedelta(days=1)).day
    return date(y, m, min(day, last))


# ── Tier 1: FRED API (official, exact) ────────────────────────────────────────
def _fred_dates(lo: date, hi: date) -> dict:
    """{release_key: [date,...]} from FRED for releases with a fred_id. {} on any
    failure (caller falls to the next tier)."""
    import config
    key = getattr(config, "FRED_API_KEY", "")
    if not key:
        return {}
    out: dict = {}
    for rel in RELEASES:
        if rel.fred_id is None:
            continue
        try:
            url = (f"{config.FRED_BASE}/release/dates?release_id={rel.fred_id}"
                   f"&api_key={key}&file_type=json&include_release_dates_with_no_data=true"
                   f"&sort_order=asc&realtime_start={lo.isoformat()}")
            req = urllib.request.Request(url, headers={"User-Agent": "loch-raven/1.0"})
            with urllib.request.urlopen(req, timeout=getattr(config, "FRED_TIMEOUT_S", 8.0)) as r:
                data = json.loads(r.read().decode("utf-8"))
            dates = []
            for row in data.get("release_dates", []):
                d = _safe_date(row.get("date"))
                if d and lo <= d <= hi:
                    dates.append(d)
            if dates:
                out[rel.key] = sorted(set(dates))
        except Exception:
            continue
    return out


# Major releases the committee should SEE on the forward calendar (beyond the
# high-impact reopen registry) — matched against FRED's bulk release names.
# This is a what-MATTERS allowlist (stable names), not a date list, so it never
# goes stale; the DATES come live from FRED.
_FRED_BROAD = (
    "consumer price index", "producer price index", "employment situation",
    "personal income and outlays", "personal consumption expenditures",
    "gross domestic product", "advance monthly sales for retail",
    "industrial production", "new residential construction", "durable goods",
    "manufacturers' new orders", "consumer confidence", "surveys of consumers",
    "ism", "manufacturing business outlook", "empire state manufacturing",
    "job openings", "unemployment insurance weekly claims")
_FRED_BROAD_EXCLUDE = ("gdpnow", "harmonized", "state unemployment", "nowcast",
                       "real retail", "inventories and sales", "chicago fed advance")


def _fred_broad(lo: date, hi: date) -> list[dict]:
    """The BROAD official forward calendar from FRED's bulk releases/dates —
    every major macro print (Empire State, Industrial Production, Philly Fed,
    Consumer Confidence, Housing, Durable Goods, ...), not just the reopen
    registry. [{date, event, impact}]. [] without a key / on any failure."""
    import config
    key = getattr(config, "FRED_API_KEY", "")
    if not key:
        return []
    try:
        url = (f"{config.FRED_BASE}/releases/dates?api_key={key}&file_type=json"
               f"&include_release_dates_with_no_data=true&sort_order=asc"
               f"&realtime_start={lo.isoformat()}&limit=1000")
        req = urllib.request.Request(url, headers={"User-Agent": "loch-raven/1.0"})
        with urllib.request.urlopen(req, timeout=getattr(config, "FRED_TIMEOUT_S", 8.0)) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:
        return []
    out, seen = [], set()
    for row in data.get("release_dates", []):
        d = _safe_date(row.get("date"))
        nm = str(row.get("release_name", ""))
        low = nm.lower()
        if not d or not (lo <= d <= hi):
            continue
        if any(x in low for x in _FRED_BROAD_EXCLUDE) or not any(x in low for x in _FRED_BROAD):
            continue
        k = (d.isoformat(), low[:30])
        if k in seen:
            continue
        seen.add(k)
        out.append({"date": d.isoformat(), "event": nm, "impact": "scheduled"})
    return sorted(out, key=lambda e: (e["date"], e["event"]))


# ── Tier 2: Forex Factory weekly JSON (free, no key, near-term exact) ─────────
_FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"


def _forexfactory_raw() -> list[dict]:
    """Raw US High/Medium-impact events from the Forex Factory weekly feed:
    [{date, title, impact}]. The BROAD near-term calendar — Consumer Confidence,
    Housing, Durable Goods, sentiment, etc. — the committee should SEE even for
    releases that aren't bench reopen-catalysts ("fed gives way more data";
    2026-06-14). Keyless, near-term. [] on any failure (unofficial endpoint)."""
    try:
        req = urllib.request.Request(_FF_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            rows = json.loads(r.read().decode("utf-8"))
    except Exception:
        return []
    out = []
    for row in rows if isinstance(rows, list) else []:
        if row.get("country") != "USD" or row.get("impact") not in ("High", "Medium"):
            continue
        d = _safe_date(row.get("date"))
        if d:
            out.append({"date": d.isoformat(), "title": str(row.get("title", "")),
                        "impact": str(row.get("impact", "")).lower()})
    return out


def _ff_overlay(raw: list, lo: date, hi: date) -> dict:
    """{release_key: [date,...]} — match raw FF events to registry releases."""
    out: dict = {}
    for ev in raw:
        d = _safe_date(ev.get("date"))
        if not d or not (lo <= d <= hi):
            continue
        rel = _match_release(ev.get("title", ""))
        if rel:
            out.setdefault(rel.key, [])
            if d not in out[rel.key]:
                out[rel.key].append(d)
    return out


def _match_release(text: str) -> Release | None:
    """First release whose keyword appears in the text (lowercased)."""
    t = (text or "").lower()
    for rel in RELEASES:
        if any(kw in t for kw in rel.keywords):
            return rel
    return None


# ── Snapshot (cache of the resolved dynamic overlay + broad FF calendar) ──────
def refresh() -> str:
    """Pull live dates (FRED -> Forex Factory) and cache them. Called once per
    cycle. Returns the tier achieved. Fail-soft: a feed outage leaves the last
    good cache in place and the floor still works. Skips the network if the
    cached snapshot is still within MACRO_CACHE_HOURS (one nightly fetch serves
    every cycle, and tests pre-seed a fresh snapshot to stay offline)."""
    from firm import state
    cached = state.load("macro_calendar", {})
    now = datetime.now(timezone.utc)
    # Skip the network if we ATTEMPTED within the TTL (last_attempt, not last
    # success) — so a sustained feed outage retries at most once per TTL instead
    # of every cycle, while the last-good overlay is preserved (not wiped).
    last = cached.get("last_attempt") or cached.get("ts")
    try:
        if last and (now - datetime.fromisoformat(last)).total_seconds() < MACRO_CACHE_HOURS * 3600:
            return cached.get("tier", "rule")
    except (ValueError, TypeError):
        pass
    today = now.date()
    lo, hi = today - timedelta(days=10), today + timedelta(days=200)
    ff_raw = _forexfactory_raw()               # keyless broad calendar (current week)
    broad = _fred_broad(today, today + timedelta(days=45))  # official forward broad calendar
    tier, overlay = "rule", {}
    fred = _fred_dates(lo, hi)
    if fred:
        tier, overlay = "fred", fred
    elif ff_raw:
        tier, overlay = "forexfactory", _ff_overlay(ff_raw, lo, hi)
    if overlay or ff_raw or broad:
        snap = {"ts": now.isoformat(), "last_attempt": now.isoformat(), "tier": tier,
                "overlay": {k: [d.isoformat() for d in v] for k, v in overlay.items()},
                "ff_raw": ff_raw or cached.get("ff_raw", []),
                "broad": broad or cached.get("broad", [])}
        state.save("macro_calendar", snap)
    else:
        # Both feeds down: keep the last-good data, just stamp the attempt so we
        # rate-limit retries (don't discard valid cached dates on a blip).
        cached["last_attempt"] = now.isoformat()
        state.save("macro_calendar", cached)
    return tier


def upcoming_all(days: int = 10) -> list[dict]:
    """The BROAD near-term US economic calendar — every major macro print, not
    just the high-impact reopen registry: Empire State, Industrial Production,
    Philly Fed, Consumer Confidence, Housing, Durable Goods, sentiment, claims,
    etc. For the committee to SEE and de-risk around. Prefers the official FRED
    forward calendar; falls back to the keyless Forex Factory feed. [] if neither
    is cached. (upcoming() stays the registry high-impact REOPEN set.)"""
    snap = _snapshot()
    today = datetime.now(timezone.utc).date()
    hi = today + timedelta(days=days)
    broad = (snap.get("broad") or []) if snap else []
    if broad:
        out = [{"date": ev["date"], "event": ev.get("event", ""), "impact": ev.get("impact", "scheduled")}
               for ev in broad if (lambda d: d and today <= d <= hi)(_safe_date(ev.get("date")))]
    else:                                          # keyless fallback: Forex Factory raw
        out = [{"date": ev["date"], "event": ev.get("title", ""), "impact": ev.get("impact", "")}
               for ev in ((snap.get("ff_raw") or []) if snap else [])
               if (lambda d: d and today <= d <= hi)(_safe_date(ev.get("date")))]
    # Market-structure events (OPEX/quad-witching) are deterministic and not in
    # any FRED/FF feed — merge them in from the registry so the committee and the
    # options sleeve always see expiration coming, whatever the live data tier.
    for key in _MARKET_STRUCTURE:
        rel = _BY_KEY.get(key)
        if not rel:
            continue
        for d in _release_dates(rel, today, hi):
            out.append({"date": d.isoformat(), "event": rel.name, "impact": rel.impact})
    return sorted(out, key=lambda e: (e["date"], e["event"]))


def _snapshot() -> dict:
    """Cached overlay if fresh enough, else empty (-> floor only)."""
    try:
        from firm import state
        snap = state.load("macro_calendar", {})
        ts = _safe_date(snap.get("ts"))
        if not ts:
            return {}
        age = (datetime.now(timezone.utc).date() - ts).days
        return snap if age <= _CACHE_MAX_AGE_DAYS else {}
    except Exception:
        return {}


def calendar_tier() -> str:
    """Which tier is currently feeding the calendar — for the health desk."""
    return (_snapshot() or {}).get("tier", "rule")


def _overlay_for(rel_key: str, lo: date, hi: date) -> list[date]:
    snap = _snapshot()
    raw = (snap.get("overlay") or {}).get(rel_key) if snap else None
    if not raw:
        return []
    return [d for d in (_safe_date(x) for x in raw) if d and lo <= d <= hi]


def _release_dates(rel: Release, lo: date, hi: date) -> list[date]:
    """Resolved dates for one release: dynamic overlay (exact) REPLACES the rule
    floor per calendar-month, so we never double-count an approx + exact CPI."""
    floor = _rule_dates(rel, lo, hi)
    overlay = _overlay_for(rel.key, lo, hi)
    if not overlay:
        return floor
    ov_months = {(d.year, d.month) for d in overlay}
    kept_floor = [d for d in floor if (d.year, d.month) not in ov_months]
    return sorted(set(kept_floor) | set(overlay))


# ── Public API (unchanged signatures) ─────────────────────────────────────────
def event_dates(lo: date, hi: date) -> list[date]:
    """All HIGH-impact macro release dates in [lo, hi] (FOMC, CPI, PCE, NFP,
    retail, GDP, ISM). These are the global reopen/de-risk triggers. Medium-
    impact prints (PPI, claims, JOLTS) are tracked only when a thesis names
    them (events_mentioned), so a weekly claims print doesn't churn the bench."""
    out = set()
    for rel in RELEASES:
        if rel.impact != _HIGH:
            continue
        out.update(_release_dates(rel, lo, hi))
    return sorted(out)


def landed_since(since: date, today: date) -> date | None:
    ev = [d for d in event_dates(since, today) if since < d <= today]
    return max(ev) if ev else None


def imminent_within(today: date, days: int) -> bool:
    hi = today + timedelta(days=days)
    return any(today <= d <= hi for d in event_dates(today, hi))


def future_events(today: date) -> int:
    """How many HIGH-impact release dates are resolvable in the next ~120 days
    (from whatever tier). A drop toward zero means the floor is exhausted AND no
    live source is filling — health warns so it never silently goes blind."""
    return len(event_dates(today, today + timedelta(days=120)))


def events_mentioned(text: str, today: date, horizon_days: int = 75) -> list[dict]:
    """A bench thesis that NAMES a macro release -> that release's NEXT date.
    This is "if the bench mentions a number, auto-track it" — ANY release
    (incl. medium-impact), resolved through the same tiers. Returns
    [{key, name, date, impact}], soonest first."""
    t = (text or "").lower()
    hi = today + timedelta(days=horizon_days)
    out = []
    for rel in RELEASES:
        if not any(kw in t for kw in rel.keywords):
            continue
        upcoming_dates = [d for d in _release_dates(rel, today, hi) if d >= today]
        if upcoming_dates:
            out.append({"key": rel.key, "name": rel.name,
                        "date": upcoming_dates[0].isoformat(), "impact": rel.impact})
    return sorted(out, key=lambda e: e["date"])


# Broad-release reopen taxonomy (IG 2026-07-06 med, ratified). The RELEASES
# registry is a small curated high-impact set (FOMC/CPI/PCE/NFP/retail/GDP/ISM);
# the market-moving SIBLINGS the IG flagged — housing, durable goods, UMich
# sentiment/inflation-expectations, Empire State, Philly Fed, consumer confidence,
# industrial production — are already SUPPLIED (dates) by the cached broad feed
# (_fred_broad + Forex Factory in state['macro_calendar']), just not wired into the
# reopen trigger. This maps a thesis's own words -> the broad-event NAME substring
# so a bench HOLD 'waiting on the housing report' reopens when housing actually
# lands, WITHOUT hardcoding FRED ids and WITHOUT new per-cycle network (cache read).
_BROAD_REOPEN_KEYWORDS: dict = {
    "residential": ("housing", "home sales", "housing starts", "residential",
                    "building permits", "new home"),
    "durable goods": ("durable goods", "durables", "capital goods", "factory orders",
                      "new orders"),
    "surveys of consumers": ("umich", "michigan", "consumer sentiment",
                             "inflation expectation"),
    "consumer confidence": ("consumer confidence",),
    "empire state": ("empire state", "empire manufacturing", "ny fed manufacturing"),
    "manufacturing business outlook": ("philly fed", "philadelphia fed",
                                       "business outlook"),
    "industrial production": ("industrial production", "capacity utilization"),
}


def _broad_cached_events() -> list[dict]:
    """The cached broad macro calendar (FRED broad + Forex Factory), refreshed once
    per cycle by refresh() — read-only, no network. [{event, date}]."""
    from firm import state
    snap = state.load("macro_calendar", {}) or {}
    out = [{"event": e.get("event", ""), "date": e.get("date")}
           for e in (snap.get("broad") or [])]
    out += [{"event": e.get("title", ""), "date": e.get("date")}
            for e in (snap.get("ff_raw") or [])]
    return out


def _broad_landed(text: str, since: date, today: date) -> date | None:
    """Most recent BROAD release the thesis NAMES that landed in (since, today].
    Data-backed: dates come from the cached broad feed, matched by event NAME."""
    t = (text or "").lower()
    if not t:
        return None
    best = None
    for ev in _broad_cached_events():
        name = str(ev.get("event", "")).lower()
        if not any(canon in name and any(kw in t for kw in phrases)
                   for canon, phrases in _BROAD_REOPEN_KEYWORDS.items()):
            continue
        d = _safe_date(ev.get("date"))
        if d and since < d <= today and (best is None or d > best):
            best = d
    return best


def mentioned_landed(text: str, since: date, today: date) -> date | None:
    """Most recent date of a release the thesis NAMES, in (since, today]. Catches
    the SPECIFIC number a bench thesis is waiting on — including medium-impact
    prints (PPI/claims/JOLTS) the global high-impact triggers deliberately skip,
    AND the broad siblings (housing/durables/UMich/Empire/Philly) from the cached
    broad feed (IG 2026-07-06 med)."""
    t = (text or "").lower()
    best = None
    for rel in RELEASES:
        if not any(kw in t for kw in rel.keywords):
            continue
        for d in _release_dates(rel, since, today):
            if since < d <= today and (best is None or d > best):
                best = d
    _b = _broad_landed(text, since, today)
    if _b and (best is None or _b > best):
        best = _b
    return best


def mentioned_imminent(text: str, today: date, days: int) -> bool:
    """Does the thesis NAME a release whose next date is within `days`? Keeps a
    name alive through the specific catalyst it's waiting on (any impact)."""
    t = (text or "").lower()
    hi = today + timedelta(days=days)
    for rel in RELEASES:
        if not any(kw in t for kw in rel.keywords):
            continue
        if any(today <= d <= hi for d in _release_dates(rel, today, hi)):
            return True
    return False


def upcoming(days: int = 7) -> list[dict]:
    """High-impact events in the next `days`, with real resolved dates + impact +
    source tier — for the committee's de-risk view. (Replaces the old
    date(y,m,12) CPI placeholder that could mislead the committee.)"""
    today = datetime.now(timezone.utc).date()
    hi = today + timedelta(days=days)
    tier = calendar_tier()
    out = []
    for rel in RELEASES:
        if rel.impact != _HIGH:
            continue
        for d in _release_dates(rel, today, hi):
            if today <= d <= hi:
                out.append({"date": d.isoformat(),
                            "event": f"{rel.name} ({rel.impact} impact)",
                            "source": tier})
    return sorted(out, key=lambda e: e["date"])
