"""The console server's settings: required keys raise their names, the
NATS URL rule is the transport's (tls:// anywhere, nats:// loopback only),
the cookie is secure off loopback."""
from __future__ import annotations

import pytest

from console_server import config


def test_required(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(config.ConfigError, match="CONSOLE_PUBLIC_URL"):
        config.find()


def test_nat_url_rules(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    base = {"CONSOLE_PUBLIC_URL": "https://console.example", "CONSOLE_LOGIN_OFF": "1"}
    for k, v in base.items():
        monkeypatch.setenv(k, v)

    # nats:// anywhere but loopback is refused, seed or not
    monkeypatch.setenv("CONSOLE_NATS_URL", "nats://nats.example:4222")
    with pytest.raises(config.ConfigError, match="loopback"):
        config.find()

    # the loopback URL needs the seed of the console's own account
    monkeypatch.setenv("CONSOLE_NATS_URL", "nats://127.0.0.1:4222")
    with pytest.raises(config.ConfigError, match="CONSOLE_NATS_SEED_FILE"):
        config.find()

    seed = tmp_path / "seed"
    seed.write_text("seed bytes")
    seed.chmod(0o600)
    monkeypatch.setenv("CONSOLE_NATS_SEED_FILE", str(seed))
    s = config.find()
    assert s.nats_url == "nats://127.0.0.1:4222"


def test_login_off_needs_no_keycloak(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    seed = tmp_path / "seed"
    seed.write_text("seed bytes")
    seed.chmod(0o600)
    monkeypatch.setenv("CONSOLE_PUBLIC_URL", "http://127.0.0.1:9999")
    monkeypatch.setenv("CONSOLE_LOGIN_OFF", "1")
    monkeypatch.setenv("CONSOLE_NATS_URL", "nats://127.0.0.1:4222")
    monkeypatch.setenv("CONSOLE_NATS_SEED_FILE", str(seed))
    s = config.find()
    assert s.login_off
    assert not config.secure_cookie(s)


def test_cookie_secure_off_loopback(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    seed = tmp_path / "seed"
    seed.write_text("seed bytes")
    seed.chmod(0o600)
    monkeypatch.setenv("CONSOLE_NATS_URL", "nats://127.0.0.1:4222")
    monkeypatch.setenv("CONSOLE_NATS_SEED_FILE", str(seed))
    monkeypatch.setenv("CONSOLE_PUBLIC_URL", "https://console.example")
    monkeypatch.setenv("CONSOLE_LOGIN_OFF", "1")
    assert config.secure_cookie(config.find())
    monkeypatch.setenv("CONSOLE_PUBLIC_URL", "http://console.example")
    with pytest.raises(config.ConfigError, match="https"):
        config.find()


def test_the_operators_group_is_required(monkeypatch: pytest.MonkeyPatch,
                                        tmp_path) -> None:
    """Fail closed (the reviews' blocker): an unset group admits nobody of
    the realm's choosing — the console refuses to start."""
    seed = tmp_path / "seed"
    seed.write_text("seed")
    seed.chmod(0o600)
    for k, v in {"CONSOLE_PUBLIC_URL": "https://console.example",
                 "CONSOLE_ISSUER": "https://kc", "CONSOLE_CLIENT_ID": "console",
                 "CONSOLE_NATS_URL": "nats://127.0.0.1:4222",
                 "CONSOLE_NATS_SEED_FILE": str(seed)}.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(config.ConfigError, match="CONSOLE_OPERATOR_GROUP"):
        config.find()
    monkeypatch.setenv("CONSOLE_OPERATOR_GROUP", "operators")
    assert config.find().operator_group == "operators"


def test_login_off_refuses_a_public_listener(monkeypatch: pytest.MonkeyPatch,
                                             tmp_path) -> None:
    seed = tmp_path / "seed"
    seed.write_text("seed")
    seed.chmod(0o600)
    for k, v in {"CONSOLE_PUBLIC_URL": "http://127.0.0.1:8747",
                 "CONSOLE_LOGIN_OFF": "1",
                 "CONSOLE_NATS_URL": "nats://127.0.0.1:4222",
                 "CONSOLE_NATS_SEED_FILE": str(seed),
                 "CONSOLE_ADDR": "0.0.0.0:8747"}.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(config.ConfigError, match="non-loopback"):
        config.find()
    monkeypatch.setenv("CONSOLE_ADDR", "127.0.0.1:8747")
    assert config.find().listen_host == "127.0.0.1"


def test_the_seed_file_is_the_owners_only(monkeypatch: pytest.MonkeyPatch,
                                          tmp_path) -> None:
    seed = tmp_path / "seed"
    seed.write_text("seed")
    seed.chmod(0o644)
    for k, v in {"CONSOLE_PUBLIC_URL": "http://127.0.0.1:8747",
                 "CONSOLE_LOGIN_OFF": "1",
                 "CONSOLE_NATS_URL": "nats://127.0.0.1:4222",
                 "CONSOLE_NATS_SEED_FILE": str(seed)}.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(config.ConfigError, match="owner"):
        config.find()
    seed.chmod(0o600)
    assert config.find().nats_seed_file == str(seed)


def test_an_unreadable_seed_is_a_config_error(monkeypatch: pytest.MonkeyPatch,
                                              tmp_path) -> None:
    """Not only the mode: the file must open and read — a directory named
    as the seed, or an unreadable file, refuses the start with a
    ConfigError, not a later traceback."""
    seed = tmp_path / "seed"
    seed.write_text("seed")
    seed.chmod(0o600)
    for k, v in {"CONSOLE_PUBLIC_URL": "http://127.0.0.1:8747",
                 "CONSOLE_LOGIN_OFF": "1",
                 "CONSOLE_NATS_URL": "nats://127.0.0.1:4222",
                 "CONSOLE_NATS_SEED_FILE": str(seed)}.items():
        monkeypatch.setenv(k, v)
    assert config.find().nats_seed_file == str(seed)
    gone = tmp_path / "gone"
    gone.mkdir()
    monkeypatch.setenv("CONSOLE_NATS_SEED_FILE", str(gone))
    # a directory trips the mode check first (0755) or the read: either
    # way the start refuses, the point of the test
    with pytest.raises(config.ConfigError):
        config.find()
    no_read = tmp_path / "noread"
    no_read.write_text("seed")
    no_read.chmod(0o000)
    monkeypatch.setenv("CONSOLE_NATS_SEED_FILE", str(no_read))
    with pytest.raises(config.ConfigError, match="cannot be read"):
        config.find()


def test_the_health_sync_bound_is_bounded(monkeypatch: pytest.MonkeyPatch,
                                           tmp_path) -> None:
    """The probe's bound: 30 s to 24 h, or the start refuses; a value that
    is no number names itself."""
    seed = tmp_path / "seed"
    seed.write_text("seed")
    seed.chmod(0o600)
    base = {"CONSOLE_PUBLIC_URL": "http://127.0.0.1:8747",
            "CONSOLE_LOGIN_OFF": "1",
            "CONSOLE_NATS_URL": "nats://127.0.0.1:4222",
            "CONSOLE_NATS_SEED_FILE": str(seed)}
    for k, v in base.items():
        monkeypatch.setenv(k, v)
    assert config.find().health_sync_bound == 180.0
    for raw in ("0", "-10", "99999999"):
        monkeypatch.setenv("CONSOLE_HEALTH_SYNC_BOUND", raw)
        with pytest.raises(config.ConfigError, match="SYNC_BOUND"):
            config.find()
    monkeypatch.setenv("CONSOLE_HEALTH_SYNC_BOUND", "abc")
    with pytest.raises(config.ConfigError, match="number of seconds"):
        config.find()
    monkeypatch.setenv("CONSOLE_HEALTH_SYNC_BOUND", "60")
    assert config.find().health_sync_bound == 60.0
