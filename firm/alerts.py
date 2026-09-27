"""Alerts — when something needs your eyes, you actually find out.

Four channels, all best-effort and non-blocking:
  1. Windows toast notification (visible even if the dashboard is closed)
  2. Persistent red banner on the dashboard (until acknowledged)
  3. Journal entry (permanent audit trail)
  4. Slack webhook (SLACK_WEBHOOK_URL), the VPS stand-in for the toast. Posted
     LAST and from a daemon thread, so alert() never waits on the network.

Severity 'critical' = trading is impacted (halts, corruption, reconcile
drift). 'info' = recovered/resolved notices.
"""
import json
import os
import re
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timezone

from firm import state

MAX_ALERTS = 30

# Toast text is read from env vars, NOT interpolated into the -Command string
# (2026-06-14 audit A8). PowerShell treats $env: values as literal data and never
# re-evaluates them as code, so a crafted ticker/headline ("`; rm ...", "$(...)")
# in an alert title can no longer break out of CreateTextNode into command exec.
_TOAST_PS = (
    "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
    "ContentType = WindowsRuntime] | Out-Null;"
    "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
    "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
    "$n = $t.GetElementsByTagName('text');"
    "$n.Item(0).AppendChild($t.CreateTextNode($env:LR_TOAST_TITLE)) | Out-Null;"
    "$n.Item(1).AppendChild($t.CreateTextNode($env:LR_TOAST_MSG)) | Out-Null;"
    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
    "'Loch Raven Capital').Show("
    "[Windows.UI.Notifications.ToastNotification]::new($t))"
)


def _post_json(url: str, payload: dict) -> bool:
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return 200 <= resp.status < 300


# Operator 2026-09-22: "stop sending me repetitive messages that aren't useful".
# A research outage posted RESEARCH PAUSED and RESEARCH DEGRADED every cycle,
# about once an hour all day, and the health sweep re posted its own line on
# top. The dashboard banner and the journal keep every alert. Slack now hears a
# given alert once per SLACK_REPEAT_S, the key ignoring the counts in it, and in
# plain words without the dashes and colons Office rule 1 bans.
SLACK_REPEAT_S = 6 * 3600
_SLACK_LOCK = threading.Lock()


def _slack_key(title: str) -> str:
    return re.sub(r"[0-9.,/:%]+", "#", title or "").strip().lower()


def _plain(text: str) -> str:
    text = re.sub(r"\s*[—–]\s*|\s+-\s+", ", ", text)
    text = re.sub(r"\s*[:;]\s+", ", ", text)
    return re.sub(r",\s*,", ",", text)


def _due(title: str, now: float) -> bool:
    key = _slack_key(title)
    with _SLACK_LOCK:
        seen = state.load("slack_alerts", {}) or {}
        if now - float(seen.get(key, 0) or 0) < SLACK_REPEAT_S:
            return False
        seen[key] = now
        state.save("slack_alerts", {k: v for k, v in seen.items() if now - float(v or 0) < 7 * 86400})
    return True


def _slack(title: str, msg: str) -> bool:
    """Post the alert to the fund's Slack channel when SLACK_WEBHOOK_URL is set.
    This is the VPS replacement for the Windows toast. The same alert is posted
    at most once per SLACK_REPEAT_S."""
    url = os.environ.get("SLACK_WEBHOOK_URL", "")
    if not url:
        return False
    try:
        if not _due(title, time.time()):
            return False
        return _post_json(url, {"text": _plain(f"Loch Raven. {title}. {msg}")})
    except Exception:  # noqa: BLE001
        return False


def _toast(title: str, msg: str) -> None:
    """Best-effort Windows toast; never blocks, never raises. Text passed via env
    vars (injection-safe), not interpolated into the PowerShell command."""
    if os.name != "nt":
        return
    try:
        env = dict(os.environ, LR_TOAST_TITLE=str(title)[:120], LR_TOAST_MSG=str(msg)[:240])
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", _TOAST_PS],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
            creationflags=0x08000000)  # CREATE_NO_WINDOW
    except Exception:
        pass


def alert(title: str, msg: str, severity: str = "critical") -> None:
    safe_title = title.replace("'", "")[:80]
    safe_msg = msg.replace("'", "")[:200]
    rec = {"ts": datetime.now(timezone.utc).isoformat(), "title": title,
           "msg": msg, "severity": severity, "acked": False}
    with state.SLEEVE_LOCK:
        al = state.load("alerts", {"items": []})
        al["items"] = (al.get("items", []) + [rec])[-MAX_ALERTS:]
        state.save("alerts", al)
    state.journal("alert", title=title, msg=msg, severity=severity)
    if severity == "critical":
        _toast(safe_title, safe_msg)
    # Slack LAST and off-thread (review 2026-09-17): a slow webhook must never
    # delay the journal write or the dashboard banner on a kill switch or
    # circuit breaker event. _slack is a no-op without SLACK_WEBHOOK_URL.
    threading.Thread(target=_slack, args=(title, msg), daemon=True).start()


# Transient pricing alerts auto-clear from the feed once the condition ends — they
# must NOT linger as permanent 'critical's after they resolve. (2026-06-22 operator:
# 30 CANNOT-PRICE/STALE-MARK alerts from a fixed morning outage still filled the feed
# hours later and read as a live emergency.) The journal keeps the full audit trail.
_RESOLVABLE_PREFIXES = ("CANNOT PRICE ", "STALE MARK ")


def resolve(sym: str) -> int:
    """Auto-ack any unacked transient pricing alert for `sym` ('CANNOT PRICE {sym}' /
    'STALE MARK {sym}') once it prices cleanly again, so the live feed self-heals.
    Returns how many were cleared. Same lock as alert()/ack_all() (never held by the
    monitor's per-position loop, so no deadlock)."""
    if not sym:
        return 0
    titles = {p + sym for p in _RESOLVABLE_PREFIXES}
    n = 0
    with state.SLEEVE_LOCK:
        al = state.load("alerts", {"items": []})
        for a in al.get("items", []):
            if not a.get("acked") and a.get("title", "") in titles:
                a["acked"] = True
                n += 1
        if n:
            state.save("alerts", al)
    return n


def unacked() -> list[dict]:
    return [a for a in state.load("alerts", {}).get("items", [])
            if not a.get("acked")]


def ack_all() -> None:
    with state.SLEEVE_LOCK:
        al = state.load("alerts", {"items": []})
        for a in al.get("items", []):
            a["acked"] = True
        state.save("alerts", al)
