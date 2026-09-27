import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import keepalive  # noqa: E402
from firm import alerts, cycle  # noqa: E402

FUND_APP = str(keepalive.BASE / "app.py")


class P:
    def __init__(self, pid, cmd, name="python.exe", cwd=None):
        self.pid = pid
        self._cmd = cmd
        self._cwd = cwd
        self.info = {"name": name, "cmdline": cmd}
        self.killed = False

    def cwd(self):
        if self._cwd is None:
            raise OSError("cwd unavailable")
        return self._cwd

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


def _fake_psutil(procs):
    return SimpleNamespace(process_iter=lambda attrs=None: procs, Error=Exception,
                           NoSuchProcess=Exception, AccessDenied=Exception)


def test_kill_all_app_instances_kills_every_fund_app_py(monkeypatch):
    procs = [P(1, ["python", "app.py"], cwd=str(keepalive.BASE)),
             P(2, ["python", FUND_APP]),
             P(3, ["python", "other.py"])]
    monkeypatch.setitem(sys.modules, "psutil", _fake_psutil(procs))
    assert keepalive._kill_all_app_instances() == 2
    assert [p.killed for p in procs] == [True, True, False]


def test_kill_all_app_instances_matches_only_the_fund_app(monkeypatch):
    procs = [P(1, ["python", "C:/other/app.py"]),
             P(2, ["python", FUND_APP]),
             P(3, ["python", "app.py"], cwd="C:/other")]
    expected = [False, True, False]
    if os.name == "nt":
        # Backslashes and case must not hide the fund's own app on Windows.
        procs.append(P(4, ["python", FUND_APP.replace("/", "\\").upper()]))
        expected.append(True)
    monkeypatch.setitem(sys.modules, "psutil", _fake_psutil(procs))
    killed = keepalive._kill_all_app_instances()
    assert [p.killed for p in procs] == expected
    assert killed == expected.count(True)


def test_slack_alert_posts_when_webhook_set(monkeypatch):
    sent = {}
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.example/abc")
    monkeypatch.setattr(alerts, "_post_json", lambda url, payload: sent.update(url=url, payload=payload) or True)
    assert alerts._slack("KILL SWITCH", "halted") is True
    assert sent["url"] == "https://hooks.example/abc"
    assert "KILL SWITCH" in sent["payload"]["text"]


def test_slack_alert_noop_without_webhook(monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    assert alerts._slack("x", "y") is False


def test_toast_skipped_off_windows(monkeypatch):
    monkeypatch.setattr(alerts.os, "name", "posix")
    calls = []
    monkeypatch.setattr(alerts.subprocess, "Popen", lambda *a, **k: calls.append(a))
    alerts._toast("t", "m")
    assert calls == []


def test_janitor_skipped_off_windows(monkeypatch):
    monkeypatch.setattr(cycle.os, "name", "posix")
    calls = []
    monkeypatch.setattr(cycle.subprocess, "run", lambda *a, **k: calls.append(a))
    cycle._fire_janitor()
    assert calls == []


def test_janitor_skipped_on_windows_without_schtasks(monkeypatch):
    monkeypatch.setattr(cycle.os, "name", "nt")
    monkeypatch.setattr(cycle.shutil, "which", lambda name: None)
    calls = []
    monkeypatch.setattr(cycle.subprocess, "run", lambda *a, **k: calls.append(a))
    cycle._fire_janitor()
    assert calls == []


def test_janitor_runs_schtasks_on_windows(monkeypatch):
    monkeypatch.setattr(cycle.os, "name", "nt")
    monkeypatch.setattr(cycle.shutil, "which", lambda name: "C:/Windows/System32/schtasks.exe")
    calls = []
    monkeypatch.setattr(cycle.subprocess, "run", lambda *a, **k: calls.append((a, k)))
    cycle._fire_janitor()
    assert len(calls) == 1
    argv = calls[0][0][0]
    assert argv[0] == "schtasks"
    assert argv[1:3] == ["/Run", "/TN"]
