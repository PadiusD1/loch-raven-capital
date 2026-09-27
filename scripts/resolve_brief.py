"""Mark a fix brief resolved (or reopen it) and regenerate state/FIX_BRIEFS.md.

learning._file_fix_requests already RENDERS `**Resolved:**` when a brief carries
a `resolved` note — but nothing in the codebase ever wrote that field, so every
brief rendered as OPEN forever no matter how long ago it was fixed. A queue with
no notion of done is a queue nobody can trust: on 2026-07-28 four of the ten
open briefs were already fixed, and three more rested on premises that did not
survive reading the code, which is only discoverable by re-deriving all ten.

This is the missing producer.

    py -3.11 scripts/resolve_brief.py --list
    py -3.11 scripts/resolve_brief.py 5 "fixed: route_coverage was missing
        buy_skipped_price_drift; verified by tests/test_firm.py block 89"
    py -3.11 scripts/resolve_brief.py 8 --not-a-defect "premise false: parking
        never consumes a book slot (sleeves.py real_count)"
    py -3.11 scripts/resolve_brief.py 5 --reopen
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config          # noqa: E402
from firm import state  # noqa: E402


def render(fb: dict) -> str:
    items = fb.get("items") or []
    open_n = sum(1 for r in items if not r.get("resolved"))
    lines = ["# Fix Briefs — filed by the firm's own performance review", "",
             f"**{open_n} open · {len(items) - open_n} resolved**", "",
             'Open Claude Code in this folder and say: '
             '"read state/FIX_BRIEFS.md and fix item N".',
             "Mark one done with "
             '`py -3.11 scripts/resolve_brief.py N "what you did"`.', ""]
    for i, r in enumerate(items, 1):
        status = "RESOLVED" if r.get("resolved") else "OPEN"
        lines += [f"## Item {i} ({str(r.get('filed_at', ''))[:10]}) — {status}",
                  f"**Problem:** {r.get('problem', '')}",
                  f"**Evidence:** {r.get('evidence', '')}",
                  f"**Suspected area:** {r.get('suspected_area', '')}",
                  f"**Ask Claude:** {r.get('ask_claude', '')}"]
        if r.get("resolved"):
            lines += [f"**Resolved:** {r['resolved']}"]
        lines += [""]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    holder = state.cycle_lock_holder()
    if holder is not None and "--list" not in argv:
        print(f"REFUSING: a cycle holds the state lock (pid {holder}).")
        return 2
    fb = state.load("fix_briefs", {"items": []}) or {"items": []}
    items = fb.get("items") or []

    if "--list" in argv or not argv:
        for i, r in enumerate(items, 1):
            mark = "RESOLVED" if r.get("resolved") else "open    "
            print(f"{i:2d}. [{mark}] {str(r.get('problem', ''))[:88]}")
            if r.get("resolved"):
                print(f"        -> {r['resolved'][:100]}")
        return 0

    try:
        n = int(argv[0])
    except ValueError:
        print(f"first argument must be a brief number, got {argv[0]!r}")
        return 1
    if not 1 <= n <= len(items):
        print(f"no brief {n} (there are {len(items)})")
        return 1
    r = items[n - 1]

    if "--reopen" in argv:
        r.pop("resolved", None)
        print(f"brief {n} reopened")
    else:
        note = " ".join(a for a in argv[1:] if not a.startswith("--")).strip()
        if not note:
            print("give a reason: resolve_brief.py N \"what you did\"")
            return 1
        # NOT-A-DEFECT is recorded distinctly from FIXED. Three of the ten briefs
        # open on 2026-07-28 described behaviour the code did not actually have;
        # collapsing "we fixed it" and "it was never broken" into one word loses
        # the single most useful signal about the reviewer's accuracy.
        kind = "NOT A DEFECT" if "--not-a-defect" in argv else "FIXED"
        r["resolved"] = (f"{kind} {datetime.now(timezone.utc):%Y-%m-%d} — {note}")
        print(f"brief {n} -> {kind}")

    fb["items"] = items
    state.save("fix_briefs", fb)
    (config.STATE_DIR / "FIX_BRIEFS.md").write_text(render(fb), encoding="utf-8")
    state.journal("fix_brief_status_changed", brief=n,
                  status=r.get("resolved", "reopened")[:120])
    open_n = sum(1 for x in items if not x.get("resolved"))
    print(f"{open_n} open · {len(items) - open_n} resolved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
