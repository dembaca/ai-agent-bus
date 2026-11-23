"""RT-6.10 error handling: the exit work removes the endpoint file whenever
it exists on disk, so the file's owner must be known before the file can be
there.

Each test takes the one moment CI's flake hit (PR #27): inside ``os.replace``,
right after the file appeared. There the exit work runs in-process, so nothing
races and nothing sleeps.
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
    assigned the path after the replace: the file stayed, and CI's SIGTERM
    landed in exactly that gap (PR #27)."""
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
    it after the await: a SIGTERM in the write's gap found ``None`` and left
    the file behind (PR #27)."""
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
    work saw ``None`` with the old file still on disk and left it there, the
    same flake (PR #27)."""
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
