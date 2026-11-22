"""The login's key stores (RT-6.34): file modes, symlinks, atomic writes, no secret in a repr or error."""

import os
import stat
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
