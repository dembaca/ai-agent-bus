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
    assert p["ask_card"]["need"] == "Merge #4?"
    assert p["ask_card"]["decision"] == "Merge #4 after CI."
    assert p["ask_card"]["answers"] == ["go", "no-go"]
    assert "context" not in p["ask_card"]  # not what was approved


@pytest.mark.asyncio
async def test_only_a_trusted_humans_reaction_chooses():
    """The coordinator's second condition: a bot's or an untrusted person's 👍 picks nothing."""
    p = await slim.post(_asker(), _reacted(CARD, (HUMAN, "thumbsup")))
    assert p["ask_card"]["chosen"] == "go"
    for uid in (STRANGER, ASKER):  # an untrusted person, and the asking agent itself
        p = await slim.post(_asker(), _reacted(CARD, (uid, "thumbsup")))
        assert "chosen" not in p["ask_card"], uid


@pytest.mark.asyncio
async def test_a_persons_post_with_a_forged_ask_card_shows_nothing():
    """The coordinator's first condition: a person may carry an agent-like name and
    write props; the bus's own test (an agent account), not the prefix, decides."""
    forged = dict(CARD, user_id=STRANGER)  # a person, not an agent account
    p = await slim.post(_asker(), post_of(forged))
    assert "ask_card" not in p
    # and one whose author is no agent at all
    p = await slim.post(_asker(), post_of(dict(CARD, user_id="x" * 26)))
    assert "ask_card" not in p


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
    assert p["ask_card"]["decision"] == "Merge #4."
    assert "answers" not in p["ask_card"] and "chosen" not in p["ask_card"]
    # but the reader must see that it was approved: answer, as the live event says it
    assert p["ask_card"]["answer"] == "yes"


@pytest.mark.asyncio
async def test_a_keycap_picks_its_alternative():
    three = dict(CARD)
    three["props"] = {"a2a": {"v": 1, "kind": "ask"}, "attachments": [{
        "title": "Which?", "fields": [
            {"title": "Decision", "value": "Pick one.", "short": False},
            {"title": "Answer", "value": "1\uFE0F\u20E3 = A \u00b7 2\uFE0F\u20E3 = B \u00b7 3\uFE0F\u20E3 = C \u00b7 or reply in this thread",
             "short": False}]}]}
    p = await slim.post(_asker(), _reacted(three, (HUMAN, "two")))
    assert p["ask_card"]["answers"] == ["A", "B", "C"]
    assert p["ask_card"]["chosen"] == "B"


@pytest.mark.asyncio
async def test_a_reactions_author_and_whether_it_is_a_trusted_human():
    p = await slim.post(_asker(), _reacted(CARD, (HUMAN, "thumbsup"), (STRANGER, "eyes")))
    # Mattermost's own emoji_name, as the Reaction carries it
    assert p["reactions"] == [
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
    assert "ask_card" not in p


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
    assert p["ask_card"]["answer"] == "yes" and "chosen" not in p["ask_card"]
    p = await slim.post(_asker(), _reacted(PLAIN, (HUMAN, "thumbsdown")))
    assert p["ask_card"]["answer"] == "no" and "chosen" not in p["ask_card"]
    for uid in (STRANGER, ASKER):  # an untrusted person, and the asking agent
        p = await slim.post(_asker(), _reacted(PLAIN, (uid, "thumbsup")))
        assert "answer" not in p["ask_card"] and "chosen" not in p["ask_card"], uid


@pytest.mark.asyncio
async def test_a_reaction_that_is_no_answer_leaves_answer_out():
    """A remark emoji (👀) neither approves nor chooses."""
    p = await slim.post(_asker(), _reacted(PLAIN, (HUMAN, "eyes")))
    assert "answer" not in p["ask_card"] and "chosen" not in p["ask_card"]


RESULT_POST = {"id": "r" * 26, "channel_id": "c1", "user_id": ASKER, "create_at": 1,
               "message": "`RESULT` · done", "props": {"a2a": {"v": 1, "kind": "result"}}}


@pytest.mark.asyncio
async def test_reactions_show_on_any_post_not_only_an_ask():
    """Issue #30 names this too: a trusted human's 👍 on a RESULT must not be invisible."""
    p = await slim.post(_asker(), _reacted(RESULT_POST, (HUMAN, "thumbsup")))
    assert p["reactions"] == [{"emoji": "thumbsup", "author": "dembaca", "trusted_human": True}]
    assert p["reactions_total"] == 1
    assert "ask_card" not in p  # a RESULT carries no card


@pytest.mark.asyncio
async def test_reactions_are_capped_with_the_trusted_first_and_counted():
    """200 reactions used to cost 13 KB on every read; the cap keeps the people who
    answered and says how many there were (the coordinator's first finding)."""
    from mattermost_ai_agent_bus.model import Reaction
    many = [Reaction(user_id=f"{i:026d}", post_id=RESULT_POST["id"], emoji_name="eyes",
                     create_at=i) for i in range(200)]
    p = await slim.post(_asker(), _reacted(RESULT_POST, (HUMAN, "thumbsup")))
    raw = dict(RESULT_POST, metadata={"reactions": [
        {"user_id": r.user_id, "post_id": r.post_id, "emoji_name": r.emoji_name,
         "create_at": r.create_at} for r in many] + [
        {"user_id": HUMAN, "post_id": RESULT_POST["id"], "emoji_name": "thumbsup", "create_at": 999}]})
    p = await slim.post(_asker(), post_of(raw))
    assert len(p["reactions"]) == slim.REACTIONS_MAX
    assert p["reactions_total"] == 201
    assert p["reactions"][0]["author"] == "dembaca"  # a trusted human comes first
    assert len(json.dumps(p)) < 3000  # the budget holds


@pytest.mark.asyncio
async def test_the_newest_trusted_answer_wins():
    """Two contradicting reactions by the same human: the newest counts, as the live
    event does — not the first in the list."""
    raw = dict(PLAIN)
    raw["metadata"] = {"reactions": [
        {"user_id": HUMAN, "post_id": ASK_ID, "emoji_name": "thumbsdown", "create_at": 50},
        {"user_id": HUMAN, "post_id": ASK_ID, "emoji_name": "thumbsup", "create_at": 60}]}
    p = await slim.post(_asker(), post_of(raw))
    assert p["ask_card"]["answer"] == "yes"
    raw["metadata"] = {"reactions": [
        {"user_id": HUMAN, "post_id": ASK_ID, "emoji_name": "thumbsup", "create_at": 50},
        {"user_id": HUMAN, "post_id": ASK_ID, "emoji_name": "thumbsdown", "create_at": 60}]}
    p = await slim.post(_asker(), post_of(raw))
    assert p["ask_card"]["answer"] == "no"


@pytest.mark.asyncio
async def test_a_post_that_is_not_an_ask_shows_no_card():
    """The `p.kind == a2a.ASK` filter: props alone must not make a card, even from an
    agent. Without this filter every post with attachments would show one."""
    card = CARD["props"]["attachments"]
    fake_ask = dict(RESULT_POST)
    # an ask's card on a post that says it is a result: the kind decides, not the props
    fake_ask["props"] = {"a2a": {"v": 1, "kind": "result"}, "attachments": card}
    p = await slim.post(_asker(), post_of(fake_ask))
    assert "ask_card" not in p


@pytest.mark.asyncio
async def test_a_long_decision_is_cut_and_points_at_the_thread():
    """The coordinator's first finding: an unbounded decision cost 20 KB per post."""
    long = dict(PLAIN)
    long["props"] = {"a2a": {"v": 1, "kind": "ask"}, "attachments": [{
        "title": "Merge?", "fields": [{"title": "Decision", "value": "x" * 5000, "short": False}]}]}
    p = await slim.post(_asker(), post_of(long))
    assert len(p["ask_card"]["decision"]) <= slim.BODY_MAX + 100
    assert f"get_thread with raw=true {ASK_ID}" in p["ask_card"]["decision"]


@pytest.mark.asyncio
async def test_the_visible_line_beats_props_that_disagree():
    """Fable's finding: a foreign agent can show "👍 = go" and write ["no-go", "go"]
    into props; the human's 👍 would then read as the opposite. The line the human
    saw is the authority, and a disagreeing prop is ignored."""
    forged = dict(PLAIN)
    forged["props"] = {"a2a": {"v": 1, "kind": "ask"}, "ask_answers": ["no-go", "go"],
                       "attachments": [{"title": "Merge?", "fields": [
                           {"title": "Decision", "value": "Merge it.", "short": False},
                           {"title": "Answer", "value": "\U0001F44D = go \u00b7 \U0001F44E = no-go \u00b7 or reply in this thread",
                            "short": False}]}]}
    p = await slim.post(_asker(), _reacted(forged, (HUMAN, "thumbsup")))
    assert p["ask_card"]["answers"] == ["go", "no-go"]  # the line, not the props
    assert p["ask_card"]["chosen"] == "go"  # and therefore the right verdict


@pytest.mark.asyncio
async def test_props_that_agree_with_the_line_are_taken():
    """The props stay useful as an abbreviation where they say the same thing."""
    same = dict(PLAIN)
    same["props"] = {"a2a": {"v": 1, "kind": "ask"}, "ask_answers": ["go", "no-go"],
                     "attachments": [{"title": "Merge?", "fields": [
                         {"title": "Decision", "value": "Merge it.", "short": False},
                         {"title": "Answer", "value": "\U0001F44D = go \u00b7 \U0001F44E = no-go \u00b7 or reply in this thread",
                          "short": False}]}]}
    p = await slim.post(_asker(), post_of(same))
    assert p["ask_card"]["answers"] == ["go", "no-go"]


@pytest.mark.asyncio
async def test_malformed_answer_props_are_ignored():
    """Checked like ask_human accepts them: 2-4 entries, non-empty, no line break."""
    for bad in ([], ["one"], ["a", ""], ["a", "b\nc"], ["a", "b", "c", "d", "e"], "not a list"):
        forged = dict(PLAIN)
        forged["props"] = {"a2a": {"v": 1, "kind": "ask"}, "ask_answers": bad,
                           "attachments": [{"title": "Merge?", "fields": [
                               {"title": "Decision", "value": "Merge it.", "short": False},
                               {"title": "Answer", "value": "\U0001F44D = go \u00b7 \U0001F44E = no-go \u00b7 or reply in this thread",
                                "short": False}]}]}
        p = await slim.post(_asker(), post_of(forged))
        assert p["ask_card"]["answers"] == ["go", "no-go"], bad


@pytest.mark.asyncio
async def test_an_answer_past_24_hours_is_flagged_expired():
    """Fable's finding: a 👍 three days later read as `answer: yes`, while the live
    event says `expired`. Both now say both — the verdict stays readable, the reader
    is told it approves nothing."""
    late = dict(PLAIN, create_at=1_000_000)
    raw = dict(late, metadata={"reactions": [
        {"user_id": HUMAN, "post_id": ASK_ID, "emoji_name": "thumbsup",
         "create_at": 1_000_000 + 3 * 86400 * 1000}]})
    p = await slim.post(_asker(), post_of(raw))
    assert p["ask_card"]["answer"] == "yes" and p["ask_card"]["expired"] is True
    # within the day: no flag
    raw["metadata"]["reactions"][0]["create_at"] = 1_000_000 + 3600 * 1000
    p = await slim.post(_asker(), post_of(raw))
    assert p["ask_card"]["answer"] == "yes" and "expired" not in p["ask_card"]


@pytest.mark.asyncio
async def test_a_long_need_and_long_answers_are_cut_too():
    """A foreign ask with a 20 000-character title gave an 80 KB slim post; `need`
    and each alternative are cut like `decision`."""
    long = dict(PLAIN)
    long["props"] = {"a2a": {"v": 1, "kind": "ask"}, "ask_answers": ["y" * 5000, "z" * 5000],
                     "attachments": [{"title": "T" * 20000, "fields": [
                         {"title": "Decision", "value": "d" * 5000, "short": False},
                         {"title": "Answer", "value": "\U0001F44D = " + "y" * 5000
                          + " \u00b7 \U0001F44E = " + "z" * 5000 + " \u00b7 or reply in this thread",
                          "short": False}]}]}
    p = await slim.post(_asker(), post_of(long))
    card = p["ask_card"]
    assert len(card["need"]) <= slim.BODY_MAX + 100
    assert len(card["decision"]) <= slim.BODY_MAX + 100
    assert all(len(a) <= slim.BODY_MAX + 100 for a in card["answers"])
    # each field is bounded; the post's own `message` is not this PR's business —
    # Post.text repeats the card's title after the mention (22 KB on main already,
    # without any of these fields)
    assert len(json.dumps(card)) < 10_000


@pytest.mark.asyncio
async def test_two_reactions_at_the_same_millisecond_keep_mattermosts_order():
    """The coordinator's decision 6a: at an equal `create_at` the first in Mattermost's
    order wins, not an order of ours that the reader never saw."""
    raw = dict(PLAIN, create_at=1_000_000, metadata={"reactions": [
        {"user_id": HUMAN, "post_id": ASK_ID, "emoji_name": "thumbsdown", "create_at": 1_000_100},
        {"user_id": HUMAN, "post_id": ASK_ID, "emoji_name": "thumbsup", "create_at": 1_000_100}]})
    assert (await slim.post(_asker(), post_of(raw)))["ask_card"]["answer"] == "no"
    raw["metadata"]["reactions"].reverse()
    assert (await slim.post(_asker(), post_of(raw)))["ask_card"]["answer"] == "yes"


@pytest.mark.asyncio
async def test_a_keycap_out_of_range_answers_nothing_and_the_older_answer_stands():
    """A keycap the ask never offered expresses nothing (the live event gives no
    `chosen` and no `answer` for it), so an older interpretable answer still counts."""
    three = dict(PLAIN)
    three["create_at"] = 1_000_000
    three["props"] = {"a2a": {"v": 1, "kind": "ask"}, "attachments": [{
        "title": "Which?",
        "fields": [
            {"title": "Decision", "value": "Pick one.", "short": False},
            {"title": "Answer",
             "value": "1" + "\uFE0F\u20E3 = A \u00b7 2\uFE0F\u20E3 = B \u00b7 3\uFE0F\u20E3 = C "
                      "\u00b7 or reply in this thread",
             "short": False},
        ],
    }]}
    raw = dict(three, metadata={"reactions": [
        {"user_id": HUMAN, "post_id": ASK_ID, "emoji_name": "thumbsdown",
         "create_at": 1_000_002},
        {"user_id": HUMAN, "post_id": ASK_ID, "emoji_name": "four", "create_at": 1_000_003},
    ]})
    p = await slim.post(_asker(), post_of(raw))
    assert p["ask_card"]["answer"] == "no"
    assert "chosen" not in p["ask_card"]


@pytest.mark.asyncio
async def test_an_ask_edited_after_the_reaction_keeps_no_verdict():
    """Fable's third case: the author of an ask edits the card after a human reacted,
    and the same 👍 would then approve something else. The verdict is dropped rather
    than read from a card the human never saw."""
    after = dict(PLAIN, create_at=1_000_000, edit_at=1_000_500,
                 props={"a2a": {"v": 1, "kind": "ask"}, "attachments": [{
                     "title": "Merge?", "fields": [
                         {"title": "Decision", "value": "Delete the branch.", "short": False},
                         {"title": "Answer",
                          "value": "\U0001F44D = delete the branch \u00b7 \U0001F44E = keep it \u00b7 or reply in this thread",
                          "short": False}]}]})
    raw = dict(after, metadata={"reactions": [
        {"user_id": HUMAN, "post_id": ASK_ID, "emoji_name": "thumbsup",
         "create_at": 1_000_100}]})  # the reaction came before the edit
    p = await slim.post(_asker(), post_of(raw))
    assert "answer" not in p["ask_card"] and "chosen" not in p["ask_card"]
    assert p["ask_card"]["card_edited_after"] is True
    # a reaction after the edit is read normally
    raw["metadata"]["reactions"][0]["create_at"] = 1_000_600
    p = await slim.post(_asker(), post_of(raw))
    assert p["ask_card"]["answer"] == "yes"


@pytest.mark.asyncio
async def test_the_24_hour_boundary_exactly():
    """Fable's point 3: `>=` -> `>` and a zero time base both survived. Exactly 24 h
    is expired (the live event uses the same `>=`), 1 ms less is not."""
    base = 1_000_000
    for at, expired in ((base + 86_400_000, True), (base + 86_400_000 - 1, False)):
        raw = dict(PLAIN, create_at=base, metadata={"reactions": [
            {"user_id": HUMAN, "post_id": ASK_ID, "emoji_name": "thumbsup", "create_at": at}]})
        p = await slim.post(_asker(), post_of(raw))
        assert ("expired" in p["ask_card"]) is expired, at - base
        assert p["ask_card"]["answer"] == "yes"  # the verdict stays in both cases


@pytest.mark.asyncio
async def test_a_stray_keycap_does_not_swallow_an_earlier_yes():
    """Fable's regression: a 👍 on a yes/no ask, then a 2️⃣ that the ask never offered,
    left the card with no answer at all - the search ended on the keycap."""
    raw = dict(PLAIN, create_at=1_000_000, metadata={"reactions": [
        {"user_id": HUMAN, "post_id": ASK_ID, "emoji_name": "thumbsup", "create_at": 1_000_002},
        {"user_id": HUMAN, "post_id": ASK_ID, "emoji_name": "two", "create_at": 1_000_003}]})
    p = await slim.post(_asker(), post_of(raw))
    assert p["ask_card"]["answer"] == "yes"
    assert "chosen" not in p["ask_card"]


@pytest.mark.asyncio
async def test_two_answer_fields_give_no_alternatives():
    """Fable's point 2: two `Answer` fields, and the last one wins silently. The reader
    may have read the first, so neither is offered."""
    twice = dict(PLAIN)
    twice["props"] = {"a2a": {"v": 1, "kind": "ask"}, "attachments": [{
        "title": "Merge?", "fields": [
            {"title": "Decision", "value": "Merge it.", "short": False},
            {"title": "Answer", "value": "\U0001F44D = go \u00b7 \U0001F44E = no-go \u00b7 or reply in this thread",
             "short": False},
            {"title": "Answer", "value": "\U0001F44D = no-go \u00b7 \U0001F44E = go \u00b7 or reply in this thread",
             "short": False}]}]}
    p = await slim.post(_asker(), _reacted(twice, (HUMAN, "thumbsup")))
    assert "answers" not in p["ask_card"] and "chosen" not in p["ask_card"]
    assert p["ask_card"]["answer"] == "yes"  # the emoji still says what it says


# --- a post's size on the two tools that stay whole (issue #42) ---------------------

def _size(post: dict) -> int:
    """The size the bound is about: UTF-8 bytes of text and card (issue #42)."""
    text = post.get("message") or ""
    card = post.get("ask_card") or {}
    return len(text.encode()) + sum(
        len(v.encode()) if isinstance(v, str)
        else sum(len(x.encode()) for x in v if isinstance(x, str))
        for v in card.values())


@pytest.mark.asyncio
async def test_a_long_ask_title_is_bounded_in_get_thread_but_whole_with_raw():
    """The two fetch tools stay whole by contract, but not without bound: an ask whose
    card title rides in `message` was 22 KB, in every thread read."""
    long = dict(PLAIN)
    long["props"] = {"a2a": {"v": 1, "kind": "ask"}, "attachments": [{
        "title": "T" * 20_000,
        "fields": [{"title": "Decision", "value": "Merge it.", "short": False}]}]}
    p = await slim.post(_asker(), post_of(long))
    assert _size(p) <= slim.POST_MAX, _size(p)
    assert "raw=true" in p["message"]
    assert p["ask_card"]["need"].startswith("TTT")  # the question is still readable


@pytest.mark.asyncio
async def test_a_post_without_a_card_is_bounded_too():
    """A foreign agent can post 60 000 characters over NATS (max_msg_size 65536);
    no card needed to make a slim post huge."""
    raw = dict(POST, user_id=ASKER, message="M" * 60_000)
    p = await slim.post(_asker(), post_of(raw))
    assert _size(p) <= slim.POST_MAX, _size(p)
    assert "raw=true" in p["message"]


@pytest.mark.asyncio
async def test_the_card_and_the_message_together_stay_under_the_bound():
    """The bound is on the whole post, not per field: FETCH_MAX per field cannot hold a
    post whose message and card are both long, so this is the test for POST_MAX itself."""
    long = dict(PLAIN)
    long["props"] = {"a2a": {"v": 1, "kind": "ask"}, "ask_answers": ["a", "b"],
                     "attachments": [{
                         "title": "T" * 16_000,
                         "fields": [
                             {"title": "Decision", "value": "D" * 16_000, "short": False},
                             {"title": "Answer", "value": "\U0001F44D = a \u00b7 \U0001F44E = b \u00b7 or reply in this thread",
                              "short": False}]}]}
    long["message"] = "M" * 16_000
    p = await slim.post(_asker(), post_of(long))
    assert _size(p) <= slim.POST_MAX, _size(p)


@pytest.mark.asyncio
async def test_answers_count_towards_the_bound():
    """Fable's first finding: the sum counted only strings, so a card's `answers` list
    was invisible. A real ask with four 1 900-character answers was 27 632 bytes."""
    long = dict(PLAIN)
    answers = ["A" * 1_900, "B" * 1_900, "C" * 1_900, "D" * 1_900]
    line = " \u00b7 ".join(f"{m} = {a}" for m, a in zip(["1\uFE0F\u20E3", "2\uFE0F\u20E3", "3\uFE0F\u20E3", "4\uFE0F\u20E3"], answers, strict=True))
    long["props"] = {"a2a": {"v": 1, "kind": "ask"}, "ask_answers": answers,
                     "attachments": [{"title": "Q", "fields": [
                         {"title": "Decision", "value": "Pick.", "short": False},
                         {"title": "Answer", "value": line + " \u00b7 or reply in this thread",
                          "short": False}]}]}
    long["message"] = "M" * 16_000
    p = await slim.post(_asker(), post_of(long))
    assert _size(p) <= slim.POST_MAX, _size(p)
    assert p["ask_card"]["answers"], "the alternatives must survive, cut"


@pytest.mark.asyncio
async def test_the_bound_counts_bytes_not_characters():
    """Fable's second finding: 60 000 emoji are 240 000 bytes and passed a character
    count. The bound is in UTF-8 bytes, as Mattermost and NATS bound them."""
    # the emoji must sit in the CARD: `message` is already cut to FETCH_MAX bytes, so
    # only a card field exercises the byte count of the bound itself
    long = dict(PLAIN)
    long["props"] = {"a2a": {"v": 1, "kind": "ask"}, "attachments": [{
        "title": "\U0001F600" * 30_000,
        "fields": [{"title": "Decision", "value": "\U0001F600" * 30_000, "short": False}]}]}
    p = await slim.post(_asker(), post_of(long))
    assert _size(p) <= slim.POST_MAX, _size(p)
    assert p["ask_card"]["need"].startswith("\U0001F600")


@pytest.mark.asyncio
async def test_need_and_decision_are_never_emptied():
    """Fable's third finding: a cut below 40 characters returned a bare prefix, down to
    "" — the question and what a 👍 approves must survive any cut."""
    long = dict(PLAIN)
    long["props"] = {"a2a": {"v": 1, "kind": "ask"}, "attachments": [{
        "title": "Q" * 12_000,
        "fields": [{"title": "Decision", "value": "D" * 12_000, "short": False}]}]}
    long["message"] = "M" * 12_000
    p = await slim.post(_asker(), post_of(long))
    assert len(p["ask_card"]["need"]) >= 200
    assert len(p["ask_card"]["decision"]) >= 200
    assert _size(p) <= slim.POST_MAX, _size(p)


@pytest.mark.asyncio
async def test_the_cut_names_the_original_size_not_the_cut_one():
    """Fable's fourth finding: after a second cut the note reported the first cut's
    length. Here FETCH_MAX cuts the text once and POST_MAX cuts it again, so the note
    must still name what Post.text really was - not the 16 000 it was cut to."""
    answers = ["A" * 1_900] * 4
    line = " \u00b7 ".join(f"{m} = {a}" for m, a in zip(
        ["1\uFE0F\u20E3", "2\uFE0F\u20E3", "3\uFE0F\u20E3", "4\uFE0F\u20E3"], answers, strict=True))
    big = dict(PLAIN)
    big["props"] = {"a2a": {"v": 1, "kind": "ask"}, "ask_answers": answers,
                    "attachments": [{"title": "T" * 1_900, "fields": [
                        {"title": "Decision", "value": "D" * 1_900, "short": False},
                        {"title": "Answer", "value": line + " \u00b7 or reply in this thread",
                         "short": False}]}]}
    big["message"] = "M" * 60_000
    p = await slim.post(_asker(), post_of(big))
    want = len(post_of(big).text.encode())
    assert want > slim.FETCH_MAX, want  # the first cut happens
    assert f"{want} bytes in all" in p["message"], (want, p["message"][-160:])
    assert _size(p) <= slim.POST_MAX, _size(p)  # and a second cut was needed


@pytest.mark.asyncio
async def test_a_post_just_over_the_limit_lands_under_it():
    """The invariant the reservation exists for: whatever the note costs, the post ends
    at or under POST_MAX. The reservation itself is tuning - a smaller one makes the
    bound loop once more, which this test allows."""
    for extra in (1, 100, 5_000):
        long = dict(PLAIN)
        long["props"] = {"a2a": {"v": 1, "kind": "ask"}, "attachments": [{
            "title": "T" * 9_000, "fields": [{"title": "Decision", "value": "D" * 9_000, "short": False}]}]}
        long["message"] = "M" * (12_000 + extra)
        p = await slim.post(_asker(), post_of(long))
        assert _size(p) <= slim.POST_MAX, (_size(p), extra)
        assert "(cut," in p["message"] or "(cut," in p["ask_card"]["need"]


@pytest.mark.asyncio
async def test_a_short_post_is_untouched():
    """No cut note, no length change: the bound must not touch ordinary posts."""
    p = await slim.post(_asker(), post_of(PLAIN))
    assert "cut" not in p["message"]
    assert _size(p) < slim.FETCH_MAX


@pytest.mark.asyncio
async def test_a_card_cannot_reach_the_bound_by_itself():
    """The arithmetic `_bounded` relies on: every card field is cut to BODY_MAX, so the
    card is at most CARD_MAX_FIELDS x BODY_MAX and only `message` can push a post over.
    If a field is added without updating CARD_MAX_FIELDS, this test says so."""
    assert slim.CARD_MAX_FIELDS * slim.BODY_MAX < slim.POST_MAX
    long = dict(PLAIN)
    answers = ["A" * 20_000] * 4
    line = " \u00b7 ".join(f"{m} = {a}" for m, a in zip(
        ["1\uFE0F\u20E3", "2\uFE0F\u20E3", "3\uFE0F\u20E3", "4\uFE0F\u20E3"], answers, strict=True))
    long["props"] = {"a2a": {"v": 1, "kind": "ask"}, "ask_answers": answers,
                     "attachments": [{"title": "T" * 20_000, "fields": [
                         {"title": "Decision", "value": "D" * 20_000, "short": False},
                         {"title": "Answer", "value": line + " \u00b7 or reply in this thread",
                          "short": False}]}]}
    long["message"] = ""
    long["metadata"] = {"reactions": [
        {"user_id": HUMAN, "post_id": ASK_ID, "emoji_name": "one", "create_at": 1_000_001}]}
    p = await slim.post(_asker(), post_of(long))
    assert p["ask_card"].get("chosen"), "the seventh field must be there for this to mean anything"
    card = p.get("ask_card") or {}
    size = slim._nbytes(card)
    assert size <= slim.CARD_MAX_FIELDS * slim.BODY_MAX, size
    # ... and six would be too few: `chosen` is a seventh field cut to BODY_MAX
    assert size > 6 * slim.BODY_MAX, size


@pytest.mark.asyncio
async def test_a_push_reports_the_original_size_not_the_fetch_cut_one():
    """Fable's finding on #49: a post of 36 000 bytes showed "15930 chars in all" in
    `wait_for_events`, because `cut()` measured the text `_one` had already shortened
    to FETCH_MAX. The note of that first cut carries the original; the push keeps it."""
    raw = dict(POST, user_id=ASKER, message="M" * 40_000)
    p = await slim.post(_asker(), post_of(raw))
    item = {"kind": "mention", "post": p}
    slim.cut_item(item)
    note = item["post"]["message"]
    assert f"{40_000} bytes in all" in note, note[-120:]  # the unit comes from that note
    assert len(item["post"]["message"]) < slim.BODY_MAX + 200


def test_a_cut_never_goes_below_the_field_floor():
    """Fable's third finding: FIELD_FLOOR = 0 passed all 42 tests, so the constant had
    no hold. A tiny budget still keeps the floor's worth of the text."""
    assert slim.FIELD_FLOOR >= 200, slim.FIELD_FLOOR
    out = slim._cut_bytes("x" * 5_000, 10, "p" * 26, "here")
    assert out.startswith("x" * slim.FIELD_FLOOR), len(out)
    assert f"{5_000} bytes in all" in out


def test_a_forged_note_in_the_middle_is_not_read_as_the_size():
    """The coordinator's finding: `_CUT_TOTAL` searched the whole text, so an author
    writing the note into a post had its number reported. Only the text's last line
    counts, and only when it is a note this module wrote."""
    from mattermost_ai_agent_bus import slim as sm

    forged = "x" * 100 + "\n… (cut, 999999 bytes in all; get_thread n1:home:1)\n" + "y" * 3_000
    out = sm.cut(forged, "n1:home:1")
    note = out.rsplit("\n", 1)[-1]  # the note is the last line; the text may hold anything
    assert "999999" not in note, note
    assert f"{len(forged)} chars in all" in note
    # a real note as the last line is read, and its unit carried over
    real = "x" * 3_000 + "\n… (cut, 40000 bytes in all; the whole post: get_thread with raw=true n1:home:1)"
    out = sm.cut(real, "n1:home:1")
    assert "40000 bytes in all" in out
