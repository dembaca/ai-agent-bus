"""The CB_ rename (issue #169, docs/env-names.md): the reading rule, the lists
of names that carry both spellings, and the values the bus writes for itself
under both names and reads under either."""

import logging
import os

import pytest

from mattermost_ai_agent_bus import config, entry, hookapi, hosted, owners, release, sandbox, session_file, stats

SECRET = "s3cret-value-never-shown"


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records
            if r.name == config.__name__ and r.levelno == logging.WARNING]


# --- the reading rule ----------------------------------------------------------------


def test_the_new_name_alone_is_read_without_a_warning(monkeypatch, caplog):
    monkeypatch.setenv("CB_LEASE_SEC", "600")
    assert config.env("CB_LEASE_SEC") == "600"
    assert _warnings(caplog) == []


def test_the_old_name_alone_is_read_with_one_warning_per_process(monkeypatch, caplog):
    monkeypatch.setenv("MM_LEASE_SEC", "600")
    assert config.env("CB_LEASE_SEC") == "600"
    assert config.env("CB_LEASE_SEC") == "600"
    assert config.env("MM_LEASE_SEC") == "600"  # either name asks for the one setting
    assert _warnings(caplog) == [
        "MM_LEASE_SEC is deprecated; set CB_LEASE_SEC instead (the old name goes in a later release)"]


def test_both_names_agreeing_read_the_value_without_a_warning(monkeypatch, caplog):
    monkeypatch.setenv("CB_LEASE_SEC", "600")
    monkeypatch.setenv("MM_LEASE_SEC", "600")
    assert config.env("CB_LEASE_SEC") == "600"
    assert _warnings(caplog) == []


def test_both_names_differing_refuse_and_name_both_never_a_value(monkeypatch):
    monkeypatch.setenv("CB_MM_BOT_TOKEN", SECRET)
    monkeypatch.setenv("MM_BOT_TOKEN", SECRET + "-other")
    with pytest.raises(config.EnvConflict) as exc:
        config.env("CB_MM_BOT_TOKEN")
    assert "CB_MM_BOT_TOKEN" in str(exc.value) and "MM_BOT_TOKEN" in str(exc.value)
    assert SECRET not in str(exc.value)
    with pytest.raises(config.EnvConflict) as exc:
        config.check()
    assert SECRET not in str(exc.value)


def test_empty_counts_as_set_both_ways(monkeypatch):
    """An empty new name never falls back to a set old one: they differ. An empty
    old name next to a set new one differs too. Both empty agree."""
    monkeypatch.setenv("CB_OWNER", "")
    monkeypatch.setenv("MM_OWNER", "alice")
    with pytest.raises(config.EnvConflict):
        config.lookup("CB_OWNER")
    monkeypatch.setenv("CB_OWNER", "alice")
    monkeypatch.setenv("MM_OWNER", "")
    with pytest.raises(config.EnvConflict):
        config.lookup("CB_OWNER")
    monkeypatch.setenv("CB_OWNER", "")
    assert config.lookup("CB_OWNER") == "" and config.env("CB_OWNER", "dflt") == "dflt"
    monkeypatch.delenv("MM_OWNER")
    assert config.lookup("CB_OWNER") == ""


def test_an_unexpanded_placeholder_counts_as_unset(monkeypatch, caplog):
    monkeypatch.setenv("CB_MM_PROJECT_CHANNEL", "${CB_MM_PROJECT_CHANNEL}")
    assert config.lookup("CB_MM_PROJECT_CHANNEL") is None and config.env("CB_MM_PROJECT_CHANNEL") == ""
    monkeypatch.setenv("MM_PROJECT_CHANNEL", "proj")
    assert config.env("CB_MM_PROJECT_CHANNEL") == "proj"  # no conflict: the placeholder is no value
    assert len(_warnings(caplog)) == 1
    config.check()


@pytest.mark.parametrize(("new", "old", "alias"), [
    ("CB_MM_CHAT_URL", "MM_CHAT_URL", "MATTERMOST_URL"),
    ("CB_MM_BOT_TOKEN", "MM_BOT_TOKEN", "MATTERMOST_TOKEN"),
])
def test_three_names_of_one_setting(monkeypatch, caplog, new, old, alias):
    monkeypatch.setenv(alias, SECRET)
    assert config.env(new) == SECRET and _warnings(caplog)[0].startswith(f"{alias} is deprecated; set {new}")
    monkeypatch.setenv(old, SECRET)
    monkeypatch.setenv(new, SECRET)
    assert config.env(new) == SECRET
    monkeypatch.setenv(alias, "other")
    with pytest.raises(config.EnvConflict) as exc:
        config.env(new)
    assert all(n in str(exc.value) for n in (new, old, alias)) and SECRET not in str(exc.value)
    monkeypatch.delenv(new)
    monkeypatch.delenv(old)
    assert config.env(new) == "other"


@pytest.mark.parametrize("main", [entry.main, hosted.main])
def test_the_start_is_refused_on_a_disagreement(monkeypatch, capsys, main):
    monkeypatch.setenv("CB_TRUSTED_HUMANS", "alice")
    monkeypatch.setenv("MM_TRUSTED_HUMANS", "mallory")
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "CB_TRUSTED_HUMANS, MM_TRUSTED_HUMANS are set to different values" in err
    assert "alice" not in err and "mallory" not in err


@pytest.mark.parametrize("marker", ["CB_BUS_MCP_ENDPOINT", "MM_BUS_MCP_ENDPOINT"])
def test_a_keyless_sandbox_reads_old_names_quietly(monkeypatch, caplog, marker):
    """The host hook wrote the settings there, an older one under the MM_ names
    only: no deprecation warning. Names that differ still refuse."""
    monkeypatch.setenv(marker, "tcp://127.0.0.1:1")
    monkeypatch.setenv("MM_LEASE_SEC", "600")
    assert config.env("CB_LEASE_SEC") == "600"
    assert _warnings(caplog) == []
    monkeypatch.setenv("CB_OWNER", "alice")
    monkeypatch.setenv("MM_OWNER", "mallory")
    with pytest.raises(config.EnvConflict):
        config.lookup("CB_OWNER")


def test_the_host_process_refuses_a_disagreement(monkeypatch, tmp_path, capsys):
    """--host: the start line's settings are checked like the environment."""
    monkeypatch.setattr(hosted, "read_start", lambda _stdin: (
        "k", {"CB_TRUSTED_HUMANS": "alice", "MM_TRUSTED_HUMANS": "mallory"}))
    monkeypatch.setattr(os, "environ", dict(os.environ))
    ready = tmp_path / "ready"
    code = hosted.host_main(["--ready-file", str(ready), "--state-dir", str(tmp_path / "state"), "--foreground"])
    assert code == 1 and not ready.exists()
    err = capsys.readouterr().err
    assert "CB_TRUSTED_HUMANS, MM_TRUSTED_HUMANS are set to different values; set only CB_TRUSTED_HUMANS" in err
    assert "alice" not in err and "mallory" not in err


def test_no_module_reads_a_renamed_setting_past_config():
    """Every name of a renamed setting, new or old, is read through config (lookup,
    env, value): a direct os.environ or os.getenv read would skip the old name, the
    conflict check or the deprecation."""
    import re
    from pathlib import Path

    names = {n for new in config.RENAMED for n in config.spellings(new)}
    q = r"""["']([A-Z][A-Z0-9_]*)["']"""
    reads = re.compile(rf"(?:os\.environ|os\.getenv|\benviron)\s*(?:\.get\(|\[|\()\s*{q}"
                       rf"|{q}\s+(?:not\s+)?in\s+(?:os\.)?environ\b")
    src = Path(config.__file__).parent
    found = []
    for path in sorted(src.rglob("*.py")):
        if path.name == "config.py":
            continue
        for no, line in enumerate(path.read_text().splitlines(), 1):
            found += [f"{path.name}:{no} {n}" for m in reads.finditer(line) for n in m.groups() if n in names]
    assert found == []


def test_the_registrar_names_are_not_renamed():
    for name in ("MM_REG_SECRET", "MM_REG_SECRET_GOPASS", "MM_REGISTER_URL", "MM_REG_BOT_NAME",
                 "MM_AGENT_PREFIX", "MM_BOT_ATTESTATION", "MM_REGISTRAR_KEYS"):
        assert config.spellings(name) == (name,)
        assert name not in config.NEW_NAME


# --- the two settings whose emptiness means something ----------------------------------


@pytest.mark.parametrize("name", ["CB_MM_STATS_CHANNEL", "MM_STATS_CHANNEL"])
def test_an_empty_stats_channel_switches_the_statistics_off(monkeypatch, name):
    assert stats.channel() == stats.DEFAULT_CHANNEL
    monkeypatch.setenv(name, "")
    assert stats.channel() == ""


def test_an_empty_new_stats_channel_beside_an_old_one_is_refused(monkeypatch):
    monkeypatch.setenv("CB_MM_STATS_CHANNEL", "")
    monkeypatch.setenv("MM_STATS_CHANNEL", "agents-stats")
    with pytest.raises(config.EnvConflict):
        stats.channel()


@pytest.mark.parametrize("name", ["CB_RELEASE_SIGNERS", "MM_RELEASE_SIGNERS"])
def test_empty_release_signers_reach_the_check_as_set(monkeypatch, name):
    """Set but empty refuses the update in the shell check (bin/mm-agent-lib.sh,
    mm_release_signers): the child must see the name, empty, not miss it."""
    monkeypatch.setenv(name, "")
    env = release.child_env()
    assert name in env and env[name] == ""


# --- lists of names --------------------------------------------------------------------


def test_the_lists_carry_both_spellings():
    for new in ("CB_RELEASE_URL", "CB_RELEASE_SIGNERS", "CB_BUS_HOST_DIR", "CB_MM_CHAT_URL"):
        assert {new, config.RENAMED[new][0]} <= set(release.CHILD_ENV)
    # Lists read through config.env name the new name only; env reads the old one too.
    assert set(owners.TRUSTED_VARS) == {"CB_MM_TRUSTED_HUMAN_USER_IDS", "CB_TRUSTED_HUMANS",
                                        "CB_MM_OWNER_USER_ID", "CB_OWNER"}
    assert set(sandbox.SANDBOX_MARKERS) == {"CORRAL_SANDBOX", "CB_BUS_MCP_ENDPOINT", "CB_BUS_ENDPOINT"}
    assert "CB_WORKSPACE" in session_file.WORKSPACE_VARS and "MM_WORKSPACE" not in session_file.WORKSPACE_VARS
    assert set(hosted._TOKEN_ENV) == {"CB_MM_BOT_TOKEN", "MM_BOT_TOKEN", "MATTERMOST_TOKEN"}
    for new in ("CB_AGENT_SESSION_FILE", "CB_BUS_KEY", "CB_BUS_MCP_ENDPOINT", "CB_BUS_ENDPOINT",
                "CB_BUS_UNAVAILABLE"):
        assert set(config.spellings(new)) <= hosted._HOST_ENV_DENY
    assert {"CB_WORKSPACE", "MM_WORKSPACE", "CB_CHANNEL_PUSH", "MM_CHANNEL_PUSH"} <= set(hosted.NAMING)


def test_the_start_line_takes_cb_names_and_denies_both_spellings():
    import io
    import json

    names = ["CB_MM_CHAT_URL", "MM_CHAT_URL", "CB_TRUSTED_HUMANS", "CB_MM_BOT_TOKEN",
             "CB_BUS_KEY", "MM_BUS_KEY", "CB_AGENT_SESSION_FILE", "MM_AGENT_SESSION_FILE",
             "CB_BUS_UNAVAILABLE", "MM_REG_SECRET"]
    line = json.dumps({"key": "k" * 64, "env": dict.fromkeys(names, "v")}) + "\n"
    _, env = hosted.read_start(io.BytesIO(line.encode()))
    assert sorted(env) == ["CB_MM_BOT_TOKEN", "CB_MM_CHAT_URL", "CB_TRUSTED_HUMANS", "MM_CHAT_URL"]


@pytest.mark.parametrize(("marker", "seen"), [
    ("CB_BUS_MCP_ENDPOINT", "CB_BUS_MCP_ENDPOINT"), ("CB_BUS_ENDPOINT", "CB_BUS_ENDPOINT"),
    ("MM_BUS_MCP_ENDPOINT", "CB_BUS_MCP_ENDPOINT"), ("MM_BUS_ENDPOINT", "CB_BUS_ENDPOINT")])
def test_a_sandbox_marker_is_seen_under_either_name(monkeypatch, marker, seen):
    monkeypatch.setenv(marker, "tcp://127.0.0.1:1")
    assert sandbox.corral_sandbox() == seen


# --- values the bus writes for itself ---------------------------------------------------


def test_the_session_file_holds_both_names_and_reads_either(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sid-1")
    info = session_file.SessionInfo(url="https://mm.example", name="proj", username="agent-proj",
                                    user_id="u" * 26, bot_token=SECRET)
    path = session_file.write(info, "agents", "lobby", "home")
    data = session_file.parse(path)
    for new, value in (("CB_MM_CHAT_URL", info.url), ("CB_MM_BOT_TOKEN", SECRET),
                       ("CB_MM_BOT_USERNAME", "agent-proj"), ("CB_MM_BOT_USER_ID", "u" * 26),
                       ("CB_MM_TEAM", "agents"), ("CB_MM_CHANNEL", "lobby"),
                       ("CB_MM_PROJECT_CHANNEL", "home"), ("CB_SESSION_PID", str(os.getppid()))):
        for name in config.spellings(new):
            assert data[name] == value, name
    assert "MATTERMOST_URL" in data and "MATTERMOST_TOKEN" in data
    assert data["MM_REG_BOT_NAME"] == "proj"
    assert session_file.read()["CB_MM_BOT_TOKEN"] == SECRET
    # A file of the version before: old keys only.
    path.write_text(f"MM_CHAT_URL={info.url}\nMM_REG_BOT_NAME=proj\nMM_BOT_TOKEN={SECRET}\n")
    data = session_file.read()
    assert data and config.value(data, "CB_MM_BOT_TOKEN") == SECRET
    assert config.value(data, "CB_MM_CHAT_URL") == info.url


def test_an_end_mark_is_written_under_both_names_and_read_under_either(monkeypatch, tmp_path):
    p = tmp_path / "f"
    p.write_text("MM_SERVER_ENDED=1700000000\n")
    assert session_file.ended_at(session_file.parse(p)) == 1700000000.0
    p.write_text("CB_SERVER_ENDED=1700000001\n")
    assert session_file.ended_at(session_file.parse(p)) == 1700000001.0
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sid-2")
    info = session_file.SessionInfo(url="u", name="n", username="agent-n", user_id="i", bot_token=SECRET)
    path = session_file.write(info, "agents", "agents")
    assert session_file.mark_ended(SECRET)
    data = session_file.parse(path)
    assert data["CB_SERVER_ENDED"] == data["MM_SERVER_ENDED"]


def test_the_endpoint_file_is_read_under_either_name():
    data = {"MM_BUS_ENDPOINT": "http://127.0.0.1:1", "MM_BUS_KEY": "k", "MM_BUS_PID": "1"}
    assert config.value(data, "CB_BUS_KEY") == "k" and config.value(data, "CB_BUS_PID") == "1"
    # Keys that differ never refuse in a file the bus wrote: the later one wins.
    data |= {"CB_BUS_KEY": "k2"}
    assert config.value(data, "CB_BUS_KEY") == config.value(data, "MM_BUS_KEY") == "k2"
    assert hookapi.FILE_PREFIX  # the writer is covered in test_hook_api


def test_a_file_the_bus_wrote_is_read_last_line_first(tmp_path):
    """A downgrade: the newer version wrote both keys, the older one appends its
    old key later. The last line of either spelling wins, for both twins, as
    mm_kv in bin/mm-agent-lib.sh reads it (tests/hooks/test_env_names.py)."""
    p = tmp_path / "s.env"
    p.write_text("CB_SESSION_PID=10\nMM_SESSION_PID=10\nMM_SESSION_PID=20\n")
    data = session_file.parse(p)
    assert config.value(data, "CB_SESSION_PID") == config.value(data, "MM_SESSION_PID") == "20"
    p.write_text("MM_BUS_PID=1\nCB_BUS_PID=2\nMM_SESSION_PID=3\n")
    data = session_file.parse(p)
    assert config.value(data, "CB_BUS_PID") == "2" and config.value(data, "CB_SESSION_PID") == "3"
    p.write_text("CB_SERVER_ENDED=1700000000\nMM_SERVER_ENDED=1700000005\n")
    assert session_file.ended_at(session_file.parse(p)) == 1700000005.0


def test_the_relay_sends_both_names_and_the_host_folds_them(monkeypatch):
    values = hosted.session_values({"MM_WORKSPACE": "/p"}, cwd="/p")
    assert values["CB_WORKSPACE"] == values["MM_WORKSPACE"] == "/p"
    assert values["CB_CHANNEL_PUSH"] == values["MM_CHANNEL_PUSH"]
    checked = hosted.check_session(values)
    assert checked["CB_WORKSPACE"] == "/p" and "MM_WORKSPACE" not in checked
    # A relay of the version before sends only the old names.
    old = hosted.check_session({"MM_WORKSPACE": "/p", "MM_CHANNEL_PUSH": "1", "cwd": "/p"})
    assert old == {"CB_WORKSPACE": "/p", "CB_CHANNEL_PUSH": "1", "cwd": "/p"}
    assert hosted.identity(old) == hosted.identity(checked)
    with pytest.raises(ValueError, match="CB_WORKSPACE and session.MM_WORKSPACE differ"):
        hosted.check_session({"CB_WORKSPACE": "/p", "MM_WORKSPACE": "/q"})
    env: dict[str, str] = {}
    hosted.apply_session(checked, environ=env)
    assert env["CB_CHANNEL_PUSH"] == env["MM_CHANNEL_PUSH"]


def test_put_and_drop_cover_every_name():
    env: dict[str, str] = {}
    config.put("CB_MM_BOT_TOKEN", SECRET, env)
    assert env == dict.fromkeys(("CB_MM_BOT_TOKEN", "MM_BOT_TOKEN", "MATTERMOST_TOKEN"), SECRET)
    config.drop("MM_BOT_TOKEN", env)
    assert env == {}
