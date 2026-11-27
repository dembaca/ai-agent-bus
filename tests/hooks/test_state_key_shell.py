"""RT-6.2 (state keys, ADR-014 phase 5 piece 2b): mm_bot_state_file in bin/mm-agent-lib.sh names
a state file by a bot id (26 characters) or by a NATS name (natsbus.AGENT_RE), like
lease._file and askroute._path, and by nothing else."""
import os
import subprocess

import pytest

from conftest import ROOT

LIB = ROOT / "bin" / "mm-agent-lib.sh"
NATS = "agent-" + "a2" * 13
BOT = "x" * 26
NATS25 = "a2c4e6g7" * 3 + "h"  # 25 base32 characters, no hyphen (#276)


def _file(kind: str, key: str, tmp_path) -> subprocess.CompletedProcess:
    env = {"HOME": str(tmp_path), "PATH": os.environ["PATH"], "XDG_STATE_HOME": str(tmp_path / "s")}
    return subprocess.run(["bash", "-c", 'source "$1"; mm_bot_state_file "$2" "$3"', "_", str(LIB), kind, key],
                          env=env, capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("key", [BOT, NATS, NATS25])
def test_a_bot_id_and_a_nats_name_name_a_state_file(tmp_path, key):
    r = _file("listen", key, tmp_path)
    assert r.returncode == 0
    assert r.stdout == f"{tmp_path}/s/mm-agent-bus/listen-{key}.env"


@pytest.mark.parametrize("key", ["", "../x", "a/b", "a.b", "-a", "a b", "a" * 129, "Agent-x", "A" * 26])
def test_junk_is_no_key(tmp_path, key):
    assert _file("listen", key, tmp_path).returncode == 1
