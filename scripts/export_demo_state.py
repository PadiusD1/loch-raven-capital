"""Export a small snapshot of a demo firm's books into a private folder outside this repository.

  py -3.11 scripts/export_demo_state.py --source PATH_TO_DEMO_STATE --dest PATH_TO_PRIVATE_SNAPSHOT

The snapshot is a paper book, so it never goes into this repository. Neither
folder has a default, a destination inside the repository is refused, and the
destination must be a fresh folder this run creates.
To open the populated cockpit offline from the snapshot
  py -3.11 scripts/seed_demo_state.py --source PATH_TO_PRIVATE_SNAPSHOT
  set LOCH_RAVEN_DEMO=1 and LOCH_RAVEN_PORT=8430, then py -3.11 app.py

Only the files the cockpit renders are exported. The research log keeps the
latest committee run plus a few earlier snapshots, and journals, alerts, health
records and anything tied to a broker account are left out. Every exported
string is scanned for emails and local paths before writing.
"""
import argparse
import json
import re
import shutil
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FILES = ("settings.json", "sleeve_alpha_stocks.json", "sleeve_core_blend.json", "sleeve_calls_only.json",
         "sleeve_etf_rotation.json", "watchlist.json", "lessons.json", "ledger.json", "equity_history.json",
         "etf_seats.json", "macro_calendar.json", "sector_regime.json", "firm.json", "tuning.json")
LEAK = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+|[A-Za-z]:\\\\Users|/home/|/srv/")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", type=Path, required=True,
                    help="the demo firm's state folder to read")
    ap.add_argument("--dest", type=Path, required=True,
                    help="a private folder outside this repository for the snapshot")
    args = ap.parse_args()
    src, out = args.source.resolve(), args.dest.resolve()
    if out == REPO or REPO in out.parents:
        raise SystemExit("refusing to write a paper book snapshot inside this repository")
    if src == out:
        raise SystemExit("source and destination are the same folder")
    # The destination must be a fresh folder this run creates, so no file or link
    # planted there beforehand can carry a write somewhere else, into this
    # repository included. An existing folder is refused, whatever it holds.
    if args.dest.is_symlink() or args.dest.exists():
        raise SystemExit("the destination must not exist yet, name a fresh folder")
    out.mkdir(parents=True)
    if out.is_symlink() or not out.is_dir():
        raise SystemExit("the destination is not a fresh folder")
    written = []
    for name in FILES:
        p = src / name
        if not p.is_file():
            continue
        text = p.read_text(encoding="utf-8")
        if LEAK.search(text):
            raise SystemExit(f"refusing to export {name}, it contains an email or a local path")
        shutil.copy2(p, out / name)
        written.append(name)
    research = json.loads((src / "research.json").read_text(encoding="utf-8"))
    trimmed = {"latest": research.get("latest", {}), "latest_asof": research.get("latest_asof"),
               "history": research.get("history", [])[-3:]}
    blob = json.dumps(trimmed)
    if LEAK.search(blob):
        raise SystemExit("refusing to export research.json, it contains an email or a local path")
    (out / "research.json").write_text(blob, encoding="utf-8")
    written.append("research.json")
    size = sum((out / n).stat().st_size for n in written)
    print(f"exported {len(written)} files, {size / 1e6:.1f} MB, to {out}")


if __name__ == "__main__":
    main()
