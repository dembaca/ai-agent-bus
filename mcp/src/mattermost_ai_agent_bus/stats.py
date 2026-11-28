"""Coordination overhead counters (RT-6.9 in docs/arc42/06-runtime-view.md).

What the bus adds to a session, not what the work costs: turns the plugin
caused (listener wake-ups, rounds the Stop hook forced), bus data put into the
context (tool results, the handoff after compaction), the agent's own posts,
the fixed text every turn carries (instructions, tool docs) and the skill's
size, in the context only once loaded, how long turns last, how long humans
take to answer the agent's asks, the waiting an answer on the bus saved, and
how long the session held the crown, and the bus traffic by kind (own posts by
bus word, messages handed in by author class, empty polls, hook texts put into
the context), which the reports group into bus overhead and multi-agent
coordination. report() sums the STATS lines up per version, as
bin/mm-agent-stats.sh does.

Hooks, listener and MCP server append one JSON line per event to
stats-<uid>.jsonl; every PART_SEC of session time (tick) and at session end
(summary, final) the MCP server posts one summary line to CB_MM_STATS_CHANNEL.
A day's part carries what is still open (a turn, an ask, an answer whose
saving is not known yet, the crown) into the next one, so nothing counts
twice. No model is involved anywhere, and no content is recorded: no names,
no text; an ask's post id stays in the local counter file, never in the
summary, whose `sid` is random.
"""

from __future__ import annotations

import fcntl
import json
import logging
import math
import os
import re
import secrets
import time
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import a2a, config, overhead, planb, session_file

if TYPE_CHECKING:
    from .port import ChatPort

log = logging.getLogger(__name__)

_uid = ""
_client_name = ""
DEFAULT_CHANNEL = "agents-stats"
# RT-6.9 step 2: a session posts what it counted every day of session time, not
# only at its end: long sessions (days, resumed) rarely end. A constant, no switch.
PART_SEC = 24 * 3600
# Version and fixed text for a day's part: the server's (set_source).
_source: Callable[[], tuple[str, overhead.Fixed | None]] | None = None


def channel() -> str:
    """CB_MM_STATS_CHANNEL, default agents-stats; "" when switched off."""
    # Empty switches the statistics off: set-but-empty is not unset (docs/env-names.md).
    got = config.lookup("CB_MM_STATS_CHANNEL")
    name = (DEFAULT_CHANNEL if got is None else got).strip().lstrip("~")
    return "" if name.lower() in ("", "off", "none", "0", "false") else name


def set_user(uid: str) -> None:
    global _uid
    _uid = uid


def set_client(name: str) -> None:
    """The MCP client's name from the initialize handshake (clientInfo.name)."""
    global _client_name
    _client_name = name


def set_source(fn: Callable[[], tuple[str, overhead.Fixed | None]]) -> None:
    """Where tick() gets the version and the fixed text of a day's part."""
    global _source
    _source = fn


def harness(name: str | None = None) -> str:
    """The coding harness as a short label (RT-6.9): claude-code, cursor,
    opencode, codex; any other client by its own name, lowercased; without a
    name Claude Code from its environment, else other."""
    n = (_client_name if name is None else name).strip().lower()[:32]
    for label in ("claude-code", "cursor", "opencode", "codex"):
        if n.startswith(label):
            return label
    if n == "pi" or n.startswith("pi-"):  # pi's MCP client, by its name or a variant of it
        return "pi"
    if n:
        return n
    return "claude-code" if os.environ.get("CLAUDECODE") else "other"


def _me() -> str:
    return config.env("CB_MM_BOT_USER_ID") or _uid


def bot_id() -> str:
    """The bot's user id, '' when none is known yet."""
    return _me()


def _path() -> Path | None:
    uid = _me()
    if not uid:
        return None
    return session_file.state_dir() / f"stats-{uid}.jsonl"


def record(kind: str, /, **values: Any) -> None:
    """Append one event. Never raises: a counter must not disturb a session.
    kind is positional only: a block's own kind= is one of the values (RT-6.9)."""
    f = _path()
    if f is None or not channel():
        return
    try:
        session_file.append_private(f, json.dumps({"k": kind, "t": int(time.time()), **values}) + "\n")
    except OSError:
        pass


def transcript(path: str) -> str | None:
    """The transcript a hook noted, if this process may read it (RT-6.9).

    The host process of a keyless session (RT-6.18) got the path from the
    agent's side: it reads only a file whose real path lies under the host's
    ~/.claude/projects/, else measures nothing. session_file.read_transcript
    checks the opened file once more.
    """
    if not session_file.hosted():
        return path
    try:
        real = os.path.realpath(path)
        root = session_file.claude_projects()
    except (OSError, ValueError):
        return None
    return real if real.startswith(root + os.sep) else None


def _t(e: dict[str, Any]) -> int:
    try:
        return int(e.get("t", 0))
    except (TypeError, ValueError):
        return 0


def _load(f: Path) -> list[dict[str, Any]]:
    """The counter file's events in time order: a carried event keeps its time
    and may land after newer ones (a hook appending during a cut). Stable, so
    events of one second keep the file's order."""
    try:
        text = f.read_text()
    except OSError:
        return []
    return _parse(text)


def _parse(text: str) -> list[dict[str, Any]]:
    events = []
    for line in text.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict):
            events.append(e)
    return sorted(events, key=_t)


def _part(events: list[dict[str, Any]]) -> dict[str, Any]:
    """This part's record (RT-6.9): the session's random id, the part's number
    and start, whether the runtime sends turn events. A file without one is
    the first part; it starts with its first event."""
    meta = next((e for e in reversed(events) if e.get("k") == "part"), None)
    if meta is None:
        return {"sid": "", "n": 1, "start": min(_t(e) for e in events), "hooks": False}
    return {"sid": str(meta.get("sid") or ""), "n": int(meta.get("n") or 1), "start": _t(meta),
            "hooks": bool(meta.get("hooks"))}


def _turns(events: list[dict[str, Any]]) -> tuple[list[tuple[bool, int]], dict[str, Any] | None]:
    """(started by the bus, seconds) per turn that ran from its start to the
    Stop hook that ended it, and the start still open at the end. A start no
    end follows (interrupted, hook missing) gets no duration rather than a
    wrong one (RT-6.9)."""
    out: list[tuple[bool, int]] = []
    began: dict[str, Any] | None = None
    for e in events:
        if e.get("k") == "turn":
            began = e
        elif e.get("k") == "turn_end" and began is not None:
            out.append((began.get("by") == "bus", max(0, _t(e) - _t(began))))
            began = None
    return out, began


def _asks(events: list[dict[str, Any]], now: float) -> dict[str, int]:
    """Asks posted, answered (waits), still unanswered at session end (RT-6.9),
    and the reminders sent (RT-6.19). A question to the 👑 (`coord`) is no
    wait for a human: it counts in _settled only."""
    coord = {e["id"] for e in events if e.get("k") == "ask" and e.get("id") and e.get("coord")}
    posted = {e["id"]: _t(e) for e in events if e.get("k") == "ask" and e.get("id") and e["id"] not in coord}
    waits: dict[str, int] = {}
    for e in events:
        # An answer to a question to the 👑 says so itself: its question may
        # have closed in an earlier part, with the advice.
        if (e.get("k") == "answer" and e.get("id") and e["id"] not in waits and e["id"] not in coord
                and not e.get("coord")):
            waits[e["id"]] = max(0, int(e.get("wait", 0)))  # a skewed clock never counts below 0
    open_at = [t for pid, t in posted.items() if pid not in waits]
    return {
        "asks": len(set(posted) | set(waits)),
        "asks_answered": len(waits),
        "ask_wait_sec": sum(waits.values()),
        "ask_wait_max_sec": max(waits.values(), default=0),
        "asks_open": len(open_at),
        "ask_open_sec": sum(max(0, round(now - t)) for t in open_at),
        "ask_open_max_sec": max(0, round(now - min(open_at))) if open_at else 0,
        # Reminders the server sent for them (RT-6.19).
        "ask_reminders": sum(1 for e in events if e.get("k") == "remind"),
    }


def _human_turns(events: list[dict[str, Any]]) -> list[int]:
    """When a turn started that the bus did not start: the human at the terminal."""
    return sorted(_t(e) for e in events if e.get("k") == "turn" and e.get("by") != "bus")


def _answered_at(e: dict[str, Any]) -> int:
    """The answer's own time (its post's create_at); before 0.91.0 when counted."""
    return int(e.get("at") or _t(e))


def _saved(events: list[dict[str, Any]]) -> tuple[dict[str, int], list[int]]:
    """Saved waiting (RT-6.9 step 1): of the asks a trusted human answered on
    the bus, those answered while the terminal was idle (no turn a human
    started between the ask and the answer; a carried ask's `busy` remembers
    one before the cut), and for each the seconds from the answer to the next
    turn a human started (`saved`). Also the answers no such turn followed
    yet: the caller carries them on or counts them open."""
    human = _human_turns(events)
    asked: dict[str, dict[str, Any]] = {}
    for e in events:
        if e.get("k") == "ask" and e.get("id") and e["id"] not in asked:
            asked[e["id"]] = e
    seen: set[str] = set()
    idle: list[int] = []
    for e in events:
        if e.get("k") != "answer" or not e.get("id") or e["id"] in seen:
            continue
        seen.add(e["id"])
        ask, at = asked.get(e["id"]), _answered_at(e)
        if (ask is None or ask.get("coord") or ask.get("busy")
                or any(_t(ask) < h < at for h in human)):
            continue
        idle.append(at)
    waiting = idle + [_answered_at(e) for e in events if e.get("k") == "saving"]
    saved: list[int] = []
    pending: list[int] = []
    for at in waiting:
        back = next((h for h in human if h >= at), None)
        if back is None:
            pending.append(at)
        else:
            saved.append(back - at)
    return {"asks_idle": len(idle), "saved_n": len(saved), "saved_sec": sum(saved),
            "saved_max_sec": max(saved, default=0)}, pending


def _crown_until() -> int:
    from . import lease  # lease records the crown's changes here
    return lease.crown_until(_me()) if _me() else 0


def _crown(events: list[dict[str, Any]], start: int, now: float) -> tuple[int, bool]:
    """Seconds this session held the crown in the part (RT-6.9, RT-6.6), and
    whether it still holds it. The heartbeat notes each change; a crown still
    on counts until now or, when it lapsed meanwhile (a server that was down),
    until its lease ran out."""
    total, on_at = 0, None
    for e in events:
        if e.get("k") != "crown":
            continue
        if e.get("on"):
            if on_at is None:
                on_at = max(_t(e), start)
        elif on_at is not None:
            total += max(0, _t(e) - on_at)
            on_at = None
    if on_at is None:
        return total, False
    until = _crown_until()
    end = min(now, until) if until > 0 else now
    return total + max(0, round(end - on_at)), until > now


def crown_open() -> bool:
    """Whether the counter file's last crown event says held: the heartbeat's
    start, so a crown that lapsed while no server ran gets its end."""
    f = _path()
    if f is None:
        return False
    last = [e for e in _load(f) if e.get("k") == "crown"]
    return bool(last and last[-1].get("on"))


# --- what woke a turn (RT-6.9 step 1, settled without a human) ----------------
# The server notes each real hand-over to the agent (a push that went out, what
# wait_for_events returned) as `heard`, with the server-verified classes of its
# authors (RT-6.8). The summary decides, from the file alone, what woke a turn
# the bus started: the hand-overs since the turn before it ended, else the
# turn's first hand-over (what a listener that ended woke it for). Nothing is
# kept in the server: a restart loses nothing.


def _wakes(e: dict[str, Any]) -> bool:
    return e.get("k") == "heard" and bool(e.get("agent") or e.get("human"))


def _woke(events: list[dict[str, Any]], i: int) -> tuple[bool, bool] | None:
    """(an agent's, a human's) among what woke the turn events[i], None when
    nothing tells. A turn carried over a cut brings what was known then."""
    e = events[i]
    if e.get("wk"):
        return bool(e.get("wa")), bool(e.get("wh"))
    # Back to the turn before it. One that never ended (interrupted) leaves no
    # telling what came during it from what came after: none of it counts for
    # this turn (RT-6.9), at the cost of a push that came after an Escape.
    start = next((j for j in range(i - 1, -1, -1) if events[j].get("k") in ("turn", "turn_end")), -1)
    found = [] if start >= 0 and events[start].get("k") == "turn" else [
        h for h in events[start + 1:i] if _wakes(h)]
    if not found:
        end = next((j for j in range(i + 1, len(events)) if events[j].get("k") in ("turn", "turn_end")),
                   len(events))
        found = next(([h] for h in events[i + 1:end] if _wakes(h)), [])
    if not found:
        return None
    return any(h.get("agent") for h in found), any(h.get("human") for h in found)


def _woken(events: list[dict[str, Any]], out: set[int]) -> dict[str, int]:
    """bus_turns_agent / bus_turns_human: the turns the bus started (`by` bus),
    by what woke them: a human's post or reaction among it, else only agents'.
    A bus turn nothing tells about (a listen window that ran out, a stall
    event) counts in neither; one carried on to the next part counts there."""
    agent = human = 0
    for i, e in enumerate(events):
        if e.get("k") != "turn" or e.get("by") != "bus" or id(e) in out:
            continue
        woke = _woke(events, i)
        if woke is None:
            continue
        if woke[1]:
            human += 1
        elif woke[0]:
            agent += 1
    return {"bus_turns_agent": agent, "bus_turns_human": human}


def _settled(counted: list[dict[str, Any]], events: list[dict[str, Any]]) -> dict[str, int]:
    """Questions to the 👑 and to humans (counted where they close, as `asks`),
    those the 👑 answered (advice, RT-6.25 step 4), tasks this agent closed
    (DONE) with or without a human in the thread, once per thread, and what
    humans did: posts that reached the agent, trusted or not, and answers to
    its asks. The server's ask to the human for a question the 👑 left open
    (`forwarded`) is no question of its own: the question counts already."""
    advised = {e["id"] for e in events if e.get("k") == "advice" and e.get("id")}
    asks = {e["id"]: e for e in counted if e.get("k") == "ask" and e.get("id")}
    crown = [pid for pid, e in asks.items() if e.get("coord")]
    done: dict[str, dict[str, Any]] = {}
    for e in counted:
        if e.get("k") == "task_done":
            done.setdefault(str(e.get("id") or id(e)), e)
    heard = [e for e in counted if e.get("k") == "heard"]
    answers = {e["id"] for e in counted if e.get("k") == "answer" and e.get("id")}
    return {
        "asks_crown": len(crown),
        "asks_crown_answered": sum(1 for pid in crown if pid in advised),
        "asks_human": sum(1 for e in asks.values() if not e.get("coord") and not e.get("forwarded")),
        "tasks_agent_only": sum(1 for e in done.values() if not e.get("human")),
        "tasks_with_human": sum(1 for e in done.values() if e.get("human")),
        "human_posts_trusted": sum(int(e.get("trusted", 0)) for e in heard),
        "human_posts_other": sum(int(e.get("other", 0)) for e in heard),
        "human_answers": len(answers),
    }


def _add(into: dict[str, list[int]], key: str, chars: Any) -> None:
    pair = into.setdefault(key, [0, 0])
    pair[0] += 1
    pair[1] += int(chars or 0)


def _traffic(counted: list[dict[str, Any]], posts: list[dict[str, Any]],
             compacts: list[dict[str, Any]]) -> dict[str, Any]:
    """The part's bus traffic by kind (RT-6.9 step 2): own posts by bus word (`post_words`),
    posts and events handed to the agent by author class and their envelope (`inbound`),
    wait_for_events calls and the empty ones (`polls`), and the texts the hooks, the plugins,
    the listener and the pusher put into the context by kind (`inject`; a compaction handoff
    is one). Each a pair [n, characters] under a key of a fixed list; an unknown word or kind
    counts as `other`. Which category each belongs to is the reader's (CATEGORY_OF)."""
    words: dict[str, list[int]] = {}
    hours: dict[str, dict[str, int]] = {}
    for p in posts:
        w = p.get("w")
        word = w if w in POST_WORDS else "other"
        _add(words, word, p.get("chars"))
        # #290: the same posts by the UTC hour they were made in, counts only.
        if _t(p) > 0:
            hour = hours.setdefault(str(_t(p) // 3600), {})
            hour[word] = hour.get(word, 0) + 1
    inbound: dict[str, list[int]] = {}
    for e in counted:
        if e.get("k") == "in" and e.get("c") in INBOUND_CLASSES:
            _add(inbound, e["c"], e.get("chars"))
            _add(inbound, "envelope", e.get("env"))
    polls = [e for e in counted if e.get("k") == "poll"]
    inject: dict[str, list[int]] = {}
    for e in counted:
        if e.get("k") == "inject":
            name = e.get("name")
            _add(inject, name if name in INJECT_KINDS else "other", e.get("chars"))
    for c in compacts:
        _add(inject, "compact", c.get("chars"))
    return {"post_words": words, "post_hours": hours, "inbound": inbound,
            "polls": [len(polls), sum(1 for e in polls if e.get("empty"))], "inject": inject}


# Which of the bus's fixed texts a harness puts into every request (RT-6.9 step 2): pi registers
# the bus with direct exposure and sends its tool docs but not the MCP instructions; any other
# harness both.
HEAD_PARTS: dict[str, tuple[str, ...]] = {"pi": ("tools",)}
HEAD_DEFAULT = ("instructions", "tools")


def head_chars(fixed: overhead.Fixed, rt: str) -> int:
    """The characters of the bus's fixed head in one request of harness `rt`."""
    return sum(int(getattr(fixed, part)) for part in HEAD_PARTS.get(rt, HEAD_DEFAULT))


def _usage_tok(usage: list[dict[str, Any]], fixed: overhead.Fixed | None, rt: str) -> dict[str, Any] | None:
    """`tok` from the `usage` events of a harness whose transcript the server does not read
    (OpenCode, pi; RT-6.9 step 2): their sums, plus the bus's fixed head, which the plugin
    cannot see and counted as task work: per request the fixed texts `rt` sends (HEAD_PARTS) at
    CHARS_PER_TOKEN go to overhead, placed only into the input the plugin's segments did not
    cover (h_), in cache read, then cache write, then fresh input, never beyond it; the rest is
    dropped, so the head lands only where the harness sent uncovered input. No `bus`, `bus_req`, `fixed` or `skill`: the measure by cause needs the
    transcript. A usage event counts in the part it arrived in, never carried."""
    def total(f: str) -> int:
        return sum(int(e.get(f) or 0) for e in usage)
    req = total("req")
    if not req:
        return None
    tok: dict[str, Any] = {"req": req, "req_ovh": total("req_ovh"), "req_coord": total("req_coord"),
                           "m": overhead.METHOD}
    for obj, p in (("all", "a"), ("ovh", "o"), ("coord", "c")):
        tok[obj] = {k: total(f"{p}_{x}") for k, x in USAGE_TYPES.items()} | {"w1h": 0}
    head = round(req * head_chars(fixed, rt) / overhead.CHARS_PER_TOKEN) if fixed else 0
    for k, x in USAGE_HEAD.items():
        room = min(total(f"h_{x}"), tok["all"][k] - tok["ovh"][k] - tok["coord"][k])
        add = min(head, max(0, room))
        tok["ovh"][k] += add
        head -= add
    for k in overhead.KEYS:  # the invariant (RT-6.9 step 2), also on sums of odd events
        tok["ovh"][k] = min(tok["ovh"][k], tok["all"][k])
        tok["coord"][k] = min(tok["coord"][k], tok["all"][k] - tok["ovh"][k])
    return tok


def _summarize(events: list[dict[str, Any]], version: str, fixed: overhead.Fixed | None,
               now: float, final: bool) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """One STATS line for the part in `events`, and, for a day's part (not
    final), the events to carry into the next one (RT-6.9 step 2): the turn
    still running, the asks still open (with `busy` once a human turn came
    after them), the answers still waiting for the human's next turn, the
    crown when held, the part's own record and the transcript's path. What is
    carried counts in the part where it ends, or at the session's end."""
    part = _part(events)
    start = part["start"]
    carry: list[dict[str, Any]] = []
    out: set[int] = set()  # events carried on: not counted in this part
    if not final:
        _, running = _turns(events)
        if running is not None:
            # What woke it, as far as known now: the hand-overs before it are in this part.
            woke = _woke(events, next(i for i, e in enumerate(events) if e is running))
            carry.append(running | {"wk": 1, "wa": int(woke[0]), "wh": int(woke[1])} if woke else running)
            out.add(id(running))
        # Closed: answered by a trusted human; a question to the 👑 also by its
        # advice or when it was passed on to the human (RT-6.25).
        closed = {e["id"] for e in events if e.get("k") in ("answer", "advice", "forwarded") and e.get("id")}
        human = _human_turns(events)
        for e in events:
            if e.get("k") == "ask" and e.get("id") and e["id"] not in closed:
                busy = bool(e.get("busy")) or any(h > _t(e) for h in human)
                carry.append(e | {"busy": 1} if busy else e)
                out.add(id(e))
    counted = [e for e in events if id(e) not in out]
    kinds = Counter(e.get("k") for e in counted)
    hooked = part["hooks"] or any(e.get("k") in ("turn", "turn_end") for e in events)
    tools: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for e in counted:
        if e.get("k") == "tool":
            tools[e.get("name", "?")][0] += 1
            tools[e.get("name", "?")][1] += int(e.get("chars", 0))
    posts = [e for e in counted if e.get("k") == "post"]
    compacts = [e for e in counted if e.get("k") == "compact"]
    traffic = _traffic(counted, posts, compacts)
    paths = [e for e in events if e.get("k") == "transcript" and e.get("path")]
    tok: dict[str, Any] | None = None
    path = transcript(str(paths[-1]["path"])) if paths else None
    if path and fixed is not None:
        try:
            tok = overhead.measure(path, start, fixed, None if final else now)
        except Exception:  # noqa: BLE001 — a transcript we cannot read costs only this field
            tok = None
    if tok is None:
        tok = _usage_tok([e for e in counted if e.get("k") == "usage"], fixed, harness())
    turns, _ = _turns(counted)
    bus_turns = [sec for by_bus, sec in turns if by_bus]
    saved, pending = _saved(events)
    crown_sec, crowned = _crown(events, start, now)
    sid = part["sid"] or secrets.token_hex(6)
    data = {
        "v": version,
        # The session (random, RT-6.9) and which of its parts this is: the
        # report counts a session once and sums its parts.
        "sid": sid,
        "part": part["n"],
        **({"final": True} if final else {}),
        "rt": harness(),
        "kind": "autonomous" if config.env("CB_AGENT_KIND") == "autonomous" else "session",
        "min": max(0, round((now - start) / 60)),
        "turns": kinds["turn"],
        # Only a runtime with turn hooks (Claude Code) sends turn events; for
        # the others the fields stay absent, never zero (RT-6.9).
        **({
            "turns_timed": len(turns),
            "turn_sec": sum(sec for _, sec in turns),
            "turn_max_sec": max((sec for _, sec in turns), default=0),
            "bus_turns": sum(1 for e in counted if e.get("k") == "turn" and e.get("by") == "bus"),
            "bus_turns_timed": len(bus_turns),
            "bus_turn_sec": sum(bus_turns),
            "bus_turn_max_sec": max(bus_turns, default=0),
        } if hooked else {}),
        "wakes": dict(Counter(e.get("reason", "?") for e in counted if e.get("k") == "wake")),
        "blocks": dict(Counter(e.get("kind", "?") for e in counted if e.get("k") == "block")),
        "tools": dict(tools),
        "posts": [len(posts), sum(int(p.get("chars", 0)) for p in posts)],
        "compact": [len(compacts), sum(int(c.get("chars", 0)) for c in compacts)],
        # The bus traffic by kind (RT-6.9 step 2), numbers under fixed keys only.
        **traffic,
        # Per turn: instructions and tool docs. Per load: the skill, in the
        # context only when the agent loaded it; tok.skill says whether (RT-6.9).
        **({"static": fixed.instructions + fixed.tools, "skill": fixed.skill} if fixed else {}),
        **_asks(counted, now),
        # Saved waiting needs the human's turns: only with turn hooks. Answers
        # no human turn followed by the session's end are a lower bound.
        **(saved | {
            "saved_open_n": len(pending) if final else 0,
            "saved_open_sec": sum(max(0, round(now - at)) for at in pending) if final else 0,
        } if hooked else {}),
        "crown_sec": crown_sec,
        # Settled without a human (RT-6.9 step 1): who woke the bus's turns
        # (only with turn hooks), questions the 👑 settled, tasks closed
        # between agents, and how often humans stepped in.
        **(_woken(events, out) if hooked else {}),
        **_settled(counted, events),
        **({"tok": tok} if tok else {}),
    }
    _fit(data)
    if not final:
        cut = int(now)
        carry = [{"k": "part", "t": cut, "sid": sid, "n": part["n"] + 1, **({"hooks": 1} if hooked else {})},
                 *carry,
                 *([{"k": "crown", "t": cut, "on": 1}] if crowned else []),
                 *([{"k": "saving", "t": at, "at": at} for at in pending] if hooked else []),
                 *([paths[-1]] if paths else [])]
    return data, carry


def _fit(data: dict[str, Any]) -> None:
    """Keep the line within NATS_BODY_MAX (#290): `post_hours` gives up its oldest hours
    first, so a part that fit before the hourly buckets still fits, on NATS, in the chat and
    in the STATS import (which takes 8000 bytes). `post_words` keeps the part's total."""
    hours = data.get("post_hours")
    while hours and len(json.dumps(data, separators=(",", ":"))) > NATS_BODY_MAX:
        del hours[min(hours, key=int)]


def summary(version: str, fixed: overhead.Fixed | None = None) -> dict[str, Any] | None:
    """The session's last part (`final`): what the counter file holds,
    whatever is still open counted as open (RT-6.9 step 2)."""
    f = _path()
    if f is None:
        return None
    _recover(f)
    if not f.exists():
        return None
    events = _load(f)
    if not events:
        return None
    return _summarize(events, version, fixed, time.time(), final=True)[0]


_final: dict[str, Any] | None = None


def final(version: str, fixed: overhead.Fixed | None = None) -> dict[str, Any] | None:
    """summary(), once per process until post_sync: the session's last part goes on NATS
    before the bus stops and to the chat at exit, the same object (RT-6.43 step 7)."""
    global _final
    if _final is None:
        _final = summary(version, fixed)
    return _final


def due(now: float | None = None) -> float | None:
    """Seconds until the current part is PART_SEC old (0 or less: due), None
    without counters. Reads only the part records and the first event: before
    the first cut the file is in the order the events came."""
    f = _path()
    if f is None:
        return None
    try:
        text = f.read_text()
    except OSError:
        return None
    first: int | None = None
    part: int | None = None
    for line in text.splitlines():
        if first is not None and '"part"' not in line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        if first is None:
            first = _t(e)
        if e.get("k") == "part":
            part = _t(e)
    start = part if part is not None else first
    if start is None:
        return None
    return start + PART_SEC - (time.time() if now is None else now)


def _snap(f: Path) -> Path:
    return f.with_name(f.name + ".cut")


def _recover(f: Path) -> None:
    """A cut that died before it wrote the next part (a crash, a full disk)
    left its counters beside the file: they go back into it, to be counted at
    the next cut or the end (RT-6.9 step 2). Nothing of that cut was carried
    yet, so nothing counts twice."""
    snap = _snap(f)
    if not snap.exists() or snap.is_symlink():
        return
    try:
        session_file.append_private(f, snap.read_text())
        snap.unlink()
    except OSError as exc:
        log.info("overhead stats: %s not recovered: %s", snap.name, exc)


def cut(version: str, fixed: overhead.Fixed | None = None, now: float | None = None) -> dict[str, Any] | None:
    """End a day's part (RT-6.9 step 2): the counter file is moved aside (a
    hook appends to a new one from then on), summed up, and what is still
    open goes into the new file with the next part's record, in one write
    (a new file renamed in) together with what hooks appended meanwhile, to
    either file. The STATS line of the part, or None without counters."""
    f = _path()
    if f is None:
        return None
    _recover(f)
    if not f.exists() or f.is_symlink():
        return None
    now = time.time() if now is None else now
    snap = _snap(f)
    try:
        os.replace(f, snap)
        text = snap.read_text()
    except OSError:
        return None
    try:
        events = _parse(text)
        data, carry = _summarize(events, version, fixed, now, final=False) if events else (None, [])
    except Exception:
        _recover(f)  # counted at the next try or the end
        raise
    if data is None:
        _recover(f)
        return None
    # A hook that opened the old file before the move appends there: those
    # lines go on with the next part, as do those already in the new file.
    late = snap.read_text()[len(text):]
    try:
        fresh = f.read_text()
    except FileNotFoundError:
        fresh = ""
    session_file.write_private(f, "".join(json.dumps(e) + "\n" for e in carry) + late + fresh)
    snap.unlink(missing_ok=True)
    return data


def _lock(uid: str) -> int | None:
    """stats-<uid>.lock, so two servers of one bot cut a part once."""
    path = session_file.state_dir() / f"stats-{uid}.lock"
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


# --- the line on NATS (RT-6.43 step 7, ADR-015 addition of 2026-11-25, points 13 and 14) ---
# Each part also goes on `<stats>.<name>` as the same JSON object, decoded strictly by every
# reader: strings only in four fields, each by its pattern; keys only from this allow list,
# which the publisher and the console share. No key is ever shown as text: a reader maps it
# to the label the list gives it.
NATS_LINE_MAX = 8192  # bytes, what a reader accepts; the STATS stream's max_msg_size
NATS_BODY_MAX = 8000  # what the publisher sends at most: max_msg_size counts headers too
RUNTIMES = ("claude-code", "cursor", "opencode", "codex", "pi", "other")
VERSION_RE = re.compile(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,6}")
SID_RE = re.compile(r"[0-9a-f]{12}")
STRING_FIELDS: dict[str, Callable[[str], bool]] = {
    "v": lambda v: VERSION_RE.fullmatch(v) is not None,
    "sid": lambda v: SID_RE.fullmatch(v) is not None,
    "rt": lambda v: v in RUNTIMES,
    "kind": lambda v: v in ("session", "autonomous"),
}
# The top-level fields RT-6.9 names, with their labels.
LINE_KEYS: dict[str, str] = {
    "v": "version", "sid": "session", "part": "part", "final": "last part", "rt": "harness",
    "kind": "kind", "min": "minutes", "turns": "turns", "turns_timed": "turns timed",
    "turn_sec": "turn time", "turn_max_sec": "longest turn", "bus_turns": "bus turns",
    "bus_turns_timed": "bus turns timed", "bus_turn_sec": "bus turn time",
    "bus_turn_max_sec": "longest bus turn", "wakes": "wake-ups", "blocks": "forced rounds",
    "tools": "tool results", "posts": "posts", "compact": "compaction handoffs",
    "static": "fixed text", "skill": "skill size", "asks": "asks", "asks_answered": "asks answered",
    "ask_wait_sec": "answer wait", "ask_wait_max_sec": "longest answer wait",
    "asks_open": "asks open", "ask_open_sec": "open ask time", "ask_open_max_sec": "longest open ask",
    "ask_reminders": "ask reminders", "asks_idle": "answers while idle", "saved_n": "waits saved",
    "saved_sec": "waiting saved", "saved_max_sec": "longest wait saved",
    "saved_open_n": "waits saved, open", "saved_open_sec": "waiting saved, open",
    "crown_sec": "crown time", "bus_turns_agent": "bus turns woken by agents",
    "bus_turns_human": "bus turns woken by humans", "asks_crown": "questions to the coordinator",
    "asks_crown_answered": "questions the coordinator answered", "asks_human": "asks to humans",
    "tasks_agent_only": "tasks closed by agents", "tasks_with_human": "tasks closed with a human",
    "human_posts_trusted": "posts by trusted humans", "human_posts_other": "posts by other humans",
    "human_answers": "answers by humans", "tok": "tokens", "post_words": "own posts by word",
    "inbound": "messages handed in", "polls": "polls", "inject": "hook texts",
    "post_hours": "own posts by hour",
}
# The bus's own MCP tools (the names server._out counts), and "?" for none.
TOOL_NAMES = frozenset({
    "announce", "ask_human", "attach", "bus_info", "channel_ok", "coordinator", "get_file", "get_me",
    "get_thread", "list_recent", "post_message", "reply_in_thread", "resolve_ask", "session_end",
    "session_start", "stats_report", "wait_for_events", "?"})
# The reasons and kinds the turn hooks, the listener and the plugins write.
WAKE_REASONS = frozenset({"addressed", "digest", "dm", "reaction", "stall", "over", "replaced", "?"})
BLOCK_KINDS = frozenset({"drain", "signoff", "answer", "listen", "other", "?"})
# The token split (RT-6.9 step 2, Claude Code): `ovh` and `coord` by token type, the
# requests whose new input was the bus's only (`req_ovh`, `req_coord`), the method `m`.
TOK_KEYS = frozenset({"req", "bus_req", "fixed", "skill", "all", "bus", "ovh", "coord", "req_ovh",
                      "req_coord", "m"})
TOK_OBJECTS = ("all", "bus", "ovh", "coord")
TOK_TYPES = frozenset(overhead.KEYS)  # in, read, w5m, w1h, out
# The bus traffic by kind (RT-6.9 step 2): own posts by bus word, `ask` (ask_human, its link
# and resolve_ask's note), `plain` (a post without a word) and `other` (what the server posts
# on its own: reminders, the crown's takeover line, a permission prompt, a usage line);
# messages handed to the agent by the server-verified author class (`event`: the server's
# own) and the envelope around them; the texts put into the context by kind.
POST_WORDS = frozenset({*a2a.WORDS, "ask", "plain", "other"})
# #290: `post_hours` keys an hour by its UTC hour since the epoch (seconds // 3600), as text.
HOUR_RE = re.compile(r"[0-9]{1,9}")
INBOUND_CLASSES = frozenset({"agent", "human", "event"})
INBOUND_KEYS = INBOUND_CLASSES | {"envelope"}
INJECT_KINDS = frozenset({"start", "check", "digest", "drain", "signoff", "listen", "idle", "compact",
                          "wake", "other"})
# The token split a plugin attributed in process (OpenCode, pi; RT-6.9 step 1): one `usage`
# event per turn, sums over its requests, exactly these fields: a_ all tokens, o_ bus overhead,
# c_ coordination, per type o_x + c_x <= a_x; `w` is cache write of any duration. h_ the input
# the plugin's known segments did not cover (the invisible front, counted as task work in a_),
# per input type h_x <= a_x - o_x - c_x: where the server may place the bus's fixed head.
USAGE_TYPES = {"in": "in", "read": "read", "w5m": "w", "out": "out"}   # tok type: event suffix
USAGE_HEAD = {"read": "read", "w5m": "w", "in": "in"}   # the head's order: oldest first
USAGE_FIELDS = frozenset({"req", "req_ovh", "req_coord",
                          *(f"{p}_{x}" for p in "aoc" for x in USAGE_TYPES.values()),
                          *(f"h_{x}" for x in USAGE_HEAD.values())})
USAGE_MAX = 10**9
_SUB_KEYS: dict[str, frozenset[str]] = {"wakes": WAKE_REASONS, "blocks": BLOCK_KINDS,
                                        "tools": TOOL_NAMES, "tok": TOK_KEYS, "post_words": POST_WORDS,
                                        "inbound": INBOUND_KEYS, "inject": INJECT_KINDS}
# The reader's mapping of the traffic to the categories (RT-6.9 step 3), here and not on the
# line, so a changed mapping applies to old lines too. Bus overhead is what exists only
# because of the bus: sign-on and sign-off posts (HELLO, IDLE, BYE), the server's own posts
# and events, the envelope, empty polls, hook texts. Coordination is what any medium between
# agents would carry: task words, asks, plain replies, the bodies of messages from agents and
# humans, and `wake`, a hook or plugin text that carries one woken post's body. A digest
# carries peers' posts too but stays overhead (only its head of about 120 characters is
# wording of the bus), so the overhead share is an upper bound until the token split of a
# later stage separates message text. Task work, the rest of a session, needs the token
# split (`tok.ovh`, `tok.coord`; _split_of).
OVERHEAD = "overhead"
COORDINATION = "coordination"
OVERHEAD_WORDS = overhead.OVERHEAD_WORDS | {"other"}
CATEGORY_OF: dict[str, dict[str, str]] = {
    "post_words": {w: OVERHEAD if w in OVERHEAD_WORDS else COORDINATION for w in POST_WORDS},
    "inbound": {"agent": COORDINATION, "human": COORDINATION, "event": OVERHEAD, "envelope": OVERHEAD},
    "inject": {k: COORDINATION if k == "wake" else OVERHEAD for k in INJECT_KINDS},
}
TRAFFIC_KEYS = ("post_words", "inbound", "polls", "inject")


class LineError(ValueError):
    """A STATS line breaks the NATS rules: dropped whole. The text never holds any of it."""


# A number on the line: non-negative, finite, at most this (no count or second of a part comes
# near it), so no sum over a week's lines can overflow or go negative.
NUM_MAX = 1e12


def _number(v: object) -> bool:
    """Any finite number (for a key outside the allow list, which is ignored anyway)."""
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return True
    return isinstance(v, float) and math.isfinite(v)


def _num(v: object) -> bool:
    """A count or a time on the line: an integer 0 to NUM_MAX, never a boolean or a fraction
    (the builder writes integers only; a tiny fraction would blow up a share or a mean)."""
    if isinstance(v, bool) or not isinstance(v, int):
        return False
    return 0 <= v <= NUM_MAX


def _nums(v: object) -> bool:
    return isinstance(v, dict) and all(_num(x) for x in v.values())


def _pair(v: object) -> bool:
    return isinstance(v, list) and len(v) == 2 and all(_num(x) for x in v)


def _tok(v: object) -> bool:
    if not isinstance(v, dict):
        return False
    for k, x in v.items():
        if k in TOK_OBJECTS:
            if not _nums(x):
                return False
        elif k in TOK_KEYS:
            if not _num(x):
                return False
        elif not _value(x):
            return False
    return True


def _typed(key: str, v: object) -> bool:
    """The type the allow list gives `key` (protocol.md): the four strings by their pattern,
    `final` a boolean, `part` an integer, `posts`, `compact` and `polls` a pair of numbers,
    `wakes` and `blocks` objects of numbers, `tools`, `post_words`, `inbound` and `inject`
    objects of pairs, `post_hours` an object of objects of numbers, `tok` its fixed shape, every other listed key one number; each number
    0 to NUM_MAX."""
    check = STRING_FIELDS.get(key)
    if check is not None:
        return isinstance(v, str) and check(v)
    if key == "final":
        return isinstance(v, bool)
    if key == "part":
        return isinstance(v, int) and _num(v)
    if key in ("posts", "compact", "polls"):
        return _pair(v)
    if key in ("wakes", "blocks"):
        return _nums(v)
    if key in ("tools", "post_words", "inbound", "inject"):
        return isinstance(v, dict) and all(_pair(x) for x in v.values())
    if key == "post_hours":
        return isinstance(v, dict) and all(_nums(x) for x in v.values())
    if key == "tok":
        return _tok(v)
    return _num(v)


def _value(v: object, depth: int = 0) -> bool:
    """A number, a boolean, a list of numbers, or an object of such values (no string)."""
    if isinstance(v, bool) or _number(v):
        return True
    if isinstance(v, list):
        return all(_number(x) for x in v)
    if isinstance(v, dict) and depth < 3:
        return all(_value(x, depth + 1) for x in v.values())
    return False


def _allowed(key: str, value: Any) -> Any:
    """The value with every key outside the allow list left out (one level of keys below
    `wakes`, `blocks`, `tools`, `tok`, `post_words`, `inbound` and `inject`, two below
    `post_hours` (hours by HOUR_RE, then the bus words), and the token types below `tok.all`, `tok.bus`, `tok.ovh` and `tok.coord`)."""
    if key == "post_hours" and isinstance(value, dict):
        # An hour off its pattern is left out like an unknown key; below it the bus words.
        return {h: {w: n for w, n in x.items() if w in POST_WORDS}
                for h, x in value.items() if HOUR_RE.fullmatch(h)}
    sub = _SUB_KEYS.get(key)
    if sub is None or not isinstance(value, dict):
        return value
    out = {k: v for k, v in value.items() if k in sub}
    if key == "tok":
        for k in TOK_OBJECTS:
            if isinstance(out.get(k), dict):
                out[k] = {t: n for t, n in out[k].items() if t in TOK_TYPES}
    return out


def _dup_free(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise LineError("duplicate key")
        out[k] = v
    return out


def _strict(name: str) -> Any:
    raise LineError("NaN and Infinity are not JSON")


def _float(text: str) -> float:
    f = float(text)
    if not math.isfinite(f):
        raise LineError("a number out of range")
    return f


def _int(text: str) -> int:
    if len(text) > 20:
        raise LineError("a number out of range")
    return int(text)


def decode_line(data: bytes) -> dict[str, Any]:
    """A STATS line from NATS, strictly (ADR-015 point 14): at most NATS_LINE_MAX bytes, a JSON
    object without a duplicate key; strings only in `v`, `sid`, `rt` and `kind`, each by its
    pattern; every listed key of the type the allow list gives it (_typed), every other value
    a number, a boolean, a list of numbers or an object of such.
    Anything else raises LineError (the whole line is dropped). Keys outside the allow list
    are left out of what it returns, never shown."""
    if len(data) > NATS_LINE_MAX:
        raise LineError("line too big")
    try:
        obj = json.loads(data.decode("utf-8"), object_pairs_hook=_dup_free, parse_constant=_strict,
                         parse_float=_float, parse_int=_int)
    except LineError:
        raise
    except (ValueError, RecursionError, OverflowError):
        raise LineError("not JSON") from None
    if not isinstance(obj, dict):
        raise LineError("not an object")
    for key, value in obj.items():
        if key in LINE_KEYS:
            if not _typed(key, value):
                raise LineError("a value of the wrong type or range")
        elif not _value(value):
            raise LineError("a value of the wrong type")
    return {k: _allowed(k, v) for k, v in obj.items() if k in LINE_KEYS}


def nats_body(data: dict[str, Any]) -> bytes | None:
    """The part as it goes on NATS: the same object, keys outside the allow list left out,
    `rt` written as `other` for a client outside RUNTIMES; None when it would not pass
    decode_line (then it is not published)."""
    line = {k: _allowed(k, v) for k, v in data.items() if k in LINE_KEYS}
    if isinstance(line.get("rt"), str) and line["rt"] not in RUNTIMES:
        line["rt"] = "other"
    body = json.dumps(line, separators=(",", ":"), allow_nan=False).encode()
    if len(body) > NATS_BODY_MAX:
        log.info("overhead stats: the part is not published on NATS: too big")
        return None
    try:
        decode_line(body)
    except LineError as exc:
        log.info("overhead stats: the part is not published on NATS: %s", exc)
        return None
    return body


# Sends a body on `<stats>.<name>` (NatsPort.publish_stats); set by the MCP server when its
# home runs over NATS. A failed publish waits for the next part, as a failed post does.
nats_publish: Callable[[bytes], Awaitable[None]] | None = None
_unpublished: list[bytes] = []
UNPUBLISHED_MAX = 30


async def publish_nats(data: dict[str, Any] | None) -> str:
    """Publish the part on NATS beside its chat line (RT-6.43 step 7), after any whose publish
    had failed; never raises. "off" without a NATS link."""
    send = nats_publish
    if send is None:
        return "off"
    try:
        body = nats_body(data) if data else None
    except Exception as exc:  # noqa: BLE001 — never raises: the chat line goes on
        log.info("overhead stats: the part is not published on NATS: %s", type(exc).__name__)
        body = None
    if body is not None:
        _unpublished.append(body)
        del _unpublished[:-UNPUBLISHED_MAX]
    while _unpublished:
        try:
            await send(_unpublished[0])
        except Exception as exc:  # noqa: BLE001 — kept for the next part
            log.info("overhead stats: not published on NATS (%s), kept for the next part",
                     type(exc).__name__)
            return "kept"
        _unpublished.pop(0)
    return "published"


# What publish_nats answered for the session's last part (server._final_stats_on_nats); None
# while that was not tried (exit by signal, timeout, no link). post_sync reads it.
final_nats: str | None = None


def over_nats(client: ChatPort) -> bool:
    """Plan B piece 13 (RT-6.43 step 7, ADR-014 phase 5): this home's STATS go on NATS alone,
    no chat line and no Mattermost token needed. Off without a NATS link to publish on."""
    if nats_publish is None:
        return False
    try:
        home = client.work_channel_name
    except Exception:  # noqa: BLE001 — a client without a home is not over NATS
        return False
    return bool(home) and planb.on(13, home)


# Day's parts cut but not posted yet (the post failed): posted before the next
# part and before the session's last line, so no day is lost to a blip.
_unposted: list[str] = []


def _line(data: dict[str, Any]) -> str:
    return "STATS " + json.dumps(data, separators=(",", ":"))


async def tick(client: ChatPort, uid: str) -> float | None:
    """The heartbeat's part check (stall.watch, RT-6.9 step 2): once the
    current part is a day old, post its STATS line and start the next.
    The channel is joined first, so a server that cannot reach it cuts
    nothing and tries again at the next tick; a part whose post failed is
    posted first then. Seconds until the next part is due; never raises."""
    name = channel()
    if not name or _source is None or not uid:
        return None
    nats = over_nats(client)
    if not nats and not client.token:
        return None
    set_user(uid)
    left = due()
    if (left is None or left > 0) and not (_unposted and not nats):
        return left
    fd = _lock(uid)
    if fd is None:
        return None
    try:
        if nats:  # piece 13: the part goes on NATS alone, no channel to join or post to
            left = due()
            if left is None or left > 0:
                return left
            version, fixed = _source()
            data = cut(version, fixed)
            if data:
                got = await publish_nats(data)
                if got == "published":
                    log.info("overhead stats: part %s published on NATS", data["part"])
                else:
                    log.info("overhead stats: part %s not on NATS yet (%s); it goes out with the "
                             "next part", data["part"], got)
            return PART_SEC
        cid = await client.join_channel(name)
        while _unposted:
            await client.post(_unposted[0], channel_id=cid)
            _unposted.pop(0)
        left = due()  # another server may have cut it meanwhile
        if left is None or left > 0:
            return left
        version, fixed = _source()
        data = cut(version, fixed)
        if data:
            await publish_nats(data)  # beside the chat line, whatever becomes of the post
            _unposted.append(_line(data))
            await client.post(_unposted[-1], channel_id=cid)
            _unposted.pop()
            log.info("overhead stats: part %s posted", data["part"])
        return PART_SEC
    except Exception as exc:  # noqa: BLE001 — the heartbeat goes on
        log.info("overhead stats part skipped: %s", exc)
        return None
    finally:
        os.close(fd)


def post_sync(client: ChatPort, data: dict[str, Any]) -> str:
    """Post the summary to the stats channel, synchronously (exit handler),
    after any day's part whose post had failed. With plan B's piece 13 on for the home
    (over_nats) nothing is posted: the last part went on NATS, and the answer says so.

    The counter file and its lock go either way: on a server without the
    channel the counts would only pile up.
    """
    name = channel()
    # Piece 13 on and the last part really published on NATS: no chat line. Otherwise (until
    # piece 14 the server still holds a token) the chat line is the durable copy, as today.
    nats = bool(name) and over_nats(client) and final_nats == "published"
    if not name or (not nats and not client.token):
        return "off" if not name or not over_nats(client) else (
            f"last part not on NATS ({final_nats or 'not tried'}) and no token: not posted")
    lines = [*_unposted, _line(data)]
    dropped = len(_unposted)
    # On NATS the older lines are dropped unsent. Only parts whose post failed before the piece
    # was switched on mid-session can be here; their parts were published on NATS as well.
    _unposted.clear()
    try:
        if nats:  # piece 13: the last part went on NATS (server._final_stats_on_nats)
            if dropped:
                log.info("overhead stats: %d earlier unposted line(s) dropped, on NATS", dropped)
                return f"on NATS, not posted ({dropped} earlier unposted line(s) dropped)"
            return "on NATS, not posted"
        for msg in lines:
            if not client.post_by_name_sync(name, msg, join=_me()):
                return f"no channel ~{name}; nothing posted"
        return "posted"
    except Exception as exc:  # noqa: BLE001 — exit handler: fail open
        return f"failed: {type(exc).__name__}"
    finally:
        global _final
        _final = None
        f = _path()
        if f is not None:
            f.unlink(missing_ok=True)
            (f.parent / f"stats-{_me()}.lock").unlink(missing_ok=True)


# --- the report: what bin/mm-agent-stats.sh report --json computes in jq -----
# The same fields and arithmetic (jq's round is half away from zero, an absent
# value adds 0), so the shell report and the stats_report tool agree
# (tests/hooks/test_stats.py compares them).

def _round(x: float) -> int:
    return int(math.floor(abs(x) + 0.5)) * (1 if x >= 0 else -1)


def _sum(rows: list[dict[str, Any]], f: Callable[[dict[str, Any]], Any]) -> Any:
    return sum((f(r) or 0) for r in rows)


def _max(values: Iterable[Any]) -> Any:
    found = [v for v in values if v is not None]
    return max(found) if found else None


def _pct(a: float, b: float) -> int | None:
    return _round(a * 100 / b) if b > 0 else None


def _tenth(a: float, b: float) -> float | None:
    return _round(a / b * 10) / 10 if b > 0 else None


def _per(a: float, b: float) -> int | None:
    return _round(a / b) if b > 0 else None


def _eq(t: dict[str, Any]) -> float:
    """Input-token equivalents: what a token type costs against an uncached input token."""
    return ((t.get("in") or 0) + (t.get("read") or 0) * 0.1 + (t.get("w5m") or 0) * 1.25
            + (t.get("w1h") or 0) * 2 + (t.get("out") or 0) * 5)


def _vals(d: Any) -> int:
    return sum(d.values()) if isinstance(d, dict) and d else 0


def _sessions(rows: list[dict[str, Any]]) -> int:
    """Distinct sessions: a session's parts share its sid; a line without one
    (before 0.91.0) is a session of its own (report sets _s)."""
    return len({r["_s"] for r in rows})


def _turns_of(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    rows = [r for r in rows if r.get("turns_timed") is not None]
    if not rows:
        return None
    t: dict[str, Any] = {
        "sessions": _sessions(rows),
        "turns": _sum(rows, lambda r: r.get("turns")),
        "human_prompts": _sum(rows, lambda r: (r.get("turns") or 0) - (r.get("bus_turns") or 0)),
        "timed": _sum(rows, lambda r: r.get("turns_timed")),
        "sec": _sum(rows, lambda r: r.get("turn_sec")),
        "max_sec": max(r.get("turn_max_sec") or 0 for r in rows),
        "bus_turns": _sum(rows, lambda r: r.get("bus_turns")),
        "bus_timed": _sum(rows, lambda r: r.get("bus_turns_timed")),
        "bus_sec": _sum(rows, lambda r: r.get("bus_turn_sec")),
        "bus_max_sec": max(r.get("bus_turn_max_sec") or 0 for r in rows),
        "answered": _sum(rows, lambda r: r.get("asks_answered")),
    }
    return t | {
        "turns_per_prompt": _tenth(t["turns"], t["human_prompts"]),
        "mean_sec": _per(t["sec"], t["timed"]),
        "bus_mean_sec": _per(t["bus_sec"], t["bus_timed"]),
        "bus_share_pct": _pct(t["bus_sec"], t["sec"]),
        "sec_per_input": _per(t["sec"], t["human_prompts"] + t["answered"]),
    }


def _asks_of(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    rows = [r for r in rows if r.get("asks") is not None]
    if not rows:
        return None
    a: dict[str, Any] = {
        "sessions": _sessions(rows),
        "hours": _sum(rows, lambda r: r.get("min")) / 60,
        "asks": _sum(rows, lambda r: r.get("asks")),
        "answered": _sum(rows, lambda r: r.get("asks_answered")),
        "wait_sec": _sum(rows, lambda r: r.get("ask_wait_sec")),
        "max_wait_sec": max(r.get("ask_wait_max_sec") or 0 for r in rows),
        "open": _sum(rows, lambda r: r.get("asks_open")),
        "open_sec": _sum(rows, lambda r: r.get("ask_open_sec")),
        "open_max_sec": max(r.get("ask_open_max_sec") or 0 for r in rows),
    }
    h = a["hours"]
    return a | {
        "asks_per_h": _tenth(a["asks"], h),
        "answered_pct": _pct(a["answered"], a["asks"]),
        "mean_wait_sec": _per(a["wait_sec"], a["answered"]),
        "waiting_pct": _pct(a["wait_sec"] + a["open_sec"], h * 3600),
        "hours": _round(h * 10) / 10,
    }


def _saved_of(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Saved waiting (since 0.91.0, sessions with turn hooks): answers on the
    bus, those while the terminal was idle, and the time to the human's next
    turn there; `saved_open_*` a lower bound for answers no such turn followed."""
    rows = [r for r in rows if r.get("asks_idle") is not None]
    if not rows:
        return None
    s: dict[str, Any] = {
        "sessions": _sessions(rows),
        "answered": _sum(rows, lambda r: r.get("asks_answered")),
        "idle": _sum(rows, lambda r: r.get("asks_idle")),
        "saved_n": _sum(rows, lambda r: r.get("saved_n")),
        "saved_sec": _sum(rows, lambda r: r.get("saved_sec")),
        "saved_max_sec": max(r.get("saved_max_sec") or 0 for r in rows),
        "saved_open_n": _sum(rows, lambda r: r.get("saved_open_n")),
        "saved_open_sec": _sum(rows, lambda r: r.get("saved_open_sec")),
    }
    return s | {
        "idle_pct": _pct(s["idle"], s["answered"]),
        "mean_saved_sec": _per(s["saved_sec"], s["saved_n"]),
    }


def _settled_of(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Settled without a human (since 0.91.0): the share of the bus's turns
    only agents woke (with turn hooks), of the questions the 👑 answered, of
    the tasks closed with no human in the thread, and human posts and answers
    per agent-day (the parts' minutes / 1440). None without the fields."""
    rows = [r for r in rows if r.get("asks_crown") is not None]
    if not rows:
        return None
    hooked = [r for r in rows if r.get("bus_turns_agent") is not None]
    s: dict[str, Any] = {
        "sessions": _sessions(rows),
        "parts": len(rows),
        "days": _sum(rows, lambda r: r.get("min")) / 1440,
        "bus_turns_agent": _sum(hooked, lambda r: r.get("bus_turns_agent")) if hooked else None,
        "bus_turns_human": _sum(hooked, lambda r: r.get("bus_turns_human")) if hooked else None,
        "asks_crown": _sum(rows, lambda r: r.get("asks_crown")),
        "asks_crown_answered": _sum(rows, lambda r: r.get("asks_crown_answered")),
        "asks_human": _sum(rows, lambda r: r.get("asks_human")),
        "tasks_agent_only": _sum(rows, lambda r: r.get("tasks_agent_only")),
        "tasks_with_human": _sum(rows, lambda r: r.get("tasks_with_human")),
        "human_posts_trusted": _sum(rows, lambda r: r.get("human_posts_trusted")),
        "human_posts_other": _sum(rows, lambda r: r.get("human_posts_other")),
        "human_answers": _sum(rows, lambda r: r.get("human_answers")),
    }
    d = s["days"]
    return s | {
        "agent_turns_pct": (_pct(s["bus_turns_agent"], s["bus_turns_agent"] + s["bus_turns_human"])
                            if hooked else None),
        "crown_answered_pct": _pct(s["asks_crown_answered"], s["asks_crown"] + s["asks_human"]),
        "tasks_agent_pct": _pct(s["tasks_agent_only"], s["tasks_agent_only"] + s["tasks_with_human"]),
        "human_posts_per_day": _tenth(s["human_posts_trusted"] + s["human_posts_other"], d),
        "human_answers_per_day": _tenth(s["human_answers"], d),
        "days": _round(d * 10) / 10,
    }


def _tok_of(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    # The measure by cause: only lines that carry it (tok.bus). A tok from a plugin's usage
    # events (OpenCode, pi) has the split only and never counts here as a 0 % share (RT-6.9).
    rows = [r for r in rows if isinstance(r.get("tok"), dict) and isinstance(r["tok"].get("bus"), dict)]
    if not rows:
        return None
    t: dict[str, Any] = {
        "sessions": _sessions(rows),
        "hours": _sum(rows, lambda r: r.get("min")) / 60,
        "req": _sum(rows, lambda r: r["tok"].get("req")),
        "bus_req": _sum(rows, lambda r: r["tok"].get("bus_req")),
        "fixed": _round(_sum(rows, lambda r: r["tok"].get("fixed")) / len(rows)),
        # A session that loaded the skill counts once, over all its parts.
        "skill_loaded": len({r["_s"] for r in rows if (r["tok"].get("skill") or 0) > 0}),
        "all": _sum(rows, lambda r: _eq(r["tok"].get("all") or {})),
        "bus": _sum(rows, lambda r: _eq(r["tok"].get("bus") or {})),
    }
    return t | {
        "req_per_h": _per(t["req"], t["hours"]),
        "bus_req_per_h": _tenth(t["bus_req"], t["hours"]),
        "bus_per_h": _per(t["bus"], t["hours"]),
        "all_per_h": _per(t["all"], t["hours"]),
        "bus_share_pct": _round(t["bus"] * 1000 / t["all"]) / 10 if t["all"] > 0 else None,
    }


TASK = "task"
SPLIT_CATEGORIES = (TASK, OVERHEAD, COORDINATION)
# The one-line note the reports carry beside the split (RT-6.9 step 3).
SPLIT_NOTE = ("estimated: exact totals per token type from the harness (Claude Code, OpenCode, pi), split by content "
              "at 4 characters per token; text and thinking by what drove the request, a convention")


def _split_of(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The tokens by category (RT-6.9 step 3), an estimate: per token type, the line's
    overhead held to its total, coordination to the rest, task work the remainder, summed
    over the lines of the method this reader knows (tok.m == 1); raw tokens and input-token
    equivalents (_eq) with each category's share. `lines` of `of` lines carry it, from the
    harnesses named; None when none does: n/a, never 0."""
    total = len(rows)
    rows = [r for r in rows if isinstance(r.get("tok"), dict) and r["tok"].get("m") == overhead.METHOD]
    if not rows:
        return None
    types = {c: dict.fromkeys(overhead.KEYS, 0) for c in SPLIT_CATEGORIES}
    for r in rows:
        tok = r["tok"]
        for k in overhead.KEYS:
            a = (tok.get("all") or {}).get(k) or 0
            o = min(max((tok.get("ovh") or {}).get(k) or 0, 0), a)
            c = min(max((tok.get("coord") or {}).get(k) or 0, 0), a - o)
            types[OVERHEAD][k] += o
            types[COORDINATION][k] += c
            types[TASK][k] += a - o - c
    raw = {c: sum(types[c].values()) for c in SPLIT_CATEGORIES}
    eq = {c: _eq(types[c]) for c in SPLIT_CATEGORIES}
    raw_all, eq_all = sum(raw.values()), sum(eq.values())
    return {
        "estimated": True, "method": overhead.METHOD, "lines": len(rows), "of": total,
        "harnesses": sorted({str(r["rt"]) if r.get("rt") is not None else "other" for r in rows}),
        "req": _sum(rows, lambda r: r["tok"].get("req")),
        "req_ovh": _sum(rows, lambda r: r["tok"].get("req_ovh")),
        "req_coord": _sum(rows, lambda r: r["tok"].get("req_coord")),
        "raw": raw, "eq": {c: _round(v) for c, v in eq.items()},
        "raw_pct": {c: _pct(v, raw_all) for c, v in raw.items()},
        "eq_pct": {c: _pct(v, eq_all) for c, v in eq.items()},
        "types": types,
    }


def _pairs(rows: list[dict[str, Any]], key: str) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for r in rows:
        for k, v in (r.get(key) or {}).items():
            pair = out.setdefault(k, [0, 0])
            pair[0] += v[0]
            pair[1] += v[1]
    return out


def _traffic_of(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The bus traffic by category (RT-6.9 step 3), exact counts: own posts by word, messages
    handed in by author class, polls and the empty ones, hook texts by kind, each summed as
    [n, characters], and grouped by CATEGORY_OF into bus overhead and coordination (an empty
    poll counts as an overhead item without characters, the envelope as characters only).
    `lines` of `of` lines carry the counts; None when none does: n/a, never 0."""
    total = len(rows)
    rows = [r for r in rows if any(r.get(k) is not None for k in TRAFFIC_KEYS)]
    if not rows:
        return None
    posts, inbound, inject = _pairs(rows, "post_words"), _pairs(rows, "inbound"), _pairs(rows, "inject")
    calls = _sum(rows, lambda r: (r.get("polls") or [0, 0])[0])
    # A line's own word: never more empty polls than polls.
    empty = _sum(rows, lambda r: min((r.get("polls") or [0, 0])[1], (r.get("polls") or [0, 0])[0]))
    cat = {OVERHEAD: [empty, 0], COORDINATION: [0, 0]}
    for key, sums in (("post_words", posts), ("inbound", inbound), ("inject", inject)):
        for k, (n, c) in sums.items():
            into = CATEGORY_OF[key].get(k)
            if into is not None:
                cat[into][0] += 0 if k == "envelope" else n
                cat[into][1] += c
    ovh, coord = cat[OVERHEAD], cat[COORDINATION]
    return {
        "lines": len(rows), "of": total, "posts": posts, "inbound": inbound, "inject": inject,
        "polls": calls, "empty_polls": empty, "empty_polls_pct": _pct(empty, calls),
        "overhead": ovh, "coordination": coord, "overhead_chars_pct": _pct(ovh[1], ovh[1] + coord[1]),
    }


def _version_key(v: str) -> list[int]:
    return [int(p) if p.isdigit() else 0 for p in v.split(".")]


def role_of(r: dict[str, Any]) -> str:
    """A part's role (RT-6.9 step 3): coordinator when it held the crown more
    than half its time, else worker; unknown before 0.91.0 (no crown_sec). Per
    part, so a session that coordinates one day and works the next counts as
    both."""
    if r.get("crown_sec") is None:
        return "unknown"
    return "coordinator" if r["crown_sec"] * 2 > (r.get("min") or 0) * 60 else "worker"


def _marked(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r | {"_s": r["sid"] if r.get("sid") not in (None, False) else f"#{i}"}
            for i, r in enumerate(lines)]


def asks_summary(lines: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The asks of all the lines together, as report() sums them per harness (RT-6.9 step 3):
    the operator console's asks report (RT-6.43 step 8). None without the fields."""
    return _asks_of(_marked(lines))


TRAFFIC_DETAIL = ("posts", "inbound", "inject")


# What stats_report keeps of the tokens by category: the sums and shares per category
# and the coverage; the request counts, the method and the token types stay in
# `mm-agent-stats.sh report --json` and the console.
SPLIT_BRIEF = ("estimated", "lines", "of", "harnesses", "raw", "eq", "raw_pct", "eq_pct")


def _no_types(split: dict[str, Any] | None) -> dict[str, Any] | None:
    return {k: v for k, v in split.items() if k in SPLIT_BRIEF} if split else split


def brief(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """report()'s rows as the stats_report tool returns them (RT-6.9 step 3): the traffic
    by category and the tokens by category with their sums, shares and coverage only; the
    sums per key and per token type stay in `mm-agent-stats.sh report --json` and the
    console, so asking an agent for the report puts no more into its context than it needs."""
    out = []
    for row in rows:
        t = row.get("traffic")
        if t:
            row = row | {"traffic": {k: v for k, v in t.items() if k not in TRAFFIC_DETAIL}}
        row = row | {"split": _no_types(row.get("split")),
                     "roles": [r | {"split": _no_types(r.get("split"))} for r in row.get("roles") or []]}
        out.append(row)
    return out


def hours_of(lines: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The own posts by hour and bus word over `lines` (#290, the console's hourly bars):
    `hours` maps an hour's start (UTC, epoch seconds) to {word: n}, summed over the lines,
    by the hour a post was made in, not the hour its part arrived; `lines` of `of` say how
    many lines carried `post_hours`. None when none did (publishers before it): no hourly
    data, never zero. A part that left out its oldest hours to fit (_fit) keeps them only in
    `post_words`."""
    carried = [line["post_hours"] for line in lines if isinstance(line.get("post_hours"), dict)]
    if not carried:
        return None
    hours: dict[int, dict[str, int]] = {}
    for part in carried:
        for h, words in part.items():
            into = hours.setdefault(int(h) * 3600, {})
            for w, n in words.items():
                into[w] = into.get(w, 0) + int(n)
    return {"hours": dict(sorted(hours.items())), "lines": len(carried), "of": len(lines)}


def split_summary(lines: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The tokens by category of all the lines together, as report() sums them per version
    (RT-6.9 step 3): the operator console's report (RT-6.43 step 8)."""
    return _split_of(_marked(lines))


def traffic_summary(lines: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The bus traffic by category of all the lines together, as report() sums it per
    version (RT-6.9 step 3): the operator console's report (RT-6.43 step 8)."""
    return _traffic_of(_marked(lines))


def report(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The STATS lines summed up per version, oldest first (RT-6.9 step 3).
    A session's parts (one sid) count as one session."""
    lines = _marked(lines)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for line in lines:
        groups[str(line.get("v"))].append(line)
    out = []
    for v in sorted(sorted(groups), key=_version_key):
        rows = groups[v]
        by_rt: dict[str, list[dict[str, Any]]] = defaultdict(list)
        by_role: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in rows:
            by_rt[str(r["rt"]) if r.get("rt") is not None else "other"].append(r)
            by_role[role_of(r)].append(r)
        hours = _round(_sum(rows, lambda r: r.get("min")) / 60 * 10) / 10
        turns = _sum(rows, lambda r: r.get("turns"))
        forced = _sum(rows, lambda r: _vals(r.get("blocks")))
        # Before 0.36.0 (no bus_turns) the listener's wake-ups stand in.
        plugin = _sum(rows, lambda r: (r["bus_turns"] if r.get("bus_turns") is not None
                                       else _vals(r.get("wakes"))) + _vals(r.get("blocks")))
        bus_chars = _sum(rows, lambda r: sum(t[1] for t in (r.get("tools") or {}).values())
                         + (r.get("compact") or [0, 0])[1])
        post_chars = _sum(rows, lambda r: (r.get("posts") or [0, 0])[1])
        everything = turns + forced
        sessions = _sessions(rows)
        out.append({
            "version": v, "sessions": sessions, "parts": len(rows), "hours": hours, "turns": turns,
            "forced": forced, "plugin_turns": plugin, "bus_chars": bus_chars, "post_chars": post_chars,
            "static": _max(r.get("static") for r in rows),
            "skill": _max(r.get("skill") for r in rows),
            "tok": _tok_of(rows),
            "traffic": _traffic_of(rows),
            "split": _split_of(rows),
            "settled": _settled_of(rows),
            "saved": _saved_of(rows),
            "harness": [{"harness": rt, "sessions": _sessions(rs), "turns": _turns_of(rs),
                         "asks": _asks_of(rs), "saved": _saved_of(rs)}
                        for rt, rs in sorted(by_rt.items())],
            "roles": [{"role": role, "parts": len(rs), "sessions": _sessions(rs),
                       "hours": _round(_sum(rs, lambda r: r.get("min")) / 60 * 10) / 10,
                       "tok": _tok_of(rs), "split": _split_of(rs), "turns": _turns_of(rs),
                       "asks": _asks_of(rs), "settled": _settled_of(rs)}
                      for role, rs in sorted(by_role.items())],
            "plugin_turns_per_h": _tenth(plugin, hours),
            "all_turns": everything,
            "plugin_share_pct": _pct(plugin, everything),
            "bus_chars_per_turn": _per(bus_chars, everything),
            "post_chars_per_session": _round(post_chars / sessions),
        })
    return out


async def read_lines(client: ChatPort, name: str, max_pages: int = 10) -> list[dict[str, Any]]:
    """The STATS lines among the last 2000 posts of the channel `name`, read with the bot's
    own session (RT-6.9 step 3)."""
    cid = await client.join_channel(name)
    lines = []
    for p in await client.posts_after(0, channel_id=cid, max_pages=max_pages):
        if p.message.startswith("STATS "):
            try:
                data = json.loads(p.message[len("STATS "):])
            except ValueError:
                continue
            if isinstance(data, dict):
                lines.append(data)
    return lines


async def read_report(client: ChatPort, name: str, max_pages: int = 10) -> list[dict[str, Any]]:
    """report() of the lines read_lines gives."""
    return report(await read_lines(client, name, max_pages))
