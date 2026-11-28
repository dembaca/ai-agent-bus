"""RT-6.2 step 9, plan B piece 14c-1: every per-bot state file the MCP server keeps follows the
state key (session_file.state_key), the NATS name with plan B piece 2 on; session_file.latch
takes the name's lock, copies every kind once (counters and logs too, never a lock), writes the
marker and only then latches the key. A second process with the same name refuses and says so
in bus_info (lease.nats_notes). Switch off: every file name is that of 0.104.0."""

from __future__ import annotations

import fcntl
import hashlib
import os
import threading
import time
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
        # The way back keeps every copied source; the counters are moved (published once).
        assert (state / session_file._kind_file(kind, UID)).exists() == (kind not in session_file.MOVED_KINDS)
    assert not any((state / f"{lock}-{NAME}.lock").exists() for lock in ("asks", "remind", "stats"))
    assert (state / f"copied-{NAME}.env").read_text() == f"CB_COPIED_FROM={UID}\nCB_TIE={session_file.tie()}\n"
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
    # No bot id to tie to: the sweep's day rule applies; the tie names the session.
    assert (state / f"copied-{NAME}.env").read_text() == f"CB_TIE={session_file.tie()}\n"
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
    assert channel.confirmed(UID) and (state / f"channel-{UID}.env").exists()  # now the mirror
    (state / f"channel-{NAME}.env").unlink()
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


# --- review round 1 -------------------------------------------------------------------------


async def test_the_latch_names_the_key_in_the_endpoint_file(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every hook reads one key (CB_STATE_KEY), from the endpoint file alone too."""
    from mattermost_ai_agent_bus import hookapi

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-14c")

    async def no_client() -> None:
        return None

    api = hookapi.HookAPI(no_client)  # type: ignore[arg-type]
    await api.start()
    try:
        f = hookapi.file_path()
        assert "CB_STATE_KEY" not in session_file.parse(f)
        _on(monkeypatch)
        assert session_file.parse(f)["CB_STATE_KEY"] == NAME
    finally:
        await api.close()


@pytest.mark.parametrize("on", [False, True])
def test_one_sender_locks_stay_keyed_by_the_bot_id(state: Path, monkeypatch: pytest.MonkeyPatch, on: bool) -> None:
    """Two servers of one bot, one latched on the name and one refused, still take turns."""
    if on:
        _on(monkeypatch)
    assert session_file.lock_key(UID) == UID
    fd = remind._lock(UID)
    assert fd is not None and (state / f"remind-{UID}.lock").exists()
    os.close(fd)
    with asks._queue_lock(UID):
        assert (state / f"asks-{UID}.lock").exists()
    fd = stats._lock(UID)
    assert fd is not None and (state / f"stats-{UID}.lock").exists()
    os.close(fd)
    assert not list(state.glob(f"*-{NAME}.lock")) or on  # only key-<name>.lock with the latch
    assert sorted(p.name for p in state.glob(f"*-{NAME}.lock")) == ([f"key-{NAME}.lock"] if on else [])
    if on:
        assert remind._path(UID).name == f"remind-{NAME}.env"  # the state stays on the key
        monkeypatch.delenv("CB_MM_BOT_USER_ID")
        assert session_file.lock_key("") == NAME


def test_mirrors_never_touch_another_servers_files(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The second holder of the bot keys its own coord- and channel- by the bot id."""
    _on(monkeypatch)
    theirs_coord, theirs_chan = state / f"coord-{UID}.env", state / f"channel-{UID}.env"
    theirs_coord.write_text("CROWN_UNTIL=1\n")
    theirs_chan.write_text("CONFIRMED=9\n")
    lease._save_coord(UID, 2_000_000_000, 0, 0, "c" * 26)
    assert theirs_coord.read_text() == "CROWN_UNTIL=1\n"
    assert not channel.confirmed(UID)  # this process confirmed nothing
    assert theirs_chan.read_text() == "CONFIRMED=9\n"


def test_mirrors_this_process_wrote_follow_it(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch)
    lease._save_coord(UID, 2_000_000_000, 0, 0, "c" * 26)
    lease._save_coord(UID, 2_000_000_001, 0, 0, "c" * 26)
    assert "CROWN_UNTIL=2000000001" in (state / f"coord-{UID}.env").read_text()
    session_file.drop_mirror(state / f"coord-{UID}.env")
    assert not (state / f"coord-{UID}.env").exists()
    channel.confirmed_file(UID).write_text("CONFIRMED=1\n")
    assert channel.confirmed(UID) and (state / f"channel-{UID}.env").exists()
    channel.confirmed_file(UID).unlink()
    assert not channel.confirmed(UID) and not (state / f"channel-{UID}.env").exists()


def test_a_repeated_latch_moves_nothing(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch)
    (state / f"channel-{NAME}.env").write_text("CONFIRMED=1\n")  # confirmed after the latch
    (state / f"last-{UID}.env").write_text("MSG=x\n")
    assert session_file.latch(NAME, HOME, UID) == []
    assert (state / f"channel-{NAME}.env").exists() and not (state / f"last-{NAME}.env").exists()


def test_without_a_predecessor_the_copy_source_is_the_bot_id(state: Path) -> None:
    assert session_file._copy_source(UID, NAME) == (UID, [])
    assert session_file._copy_source("", NAME) == (None, [])


# --- the takeover across a restart (owner's decision on #328: CB_LOGIN_NKEY stays ephemeral) ---

NAME_B = "b3d5f7h2" * 3 + "k"  # the next process's name
TIES = {"claude": "CLAUDE_CODE_SESSION_ID", "opencode": "OPENCODE_SESSION_ID", "pi": "PI_SESSION_ID"}
KEPT = ("asks", "remind", "read", "gate", "stall", "notice", "askmode", "crown.log", "permission.log")


def _harness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, harness: str, sid: str = "s-1") -> None:
    for var in TIES.values():
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CB_WORKSPACE", str(tmp_path))
    if harness in TIES:
        monkeypatch.setenv(TIES[harness], sid)


def _restart(monkeypatch: pytest.MonkeyPatch, *, release: bool = True) -> None:
    """The process ends (its lock with it, unless `release` is False: it still runs) and a new
    one starts, with a new NATS name."""
    for fd in [*session_file._key_fds.values(), *([session_file._tie_fd] if session_file._tie_fd is not None else [])]:
        if release:
            os.close(fd)
    monkeypatch.setattr(session_file, "_key_fds", {})
    monkeypatch.setattr(session_file, "_tie_fd", None)
    monkeypatch.setattr(session_file, "_tie_refused", "")
    monkeypatch.setattr(session_file, "_claims", set())
    monkeypatch.setattr(session_file, "_stranded", [])
    monkeypatch.setattr(session_file, "_nats_name", None)
    monkeypatch.setattr(session_file, "_mirrors", {})


def _first_run(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (state / f"stats-{UID}.jsonl").write_text('{"k":"old","t":1}\n')
    _on(monkeypatch)
    for kind in KEPT:
        (state / session_file._kind_file(kind, NAME)).write_text(f"{kind} of run 1\n")
    stats.record("wake", reason="run1")


@pytest.mark.parametrize("harness", ["claude", "opencode", "pi", "workspace"])
def test_a_restart_takes_over_the_predecessors_state(state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                     harness: str) -> None:
    _harness(monkeypatch, tmp_path, harness)
    assert session_file.session_tie()[0] == harness
    _first_run(state, monkeypatch)
    _restart(monkeypatch)
    copied = session_file.latch(NAME_B, HOME, UID)
    assert set(KEPT) | {"stats.jsonl"} <= set(copied)
    for kind in KEPT:
        assert (state / session_file._kind_file(kind, NAME_B)).read_text() == f"{kind} of run 1\n"
    # The counters moved: the bot id's and run 1's lines once, under the new name alone.
    lines = (state / f"stats-{NAME_B}.jsonl").read_text().splitlines()
    assert len(lines) == 2 and '"old"' in lines[0] and "run1" in lines[1]
    assert not (state / f"stats-{NAME}.jsonl").exists() and not (state / f"stats-{UID}.jsonl").exists()
    assert f"CB_TAKEN_BY={NAME_B}" in (state / f"copied-{NAME}.env").read_text().splitlines()
    marker = (state / f"copied-{NAME_B}.env").read_text().splitlines()
    assert f"CB_TIE={session_file.tie()}" in marker and f"CB_COPIED_FROM={UID}" in marker
    assert session_file.state_key(UID) == NAME_B


@pytest.mark.parametrize("harness", ["claude", "workspace"])
def test_a_predecessor_that_still_runs_is_not_taken_over(state: Path, tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch, harness: str) -> None:
    _harness(monkeypatch, tmp_path, harness)
    _first_run(state, monkeypatch)
    (state / f"asks-{UID}.env").write_text("asks of the bot id\n")
    _restart(monkeypatch, release=False)  # run 1 holds key-<name>.lock still
    session_file.latch(NAME_B, HOME, UID)
    assert (state / f"asks-{NAME_B}.env").read_text() == "asks of the bot id\n"  # from the bot id
    assert (state / f"asks-{NAME}.env").exists() and (state / f"stats-{NAME}.jsonl").exists()
    assert "CB_TAKEN_BY" not in (state / f"copied-{NAME}.env").read_text()


def test_two_ties_never_take_each_others_state(state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _harness(monkeypatch, tmp_path, "claude", "s-1")
    _first_run(state, monkeypatch)
    _restart(monkeypatch)
    _harness(monkeypatch, tmp_path, "claude", "s-2")  # another Claude session
    session_file.latch(NAME_B, HOME, "")
    assert sorted(p.name for p in state.glob(f"*-{NAME_B}.*")) == [f"copied-{NAME_B}.env", f"key-{NAME_B}.lock"]
    assert (state / f"asks-{NAME}.env").exists() and (state / f"stats-{NAME}.jsonl").exists()
    # The same id under another harness is another tie too.
    claude = session_file.tie()
    _harness(monkeypatch, tmp_path, "opencode", "s-2")
    assert session_file.tie() != claude


def test_a_taken_over_name_is_not_taken_again(state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _harness(monkeypatch, tmp_path, "opencode")
    _first_run(state, monkeypatch)
    _restart(monkeypatch)
    session_file.latch(NAME_B, HOME, UID)
    _restart(monkeypatch)
    third = "c4e6g2j3" * 3 + "m"
    session_file.latch(third, HOME, UID)
    assert (state / f"asks-{third}.env").read_text() == "asks of run 1\n"  # from NAME_B, the newest
    assert f"CB_TAKEN_BY={third}" in (state / f"copied-{NAME_B}.env").read_text()
    lines = (state / f"stats-{third}.jsonl").read_text().splitlines()
    assert len(lines) == 2  # still once each


def test_the_tie_holds_no_raw_id(state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _harness(monkeypatch, tmp_path, "claude", "secret-session-id")
    assert session_file.tie() == "claude:" + hashlib.sha1(b"secret-session-id").hexdigest()[:16]
    _harness(monkeypatch, tmp_path, "workspace")
    assert session_file.tie() == "workspace:" + hashlib.sha1(str(tmp_path.resolve()).encode()).hexdigest()[:16]
    # The workspace's hash is keyed_path's: the two stay comparable.
    assert session_file.keyed_path("x").name == f"x-{session_file.tie().split(':')[1]}.env"


def test_switch_off_nothing_moves_for_any_harness(state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Piece 2 off, the ephemeral key: no marker, no tie, no lock, and the nonce and endpoint
    files keep their names (session_id and keyed_path ignore OPENCODE_SESSION_ID and PI_SESSION_ID)."""
    for harness in ("opencode", "pi"):
        _harness(monkeypatch, tmp_path, harness)
        before = session_file.keyed_path("nonce")
        assert session_file.session_id() == ""
        assert before == state / f"nonce-{hashlib.sha1(str(tmp_path.resolve()).encode()).hexdigest()[:16]}.env"
        (state / f"asks-{UID}.env").write_text("x\n")
        assert session_file.latch(NAME, HOME, UID) == []
        assert sorted(p.name for p in state.iterdir()) == [f"asks-{UID}.env"]


# --- review round 2: the takeover's lineage, its claim, the crash-safe move -----------------

UID2 = "c" * 26
NAME_C = "c4e6g2j3" * 3 + "m"
NAME_D = "d5f7h3k4" * 3 + "n"


def _hold(state: Path, name: str) -> int:
    fd = os.open(state / f"key-{name}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def _name_run(state: Path, name: str, uid: str, what: str) -> None:
    """A name some session ran under on this workspace tie, gone now (its lock free)."""
    (state / f"asks-{name}.env").write_text(f"{what}\n")
    (state / f"stats-{name}.jsonl").write_text(f'{{"k":"{what}"}}\n')
    ids = f"CB_COPIED_FROM={uid}\n" if uid else ""
    (state / f"copied-{name}.env").write_text(f"{ids}CB_TIE={session_file.tie()}\n")
    (state / f"key-{name}.lock").write_text("")


def _age(path: Path, sec: float) -> None:
    import time

    at = time.time() - sec
    os.utime(path, (at, at))


def test_with_a_bot_only_its_own_names_are_predecessors(state: Path, tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """Two sessions share the workspace tie (two bots): the restarting one takes its own bot's
    name, never the other bot's, however new; the other's state stays."""
    _harness(monkeypatch, tmp_path, "workspace")
    _name_run(state, NAME, UID, "mine")
    _name_run(state, NAME_C, UID2, "theirs")
    _age(state / f"copied-{NAME}.env", 60)  # the other session's name is newer
    monkeypatch.setenv("CB_PLANB_TEST", "2")
    session_file.latch(NAME_B, HOME, UID)
    assert (state / f"asks-{NAME_B}.env").read_text() == "mine\n"
    assert (state / f"asks-{NAME_C}.env").exists() and (state / f"stats-{NAME_C}.jsonl").exists()
    assert "CB_TAKEN_BY" not in (state / f"copied-{NAME_C}.env").read_text()


def test_without_a_bot_the_newest_gone_name_of_the_tie_is_taken(state: Path, tmp_path: Path,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """No bot, the workspace tie (one MCP server per workspace): a live sibling's newer name
    is passed over, the newest name whose process is gone is taken."""
    _harness(monkeypatch, tmp_path, "workspace")
    _name_run(state, NAME, "", "mine")
    _name_run(state, NAME_C, "", "live sibling")
    _age(state / f"copied-{NAME}.env", 60)
    fd = _hold(state, NAME_C)
    try:
        monkeypatch.setenv("CB_PLANB_TEST", "2")
        session_file.latch(NAME_B, HOME, "")
        assert (state / f"asks-{NAME_B}.env").read_text() == "mine\n"
        assert (state / f"stats-{NAME_B}.jsonl").read_text() == '{"k":"mine"}\n'
        assert (state / f"asks-{NAME_C}.env").exists() and (state / f"stats-{NAME_C}.jsonl").exists()
    finally:
        os.close(fd)


def test_two_latches_racing_for_one_predecessor(state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _harness(monkeypatch, tmp_path, "claude")
    _name_run(state, NAME, UID, "run 1")
    first, held = session_file._copy_source(UID, NAME_B)
    assert first == NAME and len(held) == 1
    # The second start meanwhile: the predecessor's lock is held, so it falls back.
    assert session_file._copy_source(UID, NAME_C) == (UID, [])
    session_file._mark(state / f"copied-{NAME}.env", add=[f"CB_TAKEN_BY={NAME_B}"])
    os.close(held[0])
    # A start that listed the predecessor before the mark re-reads it under the lock.
    monkeypatch.setattr(session_file, "_candidates", lambda bot, name: [NAME])
    assert session_file._copy_source(UID, NAME_D) == (UID, [])


def test_a_missing_lock_is_gone_only_for_an_old_marker(state: Path, tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    _harness(monkeypatch, tmp_path, "claude")
    _name_run(state, NAME, UID, "run 1")
    (state / f"key-{NAME}.lock").unlink()
    session = state / "mcp-x.env"
    session.write_text(f"CB_MM_BOT_USER_ID={UID}\n")
    assert session_file._claim(NAME) is None  # a process may still be starting
    _age(state / f"copied-{NAME}.env", session_file.GONE_SEC + 60)
    fd = session_file._claim(NAME)
    assert fd is not None
    os.close(fd)


def test_the_first_start_after_the_upgrade_takes_a_lockless_predecessor(state: Path, tmp_path: Path,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """f5eed5's low 2 on #328: a predecessor without a lock file (the plugin before, or a lock
    the sweep removed) is gone once its marker is older than a process's start (GONE_SEC, an
    hour, not a day), or at once when none of its bots has a session file left."""
    assert session_file.GONE_SEC == 3600
    _harness(monkeypatch, tmp_path, "claude")
    _name_run(state, NAME, UID, "run 1")
    (state / f"key-{NAME}.lock").unlink()
    _age(state / f"copied-{NAME}.env", 120)
    fd = session_file._claim(NAME)  # its bot has no session file: gone
    assert fd is not None and (state / f"key-{NAME}.lock").exists()
    os.close(fd)
    (state / f"key-{NAME}.lock").unlink()
    (state / "mcp-x.env").write_text(f"export CB_MM_BOT_USER_ID='{UID}'\n")
    assert session_file._claim(NAME) is None  # its bot runs and the marker is young
    _age(state / f"copied-{NAME}.env", session_file.GONE_SEC + 1)
    monkeypatch.setenv("CB_PLANB_TEST", "2")
    session_file.latch(NAME_B, HOME, UID)
    assert (state / f"asks-{NAME_B}.env").read_text() == "run 1\n"


def test_a_move_without_a_target_is_one_rename(state: Path) -> None:
    src, dst = state / f"stats-{NAME}.jsonl", state / f"stats-{NAME_B}.jsonl"
    src.write_text("a\n")
    ino = src.stat().st_ino
    assert session_file._move(src, dst) and dst.stat().st_ino == ino and not src.exists()


# --- plan B piece 14c-1b: the takeover hardened (#244) ------------------------------------

def _hold_for(path: Path, sec: float, how: int = fcntl.LOCK_SH) -> threading.Thread:
    """Hold a flock on `path` for `sec` seconds in a thread, as the sweep's probe does for an
    instant (a shared lock) or another start's claim (an exclusive one)."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, how | fcntl.LOCK_NB)

    def release() -> None:
        time.sleep(sec)
        os.close(fd)

    t = threading.Thread(target=release)
    t.start()
    return t


def test_a_lock_held_for_an_instant_is_waited_out(state: Path, tmp_path: Path) -> None:
    """RT-6.2 step 9: a probe or a claim that holds key-<name>.lock for an instant costs the
    starting server neither its name nor its predecessor: _hold_key and _claim try again
    (LOCK_RETRIES times, LOCK_RETRY_SEC apart) before they give up."""
    assert (session_file.LOCK_RETRIES, session_file.LOCK_RETRY_SEC) == (3, 0.05)
    t = _hold_for(state / f"key-{NAME}.lock", 0.06)
    assert session_file._hold_key(NAME) == ""
    t.join()
    (state / f"copied-{NAME_B}.env").write_text("")
    t = _hold_for(state / f"key-{NAME_B}.lock", 0.06, fcntl.LOCK_EX)
    fd = session_file._claim(NAME_B)
    t.join()
    assert fd is not None
    os.close(fd)


def test_a_lock_held_for_longer_still_refuses(state: Path) -> None:
    fd = _hold(state, NAME)
    try:
        started = time.monotonic()
        assert session_file._hold_key(NAME) == "another running MCP server keys its state by the same NATS name"
        assert time.monotonic() - started >= 3 * 0.05
        assert session_file._claim(NAME) is None
    finally:
        os.close(fd)


def test_two_servers_without_a_bot_on_one_workspace_tie(state: Path, tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.2 step 9: without a bot on the workspace tie the first server holds
    tie-<hash>.lock for its run and takes the gone predecessor over; a second server in the
    same workspace does not get it: fresh state under its own name, nothing taken, no CB_TIE
    (so no later start mixes it in), and bus_info says so."""
    _harness(monkeypatch, tmp_path, "workspace")
    _name_run(state, NAME, "", "run 1")
    monkeypatch.setenv("CB_PLANB_TEST", "2")
    session_file.latch(NAME_B, HOME, "")
    assert (state / f"asks-{NAME_B}.env").read_text() == "run 1\n"
    assert (state / f"tie-{session_file.tie().partition(':')[2]}.lock").exists()
    assert session_file.tie_refused() == ""
    _name_run(state, NAME_D, "", "a gone one the first did not take")
    _restart(monkeypatch, release=False)  # the first server still runs
    session_file.latch(NAME_C, HOME, "")
    assert session_file.state_key("") == NAME_C
    assert not (state / f"asks-{NAME_C}.env").exists() and not (state / f"stats-{NAME_C}.jsonl").exists()
    assert "CB_TAKEN_BY" not in (state / f"copied-{NAME_D}.env").read_text()
    assert "CB_TIE=" not in (state / f"copied-{NAME_C}.env").read_text()
    assert session_file.tie_refused() == "another MCP server without a bot runs in this workspace"
    assert any("takes no earlier state over" in n and "another MCP server without a bot" in n
               for n in lease.nats_notes())


def test_a_counters_file_that_is_not_utf8_is_skipped(state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                     caplog: pytest.LogCaptureFixture) -> None:
    """RT-6.2 step 9: a kept file that is not UTF-8 blocks no latch: a warning, the file stays
    where it is, every other kind is copied and the key is latched."""
    _harness(monkeypatch, tmp_path, "claude")
    _name_run(state, NAME, UID, "run 1")
    (state / f"stats-{NAME}.jsonl").write_bytes(b'{"k":"\xff\xfe"}\n')
    (state / f"remind-{NAME}.env").write_bytes(b"\xff\n")
    (state / f"stats-{NAME}.jsonl.moving-999999").write_bytes(b"\xc3\x28\n")
    monkeypatch.setenv("CB_PLANB_TEST", "2")
    with caplog.at_level("WARNING"):
        session_file.latch(NAME_B, HOME, UID)
    assert session_file.state_key(UID) == NAME_B
    assert (state / f"asks-{NAME_B}.env").read_text() == "run 1\n"
    assert (state / f"stats-{NAME}.jsonl").read_bytes() == b'{"k":"\xff\xfe"}\n'
    assert (state / f"stats-{NAME}.jsonl.moving-999999").exists()
    assert not (state / f"remind-{NAME_B}.env").exists()
    assert sum("is not UTF-8" in r.getMessage() for r in caplog.records) == 3


# A child process runs the latch and is SIGKILLed before its n-th file operation (a rename,
# replace, unlink, truncate or write), or in "half" mode halfway through that write.
_KILLED_LATCH = r'''
import os, signal, sys
from mattermost_ai_agent_bus import session_file
at, half, name, uid = int(sys.argv[1]), sys.argv[2] == "half", sys.argv[3], sys.argv[4]
count = 0
def step(data=None, fd=None):
    global count
    count += 1
    if count == at:
        if data is not None and half and len(data) > 1:
            real_write(fd, bytes(data[:len(data) // 2]))
        os.kill(os.getpid(), signal.SIGKILL)
for fn in ("rename", "replace", "unlink", "truncate"):
    def wrap(*a, _real=getattr(os, fn), **k):
        step()
        return _real(*a, **k)
    setattr(os, fn, wrap)
real_write = os.write
def write(fd, data):
    step(data, fd)
    return real_write(fd, data)
os.write = write
session_file.latch(name, "work", uid)
print("done")
'''


def _killed_latch(state: Path, tmp_path: Path, at: int, mode: str, name: str, uid: str) -> bool:
    """Run the latch of `name` in a child killed at its `at`-th file operation: whether it
    finished before that."""
    import subprocess
    import sys

    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "XDG_STATE_HOME": str(state.parent),
           "CB_PLANB_TEST": "2", "CLAUDE_CODE_SESSION_ID": "s-1", "CB_WORKSPACE": str(tmp_path),
           "CB_MM_BOT_USER_ID": uid, "PYTEST_CURRENT_TEST": os.environ.get("PYTEST_CURRENT_TEST", "x")}
    r = subprocess.run([sys.executable, "-c", _KILLED_LATCH, str(at), mode, name, uid], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode in (0, -9), r.stderr
    return r.returncode == 0


def _scenario(state: Path, kind: str) -> list[str]:
    """The state before the killed start; the counter lines the session has in all."""
    for f in state.iterdir():
        f.unlink()
    if kind == "bot":  # no predecessor: the bot id's counters and a claim an older kill left
        (state / f"stats-{UID}.jsonl").write_text('{"k":"bot 1"}\n{"k":"bot 2"}\n')
        (state / f"stats-{UID}.jsonl.moving-999999").write_text('{"k":"left"}\n')
        (state / f"asks-{UID}.env").write_text("run 1\n")
        return ['{"k":"bot 1"}', '{"k":"bot 2"}', '{"k":"left"}']
    _name_run(state, NAME, UID, "run 1")
    _age(state / f"copied-{NAME}.env", 60)
    if kind == "append":  # a claim an older kill left (or #328's target-side one): two inputs, the journaled append
        (state / f"stats-{NAME}.jsonl.moving-999999").write_text('{"k":"left 1"}\n{"k":"left 2"}\n')
        return ['{"k":"left 1"}', '{"k":"left 2"}', '{"k":"run 1"}']
    return ['{"k":"run 1"}']


@pytest.mark.parametrize(("kind", "mode"), [("rename", "whole"), ("append", "whole"), ("append", "half"),
                                            ("bot", "whole"), ("bot", "half")])
def test_a_kill_anywhere_in_the_takeover_loses_and_doubles_nothing(state: Path, tmp_path: Path,
                                                                   monkeypatch: pytest.MonkeyPatch,
                                                                   kind: str, mode: str) -> None:
    """RT-6.2 step 9 (f5eed5's low 1 on #328): a start SIGKILLed before any file operation of
    its latch (between the claim and the rename, between the journal and the append, halfway
    through the append, between the append and the claim's removal, between the marks), then
    the next start: every counter line is in the new name's file exactly once, no claim is
    left, no takeover stays unfinished, and the kept state came along."""
    _harness(monkeypatch, tmp_path, "claude")
    monkeypatch.setenv("CB_PLANB_TEST", "2")
    killed = 0
    for at in range(1, 60):
        expected = _scenario(state, kind)
        if _killed_latch(state, tmp_path, at, mode, NAME_B, UID):
            break
        killed += 1
        _restart(monkeypatch)
        session_file.latch(NAME_C, HOME, UID)
        lines = (state / f"stats-{NAME_C}.jsonl").read_text().splitlines()
        assert sorted(lines) == sorted(expected), (at, lines)
        assert not list(state.glob("*.moving-*")), at
        assert not [m.name for m in state.glob("copied-*.env") if "CB_TAKING=" in m.read_text()], at
        assert (state / f"asks-{NAME_C}.env").read_text() == "run 1\n", at
        _restart(monkeypatch)
    assert killed >= 5


# --- plan B piece 14c-1c: a stranded claim keeps the takeover open (#244) -----------------

def _lines(state: Path, name: str) -> list[str]:
    return sorted((state / f"stats-{name}.jsonl").read_text().splitlines())


def test_a_claim_of_a_pid_that_runs_keeps_the_takeover_open(state: Path, tmp_path: Path,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.2 step 9: a leftover claim whose pid runs (a taker still at work, or its pid
    reused by an unrelated process) is left, and the takeover stays open: the intent stays,
    the source gets no CB_TAKEN_BY, and the start after that one finishes it."""
    _harness(monkeypatch, tmp_path, "claude")
    _name_run(state, NAME, UID, "run 1")
    _age(state / f"copied-{NAME}.env", 60)
    left = state / f"stats-{NAME}.jsonl.moving-1"  # pid 1 runs (and may not be signalled)
    left.write_text('{"k":"left"}\n')
    monkeypatch.setenv("CB_PLANB_TEST", "2")
    session_file.latch(NAME_B, HOME, UID)
    marked = (state / f"copied-{NAME_B}.env").read_text()
    assert left.exists() and f"CB_MOVING={NAME}" in marked and "CB_TAKING" not in marked
    assert "CB_TAKEN_BY" not in (state / f"copied-{NAME}.env").read_text()
    _restart(monkeypatch)
    monkeypatch.setattr(session_file, "_pid_alive", lambda pid: False)  # that process ended
    session_file.latch(NAME_C, HOME, UID)
    assert _lines(state, NAME_C) == ['{"k":"left"}', '{"k":"run 1"}']
    assert not list(state.glob("*.moving-*"))
    assert not [m.name for m in state.glob("copied-*.env") if "CB_TAKING=" in m.read_text() or
                "CB_MOVING=" in m.read_text()]


def test_a_failed_journal_write_keeps_the_takeover_open(state: Path, tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.2 step 9: an append whose journal line cannot be written leaves its claim, and
    the takeover stays open instead of being marked done: the next start finishes it."""
    _harness(monkeypatch, tmp_path, "claude")
    _name_run(state, NAME, UID, "run 1")
    _age(state / f"copied-{NAME}.env", 60)
    (state / f"stats-{NAME}.jsonl.moving-999999").write_text('{"k":"left"}\n')  # two inputs: an append
    real = session_file._mark

    def no_journal(marker: Path, add_ids=(), add=(), drop=()) -> bool:
        if any(a.startswith("CB_APPENDING=") for a in add):
            return False
        return real(marker, add_ids, add, drop)

    monkeypatch.setattr(session_file, "_mark", no_journal)
    monkeypatch.setenv("CB_PLANB_TEST", "2")
    session_file.latch(NAME_B, HOME, UID)
    assert list(state.glob(f"stats-{NAME}.jsonl.moving-*"))
    assert f"CB_MOVING={NAME}" in (state / f"copied-{NAME_B}.env").read_text()
    assert "CB_TAKEN_BY" not in (state / f"copied-{NAME}.env").read_text()
    monkeypatch.setattr(session_file, "_mark", real)
    _restart(monkeypatch)  # the process ends: its claim is a dead one's
    session_file.latch(NAME_C, HOME, UID)
    assert _lines(state, NAME_C) == ['{"k":"left"}', '{"k":"run 1"}']
    assert not list(state.glob("*.moving-*"))


def test_a_name_being_taken_is_passed_over_under_its_lock(state: Path, tmp_path: Path,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.2 step 9: a start listed the candidates before another start wrote its intent
    (CB_TAKING) on the same predecessor; under the predecessor's lock it reads the markers
    again and passes it over: one takeover per name."""
    _harness(monkeypatch, tmp_path, "claude")
    _name_run(state, NAME, UID, "run 1")
    (state / f"copied-{NAME_B}.env").write_text(
        f"CB_COPIED_FROM={UID}\nCB_TIE={session_file.tie()}\nCB_TAKING={NAME}\n")
    fd = _hold(state, NAME_B)  # that start still runs
    try:
        monkeypatch.setattr(session_file, "_candidates", lambda bot, name: [NAME])  # listed before
        assert session_file._copy_source(UID, NAME_C) == (UID, [])
        assert "CB_TAKEN_BY" not in (state / f"copied-{NAME}.env").read_text()
    finally:
        os.close(fd)


def test_a_source_another_name_took_is_not_taken_again(state: Path, tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.2 step 9: an unfinished takeover whose source another name took over meanwhile
    (its CB_TAKEN_BY names that one) drops its intent: the claims its marker journals are
    resolved into its own file (no double), the source's state is not copied again."""
    _harness(monkeypatch, tmp_path, "claude")
    _name_run(state, NAME, UID, "run 1")
    (state / f"copied-{NAME}.env").write_text(f"CB_COPIED_FROM={UID}\nCB_TIE={session_file.tie()}\n"
                                             f"CB_TAKEN_BY={NAME_D}\n")
    (state / f"stats-{NAME}.jsonl").unlink()
    (state / f"asks-{NAME}.env").write_text("taken by the other one\n")
    part = '{"k":"b"}\n'
    (state / f"stats-{NAME_B}.jsonl").write_text(part + '{"k":"x"}\n')  # appended, not yet removed
    (state / f"stats-{NAME}.jsonl.moving-999999").write_text('{"k":"x"}\n')
    (state / f"copied-{NAME_B}.env").write_text(
        f"CB_COPIED_FROM={UID}\nCB_TIE={session_file.tie()}\nCB_TAKING={NAME}\n"
        f"CB_APPENDING=stats-{NAME}.jsonl.moving-999999:{len(part)}\n")
    (state / f"key-{NAME_B}.lock").write_text("")
    monkeypatch.setenv("CB_PLANB_TEST", "2")
    session_file.latch(NAME_C, HOME, UID)
    assert _lines(state, NAME_C) == ['{"k":"b"}', '{"k":"x"}']
    assert not (state / f"asks-{NAME_C}.env").exists()
    assert not list(state.glob("*.moving-*"))
    assert "CB_TAKING" not in (state / f"copied-{NAME_B}.env").read_text()
    assert f"CB_TAKEN_BY={NAME_C}" in (state / f"copied-{NAME_B}.env").read_text()


def test_a_name_that_ran_with_an_open_move_gets_only_its_counters_moved(state: Path, tmp_path: Path,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.2 step 9 (review of #340): a name whose counter move a claim left open runs on,
    deletes a file in its normal work (an empty unanswered- list removes it) and counts on;
    the next start moves only the counters: no deleted file comes back stale, every counter
    line is counted once."""
    _harness(monkeypatch, tmp_path, "claude")
    _name_run(state, NAME, UID, "run 1")
    _age(state / f"copied-{NAME}.env", 60)
    (state / f"unanswered-{NAME}.env").write_text("post 1\n")
    (state / f"stats-{NAME}.jsonl.moving-1").write_text('{"k":"left"}\n')  # pid 1 runs
    monkeypatch.setenv("CB_PLANB_TEST", "2")
    session_file.latch(NAME_B, HOME, UID)
    assert (state / f"unanswered-{NAME_B}.env").exists()
    (state / f"unanswered-{NAME_B}.env").unlink()  # N's own work
    with (state / f"stats-{NAME_B}.jsonl").open("a") as f:
        f.write('{"k":"own"}\n')
    _restart(monkeypatch)  # N exits
    monkeypatch.setattr(session_file, "_pid_alive", lambda pid: False)
    session_file.latch(NAME_C, HOME, UID)
    assert not (state / f"unanswered-{NAME_C}.env").exists()
    assert _lines(state, NAME_C) == ['{"k":"left"}', '{"k":"own"}', '{"k":"run 1"}']
    assert "CB_MOVING" not in (state / f"copied-{NAME_B}.env").read_text()
    assert f"CB_TAKEN_BY={NAME_B}" in (state / f"copied-{NAME}.env").read_text()
    assert not list(state.glob("*.moving-*"))


def test_a_short_write_leaves_no_fragment(state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.2 step 9 (review of #340): an append that fails after a short write (a full disk)
    is cut back to the journaled size: the retry appends the claim once, and the name's own
    next line is not glued to a fragment."""
    _harness(monkeypatch, tmp_path, "claude")
    _name_run(state, NAME, UID, "run 1")
    _age(state / f"copied-{NAME}.env", 60)
    (state / f"stats-{NAME}.jsonl.moving-999999").write_text('{"k":"left"}\n')  # two inputs: an append
    real = session_file._append_bytes

    def short(path: Path, data: bytes) -> None:
        real(path, data[: len(data) // 2])
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(session_file, "_append_bytes", short)
    monkeypatch.setenv("CB_PLANB_TEST", "2")
    session_file.latch(NAME_B, HOME, UID)
    assert (state / f"stats-{NAME_B}.jsonl").read_text().splitlines() == ['{"k":"left"}']
    # Cut back at the narrowing: a plain claim, no size line that the name's own appends
    # (or its part cut) would make wrong.
    assert "CB_APPENDING" not in (state / f"copied-{NAME_B}.env").read_text()
    monkeypatch.setattr(session_file, "_append_bytes", real)
    with (state / f"stats-{NAME_B}.jsonl").open("a") as f:
        f.write('{"k":"own"}\n')
    _restart(monkeypatch)
    session_file.latch(NAME_C, HOME, UID)
    assert _lines(state, NAME_C) == ['{"k":"left"}', '{"k":"own"}', '{"k":"run 1"}']


def test_a_narrowing_that_cannot_be_written_refuses_the_latch(state: Path, tmp_path: Path,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.2 step 9 (review of #340): the open takeover cannot be narrowed to its counters
    (CB_MOVING not written, a full disk): the name never runs with CB_TAKING, the key stays
    the bot id and bus_info says why; the next start takes the unused name over whole."""
    _harness(monkeypatch, tmp_path, "claude")
    _name_run(state, NAME, UID, "run 1")
    _age(state / f"copied-{NAME}.env", 60)
    (state / f"stats-{NAME}.jsonl.moving-1").write_text('{"k":"left"}\n')  # pid 1 runs: left open
    real = session_file._mark

    def no_narrowing(marker: Path, add_ids=(), add=(), drop=()) -> bool:
        if any(a.startswith("CB_MOVING=") for a in add):
            return False
        return real(marker, add_ids, add, drop)

    monkeypatch.setattr(session_file, "_mark", no_narrowing)
    monkeypatch.setenv("CB_PLANB_TEST", "2")
    session_file.latch(NAME_B, HOME, UID)
    assert session_file.state_key(UID) == UID
    assert "could not be recorded" in session_file.latch_refused()
    assert any("could not be recorded" in n for n in lease.nats_notes())
    assert f"CB_TAKING={NAME}" in (state / f"copied-{NAME_B}.env").read_text()
    assert session_file.latch(NAME_B, HOME, UID) == [] and session_file.state_key(UID) == UID
    monkeypatch.setattr(session_file, "_mark", real)
    _restart(monkeypatch)
    monkeypatch.setattr(session_file, "_refused", "")
    monkeypatch.setattr(session_file, "_pid_alive", lambda pid: False)
    session_file.latch(NAME_C, HOME, UID)
    assert _lines(state, NAME_C) == ['{"k":"left"}', '{"k":"run 1"}']
    assert (state / f"asks-{NAME_C}.env").read_text() == "run 1\n"


def test_an_appended_claim_whose_removal_failed_is_never_appended_again(state: Path, tmp_path: Path,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-6.2 step 9 (review of #340): the claim was appended whole but could not be removed;
    the narrowing marks it CB_APPENDED. The name runs on and cuts its part (stats.cut replaces
    the file, publishing those lines); the next start only removes the claim: no double."""
    _harness(monkeypatch, tmp_path, "claude")
    _name_run(state, NAME, UID, "run 1")
    _age(state / f"copied-{NAME}.env", 60)
    (state / f"stats-{NAME}.jsonl.moving-999999").write_text('{"k":"left"}\n')  # two inputs: an append
    real = Path.unlink

    def stuck(self: Path, missing_ok: bool = False) -> None:
        if ".moving-" in self.name and self.name.endswith(f"-{os.getpid()}"):
            raise PermissionError(1, "Operation not permitted")
        real(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", stuck)
    monkeypatch.setenv("CB_PLANB_TEST", "2")
    session_file.latch(NAME_B, HOME, UID)
    monkeypatch.setattr(Path, "unlink", real)
    claim = f"stats-{NAME}.jsonl.moving-{os.getpid()}"
    marked = (state / f"copied-{NAME_B}.env").read_text()
    assert f"CB_APPENDED={claim}" in marked and "CB_APPENDING" not in marked and f"CB_MOVING={NAME}" in marked
    assert _lines(state, NAME_B) == ['{"k":"left"}', '{"k":"run 1"}']
    # The name's own part cut: its lines published, the file replaced by what is still open.
    session_file.write_private(state / f"stats-{NAME_B}.jsonl", '{"k":"open"}\n')
    _restart(monkeypatch)
    session_file.latch(NAME_C, HOME, UID)
    assert _lines(state, NAME_C) == ['{"k":"open"}']
    assert not list(state.glob("*.moving-*"))
    assert "CB_APPENDED" not in (state / f"copied-{NAME_B}.env").read_text()
