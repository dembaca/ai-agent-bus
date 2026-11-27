"""#261: an unknown CB_ name and a home outside CB_NATS_HOMES are said, not silent (RT-6.34 step 4)."""

import asyncio
import json

from mattermost_ai_agent_bus import config, hookapi, lease, server


def test_a_wrong_prefix_gets_the_swap():
    assert config.unknown_names({"CB_PROJECT_CHANNEL": "agents"}) == [
        "unknown setting CB_PROJECT_CHANNEL (did you mean CB_MM_PROJECT_CHANNEL?)"]


def test_the_swap_works_the_other_way():
    assert config.unknown_names({"CB_MM_NATS_HOMES": "a"}) == [
        "unknown setting CB_MM_NATS_HOMES (did you mean CB_NATS_HOMES?)"]


def test_a_typo_gets_the_closest_name_and_a_stranger_none():
    out = config.unknown_names({"CB_NATS_HOMEZ": "a", "CB_QQQQQQQQ": "b"})
    assert out == ["unknown setting CB_NATS_HOMEZ (did you mean CB_NATS_HOMES?)",
                   "unknown setting CB_QQQQQQQQ"]


def test_known_names_and_other_prefixes_are_silent():
    env = {n: "x" for n in (*config.RENAMED, *config.INTERNAL, *config.OTHER)}
    env |= {"MM_OLD": "x", "HOME": "/h", "CB_BRIDGE_ANYTHING": "x", "CB_AUTH_NATS_URL": "x"}
    assert config.unknown_names(env) == []


def test_odd_names_are_counted_not_shown():
    env = {"CB_BAD NAME\n": "x", "CB_lower": "x", "CB_\x1b[31mred": "x", "CB_QQQQQQQQ": "y"}
    assert config.unknown_names(env) == ["unknown setting CB_QQQQQQQQ", "and 3 more unknown CB_ settings"]


def test_more_than_the_cap_say_how_many_more():
    env = {f"CB_ZZZZ{i:02d}": "x" for i in range(14)}
    out = config.unknown_names(env)
    assert len(out) == config.UNKNOWN_CAP + 1
    assert out[0] == "unknown setting CB_ZZZZ00"
    assert out[-2] == "unknown setting CB_ZZZZ09"
    assert out[-1] == "and 4 more unknown CB_ settings"


def test_exactly_the_cap_has_no_more_line():
    env = {f"CB_ZZZZ{i:02d}": "x" for i in range(config.UNKNOWN_CAP)}
    assert not any(l.startswith("and ") for l in config.unknown_names(env))


def test_values_never_appear():
    out = "".join(config.unknown_names({"CB_PROJECT_CHANNEL": "s3cr3t-value"}))
    assert "s3cr3t-value" not in out


def test_bus_info_off_the_bus_carries_the_warning(monkeypatch):
    for var in ("MM_CHAT_URL", "MATTERMOST_URL", "MM_BOT_TOKEN", "MATTERMOST_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(server, "_client", None)
    monkeypatch.setenv("CB_PROJECT_CHANNEL", "agents")
    out = json.loads(asyncio.run(server.bus_info()))
    assert "unknown setting CB_PROJECT_CHANNEL (did you mean CB_MM_PROJECT_CHANNEL?)" in out["warning"]


def test_a_home_outside_the_nats_homes_says_why(monkeypatch):
    lease.use_nats("", None)
    lease.left_out("agents", ["vtpm-mds", "bgl-infra"])
    try:
        want = ("home ~agents is not in CB_NATS_HOMES (bgl-infra, vtpm-mds): this session stays on "
                "Mattermost and starts no NATS login")
        assert want in lease.nats_notes()
        assert want in hookapi.start_notes()
    finally:
        lease.use_nats("", None)
    assert lease.left_out_note() == ""
