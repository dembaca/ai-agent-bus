"""RT-6.34: the login approval in the terminal (#198) — the start hook's side."""

from __future__ import annotations

import json
from pathlib import Path

LINK = "hooks/session-start-link.sh"


def test_no_login_waits_no_line(mm, run):
    """Without a waiting approval the hook prints nothing: the login is off in the
    test server, and a session's start must not carry an empty notice."""
    r = run(LINK)
    assert r.code == 0 and r.stdout.strip() == ""


def test_a_missing_server_prints_nothing_and_exits_0(run, monkeypatch):
    """The hook never holds a start: no endpoint, no answer, exit 0, no output."""
    monkeypatch.setenv("CB_BUS_ENDPOINT", "http://127.0.0.1:1")
    monkeypatch.setenv("CB_BUS_KEY", "k" * 32)
    monkeypatch.delenv("MM_BUS_ENDPOINT", raising=False)
    monkeypatch.delenv("MM_BUS_KEY", raising=False)
    r = run(LINK)
    assert r.code == 0 and r.stdout.strip() == ""


def test_the_hook_runs_at_a_sessions_start():
    """The SessionStart entry is what reaches the TUI: startup, resume and clear,
    bounded at 5 s so a slow server never holds the start."""
    page = Path(__file__).resolve().parents[2] / "hooks" / "hooks.json"
    hooks = json.loads(page.read_text())
    entries = [h for h in hooks["hooks"]["SessionStart"] if "session-start-link.sh" in json.dumps(h)]
    assert entries and entries[0]["matcher"] == "startup|resume|clear"
    assert entries[0]["hooks"][0]["timeout"] == 5
