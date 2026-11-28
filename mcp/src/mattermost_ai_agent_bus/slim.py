"""Lean tool results: what an agent needs from a post, nothing more.

Mattermost's post JSON carries metadata, props, hashtags, file ids and more;
an agent reads every token of it in every turn the result stays in context.
A slim post keeps id, thread, channel name, author name, time and text — about
a fifth of the size — and, for a post with attachments, a short `files` hint
(id, name, type, size) to fetch them by with get_file (RT-6.23). Tools take
raw=true for the full JSON.

What the bus brings unasked (a channel push, a wait_for_events item) has its
text cut at BODY_MAX characters with a pointer to get_thread (RT-6.12 step 3).
What the agent fetches by id or channel (get_thread, list_recent) stays whole —
but not without bound: a post's `message` there is cut at FETCH_MAX, and one
slim post stays under POST_MAX, the largest text going first, both pointing at
`raw=true` for the whole of it (issue #42). Without a bound, an ask whose card
title rides in its text was 22 KB in every thread read.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from typing import TYPE_CHECKING, Any

from . import files, asks, a2a

if TYPE_CHECKING:
    from .port import ChatPort
    from .model import Post


# Pushed and delivered posts are cut here (RT-6.12 step 3): about 7% of the
# measured pushes are longer, Mattermost allows 16,383 (#68).
BODY_MAX = 2000


# A cut's note as this module writes it, anchored at the **end** of the text: an author
# who writes "… (cut, 999999 bytes in all;" into a post must not have it read as the
# note, and a real note is always the last line. The unit is kept as found, since a
# note from _cut_bytes counts bytes and one from cut() characters.
_CUT_TOTAL = re.compile(r"… \(cut, (\d+) (chars|bytes) in all;[^\n]*\)\s*\Z")


def cut(text: str, post_id: str) -> str:
    """`text` at most about BODY_MAX characters, cut at a line or word end and
    ending with where the rest is: get_thread with the post's id, which works
    for a root and a reply alike. Unchanged when short or without an id.

    Text already cut once (the slim post's `message`, which `_one` shortened to
    FETCH_MAX) reports the **original** size: its own note carries it, and a
    push must not claim the post was 15 930 characters when it was 36 000
    (issue #42)."""
    if len(text) <= BODY_MAX or not post_id:
        return text
    total, unit = len(text), "chars"
    if (seen := _CUT_TOTAL.search(text)):
        total, unit = max(total, int(seen.group(1))), seen.group(2)
    head = text[:BODY_MAX]
    floor = BODY_MAX * 4 // 5  # a boundary further back drops too much
    end = head.rfind("\n", floor)
    if end < 0:
        end = max(head.rfind(" ", floor), head.rfind("\t", floor))
    if end > 0:
        head = head[:end]
    return f"{head.rstrip()}\n… (cut, {total} {unit} in all; get_thread {post_id})"


def _when(ms: int | None) -> str:
    if not ms:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ms / 1000))


# A post's `message` on the two tools that stay whole (get_thread, list_recent):
# they are where a reader goes for the rest, so they must not point back at
# themselves. Mattermost's own post limit is 16 383 bytes; a longer post comes
# only over NATS (max_msg_size 65 536), and `raw=true` is the way to the whole of
# it, without a bound (RT-6.11, issue #42).
FETCH_MAX = 16_000
# The hard bound on one slim post, text and card together. Under it, _bounded cuts
# the largest contribution first, so a field added later cannot push the whole
# over (RT-6.12 step 3, issue #42). UTF-8 bytes, since Mattermost and NATS bound
# bytes: 60 000 emoji are 64 175 bytes and would otherwise pass.
POST_MAX = 20_000
# What a cut adds: the note naming the whole-post route. Reserved before the cut
# so the result stays under POST_MAX rather than only closer to it.
CUT_NOTE_MAX = 140
# A cut keeps at least this many characters, so `need` and `decision` are never
# emptied into an unreadable stub; _bounded then cuts another field instead.
FIELD_FLOOR = 240


def _nbytes(value: Any) -> int:
    """The UTF-8 size of a slim field: a string, a list of them (answers), or the
    card's dict. Counting only strings missed `answers` entirely (issue #42)."""
    if isinstance(value, str):
        return len(value.encode())
    if isinstance(value, list):
        return sum(_nbytes(v) for v in value)
    if isinstance(value, dict):
        return sum(_nbytes(v) for v in value.values())
    return 0


def _head_bytes(text: str, budget: int) -> str:
    """The longest prefix of `text` that fits in `budget` UTF-8 bytes, cut back to
    a line or word end when that keeps most of it."""
    if budget <= 0:
        return ""
    used, out = 0, []
    for ch in text:
        n = len(ch.encode())
        if used + n > budget:
            break
        used += n
        out.append(ch)
    head = "".join(out)
    floor = len(head) * 4 // 5
    end = head.rfind("\n", floor)
    if end < 0:
        end = max(head.rfind(" ", floor), head.rfind("\t", floor))
    return head[:end].rstrip() if end > 0 else head


def _cut_bytes(text: str, budget: int, post_id: str, where: str,
               total: int | None = None) -> str:
    """`text` cut to about `budget` UTF-8 bytes, ending with a note that names
    `total` — the **original** size where the caller knows it, since a second cut
    must not report the first cut's length (issue #42) — and never shorter than
    FIELD_FLOOR characters."""
    total = len(text.encode()) if total is None else total
    if total <= budget:
        return text
    keep = _head_bytes(text, max(budget - CUT_NOTE_MAX, _nbytes(text[:FIELD_FLOOR])))
    if len(keep) < FIELD_FLOOR:
        keep = _head_bytes(text, _nbytes(text[:FIELD_FLOOR]))
    pid = f" {post_id}" if post_id else ""
    return f"{keep}\n… (cut, {total} bytes in all; {where}{pid})"


# The card cannot reach POST_MAX on its own: every field in it is cut to BODY_MAX
# first - need, decision, each of at most four answers, and `chosen`, which is one
# of those answers again - so a card is at most 7 x BODY_MAX = 14 000 bytes.
# (`answer`, `expired` and `card_edited_after` are a few bytes each.) Only
# `message` can push a post over, and only it is cut here. Asserted by
# test_a_card_cannot_reach_the_bound_by_itself so a later field cannot quietly
# break the arithmetic (issue #42).
CARD_MAX_FIELDS = 7  # need, decision, four answers, chosen
assert CARD_MAX_FIELDS * BODY_MAX < POST_MAX


def _bounded(out: dict[str, Any], pid: str, message_total: int | None = None) -> dict[str, Any]:
    """`out` under POST_MAX bytes. The card is bounded by construction (each field cut
    to BODY_MAX), so the message gets what is left; the note names `message_total`, the
    text's size before any cut (issue #42)."""
    card = _nbytes(out.get("ask_card") or {})
    room = POST_MAX - card - CUT_NOTE_MAX
    text = str(out.get("message") or "")
    if room > 0 and _nbytes(text) > room:
        out["message"] = _cut_bytes(text, room, pid,
                                    "the whole post: get_thread with raw=true",
                                    _nbytes(text) if message_total is None else message_total)
    return out


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
    try:
        # RT-6.42 step 9: what came over NATS from a person without a fresh approver
        # record is marked not_approver here too, as wait_for_events marks it.
        unapproved = await client.unapproved(posts) if trusted else set()
    except Exception:  # noqa: BLE001 — unknown: nothing marked (the events marked it)
        unapproved = set()
    out: dict[str, Any] = {}
    for p in posts:
        if p.reactions:
            out[p.id] = _reactions(p, users, trusted, unapproved)
        if p.user_id in trusted and ("post", p.id) in unapproved:
            out.setdefault(p.id, {})["not_approver"] = True
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
            facts["need"] = _cut_bytes(p.title, BODY_MAX, p.id, "get_thread with raw=true")
        if card.get("decision"):
            facts["decision"] = _cut_bytes(card["decision"], BODY_MAX, p.id,
                                          "get_thread with raw=true")
        answers = [_cut_bytes(a, BODY_MAX, p.id, "get_thread with raw=true")
                   for a in a2a.ask_answer_list(p)]
        if answers:
            facts["answers"] = answers
        # the newest trusted human's answer wins, as the live event counts it. The sort
        # is stable, so reactions in the same millisecond keep Mattermost's own order:
        # that is the order a reader of the card would have seen (RT-6.11). An answer
        # past APPROVAL_SEC approves nothing and is flagged `expired`, as the live
        # event flags it (the verdict stays readable, the reader is told)
        for r in sorted(p.reactions, key=lambda r: r.create_at, reverse=True):
            if r.user_id not in trusted:
                continue
            if (choice := asks.choice_of(r.emoji_name)) is not None:
                # a keycap the ask never offered expresses nothing: keep looking at
                # older reactions rather than letting it end the search (a 👍 on a
                # yes/no ask stays readable when a stray keycap follows)
                if not answers or not 0 < choice <= len(answers):
                    continue
                facts["answer"] = "yes" if choice == 1 else "no"
                facts["chosen"] = answers[choice - 1]
            elif (answer := asks.answer_of(r.emoji_name)) is not None:
                facts["answer"] = answer
                if len(answers) == 2:
                    facts["chosen"] = answers[0 if answer == "yes" else 1]
            else:
                continue
            if ("reaction", p.id, r.emoji_name, r.user_id) in unapproved:
                facts["answer_not_approver"] = True  # RT-6.42 step 9: no approver: approves nothing
            if p.edit_at and r.create_at < p.edit_at:
                # The card was edited after this reaction: what it approved is not what
                # the human saw. Keep the fact that an answer exists, drop the verdict.
                for k in ("answer", "chosen"):
                    facts.pop(k, None)
                facts["card_edited_after"] = True
            elif r.create_at - p.create_at >= asks.APPROVAL_SEC * 1000:
                facts["expired"] = True  # RT-6.11: a late answer approves nothing
            break
    return {pid: facts for pid, facts in out.items() if facts}


# How many reactions one slim post carries: enough to see who answered, small
# enough that a popular post costs every turn the same (200 reactions were 13 KB).
REACTIONS_MAX = 8


def _reactions(p: Post, users: dict[str, str], trusted: Mapping[str, str],
               unapproved: AbstractSet[tuple[str, ...]] = frozenset()) -> dict[str, Any]:
    """A post's reactions, the trusted humans' first, at most REACTIONS_MAX, with
    the count of all of them (RT-6.11); a trusted one that came over NATS without a
    fresh approver record is marked not_approver (RT-6.42)."""
    rows = [{"emoji": r.emoji_name, "author": users.get(r.user_id) or r.user_id,
             "trusted_human": r.user_id in trusted,
             **({"not_approver": True} if r.user_id in trusted
                and ("reaction", p.id, r.emoji_name, r.user_id) in unapproved else {})}
            for r in p.reactions]
    kept = ([row for row in rows if row["trusted_human"]]
            + [row for row in rows if not row["trusted_human"]])[:REACTIONS_MAX]
    return {"reactions": kept, "reactions_total": len(rows)}


def _one(p: Post, users: dict[str, str], channels: dict[str, str],
         facts: Mapping[str, Any] | None = None, shown: Mapping[str, str] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"id": p.id}
    if p.root_id:
        out["root_id"] = p.root_id
    out["channel"] = channels.get(p.channel_id, p.channel_id)
    out["author"] = users.get(p.user_id) or p.user_id
    out["at"] = _when(p.create_at)
    # get_thread and list_recent stay whole by contract, but not without bound:
    # an ask whose card title rides in `message` can be 20 000 characters. The
    # reader gets raw=true as the way to all of it (issue #42).
    out["message"] = _cut_bytes(p.text, FETCH_MAX, "", "the whole post: get_thread with raw=true")
    if p.type:
        out["type"] = p.type
    if p.edit_at:
        out["edited"] = True
    if p.reply_count:
        out["replies"] = p.reply_count
    if p.files:
        out["files"] = [files.hint(f) for f in p.files]  # RT-6.23: get_file fetches one
        files.saw(p.files, p.channel_id)  # piece 8: the file's channel, read without Mattermost
    if facts and p.id in facts:
        got = facts[p.id]
        if got.get("reactions"):
            out["reactions"] = got["reactions"]  # RT-6.11: who answered, on any post
            out["reactions_total"] = got["reactions_total"]
        if got.get("not_approver"):
            out["not_approver"] = True  # RT-6.42: a person over NATS without the approvers' group
        card = {k: v for k, v in got.items() if k not in ("reactions", "reactions_total", "not_approver")}
        if card.pop("answer_not_approver", False):
            card["not_approver"] = True  # the answer is by a person over NATS without the group
        if card:
            out["ask_card"] = card  # what a 👍 approves; `ask` is the ask's id elsewhere
    if shown:  # #66: to show and to DM (to=), never an identity; `author` decides
        if p.user_id in shown:
            out["shown_as"] = shown[p.user_id]
        if out.get("reactions"):  # a NATS sender's author is its NATS name
            out["reactions"] = [dict(r, shown_as=shown[r["author"]]) if r["author"] in shown else r
                                for r in out["reactions"]]
    return _bounded(out, p.id, _nbytes(p.text))


async def posts(client: ChatPort, items: Sequence[Post],
                *, drop_system: bool = False) -> list[dict[str, Any]]:
    if drop_system:
        items = [p for p in items if not p.type]
    users, channels = await _names(client, items)
    facts = await _facts(client, items, users)
    from . import lease  # lease imports this module's neighbours; late, as elsewhere

    shown = await lease.shown_as(client, {p.user_id for p in items}
                                 | {r.user_id for p in items for r in p.reactions})
    return [_one(p, users, channels, facts, shown) for p in items]  # shown_as inside the bound


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
