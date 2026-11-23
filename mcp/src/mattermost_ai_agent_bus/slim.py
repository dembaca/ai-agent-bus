"""Lean tool results: what an agent needs from a post, nothing more.

Mattermost's post JSON carries metadata, props, hashtags, file ids and more;
an agent reads every token of it in every turn the result stays in context.
A slim post keeps id, thread, channel name, author name, time and text — about
a fifth of the size — and, for a post with attachments, a short `files` hint
(id, name, type, size) to fetch them by with get_file (RT-6.23). Tools take
raw=true for the full JSON.

What the bus brings unasked (a channel push, a wait_for_events item) has its
text cut at BODY_MAX characters with a pointer to get_thread (RT-6.12 step 3);
what the agent fetches by id or channel (get_thread, list_recent) stays whole.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from . import files, asks, a2a

if TYPE_CHECKING:
    from .port import ChatPort
    from .model import Post


# Pushed and delivered posts are cut here (RT-6.12 step 3): about 7% of the
# measured pushes are longer, Mattermost allows 16,383 (#68).
BODY_MAX = 2000


def cut(text: str, post_id: str) -> str:
    """`text` at most about BODY_MAX characters, cut at a line or word end and
    ending with where the rest is: get_thread with the post's id, which works
    for a root and a reply alike. Unchanged when short or without an id."""
    if len(text) <= BODY_MAX or not post_id:
        return text
    head = text[:BODY_MAX]
    floor = BODY_MAX * 4 // 5  # a boundary further back drops too much
    end = head.rfind("\n", floor)
    if end < 0:
        end = max(head.rfind(" ", floor), head.rfind("\t", floor))
    if end > 0:
        head = head[:end]
    return f"{head.rstrip()}\n… (cut, {len(text)} chars in all; get_thread {post_id})"


def _when(ms: int | None) -> str:
    if not ms:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ms / 1000))


async def _names(client: ChatPort, posts: Sequence[Post]) -> tuple[dict[str, str], dict[str, str]]:
    users: dict[str, str] = {}
    channels: dict[str, str] = {}
    ids = {p.user_id for p in posts} - {""}
    ids |= {r.user_id for p in posts for r in p.reactions} - {""}  # RT-6.11: a reactor's name too
    try:
        users = await client.usernames(ids)
    except Exception:  # noqa: BLE001 — names are a convenience; ids still identify
        pass
    for cid in {p.channel_id for p in posts} - {""}:
        try:
            channels[cid] = (await client.get_channel(cid)).name or cid
        except Exception:  # noqa: BLE001
            channels[cid] = cid
    return users, channels


async def _facts(client: ChatPort, posts: Sequence[Post],
                 users: dict[str, str]) -> dict[str, Any]:
    """What a slim post carries beyond its text (RT-6.11, issue #30): per post id
    {"reactions"?} for every post, plus {"ask_card"?} for an ask from an agent.

    `reactions` is Mattermost's own metadata, so it holds for any post: a 👍 on a
    RESULT is as visible as one on an ask. It is capped (REACTIONS_MAX) with the
    trusted humans first, so a crowd cannot push the people who answered out of
    a post's budget, and `reactions_total` says how many there were.

    `ask_card` needs the author to be an agent by the bus's own test
    (`gate.author_classes`: AGENT or FOREIGN), never a name prefix a person
    could carry: anyone may write a post's props, so a card on a person's post
    is no ask and shows nothing. Its `decision` is cut like a message. `answer`
    and `chosen` count only a trusted human's reaction; where two contradict,
    the newest wins, as the live reaction event does."""
    from . import gate

    try:
        trusted = await client.trusted_humans()
    except Exception:  # noqa: BLE001 — no names: no flag, no chosen. Reactions still show.
        trusted = {}
    out: dict[str, Any] = {}
    for p in posts:
        if p.reactions:
            out[p.id] = _reactions(p, users, trusted)
    asks_only = [p for p in posts if p.kind == a2a.ASK]
    if not asks_only:
        return out
    try:
        classes = await gate.author_classes(client, asks_only)
    except Exception:  # noqa: BLE001 — unsure whether agent or person: no card at all
        return out
    for p, cls in zip(asks_only, classes, strict=True):
        if cls not in gate.AGENTS:
            continue  # a person's post, whatever its props claim
        card = a2a.ask_card(p)
        facts: dict[str, Any] = out.setdefault(p.id, {})
        if p.title:
            facts["need"] = p.title
        if card.get("decision"):
            facts["decision"] = cut(card["decision"], p.id)
        answers = a2a.ask_answer_list(p)
        if answers:
            facts["answers"] = answers
        # the newest trusted human's answer wins, as the live event counts it
        for r in sorted(p.reactions, key=lambda r: r.create_at, reverse=True):
            if r.user_id not in trusted:
                continue
            if (choice := asks.choice_of(r.emoji_name)) is not None:
                if answers and 0 < choice <= len(answers):
                    facts["answer"] = "yes" if choice == 1 else "no"
                    facts["chosen"] = answers[choice - 1]
                    break
            elif (answer := asks.answer_of(r.emoji_name)) is not None:
                facts["answer"] = answer
                if len(answers) == 2:
                    facts["chosen"] = answers[0 if answer == "yes" else 1]
                break
    return {pid: facts for pid, facts in out.items() if facts}


# How many reactions one slim post carries: enough to see who answered, small
# enough that a popular post costs every turn the same (200 reactions were 13 KB).
REACTIONS_MAX = 8


def _reactions(p: Post, users: dict[str, str], trusted: Mapping[str, str]) -> dict[str, Any]:
    """A post's reactions, the trusted humans' first, at most REACTIONS_MAX, with
    the count of all of them (RT-6.11)."""
    rows = [{"emoji": r.emoji_name, "author": users.get(r.user_id) or r.user_id,
             "trusted_human": r.user_id in trusted} for r in p.reactions]
    kept = ([row for row in rows if row["trusted_human"]]
            + [row for row in rows if not row["trusted_human"]])[:REACTIONS_MAX]
    return {"reactions": kept, "reactions_total": len(rows)}


def _one(p: Post, users: dict[str, str], channels: dict[str, str],
         facts: Mapping[str, Any] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"id": p.id}
    if p.root_id:
        out["root_id"] = p.root_id
    out["channel"] = channels.get(p.channel_id, p.channel_id)
    out["author"] = users.get(p.user_id) or p.user_id
    out["at"] = _when(p.create_at)
    out["message"] = p.text
    if p.type:
        out["type"] = p.type
    if p.edit_at:
        out["edited"] = True
    if p.reply_count:
        out["replies"] = p.reply_count
    if p.files:
        out["files"] = [files.hint(f) for f in p.files]  # RT-6.23: get_file fetches one
    if facts and p.id in facts:
        got = facts[p.id]
        if got.get("reactions"):
            out["reactions"] = got["reactions"]  # RT-6.11: who answered, on any post
            out["reactions_total"] = got["reactions_total"]
        card = {k: v for k, v in got.items() if k not in ("reactions", "reactions_total")}
        if card:
            out["ask_card"] = card  # what a 👍 approves; `ask` is the ask's id elsewhere
    return out


async def posts(client: ChatPort, items: Sequence[Post],
                *, drop_system: bool = False) -> list[dict[str, Any]]:
    if drop_system:
        items = [p for p in items if not p.type]
    users, channels = await _names(client, items)
    facts = await _facts(client, items, users)
    return [_one(p, users, channels, facts) for p in items]


async def post(client: ChatPort, item: Post) -> dict[str, Any]:
    return (await posts(client, [item]))[0]


def text_of(item: Mapping[str, Any]) -> str:
    """The message of a wait_for_events item's slim post; '' without one."""
    return str((item.get("post") or {}).get("message") or "")


def cut_item(item: dict[str, Any]) -> None:
    """A wait_for_events item's post text cut in place (see `cut`)."""
    post = item.get("post")
    if isinstance(post, dict) and isinstance(post.get("message"), str):
        post["message"] = cut(post["message"], str(post.get("id") or ""))


def root_of(item: Mapping[str, Any]) -> str:
    """The thread root of a wait_for_events item's slim post; '' for a root."""
    return str((item.get("post") or {}).get("root_id") or "")
