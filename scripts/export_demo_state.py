"""Export a small, shareable snapshot of a demo firm's books into data/demo_state.

  py -3.11 scripts/export_demo_state.py

Judges can then run the populated cockpit offline:
  py -3.11 scripts/seed_demo_state.py --source data/demo_state
  set LOCH_RAVEN_DEMO=1 and LOCH_RAVEN_PORT=8430, then py -3.11 app.py

Only the files the cockpit renders are exported. The research log keeps the
latest committee run plus a few earlier snapshots, and journals, alerts, health
records and anything tied to a broker account are left out. Every exported
string is scanned for emails and local paths before writing.
"""
import json
import re
import shutil
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "state"
OUT = REPO / "data" / "demo_state"
FILES = ("settings.json", "sleeve_alpha_stocks.json", "sleeve_core_blend.json", "sleeve_calls_only.json",
         "sleeve_etf_rotation.json", "watchlist.json", "lessons.json", "ledger.json", "equity_history.json",
         "etf_seats.json", "macro_calendar.json", "sector_regime.json", "firm.json", "tuning.json")
LEAK = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+|[A-Za-z]:\\\\Users|/home/|/srv/")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    written = []
    for name in FILES:
        p = SRC / name
        if not p.is_file():
            continue
        text = p.read_text(encoding="utf-8")
        if LEAK.search(text):
            raise SystemExit(f"refusing to export {name}, it contains an email or a local path")
        shutil.copy2(p, OUT / name)
        written.append(name)
    research = json.loads((SRC / "research.json").read_text(encoding="utf-8"))
    trimmed = {"latest": research.get("latest", {}), "latest_asof": research.get("latest_asof"),
               "history": research.get("history", [])[-3:]}
    blob = json.dumps(trimmed)
    if LEAK.search(blob):
        raise SystemExit("refusing to export research.json, it contains an email or a local path")
    (OUT / "research.json").write_text(blob, encoding="utf-8")
    written.append("research.json")
    size = sum((OUT / n).stat().st_size for n in written)
    print(f"exported {len(written)} files, {size / 1e6:.1f} MB, to {OUT}")


if __name__ == "__main__":
    main()
