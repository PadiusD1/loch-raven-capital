"""Fetch authentic SEC company-facts data and write provenance-tagged research.

Requires SEC_USER_AGENT='Organization contact@example.com'. This is a data
collection step, not an endorsement of any trade or a model training job.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx

from firm.research_provenance import validate_research

SEC_BASE = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
METRICS = {
    "Revenues": ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"),
    "NetIncome": ("NetIncomeLoss",),
    "OperatingCashFlow": ("NetCashProvidedByUsedInOperatingActivities",),
    "CapitalExpenditure": ("PaymentsToAcquirePropertyPlantAndEquipment",),
    "Assets": ("Assets",),
    "Liabilities": ("Liabilities",),
    "OperatingIncome": ("OperatingIncomeLoss",),
    "GrossProfit": ("GrossProfit",),
    "CashAndEquivalents": ("CashAndCashEquivalentsAtCarryingValue",),
    "CurrentAssets": ("AssetsCurrent",),
    "CurrentLiabilities": ("LiabilitiesCurrent",),
    "StockholdersEquity": ("StockholdersEquity",),
    "DebtCurrent": ("LongTermDebtCurrent",),
    "DebtNoncurrent": ("LongTermDebtNoncurrent",),
    "StockBasedCompensation": ("ShareBasedCompensation",),
    "ResearchAndDevelopment": ("ResearchAndDevelopmentExpense",),
    "ShareRepurchases": ("PaymentsForRepurchaseOfCommonStock",),
    "DividendsPaid": ("PaymentsOfDividendsCommonStock",),
    "DilutedShares": ("WeightedAverageNumberOfDilutedSharesOutstanding",),
    "DilutedEPS": ("EarningsPerShareDiluted",),
}
MAX_FACTS_PER_METRIC = 12


def extract_facts(payload: dict, *, observed_at: str) -> list[dict]:
    """Extract filed, non-future SEC XBRL facts without filling missing values."""
    obs = datetime.fromisoformat(observed_at.replace("Z", "+00:00")).date()
    usgaap = payload.get("facts", {}).get("us-gaap", {})
    output = []
    for metric, concepts in METRICS.items():
        entries = []
        for concept in concepts:
            for unit, facts in usgaap.get(concept, {}).get("units", {}).items():
                expected = {"DilutedShares": "shares", "DilutedEPS": "USD/shares"}.get(metric, "USD")
                if unit != expected:
                    continue
                for f in facts:
                    filed = f.get("filed")
                    if not filed or filed > obs.isoformat():
                        continue
                    if f.get("form") not in ("10-K", "10-Q", "10-K/A", "10-Q/A"):
                        continue
                    if isinstance(f.get("val"), bool) or not isinstance(f.get("val"), (int, float)) or not __import__("math").isfinite(f["val"]):
                        continue
                    entries.append({"metric": metric, "concept": concept,
                                    "value": f["val"], "unit": unit,
                                    "start": f.get("start"), "end": f.get("end"),
                                    "filed": filed, "form": f["form"],
                                    "accession": f.get("accn")})
        entries.sort(key=lambda x: (x["filed"], x["end"] or ""), reverse=True)
        seen = set()
        for e in entries:
            key = (e["concept"], e["start"], e["end"], e["unit"])
            if key not in seen:
                output.append(e)
                seen.add(key)
            if len(seen) >= MAX_FACTS_PER_METRIC:
                break
    return output


def fetch(cik: int, *, user_agent: str, output: Path) -> dict:
    if not re.search(r"[^@ ]+@[^@ ]+\.[^@ ]+", user_agent):
        raise ValueError("SEC_USER_AGENT must contain a contact email")
    url = SEC_BASE.format(cik=cik)
    with httpx.Client(timeout=30, headers={"User-Agent": user_agent,
                                            "Accept-Encoding": "gzip, deflate"}) as client:
        response = client.get(url)
        response.raise_for_status()
        payload = response.json()
    observed = datetime.now(timezone.utc).isoformat()
    facts = extract_facts(payload, observed_at=observed)
    if not facts:
        raise ValueError("SEC returned no eligible financial facts")
    published = observed  # Snapshot only became available when actually retrieved.
    # The bundle includes multiple filings; each fact retains its own filing date.
    body = json.dumps({"cik": cik, "entity": payload.get("entityName"),
                       "facts": facts}, separators=(",", ":"))
    record = validate_research({
        "desk": "fundamental", "source_url": url,
        "published_at": published, "observed_at": observed, "asof": published,
        "title": f"SEC XBRL company facts CIK {cik}", "body": body,
        "cik": cik, "entity": payload.get("entityName"), "facts": facts,
    })
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return {"cik": cik, "facts": len(facts), "output": str(output)}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--cik", type=int, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    print(json.dumps(fetch(a.cik, user_agent=os.environ.get("SEC_USER_AGENT", ""),
                           output=a.output)))
