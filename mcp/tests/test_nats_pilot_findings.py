"""What the first NATS pilot found (#66): bus_info says that people do not see a home
over NATS in Mattermost (RT-6.36), and post_message to a person says why it refuses."""

import json

import pytest

from mattermost_ai_agent_bus import lease, server

from test_debug import chat, pinned  # noqa: F401 — fixtures


@pytest.fixture
def nats_home():
    lease.use_nats("home", None, "a test")
    yield
    lease.use_nats("", None)


async def test_bus_info_says_a_home_over_nats_is_not_seen_in_mattermost(chat, nats_home):  # noqa: F811
    info = json.loads(await server.bus_info())
    assert "~home runs over NATS" in info["over_nats"]
    assert "people do not see them" in info["over_nats"] and "ask_human" in info["over_nats"]


async def test_bus_info_says_nothing_of_nats_for_a_home_on_mattermost(chat):  # noqa: F811
    assert "over_nats" not in json.loads(await server.bus_info())


async def test_post_message_to_a_person_refuses_and_says_why(chat):  # noqa: F811
    chat.people["h" * 26] = "dembaca"
    out = json.loads(await server.post_message("hi", to="dembaca"))
    assert "is a person" in out["refused"] and "ask_human" in out["refused"]
    out = json.loads(await server.post_message("hi", to="nobody-here"))
    assert "no user" in out["refused"]
