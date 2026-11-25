"""Token overhead of the bus in a Claude Code session, from its transcript (RT-6.9).

The transcript books every model request with its exact usage (input, cache
read, cache write, output). Two kinds of cost are the bus's:

- requests that exist only because of the bus: turns an agent's message, the
  listener or the Stop hook started, and rounds whose only input is the
  result of bus tools. Their whole usage counts, exactly.
- the bus's share of every other request: the fixed text (instructions, tool
  docs, skill) and what the bus put into the context since the last
  compaction (bus tool calls and results, loaded tool schemas, pushed
  messages, hook texts). Characters at CHARS_PER_TOKEN, as a share of the
  request's input; output only for the bus tool calls the model wrote.

A turn a trusted human starts over the bus is work, not overhead: it stands for
a prompt typed in the terminal. The totals of all requests come along, since
the transcript has them anyway. Only the main thread counts (no subagents).

The token split (method 1, an estimate; the totals stay exact) puts every token of
a request into one of three categories, per token type: task work, bus overhead and
multi-agent coordination (RT-6.9 step 2). It is by content, not by cause: the same
pass keeps the context's segments since the last compaction, each with its category
and characters; lays the request's exact token types over them by position (cache
read covers the oldest part, then cache write 1 h, 5 min, fresh input); sizes the
tool calls of the output exactly and splits the rest of it (text, thinking) by the
categories of the new input that drove the request (the driver rule, a convention).
Task work is the rest: all minus overhead minus coordination, never below 0.
"""

from __future__ import annotations

import json
import re
from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import a2a, session_file

# In the MCP tool names; a server added by hand (claude mcp add, Codex, Cursor)
# may still run under its key from before 0.96.0.
BUS = ("ai-agent-bus", "mattermost-agent-bus")
# Transcripts from before 0.96.0 carry the plugin's old name.
CHANNEL = ('<channel source="plugin:ai-agent-bus:', '<channel source="plugin:mattermost-ai-agent-bus:')
TRUSTED = 'from_trusted_human="true"'
SKILL = "ai-agent-bus"
LISTENER = "mm-agent-listen.sh"
CHARS_PER_TOKEN = 4
KEYS = ("in", "read", "w5m", "w1h", "out")
_FUNCTION = re.compile(r"<function>.*?</function>", re.S)
_TOOL_USE_ID = re.compile(r"<tool-use-id>([^<]+)</tool-use-id>")
_TAG_KIND = re.compile(r'\bkind="([^"]*)"')

# The token split (RT-6.9 step 2): its method number on the line, its categories, and
# which bus tool's call and result go where. Own posts go by their bus word: HELLO, IDLE
# and BYE are overhead, every other word and a plain reply coordination. A bus tool not
# named here (a later one) counts as overhead.
METHOD = 1
TASK, OVERHEAD, COORDINATION = 0, 1, 2
OVERHEAD_WORDS = frozenset({"HELLO", "IDLE", "BYE"})
_COORDINATING = frozenset({"ask_human", "resolve_ask", "coordinator", "get_file", "attach",
                           "get_thread", "list_recent"})
_POSTING = frozenset({"post_message", "reply_in_thread"})
# Pushed tags that are the bus's own whole: the channel check, a digest (its peers' posts
# included, the issue's default), and a server event (no post id: a stall, an ask's route).
_OWN_TAGS = frozenset({"channel_check", "digest"})


def _bus(text: str) -> bool:
    return any(b in text for b in BUS)


@dataclass
class Fixed:
    """The bus's text in the context of every request, in characters."""

    instructions: int
    tools: int       # tool docs: in every request unless the tools are deferred
    skill: int       # once the skill is loaded


def _ts(entry: dict[str, Any]) -> float:
    try:
        return datetime.fromisoformat(str(entry.get("timestamp", "")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _usage(u: dict[str, Any]) -> dict[str, int]:
    w1h = int((u.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0)
    write = int(u.get("cache_creation_input_tokens") or 0)
    return {"in": int(u.get("input_tokens") or 0), "read": int(u.get("cache_read_input_tokens") or 0),
            "w5m": max(0, write - w1h), "w1h": w1h, "out": int(u.get("output_tokens") or 0)}


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(_text(c.get("text", c.get("content", ""))) if isinstance(c, dict) else str(c)
                       for c in content)
    return ""


def _ours(text: str) -> bool:
    """A Stop hook's feedback or a hook text from this plugin."""
    return "Mattermost" in text or "HELLO thread" in text or LISTENER in text


def _tool_category(name: str, inp: dict[str, Any]) -> int:
    """A bus tool's call and result: its category (RT-6.9 step 2)."""
    tool = name.rsplit("__", 1)[-1]
    if tool in _POSTING:
        return OVERHEAD if a2a.word(str(inp.get("message", ""))) in OVERHEAD_WORDS else COORDINATION
    return COORDINATION if tool in _COORDINATING else OVERHEAD


def _tag(text: str) -> list[tuple[int, int]]:
    """A pushed <channel> tag: the envelope is overhead, the body coordination, or task
    work for a trusted human's prompt; the bus's own tags are overhead whole."""
    head_end, close = text.find(">"), text.rfind("</channel>")
    if head_end < 0 or close <= head_end:
        return [(OVERHEAD, len(text))]
    head = text[:head_end]
    m = _TAG_KIND.search(head)
    kind = m[1] if m else ""
    if kind in _OWN_TAGS or (kind != "reaction" and "post_id=" not in head):
        return [(OVERHEAD, len(text))]
    body = close - head_end - 1
    return [(OVERHEAD, len(text) - body), (TASK if TRUSTED in head else COORDINATION, body)]


def _events(body: str) -> list[tuple[int, int]]:
    """A wait_for_events result: empty is overhead; else the message bodies are
    coordination (task work from a trusted human), the server's own events and the
    JSON around the bodies overhead."""
    try:
        items: Any = json.loads(body)
        if isinstance(items, dict) and isinstance(items.get("result"), str):
            items = json.loads(items["result"])
    except ValueError:
        return [(OVERHEAD, len(body))]
    if isinstance(items, dict):
        items = items.get("result")
    if not isinstance(items, list):
        return [(OVERHEAD, len(body))]
    parts = {TASK: 0, COORDINATION: 0}
    for item in items:
        post = item.get("post") if isinstance(item, dict) else None
        if not isinstance(post, dict) or (not post.get("id") and item.get("kind") != "reaction"):
            continue                                  # the server's own: overhead
        parts[TASK if item.get("from_trusted_human") is True else COORDINATION] += len(str(post.get("message", "")))
    bodies = parts[TASK] + parts[COORDINATION]
    return [(OVERHEAD, max(0, len(body) - bodies)), (TASK, parts[TASK]), (COORDINATION, parts[COORDINATION])]


class _Context:
    """The context's segments since the last compaction, in order: each a category and
    its characters, held as running sums so a request's split is a binary search."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.ends: list[float] = [0.0]                 # characters up to each segment's end
        self.cats: list[int] = [TASK]
        self.sums: list[tuple[float, float, float]] = [(0.0, 0.0, 0.0)]
        self.new = [0.0, 0.0, 0.0]                     # input since the last request, by category

    def add(self, cat: int, chars: float, new: bool = True) -> None:
        if chars <= 0:
            return
        s = list(self.sums[-1])
        s[cat] += chars
        self.ends.append(self.ends[-1] + chars)
        self.cats.append(cat)
        self.sums.append((s[0], s[1], s[2]))
        if new:
            self.new[cat] += chars

    def add_split(self, chars: float, shares: list[float], new: bool = True) -> None:
        for cat in (TASK, OVERHEAD, COORDINATION):
            self.add(cat, chars * shares[cat], new)

    def before(self, q: float) -> list[float]:
        """The characters of each category in the first q characters."""
        if q >= self.ends[-1]:
            return list(self.sums[-1])
        i = bisect_left(self.ends, q)
        out = list(self.sums[i - 1])
        out[self.cats[i]] += q - self.ends[i - 1]
        return out

    def shares(self) -> list[float]:
        total = self.ends[-1]
        return [x / total for x in self.sums[-1]] if total else [1.0, 0.0, 0.0]

    def driver(self) -> list[float]:
        total = sum(self.new)
        return [x / total for x in self.new] if total else [1.0, 0.0, 0.0]


_ORDER = ("read", "w1h", "w5m", "in")         # the input's token types from the front


def _input_split(u: dict[str, int], head: int, ctx: _Context) -> list[dict[str, float]]:
    """RT-6.9 step 2: the request's exact input types laid over its context by position.
    The harness's baseline (its system prompt and tools) is the front, the remainder of
    the input once the bus's fixed text (`head` characters) and the segments are counted
    at CHARS_PER_TOKEN, scaled down when they would exceed the exact input."""
    total = sum(u[k] for k in _ORDER)
    known = (head + ctx.ends[-1]) / CHARS_PER_TOKEN
    scale = min(1.0, total / known) if known else 0.0
    base = total - scale * known

    def at(p: float) -> list[float]:
        if p <= base or scale <= 0:
            return [min(p, base), 0.0, 0.0]
        q = (p - base) / scale * CHARS_PER_TOKEN
        c = [0.0, min(q, head), 0.0] if q <= head else ctx.before(q - head)
        if q > head:
            c[OVERHEAD] += head
        return [base + scale * c[TASK] / CHARS_PER_TOKEN, scale * c[OVERHEAD] / CHARS_PER_TOKEN,
                scale * c[COORDINATION] / CHARS_PER_TOKEN]

    out: list[dict[str, float]] = [{}, {}, {}]
    lo, edge = [0.0, 0.0, 0.0], 0.0
    for k in _ORDER:
        edge += u[k]
        hi = at(edge)
        for cat in (TASK, OVERHEAD, COORDINATION):
            out[cat][k] = max(0.0, hi[cat] - lo[cat])
        lo = hi
    return out


class _Count:
    def __init__(self, since: float, until: float | None = None) -> None:
        self.since, self.until = since, until
        self.reqs = self.bus_reqs = 0
        self.all = dict.fromkeys(KEYS, 0)
        self.bus = dict.fromkeys(KEYS, 0.0)
        # The token split (method 1): overhead and coordination per token type, and the
        # requests whose whole new input was of one category.
        self.ovh = dict.fromkeys(KEYS, 0.0)
        self.coord = dict.fromkeys(KEYS, 0.0)
        self.req_ovh = self.req_coord = 0

    def add(self, r: dict[str, Any] | None) -> None:
        """One request: all of it if the bus caused it, else the bus's share."""
        if (r is None or r["ts"] < self.since or not r["usage"]
                or (self.until is not None and r["ts"] >= self.until)):
            return
        u = _usage(r["usage"])
        self.reqs += 1
        for k in KEYS:
            self.all[k] += u[k]
        self._split(r, u)
        if r["plugin"]:
            self.bus_reqs += 1
            for k in KEYS:
                self.bus[k] += u[k]
            return
        inputs = u["in"] + u["read"] + u["w5m"] + u["w1h"]
        share = min(1.0, r["context"] / CHARS_PER_TOKEN / inputs) if inputs else 0.0
        for k in ("in", "read", "w5m", "w1h"):
            self.bus[k] += u[k] * share
        self.bus["out"] += min(u["out"], r["out_bus"] / CHARS_PER_TOKEN)

    def _split(self, r: dict[str, Any], u: dict[str, int]) -> None:
        """RT-6.9 step 2: the input by position (laid over the context when the request
        started), the output by block: tool calls by the tool's category, the rest (text,
        thinking) by the driver, the categories of the new input before the request."""
        split = r["split"]
        tools = [c / CHARS_PER_TOKEN for c in r["out_cat"]]
        if sum(tools) > u["out"]:
            tools = [t * u["out"] / sum(tools) for t in tools]
        rest = u["out"] - sum(tools)
        for cat, into in ((OVERHEAD, self.ovh), (COORDINATION, self.coord)):
            for k in _ORDER:
                into[k] += split[cat][k]
            into["out"] += tools[cat] + rest * r["driver"][cat]
        # A request whose new input held no task work: coordination when any of it was
        # (a pushed message's envelope comes with its body), else overhead (an empty poll).
        new = r["new"]
        if not new[TASK] and new[COORDINATION]:
            self.req_coord += 1
        elif not new[TASK] and new[OVERHEAD]:
            self.req_ovh += 1


def _hook_context(c: str) -> list[tuple[int, int]]:
    """A hook's added context: corral's lines about Mattermost and this plugin's texts
    are overhead, the rest task work."""
    if c.startswith("You are running inside corral"):
        ours = sum(len(x) for x in c.splitlines() if "Mattermost" in x)
        return [(OVERHEAD, ours), (TASK, len(c) - ours)]
    return [(OVERHEAD if _ours(c) else TASK, len(c))]


def _skill_text(text: str) -> bool:
    """The text of a skill Claude Code put in after the Skill tool (this bus's)."""
    return text.startswith("Base directory for this skill:") and SKILL in text.split("\n", 1)[0]


def measure(path: str | Path, since: float, fixed: Fixed,
            until: float | None = None) -> dict[str, Any] | None:
    """Overhead and totals of the requests after `since` (and before `until`,
    a part's cut, RT-6.9), and the token split by category (method 1), or None
    without a transcript."""
    # Never through a symlink, a FIFO or out of ~/.claude/projects (RT-6.18).
    text = session_file.read_transcript(path)
    if text is None:
        return None
    lines = text.splitlines()
    resident = 0                      # bus characters in the context since the last compaction
    deferred = skill = by_bus = False
    pending: list[bool] = []          # tool results since the last request: bus or not
    ids: dict[str, str] = {}          # tool_use id -> "bus" | "search" | "listener" | "wait"
    cats: dict[str, int] = {}         # tool_use id -> its category (the split)
    ctx = _Context()                  # the split's segments since the last compaction
    summary: list[float] | None = None  # the shares before a compaction, for its summary
    count = _Count(since, until)
    req: dict[str, Any] | None = None

    def base() -> int:
        return fixed.instructions + (0 if deferred else fixed.tools) + (fixed.skill if skill else 0)

    def head() -> int:
        # The bus's fixed text at the front; the skill comes in later, as a segment.
        return fixed.instructions + (0 if deferred else fixed.tools)

    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict) or e.get("isSidechain"):
            continue
        kind = e.get("type")
        if kind == "system" and e.get("subtype") == "compact_boundary":
            resident, pending = 0, []
            summary = ctx.shares()
            ctx.reset()
        elif kind == "attachment":
            a = e.get("attachment") or {}
            if a.get("type") == "deferred_tools_delta" and any(_bus(n) for n in a.get("addedNames", [])):
                deferred = True
            elif a.get("type") == "queued_command" and str(a.get("prompt", "")).startswith(CHANNEL):
                if TRUSTED not in a["prompt"]:
                    resident += len(a["prompt"])
                for cat, n in _tag(a["prompt"]):
                    ctx.add(cat, n)
            elif a.get("type") == "hook_additional_context":
                for c in a.get("content") or []:
                    c = str(c)
                    if c.startswith("You are running inside corral"):
                        resident += sum(len(x) for x in c.splitlines() if "Mattermost" in x)
                    elif _ours(c):
                        resident += len(c)
                    for cat, n in _hook_context(c):
                        ctx.add(cat, n)
            elif a.get("type") == "invoked_skills":       # skills put back after a compaction
                for sk in a.get("skills") or []:
                    if isinstance(sk, dict):
                        ctx.add(OVERHEAD if SKILL in str(sk.get("name", "")) else TASK,
                                len(str(sk.get("content", ""))))
        elif kind == "user":
            content = (e.get("message") or {}).get("content")
            if isinstance(content, list) and any(isinstance(c, dict) and c.get("type") == "tool_result"
                                                 for c in content):
                for c in content:
                    if not isinstance(c, dict) or c.get("type") != "tool_result":
                        continue
                    tid = c.get("tool_use_id", "")
                    what = ids.get(tid)
                    pending.append(what in ("bus", "wait"))
                    body = _text(c.get("content"))
                    if what in ("bus", "wait", "listener"):
                        resident += len(body)
                    elif what == "search":
                        ours = sum(len(f) for f in _FUNCTION.findall(body) if _bus(f))
                        resident += ours
                        ctx.add(OVERHEAD, ours)
                        ctx.add(TASK, len(body) - ours)
                        continue
                    if what == "wait":
                        for cat, n in _events(body):
                            ctx.add(cat, n)
                    else:
                        ctx.add(cats.get(tid, TASK), len(body))
                continue
            origin = e.get("origin")
            origin = origin.get("kind") if isinstance(origin, dict) else origin
            text = _text(content)
            if e.get("isCompactSummary"):
                ctx.add_split(len(text), summary or [1.0, 0.0, 0.0])
            elif origin == "human":
                by_bus, pending = False, []
                ctx.add(TASK, len(text))
            elif origin == "channel" or text.startswith(CHANNEL):
                by_bus, pending = TRUSTED not in text, []
                if by_bus:
                    resident += len(text)
                for cat, n in _tag(text):
                    ctx.add(cat, n)
            elif origin == "task-notification":
                m = _TOOL_USE_ID.search(text)
                by_bus, pending = bool(m and ids.get(m[1]) == "listener"), []
                if by_bus:
                    resident += len(text)
                ctx.add(OVERHEAD if by_bus else TASK, len(text))
            elif text.startswith("Stop hook feedback") and _ours(text):
                by_bus, pending = True, []
                resident += len(text)
                ctx.add(OVERHEAD, len(text))
            else:
                ctx.add(OVERHEAD if _skill_text(text) else TASK, len(text))
        elif kind == "assistant":
            msg = e.get("message") or {}
            rid = e.get("requestId") or msg.get("id") or ""
            if req is None or req["id"] != rid:
                count.add(req)
                usage = msg.get("usage")
                req = {"id": rid, "ts": _ts(e), "usage": usage, "out_bus": 0,
                       "plugin": by_bus or (bool(pending) and all(pending)),
                       "context": base() + resident, "out_cat": [0, 0, 0],
                       "split": _input_split(_usage(usage), head(), ctx) if isinstance(usage, dict) else None,
                       "driver": ctx.driver(), "new": list(ctx.new)}
                pending = []
                ctx.new = [0.0, 0.0, 0.0]
            for c in msg.get("content") or []:
                if not isinstance(c, dict):
                    continue
                if c.get("type") != "tool_use":
                    # Text and thinking stay in the context, by the request's driver.
                    ctx.add_split(len(str(c.get("text") or c.get("thinking") or "")), req["driver"], new=False)
                    continue
                name, inp = c.get("name", ""), c.get("input") or {}
                size = len(json.dumps(inp, ensure_ascii=False))
                tid = c.get("id", "")
                if _bus(name):
                    ids[tid] = "wait" if name.endswith("__wait_for_events") else "bus"
                    cats[tid] = _tool_category(name, inp if isinstance(inp, dict) else {})
                elif name == "Bash" and LISTENER in str(inp.get("command", "")):
                    ids[tid] = "listener"
                    cats[tid] = OVERHEAD
                else:
                    if name == "ToolSearch":
                        ids[tid] = "search"
                    elif name == "Skill" and SKILL in str(inp.get("skill", "")):
                        skill = True
                    req["out_cat"][TASK] += size
                    ctx.add(TASK, size, new=False)
                    continue
                req["out_cat"][cats[tid]] += size
                ctx.add(cats[tid], size, new=False)
                req["out_bus"] += size
                resident += size
    count.add(req)
    if not count.reqs:
        return None
    ovh, coord = {}, {}
    for k in KEYS:  # the invariant: task = all - ovh - coord, never below 0 (RT-6.9 step 2)
        ovh[k] = min(count.all[k], round(count.ovh[k]))
        coord[k] = min(count.all[k] - ovh[k], round(count.coord[k]))
    return {"req": count.reqs, "bus_req": count.bus_reqs, "fixed": round(base() / CHARS_PER_TOKEN),
            "skill": int(skill), "all": count.all, "bus": {k: round(v) for k, v in count.bus.items()},
            "ovh": ovh, "coord": coord, "req_ovh": count.req_ovh, "req_coord": count.req_coord, "m": METHOD}
