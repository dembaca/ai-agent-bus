"""Plan B piece 14a, the decision, at corral's host hooks (RT-6.18 step 13): a session whose every
channel CB_NATS_HOMES lists starts its host process without a bot; preStart registers nothing and
hands no token on, the host process decides after its NATS read and registers only when a channel
or piece is off or it cannot decide (here: no NATS side at all, so it registers as before); the
stop hook ends it and leaves nothing behind. Any other session registers in preStart as before.
The decision on the records themselves: mcp/tests/test_planb_piece14.py.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time

from conftest import ROOT
from test_keyless_session import END, HOST, PY, START, STOP, _alive, _field, _wait_gone

import pytest

pytestmark = pytest.mark.skipif(not PY.exists(), reason="needs mcp/.venv: uv run --directory mcp")

SWEEP = "bin/mm-agent-sweep.sh"
LISTED = {"MM_NATS_HOMES": "~Proj-X, agents"}  # the home (proj-x) and the lobby (agents)


def _registrations(mm) -> list[dict]:
    return [body for method, path, body in mm.writes if method == "POST" and path == "/register/v1/agents"]


def _last(path, key: str) -> str:
    """The value of key as mm_kv reads it: its last line wins."""
    got = ""
    for line in path.read_text().splitlines() if path.exists() else ():
        if line.startswith(f"{key}="):
            got = line.split("=", 1)[1]
    return got


def _until(cond, timeout: float = 20.0) -> None:
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end, "timed out"
        time.sleep(0.1)


def test_every_channel_listed_starts_the_host_without_a_bot(mm, run):
    r = run(START, CORRAL_SESSION_ID="sess-defer", **{**HOST, **LISTED})
    assert r.code == 0, r.stderr
    out = json.loads(r.stdout)
    env = out["env"]
    assert "MM_BUS_UNAVAILABLE" not in env, r.stderr
    assert "MM_BOT_TOKEN" not in env and "bot-token-for-tests" not in r.stdout
    assert "test-secret" not in r.stdout and "test-secret" not in r.stderr
    assert env["MM_BOT_USERNAME"] == "" and env["MM_BUS_MCP_ENDPOINT"].startswith("tcp://127.0.0.1:")
    assert out["status"][0].startswith("no bot yet (the host decides after the NATS login)")
    assert "No Mattermost bot is registered for you yet" in out["agentNotes"][0]
    assert "RT-6.18 step 13" in r.stderr
    session = run.state_dir / "sess-defer.env"
    name = _field(session, "MM_REG_BOT_NAME")
    assert name and _field(session, "MM_BOT_TOKEN") == ""
    assert "DEFERRED=1" in session.read_text().splitlines()  # the mark postEnd and the sweep read
    pid = int(_field(session, "MM_BUS_HOST_PID"))
    assert _alive(pid)

    # No NATS side (no CB_NATS_URL): the host process cannot decide, so it registers as before,
    # once, under the name preStart chose; preStart itself registered nothing.
    _until(lambda: _registrations(mm))
    assert [b["name"] for b in _registrations(mm)] == [name]
    # Its token goes into the host-side session file at once (nobot.record_token).
    _until(lambda: _last(session, "MM_BOT_TOKEN") == "bot-token-for-tests")

    # postEnd: the host's exit work unregisters the bot it registered; the file goes.
    r = run(STOP, CORRAL_SESSION_ID="sess-defer", **END)
    assert r.code == 0, r.stderr
    if "no start time recorded" in r.stderr:  # ps denied (a sandbox): its watchdog would; we do
        os.kill(pid, signal.SIGTERM)
    assert _wait_gone(pid)
    assert any(m == "DELETE" and p == f"/register/v1/agents/{name}" for m, p, _ in mm.writes)
    assert not session.exists()


def test_a_channel_not_listed_registers_in_prestart_as_before(mm, run):
    r = run(START, CORRAL_SESSION_ID="sess-home", **{**HOST, "MM_NATS_HOMES": "proj-x"})
    assert r.code == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["env"]["MM_BOT_USERNAME"].startswith("agent-")
    assert out["status"][0].startswith("registered agent-")
    assert len(_registrations(mm)) == 1
    assert _field(run.state_dir / "sess-home.env", "MM_BOT_TOKEN") == "bot-token-for-tests"
    # Byte for byte as before piece 14a: the status line and the start hook's last log line.
    user = out["env"]["MM_BOT_USERNAME"]
    assert out["status"][0] == (f"registered {user}, home #proj-x, lobby #agents, "
                                "trusted none (set CB_TRUSTED_HUMANS); token stays on the host")
    assert re.search(rf"session bot {re.escape(user)} ready, its MCP server on the host \(pid \d+\); host copy ",
                     r.stderr), r.stderr
    assert "without a bot yet" not in r.stderr and "DEFERRED" not in (run.state_dir / "sess-home.env").read_text()
    r = run(STOP, CORRAL_SESSION_ID="sess-home", **END)
    assert r.code == 0, r.stderr


def test_a_deferred_session_takes_its_trusted_humans_as_configured(mm, run):
    r = run(START, CORRAL_SESSION_ID="sess-trust", MM_TRUSTED_HUMANS="dembaca,kollegin", **{**HOST, **LISTED})
    assert r.code == 0, r.stderr
    out = json.loads(r.stdout)
    assert "trusted @dembaca, @kollegin" in out["status"][0]
    assert not any(p.startswith("/api/v4/users/username/") for p in mm.reads)  # no token to look up with
    r = run(STOP, CORRAL_SESSION_ID="sess-trust", **END)
    assert r.code == 0, r.stderr


def test_a_failed_unregister_leaves_the_token_for_the_sweep(mm, run):
    """The exit's unregister of the bot the host process registered fails (the registrar down):
    postEnd keeps the session file with the token the host wrote, and the sweep cleans up."""
    r = run(START, CORRAL_SESSION_ID="sess-orphan", **{**HOST, **LISTED})
    assert r.code == 0, r.stderr
    session = run.state_dir / "sess-orphan.env"
    name = _field(session, "MM_REG_BOT_NAME")
    pid = int(_field(session, "MM_BUS_HOST_PID"))
    _until(lambda: _last(session, "MM_BOT_TOKEN") == "bot-token-for-tests")
    mm.unregister_error["bot-token-for-tests"] = (503, "registrar down")
    r = run(STOP, CORRAL_SESSION_ID="sess-orphan", **END)
    assert r.code == 0, r.stderr
    if "no start time recorded" in r.stderr:
        os.kill(pid, signal.SIGTERM)
    assert _wait_gone(pid)
    assert session.exists() and _last(session, "MM_BOT_TOKEN") == "bot-token-for-tests", r.stderr
    assert f"could not unregister {name}" in r.stderr and "mm-agent-sweep.sh" in r.stderr

    mm.unregister_error.clear()
    deletes = sum(1 for m, p, _ in mm.writes if m == "DELETE" and p == f"/register/v1/agents/{name}")
    r = run(SWEEP, "--apply", "--force", MM_BOT_TOKEN=None)
    assert r.code == 0, r.stdout + r.stderr
    assert sum(1 for m, p, _ in mm.writes if m == "DELETE" and p == f"/register/v1/agents/{name}") == deletes + 1
    assert not session.exists()


def test_the_sweep_removes_a_deferred_file_without_a_bot(mm, run):
    run.state_dir.mkdir(parents=True, exist_ok=True)
    f = run.state_dir / "sess-gone.env"
    f.write_text("MM_REG_BOT_NAME=host-gone\nCB_MM_BOT_TOKEN=\nMM_BOT_TOKEN=\nDEFERRED=1\nCB_SESSION_PID=999999\n")
    r = run(SWEEP, "--apply", MM_BOT_TOKEN=None)
    assert r.code == 0, r.stdout + r.stderr
    assert not f.exists()
    assert not any(m == "DELETE" for m, _p, _ in mm.writes)


# The start line to the host process (hosted.read_start) of a session with a bot, as before 14a.
PRE_14A_FILTER = r'''
     {key: $key,
      env: ((env | with_entries(select(.key | test(
              "^(CB|MM|MATTERMOST)_[A-Z0-9_]+$|^(CORRAL_WORKDIR|CORRAL_SESSION_ID|CORRAL_AGENT|TZ|SSL_CERT_FILE|SSL_CERT_DIR)$|^(HTTPS?|NO|ALL)_PROXY$|^(https?|no|all)_proxy$")))
             | del(.MM_REG_SECRET, .MM_REG_SECRET_GOPASS, .MM_REG_BOT_NAME,
                   .CB_AGENT_SESSION_FILE, .MM_AGENT_SESSION_FILE, .CB_BUS_KEY, .MM_BUS_KEY,
                   .CB_MM_BOT_TOKEN, .MM_BOT_TOKEN, .MATTERMOST_TOKEN, .CB_BUS_KEPT_SID, .MM_BUS_KEPT_SID,
                   .CB_BUS_MCP_ENDPOINT, .MM_BUS_MCP_ENDPOINT, .CB_BUS_ENDPOINT, .MM_BUS_ENDPOINT,
                   .CB_BUS_UNAVAILABLE, .MM_BUS_UNAVAILABLE))
            + $base + {CB_MM_BOT_TOKEN: $tok, MM_BOT_TOKEN: $tok}
            + (if $kept != "" then {CB_BUS_KEPT_SID: $kept, MM_BUS_KEPT_SID: $kept} else {} end))}'''


@pytest.mark.parametrize("kept", ["", "0123456789abcdef"])
def test_the_start_line_with_a_token_is_byte_identical_to_before(tmp_path, kept):
    text = (ROOT / START).read_text()
    m = re.search(r"--rawfile sec /dev/fd/6 '(.*?)' \\\n", text, re.S)
    assert m, "the start line's jq filter moved"
    files = {"key": "k" * 32, "tok": "bot-token-for-tests", "sec": ""}
    for k, v in files.items():
        (tmp_path / k).write_text(v)
    env = {"PATH": os.environ["PATH"], "MM_CHANNEL": "agents", "CB_MM_BOT_TOKEN": "x", "MM_REG_SECRET": "s",
           "HTTPS_PROXY": "http://p", "OTHER": "o"}
    base = json.dumps({"CB_MM_TEAM_ID": "t"})
    common = ["jq", "-nc", "--rawfile", "key", str(tmp_path / "key"), "--rawfile", "tok", str(tmp_path / "tok"),
              "--argjson", "base", base, "--arg", "kept", kept]
    now = subprocess.run([*common, "--arg", "defer", "0", "--arg", "name", "n", "--arg", "display", "d",
                          "--rawfile", "sec", str(tmp_path / "sec"), m[1]],
                         env=env, capture_output=True, timeout=30, check=True).stdout
    before = subprocess.run([*common, PRE_14A_FILTER], env=env, capture_output=True, timeout=30, check=True).stdout
    assert now == before and b"bot-token-for-tests" in now and b'"s"' not in now


@pytest.mark.parametrize(("homes", "project", "lobby", "want"), [
    ("proj-x agents", "proj-x", "agents", 0),
    ("~PROJ-X,~Agents", "~proj-x", "agents", 0),
    ("proj-x guest agents", "proj-x,guest", "agents", 0),
    ("proj-x agents", "proj-x,guest", "agents", 1),  # a guest not over NATS
    ("proj-x", "proj-x", "agents", 1),  # the lobby not over NATS
    ("", "proj-x", "agents", 1),
    ("agents", "", "agents", 0),  # no project channel: the lobby is the home
    ("*", "proj-x", "agents", 1),
])
def test_the_pre_filter_reads_cb_nats_homes_as_the_server_does(tmp_path, homes, project, lobby, want):
    script = f'source "{ROOT}/bin/mm-agent-lib.sh"; mm_bot_deferrable'
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "CB_NATS_HOMES": homes,
           "CB_MM_PROJECT_CHANNEL": project, "CB_MM_CHANNEL": lobby}
    r = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=30, cwd=tmp_path)
    assert r.returncode == want, r.stderr
