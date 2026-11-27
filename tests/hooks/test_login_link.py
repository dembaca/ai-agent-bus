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


def test_the_line_is_printed_and_the_grant_marked(tmp_path):
    """RT-6.34: a waiting grant becomes one systemMessage with the link and the code —
    no token — and the shown mark goes out after it. The hook runs with the stub as its
    hook API, so this pins the display itself, not only its silence."""
    import os
    import subprocess
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from pathlib import Path

    seen: list[str] = []

    class Stub(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server's name
            body = json.dumps({"link": "https://kc.example/device?user_code=ABCD-EFGH",
                               "user_code": "ABCD-EFGH", "expires_in": 600}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            seen.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *_args: object) -> None:
            pass

    srv = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    root = Path(__file__).resolve().parents[2]
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "XDG_STATE_HOME": str(tmp_path / "state"),
           "MM_CHAT_URL": "http://127.0.0.1:9", "MM_BOT_TOKEN": "t", "MM_BOT_USER_ID": "u",
           "MM_TEAM_ID": "t", "MM_CHANNEL_ID": "c",
           "CB_BUS_ENDPOINT": f"http://127.0.0.1:{srv.server_port}", "CB_BUS_KEY": "k" * 32}
    try:
        p = subprocess.run(["bash", str(root / LINK)], input="{}", env=env,
                           capture_output=True, text=True, timeout=30)
    finally:
        srv.shutdown()
    assert p.stdout.strip(), f"no output: {p.returncode}, {p.stderr[:300]!r}, {seen}"
    out = json.loads(p.stdout)
    assert "kc.example/device?user_code=ABCD-EFGH" in out["systemMessage"]
    assert "ABCD-EFGH" in out["systemMessage"] and "token" not in out["systemMessage"].lower()
    assert "/v1/login/shown" in seen


def test_the_settings_notes_are_shown_without_a_login(tmp_path):
    """RT-6.34 step 4, #261: unknown CB_ names and a home outside CB_NATS_HOMES reach the
    TUI once at start although no approval waits, and nothing is marked shown."""
    import os
    import subprocess
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen: list[str] = []
    notes = ["unknown setting CB_PROJECT_CHANNEL (did you mean CB_MM_PROJECT_CHANNEL?)",
             "home ~agents is not in CB_NATS_HOMES (vtpm-mds, bgl-infra): this session stays on "
             "Mattermost and starts no NATS login"]

    class Stub(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server's name
            body = json.dumps({"notes": notes}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            seen.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *_args: object) -> None:
            pass

    srv = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    root = Path(__file__).resolve().parents[2]
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "XDG_STATE_HOME": str(tmp_path / "state"),
           "MM_CHAT_URL": "http://127.0.0.1:9", "MM_BOT_TOKEN": "t", "MM_BOT_USER_ID": "u",
           "MM_TEAM_ID": "t", "MM_CHANNEL_ID": "c",
           "CB_BUS_ENDPOINT": f"http://127.0.0.1:{srv.server_port}", "CB_BUS_KEY": "k" * 32}
    try:
        p = subprocess.run(["bash", str(root / LINK)], input="{}", env=env,
                           capture_output=True, text=True, timeout=30)
    finally:
        srv.shutdown()
    out = json.loads(p.stdout)
    assert "did you mean CB_MM_PROJECT_CHANNEL?" in out["systemMessage"]
    assert "starts no NATS login" in out["systemMessage"]
    assert "/v1/login/shown" not in seen
