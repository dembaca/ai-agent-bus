"""What the first NATS pilot found (#66): bus_info says that people do not see a home
over NATS in Mattermost (RT-6.36), and post_message to a person says why it refuses."""

import json

import pytest

from mattermost_ai_agent_bus import lease, server

from test_debug import chat, pinned  # noqa: F401 — fixtures


class _Side:
    """A NATS side that was built and runs, with nothing to say about the crown."""

    def down(self) -> str:
        return ""

    def crown_view(self, home: str) -> None:
        return None

    def ambiguous(self) -> list[str]:
        return []

    async def prime(self) -> None:
        return None

    bridge_bot = ""


@pytest.fixture
def nats_home():
    lease.use_nats("home", _Side())  # type: ignore[arg-type]
    yield
    lease.use_nats("", None)


@pytest.fixture
def nats_home_not_built():
    lease.use_nats("home", None, "MM_NATS_URL is not set")
    yield
    lease.use_nats("", None)


async def test_bus_info_says_a_home_over_nats_is_not_seen_in_mattermost(chat, nats_home):  # noqa: F811
    info = json.loads(await server.bus_info())
    assert "~home runs over NATS" in info["over_nats"]
    assert "people do not see them" in info["over_nats"] and "ask_human" in info["over_nats"]


async def test_bus_info_says_people_see_a_home_over_nats_through_the_bridge(chat, nats_home):  # noqa: F811
    """RT-6.36 step 6: with MM_BRIDGE_BOT the bridge mirrors the home into Mattermost."""
    lease._nats_source.bridge_bot = "bridge-bot"  # type: ignore[union-attr,misc]
    over = json.loads(await server.bus_info())["over_nats"]
    assert "@bridge-bot mirrors them into Mattermost, so people see them" in over
    assert "do not see" not in over and "ask_human" in over


async def test_bus_info_says_nothing_of_nats_for_a_home_on_mattermost(chat):  # noqa: F811
    assert "over_nats" not in json.loads(await server.bus_info())


async def test_a_home_whose_nats_side_was_not_built_stays_on_mattermost(chat, nats_home_not_built):  # noqa: F811
    """Login or MM_NATS_URL missing: compose keeps the home on Mattermost, so people see it."""
    assert "over_nats" not in json.loads(await server.bus_info())


async def test_post_message_to_a_person_refuses_and_says_why(chat):  # noqa: F811
    chat.people["h" * 26] = "dembaca"
    out = json.loads(await server.post_message("hi", to="dembaca"))
    assert "is a person" in out["refused"] and "ask_human" in out["refused"]
    out = json.loads(await server.post_message("hi", to="nobody-here"))
    assert "no user" in out["refused"]


def test_the_instructions_say_a_home_over_nats_is_not_seen_in_mattermost(monkeypatch):
    monkeypatch.delenv("MM_NATS_HOMES", raising=False)
    assert "runs over NATS" not in server._instructions()
    monkeypatch.setenv("MM_NATS_HOMES", "home")
    text = server._instructions()
    # the instructions are made before the NATS side is built: they point at over_nats,
    # which says whether the home really runs over NATS, and claim nothing themselves
    assert "When bus_info shows over_nats" in text and "ask_human" in text
    assert "shown_as is its NATS sender's bot, to show and to DM (to=), never an identity" in text


def test_the_channel_tag_carries_shown_as_beside_the_author():
    """#66: the tag a pushed event arrives in shows a NATS sender's bot too; display only."""
    from mattermost_ai_agent_bus import channel

    post = {"id": "n1:home:7", "channel": "home", "author": "a" * 25,
            "shown_as": "agent-a", "message": "hi"}
    meta = channel.notification({"kind": "mention", "post": post})["meta"]
    assert meta["author"] == "a" * 25 and meta["shown_as"] == "agent-a"
    del post["shown_as"]
    assert "shown_as" not in channel.notification({"kind": "mention", "post": post})["meta"]
