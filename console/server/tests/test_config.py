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

    # nats:// anywhere but loopback is refused
    monkeypatch.setenv("CONSOLE_NATS_URL", "nats://nats.example:4222")
    with pytest.raises(config.ConfigError, match="loopback"):
        config.find()

    # the loopback URL needs nothing else: no seed, no account (RT-6.43)
    monkeypatch.setenv("CONSOLE_NATS_URL", "nats://127.0.0.1:4222")
    s = config.find()
    assert s.nats_url == "nats://127.0.0.1:4222"


def test_login_off_needs_no_keycloak(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("CONSOLE_PUBLIC_URL", "http://127.0.0.1:9999")
    monkeypatch.setenv("CONSOLE_LOGIN_OFF", "1")
    monkeypatch.setenv("CONSOLE_NATS_URL", "nats://127.0.0.1:4222")
    s = config.find()
    assert s.login_off
    assert not config.secure_cookie(s)


def test_cookie_secure_off_loopback(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("CONSOLE_NATS_URL", "nats://127.0.0.1:4222")
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
    for k, v in {"CONSOLE_PUBLIC_URL": "https://console.example",
                 "CONSOLE_ISSUER": "https://kc", "CONSOLE_CLIENT_ID": "console",
                 "CONSOLE_NATS_URL": "nats://127.0.0.1:4222",
                 }.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(config.ConfigError, match="CONSOLE_OPERATOR_GROUP"):
        config.find()
    monkeypatch.setenv("CONSOLE_OPERATOR_GROUP", "operators")
    assert config.find().operator_group == "operators"


def test_login_off_refuses_a_public_listener(monkeypatch: pytest.MonkeyPatch,
                                             tmp_path) -> None:
    for k, v in {"CONSOLE_PUBLIC_URL": "http://127.0.0.1:8747",
                 "CONSOLE_LOGIN_OFF": "1",
                 "CONSOLE_NATS_URL": "nats://127.0.0.1:4222",
                 "CONSOLE_ADDR": "0.0.0.0:8747"}.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(config.ConfigError, match="non-loopback"):
        config.find()
    monkeypatch.setenv("CONSOLE_ADDR", "127.0.0.1:8747")
    assert config.find().listen_host == "127.0.0.1"


def test_a_static_seed_refuses_the_start_unread(monkeypatch: pytest.MonkeyPatch,
                                                tmp_path) -> None:
    """The static account is gone (RT-6.43, the console decision's point 7):
    a leftover CONSOLE_NATS_SEED_FILE refuses the start, so its secret is
    removed too, and the file is never opened — not even to check it."""
    for k, v in {"CONSOLE_PUBLIC_URL": "http://127.0.0.1:8747",
                 "CONSOLE_LOGIN_OFF": "1",
                 "CONSOLE_NATS_URL": "nats://127.0.0.1:4222"}.items():
        monkeypatch.setenv(k, v)
    settings = config.find()
    assert not hasattr(settings, "nats_seed_file")
    seed = tmp_path / "seed"
    seed.write_text("SUA-not-a-real-seed")
    seed.chmod(0o000)          # an open would fail loudly
    monkeypatch.setenv("CONSOLE_NATS_SEED_FILE", str(seed))
    import builtins
    opened: list[str] = []
    real_open = builtins.open

    def spy(file, *a, **kw):  # type: ignore[no-untyped-def]
        opened.append(str(file))
        return real_open(file, *a, **kw)
    monkeypatch.setattr(builtins, "open", spy)
    with pytest.raises(config.ConfigError, match="static NATS account is gone"):
        config.find()
    assert str(seed) not in opened
