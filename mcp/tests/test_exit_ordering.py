"""RT-6.10 error handling: the exit work removes the endpoint file whenever
it exists on disk, so the file's owner must be known before the file can be
there.

Each test takes one moment the endpoint file can exist unnamed — inside
``os.replace``, right after the file appeared. There the exit work runs
in-process, so nothing races and nothing sleeps.

These are real gaps, and they are **not** shown to be the cause of CI's flake in
PR #27: that write happens before the client's handshake, and the test sends
SIGTERM only after it, and a client without the `roots` capability never calls
`rekey` at all (review of #34).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mattermost_ai_agent_bus import hookapi, server, session_file

UID = "u" * 26


class FakeClient:
    """Enough of a bus client for the hook API's getter: it only asks for one."""

    token = "t"
    bot_id = UID

    async def get_me(self) -> dict[str, str]:
        return {"id": UID}


@pytest.fixture
def started(monkeypatch):
    """The server's module as at its start, with a bus and no exit work done."""
    monkeypatch.setattr(server, "_get_client", lambda: FakeClient())
    for name, value in (("_hook_api", None), ("_client", None), ("_confirmed", None),
                        ("_exit_handled", False)):
        monkeypatch.setattr(server, name, value)


async def test_the_path_is_known_before_the_file_can_exist(monkeypatch):
    """RT-6.10: ``_write_file`` names the path before ``os.replace`` puts the
    file there, so ``remove_file`` in that moment removes it. The old order
    assigned the path after the replace: an exit work in that gap left the file
    behind. A real gap; whether CI's flake hit it is not shown (above)."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-in-replace")
    seen: list[tuple[Path | None, bool]] = []
    real = os.replace

    async def get() -> FakeClient:
        return FakeClient()

    api = hookapi.HookAPI(get)

    def replace(src: str, dst: str) -> None:
        real(src, dst)
        seen.append((api._file, Path(dst).exists()))
        api.remove_file()  # the exit work, right after the file appeared
        seen.append((api._file, Path(dst).exists()))

    monkeypatch.setattr(os, "replace", replace)
    await api.start()
    try:
        # Named while the file is there, and gone after the exit work.
        assert seen == [(hookapi.file_path(), True), (None, False)]
    finally:
        await api.close()


async def test_the_server_knows_its_hook_api_before_the_file_can_exist(started, monkeypatch):
    """RT-6.10: ``_start_hook_api`` sets the module's ``_hook_api`` before
    ``start()`` writes the file, so the exit work reaches it. The old order set
    it after the await: an exit work in the write's gap found ``None`` and left
    the file behind. A real gap; whether CI's flake hit it is not shown."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-server-start")
    left: list[list[Path]] = []
    real = os.replace

    def replace(src: str, dst: str) -> None:
        real(src, dst)
        server._cleanup_on_exit()  # the signal, right after the file appeared
        left.append(sorted(session_file.state_dir().glob("endpoint-*")))

    monkeypatch.setattr(os, "replace", replace)
    api = await server._start_hook_api()
    assert api is not None
    try:
        assert left == [[]]
    finally:
        await api.close()


async def test_a_rekey_keeps_the_file_named_until_the_exit_can_remove_it(started, monkeypatch,
                                                                        tmp_path):
    """RT-6.10: ``rekey`` keeps ``_file`` naming the old file until
    ``_write_file`` names the new one, so an exit work that runs in between
    still finds a file to remove. The old order cleared it first: the exit
    work saw ``None`` with the old file still on disk and left it there."""
    for var in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_PROJECT_DIR", "MM_WORKSPACE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(session_file, "_roots_dir", None)
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    monkeypatch.chdir(one)
    api = await server._start_hook_api()
    assert api is not None
    left: list[list[Path]] = []
    real = api._write_file

    def write_after_exit(uid: str | None = None) -> None:
        server._cleanup_on_exit()  # the signal, before the new file is written
        left.append(sorted(session_file.state_dir().glob("endpoint-*")))
        real(uid)

    try:
        monkeypatch.setattr(api, "_write_file", write_after_exit)
        assert session_file.set_roots_dir(str(two.resolve()))
        api.rekey()
        # The one file that was there at the exit work went with it.
        assert left == [[]]
    finally:
        await api.close()


async def test_a_rekey_names_both_files_while_both_are_on_disk(started, monkeypatch, tmp_path):
    """RT-6.10: a rekey writes the new file and then removes the old one, so
    for that moment both are on disk. The exit work must remove both: it knew
    only the new file, and the old one outlived the process. The exit work is
    the last thing that runs, so whatever it leaves stays."""
    for var in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_PROJECT_DIR", "MM_WORKSPACE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(session_file, "_roots_dir", None)
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    monkeypatch.chdir(one)
    api = await server._start_hook_api()
    assert api is not None
    left: list[list[Path]] = []
    real = api._write_file

    def write_file_then_exit(uid: str | None = None) -> None:
        real(uid)  # the new file is there, the old one not yet removed
        server._cleanup_on_exit()  # the signal, in that moment
        left.append(sorted(session_file.state_dir().glob("endpoint-*")))

    try:
        monkeypatch.setattr(api, "_write_file", write_file_then_exit)
        assert session_file.set_roots_dir(str(two.resolve()))
        api.rekey()
        assert left == [[]]
    finally:
        await api.close()


async def test_a_failed_replace_keeps_the_earlier_name(started, monkeypatch, tmp_path):
    """A rewrite that fails must not forget the file that is still there.

    `_write_file` names the new path before `os.replace`, so a failed replace has
    to put the earlier name back. The case that matters is a *changed* path: if
    the name stays as it was, a missing restore is invisible — which is why the
    first version of this test passed with the restore deleted. Here the API
    names one file and the write aims at another, and the replace fails
    (review of #34).
    """
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-replace-fails")
    api = hookapi.HookAPI(lambda: FakeClient())
    await api.start()
    mine = api._file
    assert mine is not None and mine.exists()

    # A different target, so the restore has something to put back.
    other = mine.with_name("endpoint-sid-ffffffffffffffff.env")
    monkeypatch.setattr(hookapi, "file_path", lambda: other)

    def broken(_tmp, _path):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(hookapi.os, "replace", broken)
    with pytest.raises(OSError):
        api._write_file()
    assert api._file == mine, "the earlier name was not restored"
    assert mine.exists()

    # And the exit work still removes the file it knows (the write left a .tmp).
    api.remove_file()
    assert not mine.exists()
    await api.close()


async def test_a_failed_start_leaves_no_hook_api_behind(started, monkeypatch):
    """`_start_hook_api` sets `_hook_api` before `start()` writes the file, so a
    start that raises must clear it again: a half-started API must not be what
    the exit work removes files through (review of #34)."""

    async def refuse(_self, **_kw):
        raise OSError(99, "Cannot assign requested address")

    monkeypatch.setattr(hookapi.HookAPI, "start", refuse)
    assert await server._start_hook_api() is None
    assert server._hook_api is None


async def test_remove_file_unlinks_before_it_forgets(started, monkeypatch):
    """`remove_file` must not forget a name before the file is gone.

    The order is the point: clearing `_file` and `_moving` first leaves a window
    where an exit work interrupted inside `_unlink_own` (a close, a signal) finds
    nothing left to remove, and the file stays. With the old order this test
    fails; the six others stay green either way (review of #34).
    """
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-unlink-first")
    api = hookapi.HookAPI(lambda: FakeClient())
    await api.start()
    mine = api._file
    assert mine is not None and mine.exists()

    seen: list[tuple[Path | None, Path | None]] = []
    real = hookapi.HookAPI._unlink_own

    def observe(self, path):
        # What the API still names at the moment it unlinks: both must be set.
        seen.append((self._file, self._moving))
        real(self, path)

    monkeypatch.setattr(hookapi.HookAPI, "_unlink_own", observe)
    api.remove_file()
    assert seen == [(mine, None)], seen
    assert not mine.exists()
    assert api._file is None and api._moving is None
    await api.close()


def test_a_signal_does_the_exit_work_outside_the_main_thread(monkeypatch):
    """A signal handler runs in the main thread wherever it stood, also inside
    a write to stderr: a log line of its own then raised RuntimeError (a
    reentrant call into the stream) and left it before the exit work, so the
    timer ended the process with the endpoint file still there (CI on 5619f49).
    The handler only starts the exit work in a thread of its own."""
    import signal
    import threading

    done = threading.Event()
    ran: list[tuple[str, bool]] = []

    def finish(why: str, deadline: float = 0.0) -> None:
        ran.append((why, threading.current_thread() is threading.main_thread()))
        done.set()

    monkeypatch.setattr(server, "_finish", finish)
    monkeypatch.setattr(server, "_watch_parent", lambda: None)
    monkeypatch.setattr(server.atexit, "register", lambda f: None)
    old = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP)}
    try:
        server._install_exit_handlers()
        os.kill(os.getpid(), signal.SIGTERM)
        assert done.wait(5)
    finally:
        for s, h in old.items():
            signal.signal(s, h)
    assert ran == [("signal SIGTERM", False)]
