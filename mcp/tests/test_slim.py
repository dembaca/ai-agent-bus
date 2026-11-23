"""Lean tool results: slim posts, compact JSON, raw on request."""

import json

import pytest

from mattermost_ai_agent_bus import server, slim
from mattermost_ai_agent_bus.client import ask_headline, post_of

from fakes import FakeChat

POST = {
    "id": "p1", "root_id": "r1", "channel_id": "c1", "user_id": "u1",
    "create_at": 1792229058954, "message": "Listener-Test \U0001F442", "type": "",
    "props": {"from_bot": "true"}, "metadata": {"embeds": []}, "hashtags": "",
    "file_ids": [], "pending_post_id": "x", "reply_count": 0, "edit_at": 0,
}
SYSTEM = {"id": "s1", "channel_id": "c1", "user_id": "u2", "create_at": 1,
          "message": "agent-x added to the channel", "type": "system_add_to_channel"}


class _Client(FakeChat):
    """FakeChat, plus the adapter's list_recent the tool calls."""

    def __init__(self):
        super().__init__(names={"u1": "dembaca"})

    async def list_recent(self, **_):
        return [post_of(SYSTEM), post_of(POST)]


@pytest.mark.asyncio
async def test_slim_post_keeps_what_an_agent_needs():
    p = await slim.post(_Client(), post_of(POST))
    assert p == {"id": "p1", "root_id": "r1", "channel": "agents", "author": "dembaca",
                 "at": p["at"], "message": "Listener-Test \U0001F442"}
    assert len(json.dumps(p)) < len(json.dumps(POST)) / 2


@pytest.mark.asyncio
async def test_list_recent_drops_system_messages_and_is_compact(monkeypatch):
    monkeypatch.setattr(server, "_get_client", lambda: _Client())
    out = await server.list_recent()
    assert "added to the channel" not in out and "\U0001F442" in out and "\\u" not in out
    assert ": " not in out and "\n" not in out
    raw = json.loads(await server.list_recent(raw=True))
    assert raw == [SYSTEM, POST]


@pytest.mark.asyncio
async def test_names_are_optional(monkeypatch):
    class _Broken(_Client):
        async def usernames(self, ids):
            raise RuntimeError("down")

    p = await slim.post(_Broken(), post_of(POST))
    assert p["author"] == "u1" and p["message"] == "Listener-Test \U0001F442"


NEW_ASK = {"id": "k1", "channel_id": "c1", "user_id": "u1", "create_at": 1, "message": "\U0001F64B @dembaca",
           "props": {"a2a": {"v": 1, "kind": "ask"}, "attachments": [{"title": "Merge #4?"}]}}
OLD_ASK = {"id": "k2", "channel_id": "c1", "user_id": "u1", "create_at": 1,
           "message": "\U0001F64B @dembaca — **Merge #4?**", "props": {"a2a": {"v": 1, "kind": "ask"}}}


def test_post_of_reads_an_asks_title_from_its_card_or_its_old_text():
    """RT-6.11 step 1: the card's title, else the first bold text; no ask, no title."""
    assert post_of(NEW_ASK).title == "Merge #4?"
    assert post_of(OLD_ASK).title == "Merge #4?"
    assert post_of(POST).title == ""


def test_ask_headline_reads_the_card_title():
    """RT-6.15 step 2: a linked ask's headline is its card's title."""
    assert ask_headline(NEW_ASK) == "Merge #4?"
    assert ask_headline(OLD_ASK) == "Merge #4?"
    assert ask_headline(POST) == ""


@pytest.mark.asyncio
async def test_slim_puts_an_asks_title_after_the_mention_once():
    """RT-6.11 step 1: a new ask's question follows the mention as it did in the text; an old one is not doubled."""
    assert (await slim.post(_Client(), post_of(NEW_ASK)))["message"] == "\U0001F64B @dembaca — **Merge #4?**"
    assert (await slim.post(_Client(), post_of(OLD_ASK)))["message"] == "\U0001F64B @dembaca — **Merge #4?**"


def test_the_hook_api_and_the_reminder_read_the_question_from_the_card():
    """RT-6.11 step 1: the hook API's posts carry a new ask's question after the
    mention; the open asks name it (RT-6.19)."""
    from mattermost_ai_agent_bus import hookapi, tasks
    assert hookapi._slim(post_of(NEW_ASK), set(), set(), set())["message"] == "\U0001F64B @dembaca — **Merge #4?**"
    assert [a["need"] for a in tasks.open_asks([post_of(NEW_ASK)], {"u1": "agent-x"})] == ["Merge #4?"]


# --- an ask's decision and the reactions that answered it (RT-6.11, issue #30) ------

ASK_ID = "a" * 26
ASKER = "u" * 26          # the agent that asked (a bus agent)
HUMAN = "h" * 26          # a trusted human
STRANGER = "s" * 26       # a person who is not trusted
CARD = {"id": ASK_ID, "channel_id": "c1", "user_id": ASKER, "create_at": 1,
        "message": "\U0001F64B @dembaca",
        "props": {"a2a": {"v": 1, "kind": "ask"}, "attachments": [{
            "title": "Merge #4?", "fields": [
                {"title": "From", "value": "@agent-me", "short": True},
                {"title": "Context", "value": "a long story", "short": False},
                {"title": "Decision", "value": "Merge #4 after CI.", "short": False},
                {"title": "Answer", "value": "\U0001F44D = go \u00b7 \U0001F44E = no-go \u00b7 or reply in this thread",
                 "short": False}]}]}}


def _asker(**kw) -> FakeChat:
    """A chat where ASKER is a bus agent by the bus's own test (gate.author_classes)."""
    return FakeChat(names={ASKER: "agent-me", HUMAN: "dembaca", STRANGER: "someone"},
                    agents={ASKER}, trusted={HUMAN: "dembaca"}, **kw)


def _reacted(base: dict, *reactions: tuple[str, str]) -> "object":
    """The ask as Mattermost's thread read delivers it: reactions in metadata."""
    raw = dict(base)
    raw["metadata"] = {"reactions": [
        {"user_id": uid, "post_id": ASK_ID, "emoji_name": e, "create_at": 2} for uid, e in reactions]}
    return post_of(raw)


@pytest.mark.asyncio
async def test_an_asks_decision_and_answers_are_readable():
    """issue #30: what a 👍 approves, and the alternatives it names."""
    p = await slim.post(_asker(), post_of(CARD))
    assert p["ask"]["need"] == "Merge #4?"
    assert p["ask"]["decision"] == "Merge #4 after CI."
    assert p["ask"]["answers"] == ["go", "no-go"]
    assert "context" not in p["ask"]  # not what was approved


@pytest.mark.asyncio
async def test_only_a_trusted_humans_reaction_chooses():
    """The coordinator's second condition: a bot's or an untrusted person's 👍 picks nothing."""
    p = await slim.post(_asker(), _reacted(CARD, (HUMAN, "thumbsup")))
    assert p["ask"]["chosen"] == "go"
    for uid in (STRANGER, ASKER):  # an untrusted person, and the asking agent itself
        p = await slim.post(_asker(), _reacted(CARD, (uid, "thumbsup")))
        assert "chosen" not in p["ask"], uid


@pytest.mark.asyncio
async def test_a_persons_post_with_a_forged_ask_card_shows_nothing():
    """The coordinator's first condition: a person may carry an agent-like name and
    write props; the bus's own test (an agent account), not the prefix, decides."""
    forged = dict(CARD, user_id=STRANGER)  # a person, not an agent account
    p = await slim.post(_asker(), post_of(forged))
    assert "ask" not in p
    # and one whose author is no agent at all
    p = await slim.post(_asker(), post_of(dict(CARD, user_id="x" * 26)))
    assert "ask" not in p


@pytest.mark.asyncio
async def test_a_reaction_that_does_not_map_to_an_answer_chooses_nothing():
    """An ask without alternatives (`👍 = yes to the decision · 👎 = no`) has no text
    to name, so there is no `chosen` — the live event has none either."""
    plain = dict(CARD)
    plain["props"] = {"a2a": {"v": 1, "kind": "ask"}, "attachments": [{
        "title": "Merge #4?", "fields": [
            {"title": "Decision", "value": "Merge #4.", "short": False},
            {"title": "Answer", "value": "\U0001F44D = yes to the decision \u00b7 \U0001F44E = no \u00b7 or reply in this thread",
             "short": False}]}]}
    p = await slim.post(_asker(), _reacted(plain, (HUMAN, "thumbsup")))
    assert p["ask"]["decision"] == "Merge #4."
    assert "answers" not in p["ask"] and "chosen" not in p["ask"]
    # but the reader must see that it was approved: answer, as the live event says it
    assert p["ask"]["answer"] == "yes"


@pytest.mark.asyncio
async def test_a_keycap_picks_its_alternative():
    three = dict(CARD)
    three["props"] = {"a2a": {"v": 1, "kind": "ask"}, "attachments": [{
        "title": "Which?", "fields": [
            {"title": "Decision", "value": "Pick one.", "short": False},
            {"title": "Answer", "value": "1\uFE0F\u20E3 = A \u00b7 2\uFE0F\u20E3 = B \u00b7 3\uFE0F\u20E3 = C \u00b7 or reply in this thread",
             "short": False}]}]}
    p = await slim.post(_asker(), _reacted(three, (HUMAN, "two")))
    assert p["ask"]["answers"] == ["A", "B", "C"]
    assert p["ask"]["chosen"] == "B"


@pytest.mark.asyncio
async def test_a_reactions_author_and_whether_it_is_a_trusted_human():
    p = await slim.post(_asker(), _reacted(CARD, (HUMAN, "thumbsup"), (STRANGER, "eyes")))
    # Mattermost's own emoji_name, as the Reaction carries it
    assert p["ask"]["reactions"] == [
        {"emoji": "thumbsup", "author": "dembaca", "trusted_human": True},
        {"emoji": "eyes", "author": "someone", "trusted_human": False}]


@pytest.mark.asyncio
async def test_no_ask_fields_when_the_author_cannot_be_classified():
    """Sure only: when the accounts cannot be read, an ask shows nothing rather
    than a card anyone could have written."""
    broken = _asker()

    async def boom(ids):
        raise RuntimeError("down")

    broken.agents = boom  # type: ignore[method-assign]
    p = await slim.post(broken, post_of(CARD))
    assert "ask" not in p


PLAIN = {"id": ASK_ID, "channel_id": "c1", "user_id": ASKER, "create_at": 1,
         "message": "\U0001F64B @dembaca",
         "props": {"a2a": {"v": 1, "kind": "ask"}, "attachments": [{
             "title": "Merge #4?", "fields": [
                 {"title": "Decision", "value": "Merge #4.", "short": False},
                 {"title": "Answer", "value": "\U0001F44D = yes to the decision \u00b7 \U0001F44E = no \u00b7 or reply in this thread",
                  "short": False}]}]}}


@pytest.mark.asyncio
async def test_a_decision_without_alternatives_reports_yes_or_no():
    """The coordinator's addition: a plain approval has no `chosen`, yet a reader
    must see the verdict — `answer` is the same field the live event carries."""
    p = await slim.post(_asker(), _reacted(PLAIN, (HUMAN, "thumbsup")))
    assert p["ask"]["answer"] == "yes" and "chosen" not in p["ask"]
    p = await slim.post(_asker(), _reacted(PLAIN, (HUMAN, "thumbsdown")))
    assert p["ask"]["answer"] == "no" and "chosen" not in p["ask"]
    for uid in (STRANGER, ASKER):  # an untrusted person, and the asking agent
        p = await slim.post(_asker(), _reacted(PLAIN, (uid, "thumbsup")))
        assert "answer" not in p["ask"] and "chosen" not in p["ask"], uid


@pytest.mark.asyncio
async def test_a_reaction_that_is_no_answer_leaves_answer_out():
    """A remark emoji (👀) neither approves nor chooses."""
    p = await slim.post(_asker(), _reacted(PLAIN, (HUMAN, "eyes")))
    assert "answer" not in p["ask"] and "chosen" not in p["ask"]
