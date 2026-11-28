"""RT-6.17, RT-6.2 step 9 (plan B piece 14c-2b): a session without a Mattermost bot answers
/v1/me with its NATS name. mm_bus_me in bin/mm-agent-lib.sh takes it (a state key,
mm_state_key_re) so the hooks are not quiet, keys the turn and listen files by it, and never
exports it as CB_MM_BOT_USER_ID: mm_bus_uid and bin/mm-agent-statusline.sh read that variable
as a bot id and ignore a NATS name. A 503 names the server's reason (no_bot)."""
import json
import os
import subprocess
import time

from conftest import ROOT
from test_state_key_shell import NATS25, _fake_api, _key_env, _lib

STATUSLINE = ROOT / "bin" / "mm-agent-statusline.sh"


def _me_env(tmp_path, srv, **extra: str) -> dict[str, str]:
    ep = f"http://127.0.0.1:{srv.server_address[1]}"
    return _key_env(tmp_path, f"CB_BUS_ENDPOINT={ep}\nCB_BUS_KEY=k\nCB_BUS_PID=1\n", CB_MM_BOT_USER_ID="", **extra)


def test_me_takes_the_nats_name_and_keys_the_files_by_it(tmp_path):
    srv = _fake_api({"user_id": NATS25, "username": "nats-x", "crown_until": 0, "channel": True})
    try:
        env = _me_env(tmp_path, srv)
        r = _lib('mm_bus_me || exit 9; printf "%s|%s|%s|%s\\n" "$mm_bus_me_id" "${CB_MM_BOT_USER_ID:-}" '
                 '"$CB_MM_BOT_USERNAME" "$CB_BUS_CHANNEL"; mm_turn_state_file; echo; mm_bot_state_file listen', env)
    finally:
        srv.shutdown()
    state = f"{tmp_path}/s/mm-agent-bus"
    assert r.returncode == 0, r.stderr
    assert r.stdout == f"{NATS25}||nats-x|1\n{state}/turn-{NATS25}.env\n{state}/listen-{NATS25}.env"


def test_me_still_refuses_junk(tmp_path):
    for junk in ("agent-x", "A" * 25, "a" * 24, ""):
        srv = _fake_api({"user_id": junk, "username": "x"})
        try:
            assert _lib("mm_bus_me", _me_env(tmp_path, srv)).returncode == 1, junk
        finally:
            srv.shutdown()


def test_mm_bus_uid_ignores_a_nats_name(tmp_path):
    """The prompt hook's local step takes a bot id only: a NATS name in the variable or the
    endpoint file is no bot id (the hook then asks /v1/me)."""
    env = _key_env(tmp_path, f"CB_BUS_ENDPOINT=http://127.0.0.1:1\nCB_BUS_KEY=k\nCB_MM_BOT_USER_ID={NATS25}\n",
                   CB_MM_BOT_USER_ID=NATS25)
    assert _lib("mm_bus_uid", env).returncode == 1


def test_the_status_line_takes_no_nats_name_for_a_bot_id(tmp_path):
    """A NATS name in CB_MM_BOT_USER_ID shows no crown, even with a crown file keyed by it."""
    state = tmp_path / "s" / "mm-agent-bus"
    state.mkdir(parents=True)
    (state / f"coord-{NATS25}.env").write_text(f"CROWN_UNTIL={int(time.time()) + 600}\n")
    env = {"HOME": str(tmp_path), "PATH": os.environ["PATH"], "XDG_STATE_HOME": str(tmp_path / "s"),
           "CB_MM_BOT_USERNAME": "nats-x", "CB_MM_BOT_USER_ID": NATS25}
    r = subprocess.run(["bash", str(STATUSLINE)], input=json.dumps({"session_id": "s"}), env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and r.stdout == "@nats-x"


def test_a_503_names_the_servers_reason(tmp_path):
    lib = ROOT / "bin" / "mm-agent-lib.sh"
    env = {"HOME": str(tmp_path), "PATH": os.environ["PATH"], "CB_BUS_FROM": "x", "CB_BUS_STATUS": "503",
           "CB_BUS_BODY": json.dumps({"error": "no bus session", "no_bot": "the bus is not up yet"})}
    r = subprocess.run(["bash", "-c", 'source "$1"; mm_bus_explain', "_", str(lib)], env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.stderr == "mm-bus: no bus session: the bus is not up yet\n"
    env["CB_BUS_BODY"] = json.dumps({"error": "no bus session"})
    r = subprocess.run(["bash", "-c", 'source "$1"; mm_bus_explain', "_", str(lib)], env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.stderr == "mm-bus: no bot yet: register with the MCP tool session_start first\n"
