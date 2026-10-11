"""Seed a demo copy's state/ from a local private state folder (read only on the source).

  py -3.11 scripts/seed_demo_state.py --source PATH_TO_PRIVATE_STATE

The source is a state folder the operator keeps outside this repository and
names on the command line. It has no default, and a source inside the
repository is refused, so no committed snapshot can seed a demo. Copies the
books, research history, bench, lessons and caches so the demo cockpit looks
exactly like the source firm. Skips backups, scratch files, locks, heartbeats,
status, alerts and anything tied to live broker reconciliation, so the demo
boots clean and never inherits a live incident. Forces paper mode.
"""
import argparse
import json
import re
import shutil
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKIP = re.compile(r"(\.bak|\.baktmp|\.tmp|corrupt|pre-xx)|^(heartbeat|status|alerts|cycle)\.|"
                  r"^(ram_sweep|reconcile_display|recon_drift|cli_auth)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", type=Path, required=True,
                    help="a local private state folder outside this repository")
    ap.add_argument("--dest", type=Path, default=REPO / "state")
    ap.add_argument("--depth", default="standard", choices=("fast", "standard", "full"))
    args = ap.parse_args()
    src, dst = args.source.resolve(), args.dest.resolve()
    if src == REPO or REPO in src.parents:
        raise SystemExit("the source must be a private state folder outside this repository")
    if src == dst:
        raise SystemExit("source and destination are the same folder")
    dst.mkdir(parents=True, exist_ok=True)
    for name in ("alerts.json", "alerts.bak", "status.json", "heartbeat.json", "cycle.lock"):
        p = dst / name
        if p.exists():
            p.unlink()
    copied = 0
    for f in sorted(src.iterdir()):
        if f.is_file() and not SKIP.search(f.name):
            shutil.copy2(f, dst / f.name)
            copied += 1
    settings_path = dst / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
    settings.update(mode="paper", depth=args.depth)
    settings_path.write_text(json.dumps(settings), encoding="utf-8")
    print(f"copied {copied} files into {dst}, settings {settings}")


if __name__ == "__main__":
    main()
