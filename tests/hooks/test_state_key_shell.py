"""RT-6.2 (state keys, ADR-014 phase 5 piece 2b): mm_bot_state_file in bin/mm-agent-lib.sh names
a state file by a bot id (26 characters) or by a NATS name (natsbus.AGENT_RE), like
lease._file and askroute._path, and by nothing else."""
import os
import subprocess

import pytest

from conftest import ROOT

LIB = ROOT / "bin" / "mm-agent-lib.sh"
NATS = "agent-" + "a2" * 13  # the name before #276 dropped the prefix: no state key any more
BOT = "x" * 26
NATS25 = "a2c4e6g7" * 3 + "h"  # 25 base32 characters, no hyphen (#276)


def _file(kind: str, key: str, tmp_path) -> subprocess.CompletedProcess:
    env = {"HOME": str(tmp_path), "PATH": os.environ["PATH"], "XDG_STATE_HOME": str(tmp_path / "s")}
    return subprocess.run(["bash", "-c", 'source "$1"; mm_bot_state_file "$2" "$3"', "_", str(LIB), kind, key],
                          env=env, capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("key", [BOT, NATS25])
def test_a_bot_id_and_a_nats_name_name_a_state_file(tmp_path, key):
    r = _file("listen", key, tmp_path)
    assert r.returncode == 0
    assert r.stdout == f"{tmp_path}/s/mm-agent-bus/listen-{key}.env"


@pytest.mark.parametrize("key", ["", "../x", "a/b", "a.b", "-a", "a b", "a" * 129, "Agent-x", "A" * 26, NATS])
def test_junk_is_no_key(tmp_path, key):
    assert _file("listen", key, tmp_path).returncode == 1


# --- plan B piece 14c-1: the scripts follow the kept state's key (mm_state_key) -------------

import hashlib  # noqa: E402
import re  # noqa: E402

SID = "sess-14c"


def _key_env(tmp_path, endpoint: str | None, **extra: str) -> dict[str, str]:
    state = tmp_path / "s" / "mm-agent-bus"
    state.mkdir(parents=True, exist_ok=True)
    if endpoint is not None:
        key = hashlib.sha1(SID.encode()).hexdigest()[:16]
        (state / f"endpoint-sid-{key}.env").write_text(endpoint)
    return {"HOME": str(tmp_path), "PATH": os.environ["PATH"], "XDG_STATE_HOME": str(tmp_path / "s"),
            "CLAUDE_CODE_SESSION_ID": SID, "CB_MM_BOT_USER_ID": BOT, **extra}


def _lib(script: str, env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", f'source "$1"; shift; {script}', "_", str(LIB), *args],
                          env=env, capture_output=True, text=True, timeout=30)


def test_the_key_is_the_bot_id_while_the_server_names_none(tmp_path):
    """Switch off: the endpoint file has no CB_STATE_KEY, every file keeps its bot id name."""
    env = _key_env(tmp_path, "CB_BUS_ENDPOINT=http://127.0.0.1:1\nCB_BUS_KEY=k\n")
    assert _lib("mm_state_key", env).stdout == BOT
    state = f"{tmp_path}/s/mm-agent-bus"
    assert _lib("mm_turn_state_file", env).stdout == f"{state}/turn-{BOT}.env"
    assert _lib("mm_bot_state_file listen", env).stdout == f"{state}/listen-{BOT}.env"


def test_the_key_is_the_name_the_endpoint_file_names(tmp_path):
    env = _key_env(tmp_path, f"CB_BUS_ENDPOINT=http://127.0.0.1:1\nCB_BUS_KEY=k\nCB_STATE_KEY={NATS25}\n")
    state = f"{tmp_path}/s/mm-agent-bus"
    assert _lib("mm_state_key", env).stdout == NATS25
    assert _lib("mm_turn_state_file", env).stdout == f"{state}/turn-{NATS25}.env"
    for kind in ("listen", "update", "presence", "coord", "channel"):
        assert _lib(f"mm_bot_state_file {kind}", env).stdout == f"{state}/{kind}-{NATS25}.env"
    # An explicit bot id gives way to the server's key too (mm-agent-status.sh, -listen.sh).
    assert _lib('mm_state_key "$1"', env, "y" * 26).stdout == NATS25
    r = _lib("mm_stat wake reason=digest", env)
    assert r.returncode == 0
    assert '"k":"wake"' in (tmp_path / "s" / "mm-agent-bus" / f"stats-{NATS25}.jsonl").read_text()


def test_a_junk_key_falls_back_to_the_bot_id(tmp_path):
    env = _key_env(tmp_path, "CB_STATE_KEY=../x\n")
    assert _lib("mm_state_key", env).stdout == BOT
    env = _key_env(tmp_path, "CB_STATE_KEY=Agent-X\n", CB_MM_BOT_USER_ID="")
    assert _lib("mm_state_key", env).returncode == 1


def test_without_an_endpoint_file_me_names_the_key(tmp_path):
    """A keyless session (RT-6.18) has no endpoint file here: mm_bus_me keeps /v1/me's state_key."""
    env = _key_env(tmp_path, None)
    assert _lib('mm_state_key_me="$1"; mm_state_key', env, NATS25).stdout == NATS25
    assert _lib("mm_state_key", env).stdout == BOT
    # A CB_STATE_KEY inherited from the environment is no key (switch off: the bot id).
    assert _lib("mm_state_key", {**env, "CB_STATE_KEY": NATS25}).stdout == BOT


# Every per-bot state path the scripts name: a kind, a dash and a bot id, or mm_bot_state_file
# handed a bot id. Only mm_state_key picks the key; bin/mm-agent-sweep.sh matches file names by
# pattern and is left out.
_BOT_PATH = re.compile(r'[a-z]+-\$\{?(CB_MM_BOT_USER_ID|MM_BOT_USER_ID|uid|UID_|ME)\}?\.(env|jsonl|log|lock)'
                       r'|mm_bot_state_file [a-z]+ "\$\{?(CB_MM_BOT_USER_ID|MM_BOT_USER_ID|uid|UID_|ME|1)')


def test_no_script_keys_a_state_file_by_the_bot_id_itself():
    files = [*ROOT.glob("bin/*.sh"), *ROOT.glob("hooks/*.sh"), *ROOT.glob("adapters/*/*.sh")]
    assert len(files) > 10
    hits = [f"{f.relative_to(ROOT)}:{n}: {line.strip()}" for f in files if f.name != "mm-agent-sweep.sh"
            for n, line in enumerate(f.read_text().splitlines(), 1) if _BOT_PATH.search(line)]
    assert hits == []


def test_the_guard_finds_the_paths_it_was_written_for():
    for line in ('printf \'%s\' "$(mm_state_dir)/turn-${uid}.env"', 'f="$(mm_state_dir)/stats-${CB_MM_BOT_USER_ID}.jsonl"',
                 '"${STATE}/coord-${uid}.env"', 'mm_bot_state_file presence "$UID_"', 'mm_bot_state_file listen "$ME"'):
        assert _BOT_PATH.search(line), line


def _fake_api(body: dict):
    """A hook API (RT-6.17) that answers /v1/me with `body`, every other call with {}."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _answer(self):
            b = json.dumps(body if self.path.startswith("/v1/me") else {}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        do_GET = do_POST = _answer

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# The first lines of hooks/turn-status.sh and hooks/stop-inbox-check.sh: how each learns who
# the session is before it names its turn and listen files.
_PROMPT_START = "mm_bus_uid || mm_bus_me || exit 1"
_STOP_START = "mm_bus_me || exit 1"


@pytest.mark.parametrize("me_names_it", [True, False])
def test_the_prompt_and_the_stop_hook_name_the_same_files(tmp_path, me_names_it):
    """Review of #328: the prompt hook learns the bot from the endpoint file, the Stop hook
    from /v1/me; both name the same turn- and listen- file, since the endpoint file names the
    key (CB_STATE_KEY, written at the latch) whatever /v1/me says."""
    srv = _fake_api({"user_id": BOT, "username": "agent-x", "crown_until": 0,
                     **({"state_key": NATS25} if me_names_it else {})})
    try:
        ep = f"http://127.0.0.1:{srv.server_address[1]}"
        env = _key_env(tmp_path, f"CB_BUS_ENDPOINT={ep}\nCB_BUS_KEY=k\nCB_BUS_PID=1\n"
                                 f"CB_MM_BOT_USER_ID={BOT}\nCB_STATE_KEY={NATS25}\n", CB_MM_BOT_USER_ID="")
        names = 'mm_turn_state_file; echo; mm_bot_state_file listen'
        prompt = _lib(f"{_PROMPT_START}; {names}", env)
        stop = _lib(f"{_STOP_START}; {names}", env)
    finally:
        srv.shutdown()
    state = f"{tmp_path}/s/mm-agent-bus"
    assert prompt.stdout == stop.stdout == f"{state}/turn-{NATS25}.env\n{state}/listen-{NATS25}.env"


def test_the_hooks_preambles_are_the_ones_tested():
    assert _PROMPT_START.removesuffix(" || exit 1") + " || exit 0" in (ROOT / "hooks" / "turn-status.sh").read_text()
    assert _STOP_START.removesuffix(" || exit 1") + " || exit 0" in (ROOT / "hooks" / "stop-inbox-check.sh").read_text()


def test_the_prompt_hook_clears_the_name_keyed_turn_from_the_endpoint_file(tmp_path):
    """No server: the real prompt hook (hooks/turn-status.sh) still finds the name-keyed turn
    and listen files by the endpoint file's CB_STATE_KEY and removes them."""
    env = _key_env(tmp_path, f"CB_BUS_ENDPOINT=http://127.0.0.1:9\nCB_BUS_KEY=x\nCB_BUS_PID=999999\n"
                             f"CB_MM_BOT_USER_ID={BOT}\nCB_STATE_KEY={NATS25}\n", CB_MM_BOT_USER_ID="")
    state = tmp_path / "s" / "mm-agent-bus"
    (state / f"turn-{NATS25}.env").write_text("DRAINS=1\n")
    (state / f"listen-{NATS25}.env").write_text("UNTIL=1\nENDED=\n")
    r = subprocess.run(["bash", str(ROOT / "hooks" / "turn-status.sh")], input="{}", env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
    assert not (state / f"turn-{NATS25}.env").exists()
    assert not (state / f"listen-{NATS25}.env").exists()


def test_the_state_key_takes_only_a_bot_id_or_a_nats_name(tmp_path):
    """RT-6.2 step 9: mm_state_key and mm_bot_state_file take exactly the two forms
    session_file.key_ok does, a bot id (26 [a-z0-9]) or a NATS name (25 [a-z2-7]), since the
    key names files; anything else falls back to the bot id, or fails without one."""
    env = _key_env(tmp_path, None)
    for good in ("y" * 26, NATS25):
        assert _lib('mm_state_key_me="$1"; mm_state_key', env, good).stdout == good
        assert _lib('mm_bot_state_file turn "$1"', env, good).stdout.endswith(f"/turn-{good}.env")
    bad = ["a" * 24, "a" * 27, "b" * 24 + "1", "A" * 26, "Y" * 25, "a" * 25 + "_",
           "a" * 12 + "-" + "a" * 13, "a" * 12 + "." + "a" * 13, "a" * 12 + "/" + "a" * 12]
    for key in bad:
        assert _lib('mm_state_key_me="$1"; mm_state_key', env, key).stdout == BOT, key
        assert _lib('mm_bot_state_file turn "$1"', env, key).returncode == 1, key
    no_bot = {k: v for k, v in env.items() if k != "CB_MM_BOT_USER_ID"}
    assert _lib('mm_state_key_me="$1"; mm_state_key', no_bot, "a" * 27).returncode == 1
