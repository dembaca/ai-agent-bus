"""The login's key stores (RT-6.34): file modes, symlinks, atomic writes, no secret in a repr or error."""

import base64
import os
import stat
import sys
from pathlib import Path

import pytest

from mattermost_ai_agent_bus import keystore

SECRET = "s3cr3t-value-0123456789"


def test_ephemeral_keeps_values_in_memory_only():
    s = keystore.Ephemeral()
    assert s.load("refresh_token") is None
    s.save("refresh_token", SECRET)
    assert s.load("refresh_token") == SECRET
    assert SECRET not in repr(s)
    s.delete("refresh_token")
    assert s.load("refresh_token") is None
    assert keystore.Ephemeral().load("refresh_token") is None  # nothing shared between stores


def test_hostfile_writes_0600_in_a_0700_directory(tmp_path):
    d = tmp_path / "state" / "login"
    s = keystore.HostFile(d)
    s.save("refresh_token", SECRET)
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    assert stat.S_IMODE((d / "refresh_token").stat().st_mode) == 0o600
    assert s.load("refresh_token") == SECRET
    assert SECRET not in repr(s)


def test_hostfile_default_directory_is_login_under_the_host_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert keystore.HostFile().directory == tmp_path / "mm-agent-bus" / "login"


def test_hostfile_replaces_atomically_and_leaves_no_temp_file(tmp_path):
    s = keystore.HostFile(tmp_path / "login")
    s.save("nkey_seed", "one")
    s.save("nkey_seed", "two")
    assert s.load("nkey_seed") == "two"
    assert sorted(p.name for p in (tmp_path / "login").iterdir()) == ["nkey_seed"]


def test_hostfile_failed_write_keeps_the_old_value(tmp_path, monkeypatch):
    s = keystore.HostFile(tmp_path / "login")
    s.save("nkey_seed", "old")

    def boom(src, dst):
        raise OSError("disk")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        s.save("nkey_seed", "new")
    monkeypatch.undo()
    assert s.load("nkey_seed") == "old"
    assert [p.name for p in (tmp_path / "login").iterdir()] == ["nkey_seed"]


def test_hostfile_refuses_a_symlink(tmp_path):
    d = tmp_path / "login"
    s = keystore.HostFile(d)
    s.save("refresh_token", "x")
    target = tmp_path / "elsewhere"
    target.write_text(SECRET)
    target.chmod(0o600)
    (d / "refresh_token").unlink()
    (d / "refresh_token").symlink_to(target)
    with pytest.raises(keystore.KeyStoreError) as err:
        s.load("refresh_token")
    assert SECRET not in str(err.value)
    s.save("refresh_token", "new")  # a write replaces the link, never follows it
    assert target.read_text() == SECRET
    assert s.load("refresh_token") == "new"


def test_hostfile_refuses_a_file_readable_by_group_or_world(tmp_path):
    d = tmp_path / "login"
    s = keystore.HostFile(d)
    s.save("refresh_token", SECRET)
    for mode in (0o640, 0o604, 0o644):
        (d / "refresh_token").chmod(mode)
        with pytest.raises(keystore.KeyStoreError) as err:
            s.load("refresh_token")
        assert SECRET not in str(err.value)


def test_hostfile_refuses_a_file_of_another_owner(tmp_path, monkeypatch):
    s = keystore.HostFile(tmp_path / "login")
    s.save("refresh_token", SECRET)
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(tmp_path).st_uid + 1)
    with pytest.raises(keystore.KeyStoreError):
        s.load("refresh_token")


def test_hostfile_refuses_a_directory_open_to_group_or_world_for_reading(tmp_path):
    d = tmp_path / "login"
    s = keystore.HostFile(d)
    s.save("refresh_token", SECRET)
    d.chmod(0o755)
    with pytest.raises(keystore.KeyStoreError):
        s.load("refresh_token")
    s.save("refresh_token", SECRET)  # writing tightens a directory of ours
    assert stat.S_IMODE(d.stat().st_mode) == 0o700


def test_hostfile_refuses_a_symlinked_directory(tmp_path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "login"
    link.symlink_to(real)
    with pytest.raises(keystore.KeyStoreError):
        keystore.HostFile(link).save("refresh_token", SECRET)
    assert list(real.iterdir()) == []


def test_hostfile_missing_is_none_and_delete_is_quiet(tmp_path):
    s = keystore.HostFile(tmp_path / "nothing" / "login")
    assert s.load("refresh_token") is None
    s.delete("refresh_token")


def test_hostfile_entry_names_are_checked(tmp_path):
    s = keystore.HostFile(tmp_path / "login")
    for bad in ("../x", "a/b", "", "X"):
        with pytest.raises(keystore.KeyStoreError):
            s.save(bad, "v")


def test_both_stores_fit_the_protocol(tmp_path):
    stores: list[keystore.KeyStore] = [keystore.Ephemeral(), keystore.HostFile(tmp_path / "login")]
    for s in stores:
        s.save(keystore.NKEY_SEED, "v")
        assert s.load(keystore.NKEY_SEED) == "v"
        s.delete(keystore.NKEY_SEED)
        assert s.load(keystore.NKEY_SEED) is None
    assert isinstance(Path(tmp_path), Path)


def test_hostfile_syncs_its_directory_after_a_replace_and_a_delete(tmp_path, monkeypatch):
    synced = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(stat.S_ISDIR(os.fstat(fd).st_mode)), real(fd))[1])
    s = keystore.HostFile(tmp_path / "login")
    s.save("refresh_token", SECRET)
    assert synced == [False, True]      # the file, then the directory
    s.delete("refresh_token")
    assert synced[-1] is True


def test_ephemeral_lock_is_exclusive_per_name():
    s = keystore.Ephemeral()
    h = s.try_lock("a")
    assert h is not None and s.try_lock("a") is None and s.try_lock("b") is not None
    s.unlock(h)
    assert s.try_lock("a") is not None


def test_try_lock_reports_a_held_lock_as_none_and_any_other_error_as_an_error(tmp_path, monkeypatch):
    import errno
    files = keystore.HostFile(tmp_path / "login")

    def busy(fd, op):
        raise BlockingIOError(errno.EWOULDBLOCK, "held")
    monkeypatch.setattr(keystore.fcntl, "flock", busy)
    assert files.try_lock("refresh_a") is None

    def no_locks(fd, op):
        raise OSError(errno.ENOLCK, "No locks available")
    monkeypatch.setattr(keystore.fcntl, "flock", no_locks)
    with pytest.raises(OSError) as err:
        files.try_lock("refresh_a")
    assert err.value.errno == errno.ENOLCK


# --- the system keychain and the chooser (RT-6.34, #154 decision 10) ---------

COOLDOWN = "1764000000.5 approval denied\nby the human"  # spaces and a newline survive


class FakeCli:
    """Plays `security` and `secret-tool` in memory and records every call.

    The value a store hands over must arrive on stdin only: the tests check
    each recorded argv against it."""

    def __init__(self, *, mac_i_takes: bool = True) -> None:
        self.items: dict[tuple[str, str], bytes] = {}
        self.calls: list[tuple[list[str], bytes | None, float]] = []
        self.mac_i_takes = mac_i_takes

    def __call__(self, argv, stdin, timeout):
        argv = list(argv)
        self.calls.append((argv, stdin, timeout))
        tool, args = argv[0], argv[1:]
        if tool.endswith("security"):
            return self._mac(args, stdin)
        return self._secret_tool(args, stdin)

    @staticmethod
    def _opt(args, flag):
        return args[args.index(flag) + 1]

    def _mac(self, args, stdin):
        if args == ["-i"]:
            words = stdin.decode().split()
            assert words[0] == "add-generic-password" and "-U" in words
            if self.mac_i_takes:
                self.items[(self._opt(words, "-s"), self._opt(words, "-a"))] = self._opt(words, "-w").encode()
            return keystore.Ran(0, b"", b"")
        key = (self._opt(args, "-s"), self._opt(args, "-a"))
        missing = keystore.Ran(44, b"", b"security: The specified item could not be found in the keychain.\n")
        if args[0] == "find-generic-password":
            if key not in self.items:
                return missing
            # without -w `security` prints attributes only
            return keystore.Ran(0, self.items[key] + b"\n" if "-w" in args else b"attributes", b"")
        if args[0] == "delete-generic-password":
            return keystore.Ran(0, b"", b"") if self.items.pop(key, None) is not None else missing
        raise AssertionError(args)

    def _secret_tool(self, args, stdin):
        i = args.index("service")
        key = (args[i + 1], args[args.index("account") + 1])
        if args[0] == "store":
            self.items[key] = stdin
            return keystore.Ran(0)
        if args[0] == "lookup":
            return keystore.Ran(0, self.items[key], b"") if key in self.items else keystore.Ran(1)
        if args[0] == "clear":
            self.items.pop(key, None)
            return keystore.Ran(0)
        raise AssertionError(args)

    def assert_never_on_argv(self, *values):
        for argv, _, _ in self.calls:
            for v in values:
                assert not any(v in a for a in argv), argv


def _encoded(v: str) -> str:
    return base64.b64encode(v.encode()).decode()


@pytest.mark.parametrize("kind", [keystore.MacKeychain, keystore.SecretTool])
def test_keychain_roundtrip_with_the_secret_only_on_stdin(kind, tmp_path):
    cli = FakeCli()
    s = kind(tmp_path / "login", runner=cli)
    assert s.load("refresh_a") is None
    s.save("refresh_a", SECRET)
    s.save(keystore.NKEY_SEED, "SUAMKEYSEED")
    s.save("cool_a", COOLDOWN)
    assert s.load("refresh_a") == SECRET
    assert s.load("cool_a") == COOLDOWN.strip()
    s.save("refresh_a", SECRET + "2")  # an update replaces
    assert s.load("refresh_a") == SECRET + "2"
    s.delete("refresh_a")
    s.delete("refresh_a")  # a missing entry deletes quietly
    assert s.load("refresh_a") is None
    assert {svc for svc, _ in cli.items} == {keystore.SERVICE}
    cli.assert_never_on_argv(SECRET, _encoded(SECRET), "SUAMKEYSEED", "approval")
    assert any(stdin and (SECRET.encode() in stdin or _encoded(SECRET).encode() in stdin)
               for _, stdin, _ in cli.calls)
    assert all(t == keystore.OP_TIMEOUT for _, _, t in cli.calls)
    assert SECRET not in repr(s)


def test_mac_keychain_uses_the_system_tool_and_account_is_the_entry(tmp_path):
    cli = FakeCli()
    s = keystore.MacKeychain(tmp_path / "login", runner=cli)
    s.save("refresh_a", SECRET)
    argv, stdin, _ = cli.calls[0]
    assert argv == ["/usr/bin/security", "-i"]
    assert stdin is not None and stdin.endswith(b"\n") and stdin.count(b"\n") == 1
    assert ("mm-agent-bus-login", "refresh_a") in cli.items


def test_mac_keychain_write_that_did_not_take_is_an_error(tmp_path):
    s = keystore.MacKeychain(tmp_path / "login", runner=FakeCli(mac_i_takes=False))
    with pytest.raises(keystore.KeyStoreError, match="refresh_a: keychain write failed"):
        s.save("refresh_a", SECRET)


def test_mac_keychain_keeps_a_value_too_long_for_one_command_line_in_the_0600_file(tmp_path, caplog):
    cli = FakeCli()
    d = tmp_path / "login"
    s = keystore.MacKeychain(d, runner=cli)
    s.save("refresh_a", "short")
    long_value = SECRET * 200
    with caplog.at_level("WARNING"):
        s.save("refresh_a", long_value)
    assert "refresh_a is too long for the keychain, kept in the 0600 file" in caplog.text
    assert SECRET not in caplog.text
    assert (keystore.SERVICE, "refresh_a") not in cli.items  # the stale short item is gone
    assert stat.S_IMODE((d / "overflow" / "refresh_a").stat().st_mode) == 0o600
    assert s.load("refresh_a") == long_value
    cli.assert_never_on_argv(SECRET)
    s.save("refresh_a", "short again")  # back in the keychain, the file goes
    assert not (d / "overflow" / "refresh_a").exists() and s.load("refresh_a") == "short again"
    s.save("refresh_a", long_value)
    s.delete("refresh_a")
    assert s.load("refresh_a") is None


@pytest.mark.parametrize("kind", [keystore.MacKeychain, keystore.SecretTool, keystore.HostFile])
def test_every_store_refuses_an_empty_value(kind, tmp_path):
    cli = FakeCli()
    s = kind(tmp_path / "login") if kind is keystore.HostFile else kind(tmp_path / "login", runner=cli)
    with pytest.raises(keystore.KeyStoreError, match="refresh_a: an empty value is not stored"):
        s.save("refresh_a", "")
    assert cli.calls == []


@pytest.mark.parametrize("service", ["", "Mm", "a b", "a;rm", "x" * 70, "-s"])
def test_keychain_service_name_is_checked(service, tmp_path):
    with pytest.raises(keystore.KeyStoreError, match="invalid keychain service name"):
        keystore.MacKeychain(tmp_path, runner=FakeCli(), service=service)


@pytest.mark.parametrize("kind", [keystore.MacKeychain, keystore.SecretTool])
def test_keychain_errors_name_the_entry_never_a_value_or_the_tools_output(kind, tmp_path):
    def failing(argv, stdin, timeout):
        return keystore.Ran(51, SECRET.encode(), b"error near " + SECRET.encode())
    s = kind(tmp_path / "login", runner=failing)
    for op in (lambda: s.load("refresh_a"), lambda: s.save("refresh_a", SECRET),
               lambda: s.delete("refresh_a")):
        with pytest.raises(keystore.KeyStoreError) as err:
            op()
        assert "refresh_a" in str(err.value) and SECRET not in str(err.value)
        assert err.value.__cause__ is None and err.value.__context__ is None
    with pytest.raises(keystore.KeyStoreError, match="invalid store entry name"):
        s.load("../x")


def test_mac_keychain_refuses_an_item_that_is_not_its_encoding(tmp_path):
    cli = FakeCli()
    cli.items[(keystore.SERVICE, "refresh_a")] = b"not base64 !"
    with pytest.raises(keystore.KeyStoreError, match="not a value of this store"):
        keystore.MacKeychain(tmp_path / "login", runner=cli).load("refresh_a")


def test_secret_tool_reports_a_lookup_error_apart_from_a_missing_item(tmp_path):
    s = keystore.SecretTool(tmp_path / "login",
                            runner=lambda a, i, t: keystore.Ran(1, b"", b"secret-tool: Cannot autolaunch D-Bus"))
    with pytest.raises(keystore.KeyStoreError, match="read failed"):
        s.load("refresh_a")


def test_ran_has_no_value_in_its_repr():
    assert SECRET not in repr(keystore.Ran(0, SECRET.encode(), SECRET.encode()))


def test_run_feeds_stdin_and_never_the_servers_own(tmp_path):
    count = [sys.executable, "-c", "import sys; print(len(sys.stdin.buffer.read()))"]
    assert keystore.run(count, None, 10).out.strip() == b"0"
    assert keystore.run(count, b"abc", 10).out.strip() == b"3"


def test_run_kills_a_hanging_tool_and_names_only_the_tool():
    with pytest.raises(keystore.KeyStoreError, match="timed out") as err:
        keystore.run([sys.executable, "-c", "import time; time.sleep(30)", SECRET], None, 0.3)
    assert SECRET not in str(err.value)
    assert err.value.__cause__ is None and err.value.__context__ is None  # no partial output along


def test_run_of_a_missing_tool_is_a_store_error(tmp_path):
    with pytest.raises(keystore.KeyStoreError, match="cannot run"):
        keystore.run([str(tmp_path / "nope")], None, 1)


def test_keychain_locks_are_flock_files_shared_across_store_objects(tmp_path):
    d = tmp_path / "login"
    a = keystore.MacKeychain(d, runner=FakeCli())
    b = keystore.MacKeychain(d, runner=FakeCli())
    files = keystore.HostFile(d)
    h = a.try_lock("refresh_a")
    assert h is not None
    assert b.try_lock("refresh_a") is None and files.try_lock("refresh_a") is None
    hb = b.try_lock("refresh_b")
    assert hb is not None
    b.unlock(hb)
    a.unlock(h)
    h2 = keystore.SecretTool(d, runner=FakeCli()).try_lock("refresh_a")
    assert h2 is not None
    files.unlock(h2)
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    assert (d / ".refresh_a.lock").exists()


def test_keychain_stores_fit_the_protocol(tmp_path):
    stores: list[keystore.KeyStore] = [keystore.MacKeychain(tmp_path, runner=FakeCli()),
                                       keystore.SecretTool(tmp_path, runner=FakeCli())]
    for s in stores:
        s.save(keystore.NKEY_SEED, "v")
        assert s.load(keystore.NKEY_SEED) == "v"


# the chooser


@pytest.fixture
def state(tmp_path, monkeypatch):
    """The host's state directory of these tests (session_file.host_state_dir)."""
    d = tmp_path / "host-state"
    monkeypatch.setenv("XDG_STATE_HOME", str(d))
    return d



def _tools(**found):
    return lambda name: found.get(name.replace("-", "_"))


MAC = {"platform": "darwin", "find_tool": _tools(security="/usr/bin/security")}


def test_find_tool_takes_fixed_paths_never_path(monkeypatch, tmp_path):
    fake = tmp_path / "secret-tool"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    seen = []
    monkeypatch.setattr(keystore.os, "access", lambda p, m: seen.append(p) or False)
    assert keystore._find_tool("secret-tool") is None and keystore._find_tool("security") is None
    assert seen == ["/usr/bin/secret-tool", "/usr/local/bin/secret-tool", "/usr/bin/security"]


def test_choose_reads_first_and_takes_a_keychain_that_holds_the_seed(state):
    cli = FakeCli()
    cli.items[(keystore.SERVICE, keystore.NKEY_SEED)] = _encoded("SUAMKEYSEED").encode()
    s = keystore.choose({}, runner=cli, **MAC)
    assert isinstance(s, keystore.MacKeychain) and s.timeout == keystore.OP_TIMEOUT
    assert [c[0] for c in cli.calls] == [["/usr/bin/security", "find-generic-password", "-s",
                                          keystore.SERVICE, "-a", "nkey_seed"]]  # no -w, no write
    assert all(t == keystore.PROBE_TIMEOUT for _, _, t in cli.calls)
    assert (state / "mm-agent-bus" / "login" / "store_backend").read_text() == "keychain"


def test_choose_writes_a_probe_of_its_own_name_only_when_there_is_nothing_to_read(state):
    cli = FakeCli()
    s = keystore.choose({}, runner=cli, **MAC)
    assert isinstance(s, keystore.MacKeychain)
    assert cli.calls[0][0][1:] == ["find-generic-password", "-s", keystore.SERVICE, "-a", "nkey_seed"]
    assert cli.items == {}  # the probe entry is gone
    probes = {argv[argv.index("-a") + 1] for argv, _, _ in cli.calls if "-a" in argv}
    probes |= {stdin.decode().split()[5] for argv, stdin, _ in cli.calls if argv[-1] == "-i"}
    probes.discard("nkey_seed")
    (probe,) = probes
    assert keystore._NAME_RE.fullmatch(probe) and probe.startswith("probe_") and len(probe) == 22


def test_two_first_starts_never_share_a_probe_name(state, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "a"))
    c1, c2 = FakeCli(), FakeCli()
    keystore.choose({}, runner=c1, **MAC)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "b"))
    keystore.choose({}, runner=c2, **MAC)
    n1 = {a[a.index("-a") + 1] for a, _, _ in c1.calls if "-a" in a} - {"nkey_seed"}
    n2 = {a[a.index("-a") + 1] for a, _, _ in c2.calls if "-a" in a} - {"nkey_seed"}
    assert n1 and n2 and not n1 & n2


def test_choose_takes_secret_tool_on_linux(state):
    s = keystore.choose({}, platform="linux", runner=FakeCli(),
                        find_tool=_tools(secret_tool="/usr/bin/secret-tool"))
    assert isinstance(s, keystore.SecretTool) and s.tool == "/usr/bin/secret-tool"


@pytest.mark.parametrize("platform,tools", [("darwin", {}), ("linux", {}), ("win32", {"security": "x"})])
def test_choose_without_a_keychain_takes_the_file(state, platform, tools):
    def never(*a):
        raise AssertionError("no keychain tool may run")
    s = keystore.choose({}, platform=platform, runner=never, find_tool=_tools(**tools))
    assert isinstance(s, keystore.HostFile)
    assert s.directory == state / "mm-agent-bus" / "login"


def test_choose_falls_back_to_the_file_when_the_keychain_is_denied(state, caplog):
    def denied(argv, stdin, timeout):
        return keystore.Ran(1, b"", b"Mach lookup denied")
    with caplog.at_level("INFO"):
        s = keystore.choose({}, runner=denied, **MAC)
    assert isinstance(s, keystore.HostFile)
    assert "using the 0600 file" in caplog.text


def test_choose_falls_back_within_the_probe_timeout_when_the_keychain_hangs(state):
    hang = [sys.executable, "-c", "import time; time.sleep(30)"]

    def hanging(argv, stdin, timeout):
        return keystore.run(hang, stdin, timeout)  # the real runner, its timeout kills it
    import time
    t0 = time.monotonic()
    s = keystore.choose({}, platform="linux", runner=hanging,
                        find_tool=_tools(secret_tool="secret-tool"), probe_timeout=0.3)
    assert isinstance(s, keystore.HostFile)
    assert time.monotonic() - t0 < 5


def test_the_first_choice_sticks_so_sessions_never_split(state):
    def denied(argv, stdin, timeout):
        return keystore.Ran(1)
    assert isinstance(keystore.choose({}, runner=denied, **MAC), keystore.HostFile)
    # the keychain answers at the next start: the host stays with the file

    def never(*a):
        raise AssertionError("a recorded choice probes nothing")
    assert isinstance(keystore.choose({}, runner=never, **MAC), keystore.HostFile)
    marker = state / "mm-agent-bus" / "login" / "store_backend"
    assert marker.read_text() == "file" and stat.S_IMODE(marker.stat().st_mode) == 0o600


def test_a_keychain_once_chosen_stays_chosen_even_when_it_is_slow_at_a_start(state):
    assert isinstance(keystore.choose({}, runner=FakeCli(), **MAC), keystore.MacKeychain)

    def never(*a):
        raise AssertionError("a recorded choice probes nothing")
    s = keystore.choose({}, runner=never, **MAC)
    assert isinstance(s, keystore.MacKeychain) and s.timeout == keystore.OP_TIMEOUT
    # without the tool any more: the file, with a warning, the marker kept
    assert isinstance(keystore.choose({}, runner=never, platform="darwin", find_tool=_tools()),
                      keystore.HostFile)
    assert (state / "mm-agent-bus" / "login" / "store_backend").read_text() == "keychain"


def test_choose_under_corral_takes_the_file_in_the_sandbox_home_never_a_keychain(state, tmp_path):
    home = tmp_path / "sandbox-home"
    env = {"CORRAL_SANDBOX": "1", "HOME": str(home)}

    def never(*a):
        raise AssertionError("no keychain under corral")
    s = keystore.choose(env, runner=never, **MAC)
    assert isinstance(s, keystore.HostFile)
    assert s.directory == home / ".local" / "state" / "mm-agent-bus" / "login"
    s.save("refresh_a", SECRET)
    assert stat.S_IMODE((s.directory / "refresh_a").stat().st_mode) == 0o600
    assert not state.exists()  # the host state dir (XDG_STATE_HOME) stays untouched


def test_two_corral_sandboxes_never_share_an_entry_or_a_keychain_item(state, tmp_path):
    cli = FakeCli()  # one keychain of the whole host: no sandbox may reach it
    a = keystore.choose({"CORRAL_SANDBOX": "1", "HOME": str(tmp_path / "a")}, runner=cli, **MAC)
    b = keystore.choose({"CORRAL_SANDBOX": "1", "HOME": str(tmp_path / "b")}, runner=cli, **MAC)
    a.save("refresh_a", SECRET)
    assert b.load("refresh_a") is None
    assert cli.calls == [] and cli.items == {}
    ha, hb = a.try_lock("refresh_a"), b.try_lock("refresh_a")  # each its own lock file
    assert ha is not None and hb is not None
    a.unlock(ha)
    b.unlock(hb)


def test_without_corral_the_keychain_locks_in_the_one_host_state_dir(state):
    k = keystore.choose({}, runner=FakeCli(), **MAC)
    assert isinstance(k, keystore.MacKeychain)
    assert k._locks.directory == state / "mm-agent-bus" / "login"
    other = keystore.HostFile(state / "mm-agent-bus" / "login")
    h = k.try_lock("refresh_a")
    assert h is not None and other.try_lock("refresh_a") is None
    k.unlock(h)


def test_default_chooses_once_per_process(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(keystore, "_chosen", None)
    monkeypatch.setattr(keystore, "choose", lambda: calls.append(1) or keystore.HostFile(tmp_path))
    first = keystore.default()
    assert keystore.default() is first and calls == [1]


# migration of an earlier store


def test_migrate_copies_what_the_new_store_lacks_and_clears_the_old(tmp_path):
    old = keystore.HostFile(tmp_path / "old")
    new = keystore.MacKeychain(tmp_path / "old", runner=FakeCli())  # locks beside the old files
    old.save("refresh_a", SECRET)
    old.save("cool_a", "1 why")
    old.save(keystore.NKEY_SEED, "OLDSEED")
    new.save(keystore.NKEY_SEED, "NEWSEED")
    moved = keystore.migrate_from(old, new, ["refresh_a", "cool_a", keystore.NKEY_SEED, "refresh_b"])
    assert moved == ["refresh_a", "cool_a", keystore.NKEY_SEED]
    assert new.load("refresh_a") == SECRET and new.load("cool_a") == "1 why"
    assert new.load(keystore.NKEY_SEED) == "NEWSEED"  # the new store's own value wins
    assert all(old.load(n) is None for n in ("refresh_a", "cool_a", keystore.NKEY_SEED))
    assert keystore.migrate_from(old, new, ["refresh_a"]) == []  # once


def test_migrate_skips_an_entry_whose_lock_another_holder_has(tmp_path):
    old, new = keystore.HostFile(tmp_path / "old"), keystore.HostFile(tmp_path / "new")
    old.save("refresh_a", SECRET)
    h = keystore.HostFile(tmp_path / "new").try_lock("refresh_a")
    assert keystore.migrate_from(old, new, ["refresh_a"]) == []
    assert old.load("refresh_a") == SECRET and new.load("refresh_a") is None
    keystore.HostFile(tmp_path / "new").unlock(h)
    assert keystore.migrate_from(old, new, ["refresh_a"]) == ["refresh_a"]


def test_migrate_keeps_the_old_entry_when_the_copy_fails(tmp_path):
    old = keystore.HostFile(tmp_path / "old")
    old.save("refresh_a", SECRET)
    broken = keystore.SecretTool(tmp_path / "new", runner=lambda a, i, t: keystore.Ran(5, b"", b"x"))
    with pytest.raises(keystore.KeyStoreError) as err:
        keystore.migrate_from(old, broken, ["refresh_a"])
    assert SECRET not in str(err.value)
    assert old.load("refresh_a") == SECRET
    assert broken.try_lock("refresh_a") is not None  # the lock was given back


def test_migrate_between_stores_at_the_same_place_does_nothing(tmp_path):
    a, b = keystore.HostFile(tmp_path / "login"), keystore.HostFile(tmp_path / "login")
    a.save("refresh_a", SECRET)
    assert keystore.migrate_from(a, b, ["refresh_a"]) == []
    assert a.load("refresh_a") == SECRET
