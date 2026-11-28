"""RT-6.2 step 9, plan B piece 14c-1: every per-bot state file the MCP server keeps follows the
state key (session_file.state_key), the NATS name with plan B piece 2 on; session_file.latch
takes the name's lock, copies every kind once (counters and logs too, never a lock), writes the
marker and only then latches the key. A second process with the same name refuses and says so
in bus_info (lease.nats_notes). Switch off: every file name is that of 0.104.0."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

import pytest

from mattermost_ai_agent_bus import (
    askroute, asks, channel, crownlog, gate, lease, nats_port, permission, remind, session_file, stall, stats,
    watermark,
)

UID = "b" * 26
NAME = "a2c4e6g7" * 3 + "h"  # 25 base32 characters, as natsbus.agent_name makes them
HOME = "work"


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("CB_PLANB_TEST", raising=False)
    monkeypatch.setenv("CB_MM_BOT_USER_ID", UID)
    d = session_file.state_dir()
    d.mkdir(parents=True)
    return d


def _paths(uid: str) -> dict[str, Path | None]:
    return {
        "asks": asks._path(uid), "read": watermark.path(uid), "remind": remind._path(uid),
        "gate": gate._path(uid), "stall": stall._path(uid), "notice": nats_port._notice_file(uid),
        "askmode": askroute._path(uid), "presence": lease._file("presence", uid),
        "coord": lease._file("coord", uid), "channel": channel.confirmed_file(uid),
        "stats": stats._path(), "crown": crownlog.log_path(), "permission": permission.log_path(),
    }


def _names(key: str) -> dict[str, str]:
    env = {k: f"{k}-{key}.env" for k in ("asks", "read", "remind", "gate", "stall", "notice", "askmode",
                                          "presence", "coord", "channel")}
    return {**env, "stats": f"stats-{key}.jsonl", "crown": f"crown-{key}.log", "permission": f"permission-{key}.log"}


def _on(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    monkeypatch.setenv("CB_PLANB_TEST", "2")
    return session_file.latch(NAME, HOME, UID)


def test_switch_off_every_file_keeps_its_bot_id_name(state: Path) -> None:
    assert session_file.latch(NAME, HOME, UID) == []
    assert {k: p.name if p else None for k, p in _paths(UID).items()} == _names(UID)
    assert session_file.state_key("") == ""
    assert sorted(p.name for p in state.iterdir()) == []  # no lock, no marker
    assert session_file.latch_refused() == ""


def test_switch_on_every_file_takes_the_name(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch)
    assert {k: p.name if p else None for k, p in _paths(UID).items()} == _names(NAME)
    assert session_file.state_key("") == NAME
    # A session without a bot id keeps its state under the name too.
    monkeypatch.delenv("CB_MM_BOT_USER_ID")
    monkeypatch.setattr(stats, "_uid", "")
    assert stats._path() == state / f"stats-{NAME}.jsonl"
    assert lease._file("coord", "") == state / f"coord-{NAME}.env"


def test_latch_copies_every_kind_and_skips_locks(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for kind in session_file.STATE_KINDS:
        (state / session_file._kind_file(kind, UID)).write_text(f"{kind}\n")
    for lock in ("asks", "remind", "stats"):
        (state / f"{lock}-{UID}.lock").write_text("")
    copied = _on(monkeypatch)
    assert copied == list(session_file.STATE_KINDS)
    for kind in session_file.STATE_KINDS:
        assert (state / session_file._kind_file(kind, NAME)).read_text() == f"{kind}\n"
        assert (state / session_file._kind_file(kind, UID)).exists()  # the way back
    assert not any((state / f"{lock}-{NAME}.lock").exists() for lock in ("asks", "remind", "stats"))
    assert (state / f"copied-{NAME}.env").read_text() == f"CB_COPIED_FROM={UID}\n"
    assert (state / f"key-{NAME}.lock").exists()


def test_every_kind_the_server_reads_is_copied() -> None:
    kinds = set(session_file.STATE_KINDS)
    assert {"last", "threads", "unanswered", "asks", "read", "remind", "gate", "stall", "notice", "askmode",
            "presence", "coord", "stats.jsonl", "crown.log", "permission.log"} == kinds
    assert not {"nonce", "channel", "listen", "update", "turn"} & kinds


def test_the_copy_comes_before_the_key(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Counters carry over: a hook that learns the key appends to a file that holds the copy."""
    (state / f"stats-{UID}.jsonl").write_text('{"k":"wake","t":1}\n')
    seen: list[bool] = []
    real = session_file.use_nats_name

    def spy(name: str, home: str) -> None:
        seen.append((state / f"stats-{NAME}.jsonl").exists())
        real(name, home)

    monkeypatch.setattr(session_file, "use_nats_name", spy)
    _on(monkeypatch)
    assert seen == [True]
    stats.record("wake", reason="digest")
    lines = (state / f"stats-{NAME}.jsonl").read_text().splitlines()
    assert lines[0] == '{"k":"wake","t":1}' and '"reason": "digest"' in lines[1]


def test_latch_without_a_bot_writes_the_marker(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CB_PLANB_TEST", "2")
    assert session_file.latch(NAME, HOME, "") == []
    assert (state / f"copied-{NAME}.env").read_text() == ""  # the sweep's day rule applies
    assert session_file.state_key("") == NAME


def test_a_second_holder_of_the_name_refuses(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fd = os.open(state / f"key-{NAME}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)  # another live server of the same name
    try:
        (state / f"last-{UID}.env").write_text("MSG=x\n")
        assert _on(monkeypatch) == []
        assert session_file.state_key(UID) == UID
        assert session_file.state_key("") == ""
        assert not (state / f"last-{NAME}.env").exists()
        assert "same NATS name" in session_file.latch_refused()
        assert any("keyed by the bot id" in n for n in lease.nats_notes())
    finally:
        os.close(fd)


def test_the_lock_is_held_for_the_run(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch)
    fd = os.open(state / f"key-{NAME}.lock", os.O_RDWR)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)


def test_a_channel_confirmation_moves_and_an_old_one_of_the_name_goes(state: Path,
                                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    (state / f"channel-{UID}.env").write_text("CONFIRMED=1\n")
    (state / f"channel-{NAME}.env").write_text("CONFIRMED=0\n")  # an earlier process's
    _on(monkeypatch)
    assert (state / f"channel-{NAME}.env").read_text() == "CONFIRMED=1\n"
    (state / f"channel-{UID}.env").unlink()
    (state / f"channel-{NAME}.env").unlink()
    (state / f"channel-{UID}.env").write_text("CONFIRMED=2\n")
    session_file.use_nats_name(NAME, HOME)
    assert not channel.confirmed(UID)
    assert not (state / f"channel-{UID}.env").exists()  # the bot id's follows the name's


def test_mixed_versions_the_bot_id_keeps_coord_and_channel(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An old hook (status line, the channel check) still reads the bot id's files."""
    _on(monkeypatch)
    lease._save_coord(UID, 2_000_000_000, 0, 0, "c" * 26)
    assert (state / f"coord-{NAME}.env").read_text() == (state / f"coord-{UID}.env").read_text()
    assert "CROWN_UNTIL=2000000000" in (state / f"coord-{UID}.env").read_text()
    channel.confirmed_file(UID).write_text("CONFIRMED=1\n")
    assert channel.confirmed(UID)
    assert (state / f"channel-{UID}.env").read_text() == "CONFIRMED=1\n"


def test_switch_off_writes_one_coord(state: Path) -> None:
    lease._save_coord(UID, 2_000_000_000, 0, 0, "c" * 26)
    assert sorted(p.name for p in state.iterdir()) == [f"coord-{UID}.env"]
    (state / f"channel-{UID}.env").write_text("CONFIRMED=1\n")
    assert channel.confirmed(UID)
    assert sorted(p.name for p in state.iterdir()) == [f"channel-{UID}.env", f"coord-{UID}.env"]


def test_session_end_forgets_both_keys_and_keeps_the_marker(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (state / f"read-{UID}.env").write_text("x=1\n")
    _on(monkeypatch)
    assert (state / f"read-{NAME}.env").exists()
    watermark.remove(UID)
    assert not (state / f"read-{NAME}.env").exists() and not (state / f"read-{UID}.env").exists()
    assert (state / f"copied-{NAME}.env").exists()
