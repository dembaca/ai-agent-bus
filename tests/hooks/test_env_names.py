"""The CB_ names in the shell (#169, docs/env-names.md): the reading rule's twin in
bin/mm-agent-lib.sh (mm_env, mm_env_all), the lists that carry both spellings, and the
values the bus writes for itself under both names."""
import json
import os
import re
import subprocess
from pathlib import Path

from conftest import ROOT

LIB = ROOT / "bin" / "mm-agent-lib.sh"


def _bash(script: str, env: dict[str, str], tmp_path: Path, *args: str, stdin: str = ""):
    base = {"HOME": str(tmp_path), "PATH": os.environ["PATH"], "XDG_STATE_HOME": str(tmp_path / "state")}
    return subprocess.run(["bash", "-c", script, "_", str(LIB), *args], env={**base, **env},
                          input=stdin, capture_output=True, text=True, timeout=30)


SHOW = 'source "$1"; echo "new=${CB_MM_CHAT_URL-unset} old=${MM_CHAT_URL-unset}"'


def test_the_new_name_alone_is_read_without_a_word(tmp_path):
    r = _bash(SHOW, {"CB_MM_CHAT_URL": "https://a"}, tmp_path)
    assert r.returncode == 0 and r.stderr == ""
    # Written under the old name too, for a child of an older version.
    assert r.stdout.strip() == "new=https://a old=https://a"


def test_the_old_name_alone_is_read_with_one_warning(tmp_path):
    r = _bash(SHOW + '; mm_env CB_MM_CHAT_URL MM_CHAT_URL MATTERMOST_URL', {"MM_CHAT_URL": "https://a"},
              tmp_path)
    assert r.returncode == 0
    assert r.stdout.strip() == "new=https://a old=https://a"
    assert r.stderr.count("MM_CHAT_URL is deprecated: set CB_MM_CHAT_URL") == 1


def test_both_names_agreeing_pass_without_a_warning(tmp_path):
    r = _bash(SHOW, {"CB_MM_CHAT_URL": "https://a", "MM_CHAT_URL": "https://a",
                     "MATTERMOST_URL": "https://a"}, tmp_path)
    assert r.returncode == 0 and r.stderr == ""


def test_both_names_differing_refuse_and_name_both_never_a_value(tmp_path):
    r = _bash(SHOW, {"CB_MM_BOT_TOKEN": "tok-new-secret", "MM_BOT_TOKEN": "tok-old-secret"}, tmp_path)
    assert r.returncode == 2 and r.stdout == ""
    assert "CB_MM_BOT_TOKEN and MM_BOT_TOKEN are set to different values" in r.stderr
    assert "secret" not in r.stderr


def test_the_alias_is_a_third_name_of_the_same_setting(tmp_path):
    r = _bash('source "$1"; echo "$CB_MM_BOT_TOKEN" | shasum', {"MATTERMOST_TOKEN": "t"}, tmp_path)
    assert r.returncode == 0 and "MATTERMOST_TOKEN is deprecated: set CB_MM_BOT_TOKEN" in r.stderr
    r = _bash(SHOW, {"MM_CHAT_URL": "https://a", "MATTERMOST_URL": "https://b"}, tmp_path)
    assert r.returncode == 2
    assert "MM_CHAT_URL and MATTERMOST_URL are set to different values: set only CB_MM_CHAT_URL" in r.stderr


def test_empty_counts_as_set(tmp_path):
    # An empty new name never falls back: next to a value it is a disagreement.
    r = _bash(SHOW, {"CB_MM_CHAT_URL": "", "MM_CHAT_URL": "https://a"}, tmp_path)
    assert r.returncode == 2
    # MM_RELEASE_SIGNERS set but empty refuses the update (mm_release_signers): so does its new name.
    r = _bash('source "$1"; echo "${CB_RELEASE_SIGNERS+set}"; mm_release_signers "$2" || echo "refused: $CB_REL_ERR"',
              {"MM_RELEASE_SIGNERS": ""}, tmp_path, str(tmp_path))
    assert r.stdout.splitlines()[0] == "set"
    assert "refused: CB_RELEASE_SIGNERS" in r.stdout


def test_an_unexpanded_placeholder_counts_as_unset(tmp_path):
    r = _bash(SHOW, {"CB_MM_CHAT_URL": "${CB_MM_CHAT_URL}", "MM_CHAT_URL": "https://a"}, tmp_path)
    assert r.returncode == 0 and r.stdout.strip() == "new=https://a old=https://a"
    r = _bash(SHOW, {"CB_MM_CHAT_URL": "${CB_MM_CHAT_URL}"}, tmp_path)
    assert r.returncode == 0 and r.stdout.strip() == "new=unset old=unset"


def test_a_quiet_hook_logs_to_its_file_and_decides_itself(tmp_path):
    script = 'CB_ENV_QUIET=1 CB_ENV_DEFER=1 source "$1"; echo "err=$CB_ENV_ERR"'
    r = _bash(script, {"MM_TEAM": "t", "CB_MM_CHANNEL": "a", "MM_CHANNEL": "b"}, tmp_path)
    assert r.returncode == 0 and r.stderr == ""
    assert r.stdout.strip() == "err=CB_MM_CHANNEL and MM_CHANNEL are set to different values: set only CB_MM_CHANNEL"
    log = (tmp_path / "state" / "mm-agent-bus" / "env-names.log").read_text()
    assert "MM_TEAM is deprecated: set CB_MM_TEAM" in log and "refused: CB_MM_CHANNEL and MM_CHANNEL" in log


def test_a_turn_hook_with_an_old_name_stays_quiet(tmp_path):
    r = subprocess.run(["bash", str(ROOT / "hooks" / "turn-status.sh")], input="{}",
                       env={"HOME": str(tmp_path), "PATH": os.environ["PATH"],
                            "XDG_STATE_HOME": str(tmp_path / "state"), "MM_LISTEN_SEC": "60"},
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
    log = (tmp_path / "state" / "mm-agent-bus" / "env-names.log").read_text()
    assert "MM_LISTEN_SEC is deprecated: set CB_LISTEN_SEC" in log


# --- values the bus writes for itself -----------------------------------------------

def test_the_internal_set_is_the_servers():
    """CB_ENV_INTERNAL in the lib is config.INTERNAL of the MCP server, name for name."""
    import ast
    src = (ROOT / "mcp" / "src" / "mattermost_ai_agent_bus" / "config.py").read_text()
    node = next(n for n in ast.parse(src).body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "INTERNAL" for t in n.targets))
    python = set(ast.literal_eval(node.value.args[0]))
    shell = set(re.search(r'^CB_ENV_INTERNAL="([^"]*)"$', LIB.read_text(), re.M)[1].split())
    assert shell == python


def test_an_internal_value_under_its_old_name_is_read_quietly(tmp_path):
    r = _bash('source "$1"; echo "$CB_BUS_ENDPOINT $CB_BUS_DEADLINE"',
              {"MM_BUS_ENDPOINT": "http://127.0.0.1:9", "MM_BUS_DEADLINE": "5"}, tmp_path)
    assert r.returncode == 0 and r.stderr == ""
    assert r.stdout.split() == ["http://127.0.0.1:9", "5"]


def test_the_session_file_is_written_under_both_names_and_read_new_first(tmp_path):
    f = tmp_path / "s.env"
    r = _bash('source "$1"; MM_REG_BOT_NAME=x CB_MM_BOT_USERNAME=agent-x CB_MM_BOT_USER_ID=u '
              'CB_MM_BOT_TOKEN=t CB_MM_TEAM=team CB_MM_CHANNEL=lobby mm_write_session_file',
              {"CB_AGENT_SESSION_FILE": str(f), "CB_MM_CHAT_URL": "https://a"}, tmp_path)
    assert r.returncode == 0, r.stderr
    keys = dict(line.split("=", 1) for line in f.read_text().splitlines() if "=" in line)
    for new, old in (("CB_MM_CHAT_URL", "MM_CHAT_URL"), ("CB_MM_BOT_USERNAME", "MM_BOT_USERNAME"),
                     ("CB_MM_BOT_USER_ID", "MM_BOT_USER_ID"), ("CB_MM_BOT_TOKEN", "MM_BOT_TOKEN"),
                     ("CB_MM_TEAM", "MM_TEAM"), ("CB_MM_CHANNEL", "MM_CHANNEL")):
        assert keys[new] == keys[old], new
    assert keys["MATTERMOST_URL"] == "https://a" and keys["MATTERMOST_TOKEN"] == keys["CB_MM_BOT_TOKEN"]
    # An old file (old keys only) and a mixed one: the new key wins, both names come out.
    f.write_text("MM_BOT_USERNAME=old-name\nCB_MM_TEAM=new-team\nMM_TEAM=old-team\nMATTERMOST_URL=https://o\n")
    r = _bash('source "$1"; mm_load_env_file "$2"; echo "$CB_MM_BOT_USERNAME $MM_BOT_USERNAME '
              '$CB_MM_TEAM $MM_TEAM $CB_MM_CHAT_URL $MM_CHAT_URL"', {}, tmp_path, str(f))
    assert r.stdout.split() == ["old-name", "old-name", "new-team", "new-team", "https://o", "https://o"]


def test_the_export_lines_carry_both_names(tmp_path):
    r = _bash('source "$1"; MM_REG_BOT_NAME=x CB_MM_BOT_USER_ID=u CB_MM_BOT_TOKEN=t mm_emit_session_exports',
              {"CB_AGENT_SESSION_FILE": str(tmp_path / "s.env"), "CB_MM_CHAT_URL": "https://a"}, tmp_path)
    names = set(re.findall(r"^export (\w+)=", r.stdout, re.M))
    assert {"CB_MM_CHAT_URL", "MM_CHAT_URL", "MATTERMOST_URL", "CB_MM_BOT_USER_ID", "MM_BOT_USER_ID",
            "CB_MM_BOT_TOKEN", "MM_BOT_TOKEN", "MATTERMOST_TOKEN", "CB_AGENT_SESSION_FILE",
            "MM_AGENT_SESSION_FILE"} <= names


def test_a_file_the_bus_wrote_is_read_new_key_first(tmp_path):
    f = tmp_path / "e.env"
    f.write_text("MM_BUS_PID=1\nCB_BUS_PID=2\nMM_SESSION_PID=3\n")
    r = _bash('source "$1"; echo "$(mm_kv "$2" CB_BUS_PID MM_BUS_PID) $(mm_kv "$2" CB_SESSION_PID MM_SESSION_PID)"',
              {}, tmp_path, str(f))
    assert r.stdout.split() == ["2", "3"]


def test_the_token_fd_goes_under_both_names_and_either_is_read(tmp_path):
    r = _bash('source "$1"; CB_MM_BOT_TOKEN=t; mm_with_token bash -c \'echo "$CB_MM_BOT_TOKEN_FD $MM_BOT_TOKEN_FD"\'',
              {}, tmp_path)
    assert r.stdout.split() == ["3", "3"]
    # An older caller names only MM_BOT_TOKEN_FD; the token stays unexported.
    r = _bash('exec 3<<<"tok"; MM_BOT_TOKEN_FD=3 source "$1"; [[ "$CB_MM_BOT_TOKEN" == tok ]] && echo read; '
              'env | grep -c "BOT_TOKEN=" || true', {}, tmp_path)
    assert r.stdout.split() == ["read", "0"]


def test_the_needs_grammar_takes_cb_names(tmp_path):
    r = _bash('source "$1"; for t in "$@"; do mm_release_needs_ok "$t" && echo ok || echo no; done', {}, tmp_path,
              "CB_RELEASE_SIGNERS set.", "MM_PROPS_PREFIX alike on every server", "XX_FOO set")
    assert r.stdout.split()[-3:] == ["ok", "ok", "no"]


# --- lists of names: both spellings ---------------------------------------------------

def test_the_start_line_filter_and_deny_list_carry_both_spellings():
    text = (ROOT / "adapters" / "corral" / "10-mm-session-start.sh").read_text()
    assert '"^(CB|MM|MATTERMOST)_[A-Z0-9_]+$|' in text
    deny = re.search(r"\| del\(([^)]*)\)", text)[1]
    for new, old in (("CB_AGENT_SESSION_FILE", "MM_AGENT_SESSION_FILE"), ("CB_BUS_KEY", "MM_BUS_KEY"),
                     ("CB_MM_BOT_TOKEN", "MM_BOT_TOKEN"), ("CB_BUS_KEPT_SID", "MM_BUS_KEPT_SID"),
                     ("CB_BUS_MCP_ENDPOINT", "MM_BUS_MCP_ENDPOINT"), ("CB_BUS_ENDPOINT", "MM_BUS_ENDPOINT"),
                     ("CB_BUS_UNAVAILABLE", "MM_BUS_UNAVAILABLE")):
        assert f".{new}," in deny + "," and f".{old}," in deny + ",", new
    assert ".MATTERMOST_TOKEN," in deny and ".MM_REG_SECRET," in deny
    for line in ("unset CB_MM_BOT_TOKEN MM_BOT_TOKEN MATTERMOST_TOKEN",
                 "unset CB_BUS_HOOK_DISPATCH MM_BUS_HOOK_DISPATCH"):
        assert line in text
    assert "unset CB_MM_BOT_TOKEN MM_BOT_TOKEN MATTERMOST_TOKEN" in \
        (ROOT / "adapters" / "corral" / "90-mm-session-stop.sh").read_text()


def test_the_pass_lists_carry_both_spellings():
    stop = (ROOT / "adapters" / "corral" / "90-mm-session-stop.sh").read_text()
    body = stop[stop.index("self_update() {"):stop.index("trap 'self_update' EXIT")]
    for n in ("CB_RELEASE_URL", "MM_RELEASE_URL", "CB_RELEASE_SIGNERS", "MM_RELEASE_SIGNERS",
              "CB_MM_CHAT_URL", "MM_CHAT_URL", "CB_BUS_HOST_DIR", "MM_BUS_HOST_DIR", "MM_REGISTRAR_KEYS"):
        assert re.search(rf"\b{n}\b", body), n
    lib = LIB.read_text()
    body = lib[lib.index("mm_registrar_release() {"):]
    body = body[:body.index("\n}\n")]
    assert re.search(r"for k in MM_REGISTER_URL CB_MM_CHAT_URL MM_CHAT_URL ", body)


def test_env_json_hands_the_sandbox_each_value_under_both_names(tmp_path):
    # The jq step of 10-mm-session-start.sh that adds the old names to ENV_JSON.
    text = (ROOT / "adapters" / "corral" / "10-mm-session-start.sh").read_text()
    step = re.search(r'\| \. \+ \(to_entries.*?\| from_entries\)', text, re.S)[0]
    out = subprocess.run(["jq", "-c", "." + step], input=json.dumps(
        {"CB_MM_CHAT_URL": "u", "CB_TRUSTED_HUMANS": "h", "MM_AGENT_PREFIX": "agent-"}),
        capture_output=True, text=True, timeout=30)
    assert json.loads(out.stdout) == {"CB_MM_CHAT_URL": "u", "MM_CHAT_URL": "u", "CB_TRUSTED_HUMANS": "h",
                                      "MM_TRUSTED_HUMANS": "h", "MM_AGENT_PREFIX": "agent-"}
