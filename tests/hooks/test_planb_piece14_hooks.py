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
import signal
import subprocess
import time

from conftest import ROOT
from test_keyless_session import END, HOST, PY, START, STOP, _alive, _field, _wait_gone

import pytest

pytestmark = pytest.mark.skipif(not PY.exists(), reason="needs mcp/.venv: uv run --directory mcp")

LISTED = {"MM_NATS_HOMES": "~Proj-X, agents"}  # the home (proj-x) and the lobby (agents)


def _registrations(mm) -> list[dict]:
    return [body for method, path, body in mm.writes if method == "POST" and path == "/register/v1/agents"]


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
    pid = int(_field(session, "MM_BUS_HOST_PID"))
    assert _alive(pid)

    # No NATS side (no CB_NATS_URL): the host process cannot decide, so it registers as before,
    # once, under the name preStart chose; preStart itself registered nothing.
    _until(lambda: _registrations(mm))
    assert [b["name"] for b in _registrations(mm)] == [name]

    # postEnd: the host's exit work unregisters the bot it registered; the file goes.
    r = run(STOP, CORRAL_SESSION_ID="sess-defer", **END)
    assert r.code == 0, r.stderr
    if "no start time recorded" in r.stderr:  # ps denied (a sandbox): its watchdog would; we do
        os.kill(pid, signal.SIGTERM)
    assert _wait_gone(pid)
    assert any(m == "DELETE" and p == f"/register/v1/agents/{name}" for m, p, _ in mm.writes)
    assert not session.exists()
    assert "no bot token in the session file" in r.stderr


def test_a_channel_not_listed_registers_in_prestart_as_before(mm, run):
    r = run(START, CORRAL_SESSION_ID="sess-home", **{**HOST, "MM_NATS_HOMES": "proj-x"})
    assert r.code == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["env"]["MM_BOT_USERNAME"].startswith("agent-")
    assert out["status"][0].startswith("registered agent-")
    assert len(_registrations(mm)) == 1
    assert _field(run.state_dir / "sess-home.env", "MM_BOT_TOKEN") == "bot-token-for-tests"


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
