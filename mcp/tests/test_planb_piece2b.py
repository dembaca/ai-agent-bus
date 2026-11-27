"""RT-6.2 (state keys), ADR-014 phase 5 plan B piece 2b: the session's kept state is keyed by the
NATS name when planb.on(2, home) and the bus has the name; copied once from the files keyed by
the bot's Mattermost id, never moved. Switch off: every name is what it was."""

from __future__ import annotations

from pathlib import Path

import pytest

from mattermost_ai_agent_bus import askroute, lease, ownpost, planb, session_file, unanswered
from mattermost_ai_agent_bus import websocket_inbox as _wi  # noqa: F401 — the wake rule's module
from mattermost_ai_agent_bus.model import Post

UID = "b" * 26
NAME = "agent-" + "a2" * 13
NAME25 = "a2c4e6g7" * 3 + "h"  # 25 base32 characters, no hyphen (#276)
HOME = "work"


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("CB_PLANB_TEST", raising=False)
    monkeypatch.setattr(session_file, "_nats_name", None)
    d = session_file.state_dir()
    d.mkdir(parents=True)
    return d


def _on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CB_PLANB_TEST", "2")
    session_file.use_nats_name(NAME, HOME)


def test_state_key_switch_off_is_the_bot_id(state: Path) -> None:
    session_file.use_nats_name(NAME, HOME)
    assert session_file.state_key(UID) == UID
    assert planb.on(2, HOME) is False


def test_state_key_switch_on(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch)
    assert session_file.state_key(UID) == NAME
    assert session_file.state_key("") == ""
    assert session_file.state_key(UID, "agent-other", HOME) == "agent-other"


def test_state_key_without_a_name_is_the_bot_id(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CB_PLANB_TEST", "2")
    assert session_file.state_key(UID) == UID
    session_file.use_nats_name("../x", HOME)  # no key
    assert session_file.state_key(UID) == UID


def test_the_files_keep_their_names_with_the_switch_off(state: Path) -> None:
    assert ownpost.path(UID) == state / f"last-{UID}.env"
    assert ownpost.threads_path(UID) == state / f"threads-{UID}.env"
    assert unanswered.path(UID) == state / f"unanswered-{UID}.env"


def test_the_files_take_the_name_with_the_switch_on(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch)
    assert ownpost.path(UID) == state / f"last-{NAME}.env"
    assert ownpost.threads_path(UID) == state / f"threads-{NAME}.env"
    assert unanswered.path(UID) == state / f"unanswered-{NAME}.env"
    ownpost.record(UID, "chan", Post(id="p1", channel_id="chan", user_id=UID, message="hello", create_at=1000))
    assert (state / f"last-{NAME}.env").exists() and not (state / f"last-{UID}.env").exists()


def test_copy_once_leaves_the_source_and_never_repeats(state: Path) -> None:
    (state / f"last-{UID}.env").write_text("MSG=hi\n")
    (state / f"threads-{UID}.env").write_text("root1 chan DONE\n")
    assert session_file.copy_state_once(UID, NAME) == ["last", "threads"]
    assert (state / f"last-{UID}.env").read_text() == "MSG=hi\n"  # not moved
    assert (state / f"last-{NAME}.env").read_text() == "MSG=hi\n"
    assert (state / f"threads-{NAME}.env").read_text() == "root1 chan DONE\n"
    assert not (state / f"unanswered-{NAME}.env").exists()
    assert (state / f"copied-{NAME}.env").read_text() == f"CB_COPIED_FROM={UID}\n"
    assert (state / f"last-{NAME}.env").stat().st_mode & 0o777 == 0o600
    # The session removed its copy; a second run does not bring it back.
    (state / f"last-{NAME}.env").unlink()
    assert session_file.copy_state_once(UID, NAME) == []
    assert not (state / f"last-{NAME}.env").exists()


def test_copy_does_not_overwrite_what_the_name_already_has(state: Path) -> None:
    (state / f"last-{UID}.env").write_text("MSG=old\n")
    (state / f"last-{NAME}.env").write_text("MSG=new\n")
    assert session_file.copy_state_once(UID, NAME) == []
    assert (state / f"last-{NAME}.env").read_text() == "MSG=new\n"


def test_copy_refuses_bad_keys_and_a_symlink_source(state: Path) -> None:
    assert session_file.copy_state_once("../x", NAME) == []
    assert session_file.copy_state_once(UID, "a/b") == []
    (state / "elsewhere").write_text("secret\n")
    (state / f"last-{UID}.env").symlink_to(state / "elsewhere")
    assert session_file.copy_state_once(UID, NAME) == []


def test_key_checks_accept_a_nats_name_and_still_reject_junk(state: Path) -> None:
    for key in (UID, NAME):
        assert session_file.key_ok(key)
        assert lease._file("presence", key) == state / f"presence-{key}.env"
        assert askroute._path(key) == state / f"askmode-{key}.env"
    for key in ("", "../x", "a/b", "a.b", "-a", "a b", "a" * 129):
        assert not session_file.key_ok(key)
        assert lease._file("presence", key) is None
        assert askroute._path(key) is None
    # Elsewhere a bot id stays 26 characters.
    assert lease._UID.fullmatch(NAME) is None


def test_the_wake_rule_reads_the_threads_under_the_same_key(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (state / f"threads-{UID}.env").write_text("root1 guest DONE\n")
    session_file.copy_state_once(UID, NAME)
    _on(monkeypatch)
    # websocket_inbox asks ownpost.guest_threads(bot id): the copy is found under the name.
    assert "root1" in ownpost.guest_threads(UID)
    (state / f"threads-{NAME}.env").unlink()
    assert ownpost.guest_threads(UID) == {}
    monkeypatch.setattr(session_file, "_nats_name", None)
    assert "root1" in ownpost.guest_threads(UID)  # way back: the old file


def test_remove_forgets_both_keys(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for kind in ("last", "unanswered", "threads"):
        (state / f"{kind}-{UID}.env").write_text("x=1\n")
        (state / f"{kind}-{NAME}.env").write_text("x=1\n")
    _on(monkeypatch)
    ownpost.remove(UID)
    unanswered.remove(UID)
    assert [p.name for p in state.iterdir()] == []


def test_remove_also_forgets_the_copy_marker(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (state / f"last-{UID}.env").write_text("MSG=x\n")
    session_file.copy_state_once(UID, NAME)
    assert (state / f"copied-{NAME}.env").exists()
    _on(monkeypatch)
    ownpost.remove(UID)
    assert not (state / f"copied-{NAME}.env").exists()


def test_the_key_survives_the_switch_going_away(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The bus detaches its switch source at stop(), before the exit handler's BYE: the key
    was decided at start (RT-6.2 step 9)."""
    src = lambda _ch: frozenset({2})  # noqa: E731
    monkeypatch.delenv("CB_PLANB_TEST", raising=False)
    planb.attach(src)
    try:
        assert planb.on(2, HOME)
        session_file.use_nats_name(NAME, HOME)
        assert ownpost.path(UID) == state / f"last-{NAME}.env"
    finally:
        planb.detach(src)
    assert planb.on(2, HOME) is False
    assert ownpost.path(UID) == state / f"last-{NAME}.env"
    assert unanswered.path(UID) == state / f"unanswered-{NAME}.env"


def _age(path: Path, secs: int) -> None:
    import os
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + secs * 10**9))


def test_copy_replaces_an_older_name_file_with_a_newer_source(state: Path) -> None:
    (state / f"last-{NAME}.env").write_text("MSG=old\n")
    (state / f"last-{UID}.env").write_text("MSG=newer\n")
    _age(state / f"last-{UID}.env", 10)
    assert session_file.copy_state_once(UID, NAME) == ["last"]
    assert (state / f"last-{NAME}.env").read_text() == "MSG=newer\n"
    _age(state / f"last-{NAME}.env", 20)  # written after the source, as in life
    assert session_file.copy_state_once(UID, NAME) == []  # now the name's is the newer


def test_copy_after_off_then_on_takes_what_the_off_session_wrote(state: Path) -> None:
    (state / f"last-{UID}.env").write_text("MSG=1\n")
    assert session_file.copy_state_once(UID, NAME) == ["last"]
    (state / f"last-{UID}.env").write_text("MSG=2 written with the switch off\n")
    _age(state / f"last-{UID}.env", 10)
    assert session_file.copy_state_once(UID, NAME) == ["last"]  # the marker does not block it
    assert (state / f"last-{NAME}.env").read_text().startswith("MSG=2")


def test_copy_keeps_a_newer_name_file_and_refuses_a_symlink_target(state: Path) -> None:
    (state / f"last-{UID}.env").write_text("MSG=old\n")
    (state / f"last-{NAME}.env").write_text("MSG=newer\n")
    _age(state / f"last-{NAME}.env", 10)
    assert session_file.copy_state_once(UID, NAME) == []
    assert (state / f"last-{NAME}.env").read_text() == "MSG=newer\n"
    (state / f"last-{NAME}.env").unlink()
    (state / "elsewhere").write_text("secret\n")
    (state / f"last-{NAME}.env").symlink_to(state / "elsewhere")
    assert session_file.copy_state_once(UID, NAME) == []
    assert (state / "elsewhere").read_text() == "secret\n"


def test_a_name_with_capitals_is_no_file_key(state: Path) -> None:
    assert not session_file.key_ok("Agent-" + "a2" * 13)
    assert session_file.state_key(UID, "Agent-X") == UID
    session_file.use_nats_name("Agent-X", HOME)
    assert session_file.state_key(UID) == UID
    assert session_file.copy_state_once(UID, "Agent-X") == []


def test_a_25_character_name_without_a_hyphen_is_a_key(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert session_file.key_ok(NAME25)
    assert lease._file("presence", NAME25) == state / f"presence-{NAME25}.env"
    monkeypatch.setenv("CB_PLANB_TEST", "2")
    session_file.use_nats_name(NAME25, HOME)
    assert ownpost.path(UID) == state / f"last-{NAME25}.env"
    (state / f"last-{UID}.env").write_text("MSG=hi\n")
    assert session_file.copy_state_once(UID, NAME25) == ["last"]
