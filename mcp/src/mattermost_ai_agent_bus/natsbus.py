"""NATS transport of the agent bus (ADR-014, point 16). Not wired into the server yet.

The permission model, from the host's side:

* The sender of a message is the last token of its subject
  (`<bus>.ch.<channel>.<sender>`, `<bus>.dm.<recipient>.<sender>`,
  `<presence>.<owner>.<sender>`, `<presence>.crown.<channel>.<sender>`), never a header or
  the body. No `Nats-Msg-Id` header is ever set (the stream's message-id window is
  the side channel of condition 1).
* The host never talks to JetStream. It reads only through the push consumers the
  login service makes for its agent (ack none), subscribed by their literal deliver
  subject `deliver.<name>.ch.<channel>`, `.hu.<channel>` (people, RT-6.42), `.dm` and
  `.presence`.
* The login service gets everything in the CONNECT `auth_token` (Keycloak access
  token, channels, `since`) and the host's nkey signature over the server's nonce.
  The credential lives 15 minutes; the server closes the connection at expiry. The
  bus reconnects itself with a fresh access token each time and sends `since`, the
  last stream sequence seen per channel.

Nothing here logs, stores or puts into an exception the access token or the seed:
the seed stays in the login, whose `nkey()` gives only a `sign` function. In the host
process of a keyless corral session that login is `login_relay.RelayedLogin`, which
got the seed from the relay in the sandbox that keeps it (#154).
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import hashlib
import inspect
import ipaddress
import json
import logging
import math
import random
import re
import ssl
import time
from urllib.parse import urlsplit
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from nats.aio.client import Client as NatsClient

from . import a2a
from .login import NKey

log = logging.getLogger(__name__)

# How long closing a superseded connection may take: it runs under the send
# lock, and a close that never answers must not block the next post.
CLOSE_SEC = 5.0

MAX_BODY = 65536  # the BUS stream's max_msg_size
HEARTBEAT_SEC = 30.0
PRESENCE_FRESH_SEC = 90.0  # a holder's last presence must be younger than this
OWN_ECHO_SEC = 75.0  # the echo of this agent's own presence must be younger than this (monotonic), else the view is unknown
GAP_RECONNECT_SEC = 30.0  # at most one reconnect for a gap within this time
MAX_AGE_SEC = 86400.0  # the PRESENCE stream's max_age: older claims and presence are gone
MEMBER_RENEW_SEC = 6 * 3600.0  # a membership is announced again this often (the stream keeps 24 h)
MEMBER_SYNC_WAIT_SEC = 10.0  # how long an announcement waits for the bus to be synced
PRESENCE_FRESH_KEY = "@presence"  # key of the hello's `since`: this session has not read the presence stream
RENEW_SEC = 14 * 60.0  # the login service's credential lives 15 minutes: renew a minute before
SEND_WAIT_SEC = 10.0  # a send waits this long for a planned reconnect
BUS_STREAM = "BUS"
PRESENCE_STREAM = "PRESENCE"
DELIVER = "deliver"  # consumers.DeliverPrefix / callout.DeliverPrefix

CHANNEL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")  # Go checkToken
AGENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")  # Go consumers.agentRE
PREFIX_RE = re.compile(r"^[A-Za-z0-9_-]+$")
# An owner counts only in this form (RT-6.37): a Mattermost username's characters in lower
# case, the same string the bus compares with MM_TRUSTED_HUMANS and the owners of Mattermost
# bots. Nothing is normalised: an owner in any other form is no owner.
OWNER_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
NO_OWNER = "-"  # the owner token of an owner not in OWNER_RE's form
USERNAME_RE = re.compile(r"[a-z][a-z0-9._-]{0,63}")  # a human's name as the login service reads it
MAX_TRUSTED = 32  # the hello's `trusted` and an attestation's trusted list: at most this many


def owner_token(owner: str) -> str:
    """The owner as the token of a presence subject (`<presence>.<owner>.<name>`, RT-6.37),
    as Go's ownerToken (services/auth/internal/callout/perms.go) makes it: "." as "~" (a
    subject token holds no "."; no username holds "~"), NO_OWNER for any other form."""
    return owner.replace(".", "~") if OWNER_RE.fullmatch(owner) else NO_OWNER


def owner_of_token(token: str) -> str:
    """The owner a presence subject's owner token names, '' for none."""
    owner = token.replace("~", ".")
    return owner if token != NO_OWNER and "." not in token and OWNER_RE.fullmatch(owner) else ""

_PUBLISH_DENIED_RE = re.compile(r'permissions violation for publish to "([^"]*)"', re.IGNORECASE)
_KINDS = ("post", "reaction", "unreaction")
_STATUSES = ("online", "away", "bye")


class NatsBusError(Exception):
    """A bus operation failed. The text never holds a token or a seed."""


class NameMismatch(NatsBusError):
    """The server refused the host's subscription to its own deliver subjects:
    the name the login service wrote into the credential is not the one the
    host computed. The host sends nothing under the computed name."""


class MessageError(ValueError):
    """A message body is not format v1. The text never holds content."""


# --- the agent's name --------------------------------------------------------

def agent_name(owner: str, host_nkey_public: str, prefix: str = "agent-") -> str:
    """Same as Go `name()` (services/auth/internal/callout/service.go) plus the
    prefix: SHA-256 over owner, a NUL byte and the host nkey, the first 16 bytes
    as lower-case base32 without padding (26 characters). The owner is used as
    the login service gets it (the token's `preferred_username`), not changed."""
    digest = hashlib.sha256(owner.encode() + b"\x00" + host_nkey_public.encode()).digest()[:16]
    return prefix + base64.b32encode(digest).decode().lower().rstrip("=")


# --- message format v1 -----------------------------------------------------------

Prop = str | int | float | bool


@dataclass(frozen=True)
class Message:
    """One bus message. The sender is not part of it: it is the subject's last token."""

    kind: str  # "post" | "reaction" | "unreaction"
    text: str = ""
    root: int | None = None  # post: stream sequence of the thread's root post
    ref: int | None = None  # reaction: stream sequence of the post
    emoji: str | None = None
    props: dict[str, Prop] = field(default_factory=dict)


def _seq(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 1


def _wellformed(s: str) -> bool:
    """False for a string with a lone surrogate: it has no UTF-8 form."""
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _check(m: Message, props_prefix: str = a2a.DEFAULT_PREFIX) -> None:
    if m.kind not in _KINDS:
        raise MessageError("unknown kind")
    if not isinstance(m.text, str):
        raise MessageError("text is not a string")
    if m.root is not None and not _seq(m.root):
        raise MessageError("root is not a sequence")
    if not _wellformed(m.text):
        raise MessageError("text is not valid Unicode")
    for k, v in m.props.items():
        if not isinstance(k, str) or not isinstance(v, str | int | float | bool):
            raise MessageError("props hold only strings and simple values")
        if not k.startswith(props_prefix):
            raise MessageError("props keys start with the bus's props prefix (MM_PROPS_PREFIX)")
        if not _wellformed(k) or (isinstance(v, str) and not _wellformed(v)):
            raise MessageError("props are not valid Unicode")
    if m.kind == "post":
        if m.ref is not None or m.emoji is not None:
            raise MessageError("a post has no ref or emoji")
    else:
        if not _seq(m.ref):
            raise MessageError("a reaction needs ref")
        if not isinstance(m.emoji, str) or not m.emoji:
            raise MessageError("a reaction needs an emoji")
        if len(m.emoji) > 64 or m.emoji.splitlines() != [m.emoji]:
            raise MessageError("an emoji is one line of at most 64 characters")
        if not _wellformed(m.emoji):
            raise MessageError("emoji is not valid Unicode")
        if m.text != "" or m.root is not None:
            raise MessageError("a reaction has no text or root")


def _dumps(obj: object, ensure_ascii: bool = True) -> str:
    """JSON for the wire: never NaN or Infinity (not JSON, other readers refuse them)."""
    try:
        return json.dumps(obj, ensure_ascii=ensure_ascii, separators=(",", ":"), allow_nan=False)
    except ValueError:
        raise MessageError("a number is not representable in JSON") from None


def encode(m: Message, props_prefix: str = a2a.DEFAULT_PREFIX) -> bytes:
    _check(m, props_prefix)
    body = _dumps({"v": 1, "kind": m.kind, "text": m.text, "root": m.root, "ref": m.ref,
                   "emoji": m.emoji, "props": m.props}, ensure_ascii=False).encode()
    if len(body) > MAX_BODY:
        raise MessageError(f"message is {len(body)} bytes, more than {MAX_BODY}")
    return body


def _constant(name: str) -> Any:
    raise MessageError("NaN and Infinity are not JSON")


def _float(text: str) -> float:
    f = float(text)
    if not math.isfinite(f):
        raise MessageError("a number is out of range")
    return f


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise MessageError("duplicate key")
        out[k] = v
    return out


def _obj(data: bytes) -> dict[str, Any]:
    if len(data) > MAX_BODY:
        raise MessageError("body too big")
    try:
        text = data.decode("utf-8")  # strict: no replacement characters, no other encoding
    except UnicodeDecodeError:
        raise MessageError("body is not UTF-8") from None
    try:
        obj = json.loads(text, parse_constant=_constant, parse_float=_float, object_pairs_hook=_pairs)
    except MessageError:
        raise
    except (ValueError, RecursionError):
        raise MessageError("body is not JSON") from None
    if not isinstance(obj, dict):
        raise MessageError("body is not an object")
    v = obj.get("v")
    if isinstance(v, bool) or v != 1 or not isinstance(v, int):
        raise MessageError("unknown version")
    return obj


def decode(data: bytes, props_prefix: str = a2a.DEFAULT_PREFIX) -> Message:
    """Strict: anything that is not a well-formed v1 message raises MessageError.
    Unknown keys (a forged `sender`, say) are ignored, never read."""
    obj = _obj(data)
    props = obj.get("props", {})
    if not isinstance(props, dict):
        raise MessageError("props is not an object")
    m = Message(kind=obj.get("kind"), text=obj.get("text", ""), root=obj.get("root"),  # type: ignore[arg-type]
                ref=obj.get("ref"), emoji=obj.get("emoji"), props=props)
    _check(m, props_prefix)
    return m


BOT_RE = re.compile(r"[a-z0-9]{26}")  # a Mattermost user id


# The agent card's data in the presence (RT-6.37, ADR-015): what the HELLO states, for the
# operator console. Self-stated like the name: shown as text, never trusted. Each field is
# bounded on both sides, so the presence body stays far below the PRESENCE stream's
# max_msg_size (4096 bytes) whatever an agent announces.
HARNESSES = ("claude-code", "opencode", "cursor", "codex", "pi", "other")
CARD_MODEL_MAX = 64  # characters
CARD_TASK_MAX = 120  # characters, one line
CARD_BRANCH_MAX = 100  # characters, a git ref name
CARD_ASKS_MAX = 999
# A branch as git allows it, narrowed: ASCII letters, digits and ._/+#-, starting with a
# letter, a digit or # (a HELLO may name a PR as "#118"); no "..", "//", "/.", no ".lock",
# "/" or "." at the end (git check-ref-format). Anything else is dropped, never repaired.
BRANCH_RE = re.compile(r"[A-Za-z0-9#][A-Za-z0-9._/+#-]{0,99}")
# Control and format characters (C0, DEL, C1, soft hyphen, Arabic letter mark, Mongolian
# vowel separator, zero-width, bidi overrides, line separators, BOM, interlinear
# annotations, tag characters) and lone surrogates, which no UTF-8 carries: a card line is
# plain text on one line.
_CARD_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f\xad\u061c\u180e\u200b-\u200f\u2028-\u202e"
                           "\u2060-\u2069\ufeff\ufff9-\ufffb\ud800-\udfff\U000e0000-\U000e007f]")


@dataclass(frozen=True)
class PresenceCard:
    """The card's data a presence carries; None: not stated (an older MCP server, or not
    announced yet). harness: one of HARNESSES; asks: this session's own asks to a human
    still waiting (asks.waiting)."""

    harness: str | None = None
    model: str | None = None
    task: str | None = None
    branch: str | None = None
    asks: int | None = None


def card_text(v: object, limit: int) -> str | None:
    """A card line: control characters out, whitespace collapsed, at most `limit` characters;
    None for no string."""
    if not isinstance(v, str):
        return None
    return " ".join(_CARD_CONTROL.sub(" ", v).split())[:limit].rstrip()


def card_branch(v: object) -> str | None:
    """A branch name when it is a safe git ref name (BRANCH_RE and git's rules), '' for none
    stated; None for anything else (dropped, never repaired)."""
    if not isinstance(v, str):
        return None
    v = v.strip()
    if not v:
        return ""
    if (not BRANCH_RE.fullmatch(v) or ".." in v or "//" in v or "/." in v
            or v.endswith((".lock", "/", "."))):
        return None
    return v


def card_harness(v: object) -> str | None:
    """One of HARNESSES; any other name is "other" (a harness a newer server knows); None for
    no string."""
    if not isinstance(v, str) or not v:
        return None
    return v if v in HARNESSES else "other"


def card_asks(v: object) -> int | None:
    """A non-negative int, capped at CARD_ASKS_MAX; None for anything else."""
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        return None
    return min(v, CARD_ASKS_MAX)


def clean_card(card: PresenceCard) -> PresenceCard:
    """The card with every field bounded (the encoder's and the decoder's rule)."""
    return PresenceCard(harness=card_harness(card.harness), model=card_text(card.model, CARD_MODEL_MAX),
                        task=card_text(card.task, CARD_TASK_MAX), branch=card_branch(card.branch),
                        asks=card_asks(card.asks))


_CARD_KEYS = ("harness", "model", "task", "branch", "asks")


def encode_presence(status: str, bot: str = "", bot_name: str = "",
                    card: PresenceCard | None = None) -> bytes:
    """The presence body; `bot` is the agent's Mattermost bot id (RT-6.37) and `name` that
    bot's username, the name people and coordinator("who") know the agent by (the operator
    console shows it, ADR-015); each left out when ''. `card`: the HELLO card's data
    (harness, model, task, branch, open asks), bounded by clean_card; a field it does not
    state (None) is left out."""
    if status not in _STATUSES:
        raise MessageError("unknown status")
    body: dict[str, Any] = {"v": 1, "status": status}
    if bot:
        body["bot"] = bot
    if bot_name:
        body["name"] = bot_name
    if card is not None:
        clean = clean_card(card)
        for key in _CARD_KEYS:
            value = getattr(clean, key)
            if value is not None:
                body[key] = value
    return _dumps(body).encode()


def decode_presence_card(data: bytes) -> PresenceCard:
    """The card's data a presence body states, each field bounded as the encoder bounds it;
    a missing or bad field is None and never drops the message (an older MCP server sends
    none). Self-stated: for display, never for a decision."""
    try:
        obj = _obj(data)
    except MessageError:
        return PresenceCard()
    return clean_card(PresenceCard(**{k: obj.get(k) for k in _CARD_KEYS}))


def decode_presence(data: bytes) -> str:
    status = _obj(data).get("status")
    if status not in _STATUSES:
        raise MessageError("unknown status")
    return str(status)


def decode_presence_bot(data: bytes) -> str:
    """The bot id of a presence body when it is a Mattermost user id, else ''. A bad
    bot field never drops the message."""
    try:
        bot = _obj(data).get("bot")
    except MessageError:
        return ""
    return bot if isinstance(bot, str) and BOT_RE.fullmatch(bot) else ""


def decode_presence_name(data: bytes) -> str:
    """The bot's username a presence body states when it is a valid username, else ''. The
    agent states it of itself: a label for people, never an identity (the subject's name is).
    A bad name field never drops the message."""
    try:
        name = _obj(data).get("name")
    except MessageError:
        return ""
    return name if isinstance(name, str) and USERNAME_RE.fullmatch(name) else ""


def link_tag(name: str) -> str:
    """The 16 characters of the hash part of an agent name that the roster status shows
    (RT-6.37); '' for a name shorter than the hash part (26 characters)."""
    return name[-26:][:16] if len(name) >= 26 else ""


@dataclass(frozen=True)
class Attestation:
    """The login service's record of an agent's circle, as it wrote it at the agent's
    credential (`<presence>.attest.<agent>.v1`): anchors are what the service pinned
    (stable), names what they were called at that time. `exp` is server time."""

    owner_anchor: str
    owner_name: str
    trusted: tuple[tuple[str, str], ...]  # (anchor, name)
    cut: bool
    exp: float


def _person(obj: Any, what: str) -> tuple[str, str]:
    if not isinstance(obj, dict):
        raise MessageError(f"{what} is not an object")
    anchor, name = obj.get("anchor"), obj.get("name")
    if not isinstance(anchor, str) or not OWNER_RE.fullmatch(anchor):
        raise MessageError(f"{what} has a bad anchor")
    if not isinstance(name, str) or not USERNAME_RE.fullmatch(name):
        raise MessageError(f"{what} has a bad name")
    return anchor, name


def _int(obj: dict[str, Any], key: str) -> int:
    v = obj.get(key)
    if isinstance(v, bool) or not isinstance(v, int):
        raise MessageError(f"{key} is not an integer")
    return v


def decode_attestation(data: bytes, agent: str) -> Attestation:
    """Strict: anything that is not a well-formed v1 attestation of `agent` raises MessageError."""
    obj = _obj(data)
    if obj.get("agent") != agent:
        raise MessageError("attestation of another agent")
    owner = _person(obj.get("owner"), "owner")
    trusted = obj.get("trusted")
    if not isinstance(trusted, list) or len(trusted) > MAX_TRUSTED:
        raise MessageError("trusted is not a list of at most 32")
    cut = obj.get("cut")
    if not isinstance(cut, bool):
        raise MessageError("cut is not a boolean")
    _int(obj, "iat")
    return Attestation(owner_anchor=owner[0], owner_name=owner[1],
                       trusted=tuple(_person(t, "a trusted entry") for t in trusted),
                       cut=cut, exp=float(_int(obj, "exp")))


def normalise_trusted(names: Iterable[str]) -> tuple[tuple[str, ...], bool]:
    """The hello's trusted names and whether the list was cut: a leading "@" dropped, lower
    case, only valid usernames, each once, at most MAX_TRUSTED. cut: an invalid name was
    dropped or the list truncated (a repeat or a blank is no loss)."""
    out: dict[str, None] = {}
    cut = False
    for n in names:
        n = n.strip().lstrip("@").lower()
        if USERNAME_RE.fullmatch(n):
            out.setdefault(n)
        elif n:
            cut = True
    return tuple(out)[:MAX_TRUSTED], cut or len(out) > MAX_TRUSTED


# --- the open asks, one record per agent (RT-6.43 step 5, ADR-015 addition of 2026-11-25) -----
# `<presence>.ask.<home>.<name>` in PRESENCE: the asking agent's open asks, for the operator
# console. A title and no body; bounded so that no agent can stretch it.
ASK_MAX = 20  # asks in one record; the rest is counted in `more`
ASK_TITLE_MAX = 200  # characters
ASK_RECORD_MAX = 4096  # bytes, the whole record: what a reader accepts (protocol.md)
# What the encoder makes at most: the PRESENCE stream's max_msg_size (4096) counts headers
# too; this bus sends none, the margin keeps a record valid if a client library adds one.
ASK_BODY_MAX = 3840
ASK_VALID_SEC = 1800  # `exp` - `iat`: a record not renewed runs out after 30 min
ASK_RENEW_SEC = 600.0  # renewed this often while an ask is open
ASK_KINDS = ("approval", "question")
# The asked human's anchor in subject form (owner_token's encoding: "." as "~"); "-": none.
HUMAN_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9~_-]{0,63}")
_ASK_RECORD_KEYS = frozenset({"v", "asks", "more", "iat", "exp"})
_ASK_KEYS = frozenset({"mm", "kind", "human", "title", "asked", "answered"})


@dataclass(frozen=True)
class OpenAsk:
    """One open ask as the record carries it: `mm` the ask's Mattermost post id, `human` the
    asked human's anchor in subject form or NO_OWNER (in what the MCP server hands the bus:
    the asked human's username, which the bus maps through its attestation), `asked` server
    time in Unix seconds, `answered` a trusted human's first answer was seen."""

    mm: str
    kind: str
    human: str
    title: str
    asked: int
    answered: bool = False


@dataclass(frozen=True)
class AskRecord:
    asks: tuple[OpenAsk, ...]
    more: int
    iat: int
    exp: int


def _ask_obj(a: OpenAsk) -> dict[str, Any]:
    return {"mm": a.mm, "kind": a.kind, "human": a.human, "title": a.title,
            "asked": a.asked, "answered": a.answered}


def encode_ask_record(asks: Iterable[OpenAsk], iat: int, more: int = 0) -> bytes:
    """The ask record (protocol.md): the open asks oldest first, at most ASK_MAX, each title
    cut to one line of ASK_TITLE_MAX characters, the whole within ASK_BODY_MAX bytes; the
    newest that do not fit are counted in `more`, as is an ask with a malformed id or a
    repeated one. A `human` off HUMAN_TOKEN_RE goes as NO_OWNER, an unknown kind as approval."""
    seen: set[str] = set()
    clean: list[OpenAsk] = []
    for a in asks:
        if not isinstance(a.mm, str) or not BOT_RE.fullmatch(a.mm) or a.mm in seen:
            more += 1
            continue
        seen.add(a.mm)
        clean.append(OpenAsk(
            mm=a.mm, kind=a.kind if a.kind in ASK_KINDS else "approval",
            human=a.human if a.human == NO_OWNER or HUMAN_TOKEN_RE.fullmatch(a.human or "") else NO_OWNER,
            title=card_text(a.title, ASK_TITLE_MAX) or "",
            asked=max(0, int(a.asked)), answered=bool(a.answered)))
    clean.sort(key=lambda a: a.asked)
    kept, more = clean[:ASK_MAX], more + max(0, len(clean) - ASK_MAX)
    iat = int(iat)
    while True:
        body = _dumps({"v": 1, "asks": [_ask_obj(a) for a in kept], "more": more,
                       "iat": iat, "exp": iat + ASK_VALID_SEC}).encode()
        if len(body) <= ASK_BODY_MAX or not kept:
            return body
        kept.pop()  # the newest goes first
        more += 1


def _nonneg(v: object, what: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise MessageError(f"{what} is not a non-negative integer")
    return v


def decode_ask_record(data: bytes) -> AskRecord:
    """Strict: a record that is not format v1 within ASK_RECORD_MAX bytes, with an unknown,
    missing or duplicate key, more than ASK_MAX asks, a title over ASK_TITLE_MAX characters,
    a `human` that is neither NO_OWNER nor an anchor in subject form, or a malformed or
    repeated `mm`, or an `exp` not after `iat` or more than ASK_VALID_SEC after it raises
    MessageError (dropped whole; the text carries none of it)."""
    if len(data) > ASK_RECORD_MAX:
        raise MessageError("ask record too big")
    obj = _obj(data)
    if set(obj) != _ASK_RECORD_KEYS:
        raise MessageError("ask record keys")
    raw = obj["asks"]
    if not isinstance(raw, list) or len(raw) > ASK_MAX:
        raise MessageError("ask record list")
    iat, exp = _nonneg(obj["iat"], "iat"), _nonneg(obj["exp"], "exp")
    if not iat < exp <= iat + ASK_VALID_SEC:
        raise MessageError("ask record validity")
    out: list[OpenAsk] = []
    seen: set[str] = set()
    for a in raw:
        if not isinstance(a, dict) or set(a) != _ASK_KEYS:
            raise MessageError("ask keys")
        mm, kind, human, title, answered = a["mm"], a["kind"], a["human"], a["title"], a["answered"]
        if not isinstance(mm, str) or not BOT_RE.fullmatch(mm) or mm in seen:
            raise MessageError("ask id")
        seen.add(mm)
        if kind not in ASK_KINDS:
            raise MessageError("ask kind")
        if not isinstance(human, str) or not (human == NO_OWNER or HUMAN_TOKEN_RE.fullmatch(human)):
            raise MessageError("ask human")
        if not isinstance(title, str) or len(title) > ASK_TITLE_MAX:
            raise MessageError("ask title")
        if not isinstance(answered, bool):
            raise MessageError("ask answered")
        out.append(OpenAsk(mm=mm, kind=kind, human=human, title=title,
                           asked=_nonneg(a["asked"], "asked"), answered=answered))
    return AskRecord(asks=tuple(out), more=_nonneg(obj["more"], "more"), iat=iat, exp=exp)


def encode_claim() -> bytes:
    return _dumps({"v": 1}).encode()


def decode_claim(data: bytes) -> None:
    _obj(data)


# --- people over NATS (RT-6.42) ------------------------------------------------------
# The central bridge publishes a person's post or reaction in a bound Mattermost channel
# on `<bus>.hu.<channel>.<anchor>`; the anchor (the subject's last token, "." as "~") is
# the person, UNBOUND an account bound to nobody. The login service writes one record per
# bound Mattermost account (`<presence>.account.mattermost.<user id>.v1`), the bridge its
# signal per channel (`<presence>.bridge.<channel>.v1`). Every decoder here is strict:
# format v1 with exactly its fields, no unknown or duplicate key; an empty message is no
# JSON and refused (a JetStream answer routed onto one of these subjects, ADR-016 step 2).

UNBOUND = "-"  # the sender token of an account bound to nobody: never an anchor (OWNER_RE)
PEOPLE_TEXT_MAX = 16000  # the bridge cuts a person's text there (characters)
SIGNAL_MAX_SEC = 180.0  # a bridge signal counts at most this long after it was published
ACCOUNT_MAX_SEC = 900.0  # an account record's approver flag at most this long after its write
_PEOPLE_KEYS = {
    "post": ({"v", "kind", "text", "name", "mm"}, {"mm_root", "root", "files", "cut"}),
    "reaction": ({"v", "kind", "emoji", "name", "mm"}, {"ref"}),
    "unreaction": ({"v", "kind", "emoji", "name", "mm"}, {"ref"}),
}
_ACCOUNT_KEYS = {"v", "account", "anchor", "name", "approver", "token_iat", "iat", "exp"}
_BRIDGE_KEYS = {"v", "home", "people", "iat", "exp"}


@dataclass(frozen=True)
class PersonMessage:
    """A person's post, reaction or unreaction as the bridge carried it (format v1). The
    person is not part of it: it is the subject's last token. `name` is for display only."""

    kind: str  # "post" | "reaction" | "unreaction"
    mm: str  # the Mattermost post id: the person's post, or the post reacted to
    name: str
    text: str = ""
    mm_root: str | None = None  # post: the Mattermost id of the thread's root
    root: int | None = None  # post: the root's stream sequence, when it is on NATS
    files: int = 0  # post: how many files it holds (they stay on Mattermost)
    cut: bool = False  # post: the bridge cut the text
    emoji: str | None = None
    ref: int | None = None  # reaction: the reacted post's stream sequence, when it is on NATS


def _exact(obj: dict[str, Any], required: set[str], optional: Iterable[str] = ()) -> None:
    keys = set(obj)
    if not required <= keys or not keys <= required | set(optional):
        raise MessageError("not exactly the fields of its kind")


def _mm_id(v: object, what: str) -> str:
    if not isinstance(v, str) or not BOT_RE.fullmatch(v):
        raise MessageError(f"{what} is not a Mattermost id")
    return v


def decode_person(data: bytes) -> PersonMessage:
    """Strict (RT-6.42 step 9): a `<bus>.hu` body of format v1 with exactly the fields of its
    kind, no `props`, well-formed ids; anything else raises MessageError."""
    obj = _obj(data)
    kind = obj.get("kind")
    if not isinstance(kind, str) or kind not in _PEOPLE_KEYS:
        raise MessageError("unknown kind")
    _exact(obj, *_PEOPLE_KEYS[kind])
    mm = _mm_id(obj["mm"], "mm")
    name = obj["name"]
    if not isinstance(name, str) or not USERNAME_RE.fullmatch(name):
        raise MessageError("name is not a username")
    if kind == "post":
        text = obj["text"]
        if not isinstance(text, str) or not _wellformed(text) or len(text) > PEOPLE_TEXT_MAX:
            raise MessageError("text is not a string of at most 16000 characters")
        mm_root = _mm_id(obj["mm_root"], "mm_root") if "mm_root" in obj else None
        root = obj.get("root")
        if root is not None and not _seq(root):
            raise MessageError("root is not a sequence")
        files = obj.get("files", 0)
        if isinstance(files, bool) or not isinstance(files, int) or files < 0:
            raise MessageError("files is not a count")
        cut = obj.get("cut", False)
        if not isinstance(cut, bool):
            raise MessageError("cut is not a boolean")
        return PersonMessage(kind, mm, name, text=text, mm_root=mm_root, root=root, files=files, cut=cut)
    emoji = obj["emoji"]
    if (not isinstance(emoji, str) or not emoji or len(emoji) > 64
            or emoji.splitlines() != [emoji] or not _wellformed(emoji)):
        raise MessageError("an emoji is one line of at most 64 characters")
    ref = obj.get("ref")
    if ref is not None and not _seq(ref):
        raise MessageError("ref is not a sequence")
    return PersonMessage(kind, mm, name, emoji=emoji, ref=ref)


@dataclass(frozen=True)
class AccountRecord:
    """The login service's record of a Mattermost account (RT-6.42 step 3): the person it is
    bound to (`anchor`, None once unbound), whether the Keycloak token behind it placed the
    person in the approvers' group, and until when that counts (`exp`, server time)."""

    account: str
    anchor: str | None
    name: str | None
    approver: bool
    exp: float


def decode_account(data: bytes, account: str) -> AccountRecord:
    """Strict: exactly the fields of a v1 record of `account`; anything else raises."""
    obj = _obj(data)
    _exact(obj, _ACCOUNT_KEYS)
    if obj["account"] != account:
        raise MessageError("record of another account")
    anchor, name = obj["anchor"], obj["name"]
    if anchor is not None and (not isinstance(anchor, str) or not OWNER_RE.fullmatch(anchor)):
        raise MessageError("anchor is not an anchor")
    if name is not None and (not isinstance(name, str) or not USERNAME_RE.fullmatch(name)):
        raise MessageError("name is not a username")
    if not isinstance(obj["approver"], bool):
        raise MessageError("approver is not a boolean")
    _int(obj, "token_iat")
    _int(obj, "iat")
    return AccountRecord(account, anchor, name, obj["approver"], float(_int(obj, "exp")))


def decode_bridge_signal(data: bytes, channel: str) -> float:
    """Strict: the bridge's v1 signal for `channel`, `people` true; returns its `exp`."""
    obj = _obj(data)
    _exact(obj, _BRIDGE_KEYS)
    if obj["home"] != channel:
        raise MessageError("signal of another channel")
    if obj["people"] is not True:
        raise MessageError("people is not true")
    _int(obj, "iat")
    return float(_int(obj, "exp"))


# --- JetStream metadata and gaps -----------------------------------------------------

@dataclass(frozen=True)
class Ack:
    stream_seq: int
    consumer_seq: int
    time: float  # the server's timestamp, seconds since the epoch
    pending: int = -1  # messages the consumer still has to deliver; -1: not told


def parse_ack(reply: str | None, stream: str | None = None, consumer: str | None = None) -> Ack | None:
    """`$JS.ACK.<stream>.<consumer>.<delivered>.<sseq>.<cseq>.<ts ns>.<pending>`, or with
    domain and account hash in front (and a token behind). None: not JetStream's, or,
    when `stream` and `consumer` are given, not the reply of that consumer."""
    if not reply:
        return None
    t = reply.split(".")
    if t[:2] != ["$JS", "ACK"] or len(t) not in (9, 11, 12):
        return None
    i = 4 if len(t) == 9 else 6
    if (stream is not None and t[i - 2] != stream) or (consumer is not None and t[i - 1] != consumer):
        return None
    try:
        sseq, cseq, ns, pending = int(t[i + 1]), int(t[i + 2]), int(t[i + 3]), int(t[i + 4])
    except (ValueError, IndexError):
        return None
    if sseq < 1 or cseq < 1 or pending < 0:
        return None
    return Ack(sseq, cseq, ns / 1e9, pending)


class GapTracker:
    """The last consumer sequence per deliver subject. A push consumer counts every
    delivery, so a jump of more than one is a message this connection lost."""

    def __init__(self) -> None:
        self._last: dict[str, int] = {}

    def observe(self, key: str, consumer_seq: int) -> bool:
        """Record consumer_seq; True when it is more than one past the last."""
        last = self._last.get(key)
        self._last[key] = consumer_seq
        return last is not None and consumer_seq > last + 1

    def reset(self) -> None:
        self._last.clear()


# --- the crown ---------------------------------------------------------------------------

def crown_candidates(claims: Mapping[str, int], presence: Mapping[str, tuple[float, str]],
                     now: float, is_member: Callable[[str], bool] | None = None,
                     fresh: float = PRESENCE_FRESH_SEC,
                     at: Mapping[str, float] | None = None) -> list[str]:
    """The agents that may hold the crown of one channel (RT-6.38), by claim order: a claim,
    a last presence message younger than `fresh` seconds and not `bye`, and, when
    `is_member` is given, a membership of the channel. The order is (server time of the
    claim, stream sequence), lexicographically: the sequence alone is no order between
    agents when the stream was made anew (`at` None: by sequence only).
    claims: agent -> stream sequence of its (latest) claim; at: agent -> server time of it;
    presence: agent -> (server time, status) of its last presence message."""
    out: list[tuple[float, int, str]] = []
    for agent, seq in claims.items():
        seen = presence.get(agent)
        if seen is None or seen[1] in ("bye", "offline") or now - seen[0] >= fresh:
            continue
        if is_member is not None and not is_member(agent):
            continue
        out.append((at.get(agent, 0.0) if at is not None else 0.0, seq, agent))
    return [agent for _, _, agent in sorted(out)]


def crown_holder(claims: Mapping[str, int], presence: Mapping[str, tuple[float, str]],
                 now: float, fresh: float = PRESENCE_FRESH_SEC,
                 at: Mapping[str, float] | None = None) -> str | None:
    """The crown of one channel: the first claim in crown_candidates' order among the
    agents whose last presence message is younger than `fresh` seconds and not `bye`.
    claims: agent -> stream sequence of its (latest) claim; presence: agent ->
    (server time, status) of its last presence message."""
    first = crown_candidates(claims, presence, now, None, fresh, at)
    return first[0] if first else None


# --- CONNECT ---------------------------------------------------------------------------------

def build_hello(token: str, home: str, guests: tuple[str, ...], lobby: bool,
                since: Mapping[str, int], *, fresh_presence: bool = False,
                trusted: tuple[str, ...] = (), trusted_cut: bool = False) -> str:
    """The CONNECT `auth_token` the login service reads (callout.Hello). It holds the
    access token: never log it."""
    hello: dict[str, Any] = {"token": token}
    if home:
        hello["home"] = home
    if guests:
        hello["guests"] = list(guests)
    if lobby:
        hello["lobby"] = True
    if trusted:
        hello["trusted"] = list(trusted)
    if trusted_cut:
        hello["trusted_cut"] = True
    known = {ch: seq for ch, seq in since.items() if seq > 0}
    if fresh_presence:
        known[PRESENCE_FRESH_KEY] = 1  # the login service reads the presence stream afresh (RT-6.35)
    if known:
        hello["since"] = known
    return json.dumps(hello, separators=(",", ":"))


# --- the bus ---------------------------------------------------------------------------------------

# What OpenSSL says when the first bytes back are no TLS record: the server sent its
# INFO in clear, so it waits for the classic order (or speaks no TLS at all).
_NOT_TLS_FIRST = frozenset({"WRONG_VERSION_NUMBER", "RECORD_LAYER_FAILURE", "PACKET_LENGTH_TOO_LONG",
                            "UNKNOWN_PROTOCOL", "HTTP_REQUEST"})
# OpenSSL's verify codes: the chain ends at no CA this host trusts (unable to get the issuer,
# self-signed leaf or chain, unable to get the local issuer, unable to verify the leaf) ...
_UNKNOWN_ISSUER = frozenset({2, 18, 19, 20, 21})
# ... and a certificate for another host name or IP address.
_NAME_MISMATCH = frozenset({62, 64})


def connect_reason(exc: BaseException) -> str:
    """A failed connect in words for the log (RT-6.36): a TLS failure says why, anything else
    stays its type name (an exception's text may carry what the log must not). Only an unknown
    issuer gets the CA hint: a certificate for another name may be a man in the middle, whose CA
    nobody should be pointed to trust."""
    seen: set[int] = set()
    e: BaseException | None = exc
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        if isinstance(e, ssl.SSLCertVerificationError):
            # Both set by OpenSSL, absent on one raised by hand: this runs in the retry loops.
            code = getattr(e, "verify_code", None)
            msg = getattr(e, "verify_message", None) or "no reason given"
            if code in _NAME_MISMATCH:
                return f"TLS: the certificate does not name this host ({msg})"
            if code in _UNKNOWN_ISSUER:
                return (f"TLS: the server's certificate is not trusted ({msg}): "
                        "if that CA is your NATS server's, set MM_NATS_CA_FILE to it")
            return f"TLS: the server's certificate is refused ({msg})"
        if isinstance(e, ssl.SSLError):
            reason = getattr(e, "reason", None)  # set by OpenSSL, absent on one raised by hand
            if reason in _NOT_TLS_FIRST:
                return ("TLS: the server does not speak TLS first: set handshake_first in its tls block, "
                        "or this is not a NATS TLS port")
            return f"TLS: {reason or type(e).__name__}"
        e = e.__cause__ or e.__context__
    return type(exc).__name__


_SECURE_SCHEMES = frozenset({"tls", "wss"})   # TLS is verified; wss is TLS inside the WebSocket
_PLAIN_SCHEMES = frozenset({"nats", "ws"})    # no TLS: only to this machine
_WS_SCHEMES = frozenset({"ws", "wss"})
# A URL starts with its scheme in lower case and `://`; anything else is refused rather than
# guessed. nats-py matches `ws://`/`wss://` case-sensitively on the raw string and treats
# `WSS://host` as a host name, which would dial plain TCP: never hand it a spelling it guesses.
_LOWER_SCHEMES = frozenset({"tls", "wss", "nats", "ws"})
_SCHEME_PREFIX = re.compile(r"^(tls|wss|nats|ws)://")


def _normalise_scheme(url: str) -> tuple[str, bool]:
    """The URL with its scheme in lower case, and whether it changed. Raises ValueError for a
    spelling no check knows."""
    raw = url.strip()
    m = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*)://", raw)
    if not m or m[1].lower() not in _LOWER_SCHEMES:
        raise ValueError(f"{url!r}: a NATS server URL starts with tls://, wss://, nats:// or ws://")
    scheme = m[1].lower()
    return scheme + "://" + raw[len(m[0]):], scheme != m[1]


def _check_server(url: str) -> None:
    """tls:// and wss:// anywhere; nats:// and ws:// (no TLS) only to this machine. The scheme
    must be lower case: nats-py reads the raw string and would dial `WSS://host` as plain."""
    if not _SCHEME_PREFIX.match(url):
        raise ValueError(f"{url!r}: the scheme must be lower case: tls://, wss://, nats:// or ws://")
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme in _SECURE_SCHEMES and host:
        return
    if parts.scheme in _PLAIN_SCHEMES and host:
        if host == "localhost":
            return
        try:
            if ipaddress.ip_address(host).is_loopback:
                return
        except ValueError:
            pass
    raise ValueError("a NATS server URL is tls:// or wss://, or nats:// or ws:// to a loopback host only")


def _schemes(servers: tuple[str, ...]) -> set[str]:
    schemes = {urlsplit(u).scheme for u in servers}
    if len(schemes) != 1:
        # One pool, one scheme: nats-py picks the transport and the TLS setup for the pool,
        # and a mix of a secure and a plain server would let the pool downgrade.
        raise ValueError("NATS server URLs are all of one scheme (tls://, wss://, nats://, ws://), not mixed")
    return schemes


def _websocket(servers: tuple[str, ...]) -> bool:
    """True when the servers are ws:// or wss:// (nats-py's WebSocket transport, aiohttp)."""
    return _schemes(servers) <= _WS_SCHEMES


def _websocket_transport() -> Any:
    """nats-py's WebSocket transport with redirects refused: aiohttp follows a 3xx and would
    send the CONNECT — and the access token inside it — to the redirect's target (Fable's
    review of #51). nats-py builds a transport only when it has none, so setting this one
    before connect keeps it for every server of the pool."""
    import aiohttp
    from nats.aio.transport import WebSocketTransport

    class NoRedirect(WebSocketTransport):
        def __init__(self, ws_headers: Any = None) -> None:
            # WebSocketTransport.__init__ without its session; the one below refuses redirects.
            self._ws = None
            self._pending = asyncio.Queue()
            self._close_task = asyncio.get_running_loop().create_future()
            self._using_tls = None
            self._ws_headers = ws_headers
            trace = aiohttp.TraceConfig()

            async def refuse(session: Any, context: Any, params: Any) -> None:
                raise NatsBusError("the WebSocket server redirected the connection: refused, so "
                                   "the access token never reaches another address")

            trace.on_request_redirect.append(refuse)
            self._client = aiohttp.ClientSession(trace_configs=[trace])

    return NoRedirect()


def _tls_mode(servers: tuple[str, ...]) -> bool:
    """True when the servers are tls:// or wss://. The TLS setup is one for the pool."""
    return _schemes(servers) <= _SECURE_SCHEMES


@dataclass(frozen=True)
class NatsSettings:
    servers: tuple[str, ...]
    home: str
    guests: tuple[str, ...] = ()
    lobby: bool = False
    lobby_channel: str = "lobby"  # the login service's AUTH_LOBBY: its name, to subscribe literally
    bus: str = "bus"
    presence: str = "presence"
    stats: str = "stats"  # the login service's AUTH_STATS_PREFIX: STATS lines on <stats>.<name> (RT-6.43)
    name_prefix: str = "agent-"
    props_prefix: str = a2a.DEFAULT_PREFIX  # MM_PROPS_PREFIX, one setting of the whole bus (RT-6.16)
    tls_ca_file: str | None = None  # CA bundle that verifies a tls:// server (default: the system's)
    trusted: tuple[str, ...] = ()  # this session's trusted humans' names, for the hello (normalised)
    trusted_cut: bool = False  # normalising dropped or truncated names (set by __post_init__)

    def __post_init__(self) -> None:
        # One spelling per scheme: a URL's scheme is lowercased here, and _check_server refuses
        # an upper-case one on any path (nats-py would read `WSS://host` as a host name and dial
        # plain TCP, the token in clear).
        servers = []
        for url in self.servers:
            norm, _ = _normalise_scheme(url)
            servers.append(norm)
        object.__setattr__(self, "servers", tuple(servers))
        names, cut = normalise_trusted(self.trusted)
        object.__setattr__(self, "trusted", names)
        object.__setattr__(self, "trusted_cut", cut or self.trusted_cut)
        # Store what the bus's own check returns, not what was passed: an empty
        # value means "unset" there and comes back as the default, and keeping
        # the empty string would make `k.startswith("")` accept every key,
        # `from_trusted_human` included. Raises ValueError for a bad one.
        object.__setattr__(self, "props_prefix",
                           a2a.user_prefix({a2a.PREFIX_ENV: self.props_prefix}))
        for url in self.servers:
            _check_server(url)  # the access token goes over this connection
        _tls_mode(self.servers)

    def tls_context(self) -> ssl.SSLContext | None:
        """For tls:// and wss:// servers: a context that verifies the certificate and the host name."""
        if not _tls_mode(self.servers):
            return None
        return ssl.create_default_context(cafile=self.tls_ca_file)


@dataclass(frozen=True)
class Event:
    where: str  # a channel name, or "@dm" for a direct message (no channel name can look like it)
    sender: str  # the subject's last token
    stream_seq: int
    time: float  # the server's timestamp, seconds since the epoch
    message: Message


@dataclass(frozen=True)
class PersonEvent:
    """A person's event from `<bus>.hu.<where>.<anchor>` (RT-6.42)."""

    where: str  # the channel name
    anchor: str  # the subject's last token as a person: an anchor, or UNBOUND
    stream_seq: int
    time: float  # the server's timestamp, seconds since the epoch
    message: PersonMessage


class LoginLike(Protocol):
    async def access_token(self) -> str: ...
    def nkey(self) -> NKey: ...


EventCallback = Callable[[Event], Awaitable[None] | None]
PersonCallback = Callable[[PersonEvent], Awaitable[None] | None]
FatalCallback = Callable[[NatsBusError], Awaitable[None] | None]
DM_SINCE = "@dm"  # the key of `since` for direct messages (no channel name can look like it)


class NatsBus:
    def __init__(self, settings: NatsSettings, login: LoginLike, owner: str, on_event: EventCallback,
                 *, heartbeat_sec: float = HEARTBEAT_SEC, gap_reconnect_sec: float = GAP_RECONNECT_SEC,
                 connect_timeout: float = 5.0, renew_sec: float = RENEW_SEC,
                 send_wait_sec: float = SEND_WAIT_SEC, member_renew_sec: float = MEMBER_RENEW_SEC,
                 on_fatal: FatalCallback | None = None, on_person: PersonCallback | None = None) -> None:
        for what, v in (("bus", settings.bus), ("presence", settings.presence), ("stats", settings.stats)):
            if not PREFIX_RE.fullmatch(v) or v == DELIVER:
                raise ValueError(f"invalid {what} prefix")
        if len({settings.bus, settings.presence, settings.stats}) != 3:
            raise ValueError("the bus, presence and stats prefixes must differ")
        for url in settings.servers:
            _check_server(url)
        _schemes(settings.servers)  # one scheme per pool, also for settings built around the check
        self._s = settings
        self._login = login
        self._owner = owner
        self._on_event = on_event
        self._on_fatal = on_fatal
        self._on_person = on_person
        self.on_person_gap: Callable[[str], None] | None = None  # a gap on a person's consumer (RT-6.42)
        self._heartbeat_sec = heartbeat_sec
        self._gap_sec = gap_reconnect_sec
        self._connect_timeout = connect_timeout
        self._renew_sec = renew_sec
        self._send_wait = send_wait_sec
        self._member_renew_sec = member_renew_sec
        self._name: str | None = None
        self._nc: NatsClient | None = None
        self._connected = False
        self._stopping = False
        self._fatal: NatsBusError | None = None
        self._wake = asyncio.Event()
        self._ready = asyncio.Event()  # cleared during a planned reconnect: sends wait for it
        self._ready.set()
        self._planned = False
        self._tasks: list[asyncio.Task[None]] = []
        self._renew_task: asyncio.Task[None] | None = None
        self._gap_task: asyncio.Task[None] | None = None
        self._send_lock = asyncio.Lock()
        self._denied: list[str] = []  # subjects the server refused a publish on
        self._gaps = GapTracker()
        self._last_gap_reconnect = -1e18
        self._last_echo_reconnect = -1e18
        self._echo_backoff = 0.0  # the wait after an echo reconnect that brought no echo back; 0: none yet
        self._stale: set[str] = set()  # channels with a gap, dropped until the reconnect
        self._last_seq: dict[str, int] = {}  # channel (or "@dm") -> last stream sequence delivered
        self._server_now = 0.0  # the newest server timestamp seen: the host's "now"
        self._server_now_mono = 0.0  # the monotonic clock when it was seen (for _iat)
        self._last_prune = 0.0
        self._presence: dict[str, tuple[float, str]] = {}
        self._owners: dict[str, str] = {}  # agent -> owner its presence subject names (RT-6.37)
        self._presence_seq: dict[str, tuple[int, float]] = {}  # agent -> (stream sequence, server time)
        self._bots: dict[str, str] = {}  # agent -> Mattermost bot its presence names (RT-6.37)
        # agent -> (stream sequence, server time, record): the newest attestation of the login service
        self._attest: dict[str, tuple[int, float, Attestation]] = {}
        # A valid attestation was seen in this process: the login service writes them, and
        # from then on no NATS sender is judged by name again until a restart (ADR-016).
        self._records_on = False
        self._bot = ""
        self._bot_name = ""  # the bot's username, stated in the presence for people (ADR-015)
        # the HELLO card's data for the presence (RT-6.37, ADR-015); None sends none
        self.card_source: Callable[[], PresenceCard | None] | None = None
        # The open asks for the ask record (RT-6.43 step 5): the MCP server sets it (a hosted
        # one maybe after start); `human` is the asked human's username, mapped here through
        # the own attestation. None publishes no record.
        self.ask_source: Callable[[], Iterable[OpenAsk]] | None = None
        self._ask_now = asyncio.Event()
        self._ask_sent: tuple[OpenAsk, ...] | None = None  # the asks of the last record published
        self._ask_sent_at = 0.0  # monotonic
        self._ask_failed: tuple[tuple[OpenAsk, ...], float] | None = None  # (asks, retry after)
        self._loop: asyncio.AbstractEventLoop | None = None  # the bus's loop, for asks_changed
        self._synced = False
        self._presence_read = False  # True once the stored presence stream was read (a new session says so in its hello)
        self._memberships: list[str] = []  # channels this bus announces its membership of
        self._member_task: asyncio.Task[None] | None = None
        self._member_now = asyncio.Event()  # set at each (re)connect: announce again
        self._members: dict[str, dict[str, tuple[int, float]]] = {}  # channel -> agent -> (stream sequence, server time)
        self._claims: dict[str, dict[str, int]] = {}
        self._claim_at: dict[tuple[str, str], float] = {}  # (channel, agent) -> server time of the claim
        self._presence_cseq = 0  # the presence consumer's last delivery number: a lower one is a consumer made anew
        self._own_echo = time.monotonic()  # when this agent's own presence last came back (monotonic, never server time)
        # RT-6.42: account -> (stream sequence, server time, record); None: a message there that
        # does not decode (a stray answer erased it): unbound and no approver, fail closed.
        self._accounts: dict[str, tuple[int, float, AccountRecord | None]] = {}
        # channel -> (stream sequence, server time, exp) of the bridge's signal; exp 0: none
        self._bridge_signal: dict[str, tuple[int, float, float]] = {}
        self._last_anchor: dict[str, str] = {}  # account -> the anchor of its last record that decoded

    def __repr__(self) -> str:
        return f"NatsBus(name={self._name!r}, connected={self._connected})"

    @property
    def name(self) -> str:
        if self._name is None:
            self._name = agent_name(self._owner, self._login.nkey().public_key, self._s.name_prefix)
        return self._name

    @property
    def connected(self) -> bool:
        """Whether a connection is open now (False while a reconnect is under way)."""
        return self._connected and self._nc is not None

    @property
    def fatal(self) -> NatsBusError | None:
        """The error that stopped the bus for good (a NameMismatch), else None."""
        return self._fatal

    @property
    def channels(self) -> list[str]:
        out: list[str] = []
        wanted = [self._s.home, *self._s.guests]
        if self._s.lobby:
            wanted.append(self._s.lobby_channel)
        for ch in wanted:
            if ch and ch not in out:
                out.append(ch)
        return out

    def since(self) -> dict[str, int]:
        out = {ch: self._last_seq[ch] for ch in self.channels if self._last_seq.get(ch)}
        if self._last_seq.get(DM_SINCE):
            out[DM_SINCE] = self._last_seq[DM_SINCE]
        return out

    # --- lifecycle ---------------------------------------------------------------
    async def start(self) -> None:
        for ch in self.channels:
            if not CHANNEL_RE.fullmatch(ch):
                raise ValueError("invalid channel name")
        _ = self.name
        retry = False
        try:
            await self._open()
        except (NameMismatch, ValueError):
            raise  # final: another credential, or settings that are wrong
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A first connect that fails is no end: the supervisor connects again with backoff
            # (a send raises "not connected" until it has).
            log.warning("nats bus: first connect failed, retrying: %s", connect_reason(exc))
            retry = True
        self._loop = asyncio.get_running_loop()
        self._tasks = [asyncio.create_task(self._supervise()), asyncio.create_task(self._heartbeat()),
                       asyncio.create_task(self._announce_asks())]
        if self._memberships:
            self._start_member_task()
        if retry:
            self._wake.set()

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        self._ready.set()
        self._cancel_timers()
        if self._member_task is not None:
            self._member_task.cancel()
            self._tasks.append(self._member_task)
            self._member_task = None
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        nc = self._nc
        if nc is not None and self._connected:
            if self.ask_source is not None:
                try:  # at the bye an empty list once: the console drops the asks at once (RT-6.43 step 5)
                    await nc.publish(self._ask_subject, encode_ask_record((), self._iat()))
                except Exception as exc:  # noqa: BLE001 — the record runs out by its exp anyway
                    log.info("nats bus: stop: the empty ask record: %s", type(exc).__name__)
            try:
                await nc.publish(self._presence_subject, self._presence_body("bye"))
                await asyncio.wait_for(nc.drain(), 5)
            except Exception as exc:  # the bus is going away anyway
                log.warning("nats bus: stop: %s", type(exc).__name__)
        if nc is not None:
            try:
                await nc.close()
            except Exception:
                pass
        self._connected = False
        self._nc = None

    def _cancel_timers(self) -> None:
        for t in (self._renew_task, self._gap_task):
            if t is not None and t is not asyncio.current_task():
                t.cancel()
        self._renew_task = self._gap_task = None

    # --- connection ----------------------------------------------------------------
    async def _open(self) -> None:
        """One connection with a fresh access token, `since` and the literal subscriptions."""
        self._synced = False  # the stored presences are read again from this connection
        self._own_echo = time.monotonic()  # a new connection gets the window to see its own presence
        token = await self._login.access_token()
        key = self._login.nkey()
        hello = build_hello(token, self._s.home, self._s.guests, self._s.lobby, self.since(),
                            fresh_presence=not self._presence_read, trusted=self._s.trusted, trusted_cut=self._s.trusted_cut)
        del token
        nc = NatsClient()
        # nats-py has no argument for the public key with a signature callback; this is the
        # attribute its own seed path sets. The seed itself stays in login.
        nc._public_nkey = key.public_key

        def sign(nonce: str) -> bytes:
            return base64.b64encode(key.sign(nonce.encode()))

        async def lost() -> None:
            if self._nc is nc and not self._stopping:
                self._connected = False
                self._wake.set()

        refused: list[str] = []

        async def error(exc: Exception) -> None:
            text = str(exc)
            if "permissions violation" in text.lower():
                refused.append("permissions violation")
                m = _PUBLISH_DENIED_RE.search(text)
                if m:
                    self._denied = [*self._denied[-63:], m.group(1)]
            log.warning("nats bus: %s", connect_reason(exc))

        # tls://: the TLS handshake comes first, before any NATS protocol, with the certificate
        # verified. nats-py otherwise upgrades only when the server's INFO says tls_required,
        # which a man in the middle simply leaves out (the CONNECT with the token would go in clear).
        # wss:// is TLS inside the WebSocket handshake: the same verified context, and no
        # handshake-first (the WebSocket handshake is the first thing on the wire anyway).
        if _websocket(self._s.servers):
            # No redirects: the CONNECT carries the access token. (nats-py turns a server the
            # NATS server announces — connect_urls — into nats:// unless the current one is
            # tls://, and never resets the transport: with reconnect enabled a wss pool would
            # ws_connect("nats://…") and fail. No downgrade, just no failover into a cluster.)
            nc._transport = _websocket_transport()
        tls = self._s.tls_context()
        tls_kw: dict[str, Any] = {}
        if tls is not None:
            tls_kw["tls"] = tls
            if not _websocket(self._s.servers):
                tls_kw["tls_handshake_first"] = True
        try:
            # nats-py bounds the TCP connect and the INFO, not a TLS handshake that never ends
            # (a plaintext peer behind tls:// leaves the client waiting for a TLS record).
            await asyncio.wait_for(
                nc.connect(list(self._s.servers), token=hello, signature_cb=sign, allow_reconnect=False,
                           connect_timeout=int(self._connect_timeout), error_cb=error,
                           disconnected_cb=lost, closed_cb=lost, **tls_kw),
                self._connect_timeout * 2)
        except BaseException:  # also a cancellation: no half-open client is left behind
            try:  # bounded: a TLS peer that never answers the shutdown must not hold this up
                await asyncio.wait_for(asyncio.shield(nc.close()), 2)
            except Exception:
                pass
            raise
        del hello
        self._nc = nc
        self._gaps.reset()
        self._stale.clear()
        self._cancel_gap_timer()  # this connection starts from `since`: the held-back gap is covered
        name = self.name
        try:
            for ch in self.channels:
                await nc.subscribe(f"{DELIVER}.{name}.ch.{ch}", cb=self._handler(ch))
            for ch in self.channels:
                # People's posts (RT-6.42): `<name>_hu_<channel>`; nothing comes before the
                # login service makes the consumer, and the subscription is the agent's own.
                await nc.subscribe(f"{DELIVER}.{name}.hu.{ch}", cb=self._person_handler(ch))
            await nc.subscribe(f"{DELIVER}.{name}.dm", cb=self._handler(DM_SINCE))
            await nc.subscribe(f"{DELIVER}.{name}.presence", cb=self._on_presence_msg)
            # The server answers a refused SUB with -ERR before the PONG of a flush: a
            # refusal of the own deliver subjects means the credential carries another name
            # than the computed one (the credential is the authority). Nothing has been
            # published yet.
            await self._sync(nc)
            if refused:
                raise NameMismatch("the credential's agent name is not the computed one; nothing sent")
            await nc.publish(self._presence_subject, self._presence_body("online"))
            await self._sync(nc)
            if self._presence_subject in self._denied:
                # A host and a login service of different releases (RT-6.37): the presence
                # subject changed, so this agent looks offline and foreign, and its bye
                # removes nothing. Said once, in words, not only as an exception's type.
                log.warning("nats bus: the login service refused this agent's presence subject "
                            "(<presence>.<owner>.<name>): update the login service and the hosts "
                            "together; until then this agent looks offline and foreign to the others")
        except BaseException:
            await nc.close()
            self._nc = None
            raise
        self._connected = True
        self._member_now.set()  # announce the memberships again on this connection
        if nc.is_closed:  # lost while subscribing
            self._connected = False
            self._wake.set()
        else:
            self._schedule_renew()

    @staticmethod
    async def _sync(nc: NatsClient) -> None:
        """Two round trips, so that every -ERR the server sent for what came before has
        been handled when this returns. One flush is not enough: its PING is queued behind
        the client's own buffered writes, and the PONG that resolves it may answer a PING
        that was already on the wire before them (nats-py matches PONGs to flushes in
        order), so an -ERR for the writes may still be on its way. The second PING is
        sent only after the first PONG, and the server answers in order."""
        await nc.flush()
        await nc.flush()

    def _schedule_renew(self) -> None:
        if self._renew_task is not None:
            self._renew_task.cancel()
        self._renew_task = asyncio.create_task(self._renew_after())

    async def _renew_after(self) -> None:
        await asyncio.sleep(self._renew_sec)
        if not self._stopping:
            log.info("nats bus: renewing the connection before its credential expires")
            self._planned = True
            self._wake.set()

    def _cancel_gap_timer(self) -> None:
        if self._gap_task is not None and self._gap_task is not asyncio.current_task():
            self._gap_task.cancel()
        self._gap_task = None

    async def _supervise(self) -> None:
        while not self._stopping:
            await self._wake.wait()
            self._wake.clear()
            if self._stopping:
                return
            planned = self._planned
            if planned:
                self._planned = False
                self._ready.clear()  # a send now waits for the new connection
            if planned:
                # A send under way is not cut off: the old connection closes when it is done.
                async with self._send_lock:
                    await self._close_old()
            else:
                await self._close_old()
            delay = 1.0
            try:
                while not self._stopping:
                    try:
                        await self._open()
                        log.info("nats bus: reconnected")
                        break
                    except asyncio.CancelledError:
                        raise
                    except NameMismatch as exc:
                        log.error("nats bus: the credential's agent name is not the computed one; stopped")
                        self._fatal = exc
                        self._stopping = True
                        self._cancel_timers()
                        await self._notify_fatal(exc)
                        return
                    except Exception as exc:
                        log.warning("nats bus: reconnect failed: %s", connect_reason(exc))
                        await asyncio.sleep(delay * random.uniform(0.5, 1.0))
                        delay = min(delay * 2, 30.0)
            finally:
                self._ready.set()

    async def _close_old(self) -> None:
        old, self._nc, self._connected = self._nc, None, False
        if old is not None:
            try:
                # Bounded: this runs under _send_lock, so a close that never
                # returns would block every post that follows.
                await asyncio.wait_for(old.close(), timeout=CLOSE_SEC)
            except (asyncio.TimeoutError, Exception):
                pass

    async def _notify_fatal(self, exc: NatsBusError) -> None:
        if self._on_fatal is None:
            return
        try:
            res = self._on_fatal(exc)
            if inspect.isawaitable(res):
                await res
        except Exception as err:
            log.warning("nats bus: on_fatal callback failed: %s", type(err).__name__)

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_sec)
            nc = self._nc
            if nc is not None and self._connected:
                try:
                    await nc.publish(self._presence_subject, self._presence_body("online"))
                except Exception as exc:
                    log.warning("nats bus: heartbeat: %s", type(exc).__name__)
            self._check_own_echo()

    def _check_own_echo(self) -> None:
        """The echo of the own presence missing past OWN_ECHO_SEC while connected (a stream
        made anew, the consumer gone): a reconnect makes the consumer again (RT-6.38 step 2).
        None when the server refused the presence subject (host and login service of
        different releases: a reconnect cures nothing). A reconnect that brings no echo back
        doubles the wait before the next, from OWN_ECHO_SEC up to RENEW_SEC; a real echo
        resets it. So an outage a reconnect does not cure is no reconnect storm."""
        if self._stopping or not self._connected or self.own_echo_age() <= OWN_ECHO_SEC:
            return
        if self._presence_subject in self._denied:
            return
        now = time.monotonic()
        if now - self._last_echo_reconnect < max(self._gap_sec, self._echo_backoff):
            return
        log.warning("nats bus: no echo of the own presence for %.0f s, reconnecting", self.own_echo_age())
        self._echo_backoff = min(max(self._echo_backoff * 2, OWN_ECHO_SEC), RENEW_SEC)
        self._last_echo_reconnect = now
        self._wake.set()

    # --- sending ---------------------------------------------------------------------
    async def _publish(self, subject: str, body: bytes) -> None:
        """Publish and wait until the server has handled it: a publish the server refuses
        (no right on the subject) is an error here, not a silent success."""
        if self._fatal is not None:
            raise self._fatal
        waited = False
        while True:
            if (self._nc is None or not self._connected) and not self._ready.is_set():
                try:  # a planned reconnect is under way
                    await asyncio.wait_for(self._ready.wait(), self._send_wait)
                except TimeoutError:
                    pass
                waited = True
                if self._fatal is not None:
                    raise self._fatal
            async with self._send_lock:
                nc = self._nc  # read under the lock: a planned renewal swaps it holding the lock
                if nc is None or not self._connected:
                    if not waited and not self._ready.is_set():
                        continue  # the renewal took the lock first: wait for it, outside the lock
                    raise NatsBusError("not connected")
                await self._publish_locked(nc, subject, body)
                return

    async def _publish_locked(self, nc: NatsClient, subject: str, body: bytes) -> None:
        self._denied = [d for d in self._denied if d != subject]
        try:
            await nc.publish(subject, body)  # never a Nats-Msg-Id header
            await self._sync(nc)
        except Exception as exc:
            raise NatsBusError(f"publish failed: {type(exc).__name__}") from None
        if subject in self._denied:
            self._denied = [d for d in self._denied if d != subject]
            raise NatsBusError("publish refused by the server: no permission for it")

    @staticmethod
    def _channel(ch: str) -> str:
        if not CHANNEL_RE.fullmatch(ch):
            raise ValueError("invalid channel name")
        return ch

    def _asked(self, ch: str) -> str:
        """A channel this bus asked the login service for (home, guests, the lobby)."""
        self._channel(ch)
        if ch not in self.channels:
            raise NatsBusError("not a channel this bus asked for; nothing sent")
        return ch

    async def post(self, channel: str, text: str, root: int | None = None,
                   props: dict[str, Prop] | None = None) -> None:
        body = encode(Message("post", text, root=root, props=props or {}), self._s.props_prefix)
        await self._publish(f"{self._s.bus}.ch.{self._asked(channel)}.{self.name}", body)

    async def react(self, channel: str, ref: int, emoji: str) -> None:
        body = encode(Message("reaction", ref=ref, emoji=emoji), self._s.props_prefix)
        await self._publish(f"{self._s.bus}.ch.{self._asked(channel)}.{self.name}", body)

    async def unreact(self, channel: str, ref: int, emoji: str) -> None:
        body = encode(Message("unreaction", ref=ref, emoji=emoji), self._s.props_prefix)
        await self._publish(f"{self._s.bus}.ch.{self._asked(channel)}.{self.name}", body)

    def _dm_subject(self, recipient: str) -> str:
        if not AGENT_RE.fullmatch(recipient):
            raise ValueError("invalid recipient name")
        return f"{self._s.bus}.dm.{recipient}.{self.name}"

    async def dm(self, recipient: str, text: str, root: int | None = None,
                 props: dict[str, Prop] | None = None) -> None:
        subject = self._dm_subject(recipient)
        await self._publish(subject, encode(Message("post", text, root=root, props=props or {}),
                                            self._s.props_prefix))

    async def dm_react(self, recipient: str, ref: int, emoji: str, remove: bool = False) -> None:
        subject = self._dm_subject(recipient)
        await self._publish(subject, encode(Message("unreaction" if remove else "reaction",
                                                    ref=ref, emoji=emoji), self._s.props_prefix))

    async def claim(self, channel: str) -> None:
        await self._publish(f"{self._s.presence}.crown.{self._asked(channel)}.{self.name}", encode_claim())

    async def member(self, channel: str) -> None:
        """Announce this agent's membership of a channel this bus asked for, on
        `<presence>.member.<channel>.<name>` (RT-6.37): once the bus is synced, again every
        MEMBER_RENEW_SEC and after every (re)connect. The crown claim is another subject."""
        ch = self._asked(channel)
        if ch not in self._memberships:
            self._memberships.append(ch)
        self._member_now.set()
        if self._tasks:
            self._start_member_task()

    def _start_member_task(self) -> None:
        if self._member_task is None or self._member_task.done():
            self._member_task = asyncio.create_task(self._announce_members())

    async def _announce_members(self) -> None:
        while not self._stopping:
            try:
                await asyncio.wait_for(self._member_now.wait(), self._member_renew_sec)
            except asyncio.TimeoutError:
                pass
            self._member_now.clear()
            if self._stopping or not self.connected:
                continue  # the next (re)connect announces
            for _ in range(int(MEMBER_SYNC_WAIT_SEC / 0.05)):
                if self._synced or not self.connected:
                    break
                await asyncio.sleep(0.05)
            else:
                log.warning("nats bus: the presence stream was not read within %.0f s: announcing memberships anyway",
                            MEMBER_SYNC_WAIT_SEC)
            await self._announce_once()

    async def _announce_once(self) -> None:
        """Announce each membership on its own: a channel the login service did not grant
        (a guest the human is not in) is refused, and the others go out all the same."""
        for ch in list(self._memberships):
            try:
                await self._publish(f"{self._s.presence}.member.{ch}.{self.name}", encode_claim())
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — the next renewal tries again
                log.warning("nats bus: announcing the membership of ~%s failed: %s", ch, type(exc).__name__)

    @property
    def _presence_subject(self) -> str:
        """`<presence>.<owner>.<name>` (RT-6.37): the login service lets this agent publish
        its presence there and nowhere else, so the owner in it is pinned like the name."""
        return f"{self._s.presence}.{owner_token(self._owner)}.{self.name}"

    # --- the ask record and the STATS lines (RT-6.43 steps 5 and 7) -------------------------

    @property
    def _ask_subject(self) -> str:
        """`<presence>.ask.<home>.<name>`: the one subject the login service grants this agent
        for its open asks, for its own name and its hello's home."""
        return f"{self._s.presence}.ask.{self._channel(self._s.home)}.{self.name}"

    def _iat(self) -> int:
        """A record's issue time: the server's clock as this host last saw it, carried forward
        by the monotonic clock (a quiet stream must not make a fresh record look old)."""
        if not self._server_now:
            return int(time.time())
        return int(self._server_now + max(0.0, time.monotonic() - self._server_now_mono))

    def human_token(self, name: str) -> str:
        """The asked human's anchor in subject form, as this agent's own attestation from the
        login service pairs it with that name (RT-6.41); NO_OWNER when the record names no
        such person (or two anchors for one name), or there is no fresh record."""
        att = self.attestation_of(self.name)
        if att is None or not name:
            return NO_OWNER
        people = [(att.owner_anchor, att.owner_name), *att.trusted]
        anchors = {anchor for anchor, named in people if named == name.lower()}
        return owner_token(anchors.pop()) if len(anchors) == 1 else NO_OWNER

    def asks_changed(self) -> None:
        """An ask was posted, answered or ended: the record goes out now, not at the next check.
        Safe from any thread: the event is set in the bus's own loop."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return  # not started: the first check after start publishes
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._ask_now.set()
        else:
            loop.call_soon_threadsafe(self._ask_now.set)

    async def _announce_asks(self) -> None:
        """Publish the ask record at each change and every ASK_RENEW_SEC while an ask is open;
        checked every heartbeat, at once after asks_changed."""
        while not self._stopping:
            try:
                await asyncio.wait_for(self._ask_now.wait(), self._heartbeat_sec)
            except asyncio.TimeoutError:
                pass
            self._ask_now.clear()
            if self._stopping or not self.connected or self.ask_source is None:
                continue
            try:
                await self.publish_asks()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — the next check tries again
                log.info("nats bus: the ask record was not published: %s", type(exc).__name__)

    async def publish_asks(self) -> bool:
        """The ask record, when the asks changed since the last one (the first check after start
        counts as a change) or it is due for renewal; True when one went out. A refused publish
        is tried again after a minute or a change."""
        source = self.ask_source
        if source is None:
            return False
        asks = tuple(dataclasses.replace(a, human=self.human_token(a.human)) for a in source())
        now = time.monotonic()
        due = bool(asks) and now - self._ask_sent_at >= ASK_RENEW_SEC
        # The first check publishes even an empty list: it replaces a record a predecessor of
        # this agent left (a crash), which would otherwise stand until its exp.
        if asks == self._ask_sent and not due:
            return False
        if self._ask_failed is not None and self._ask_failed[0] == asks and now < self._ask_failed[1]:
            return False
        try:
            await self._publish(self._ask_subject, encode_ask_record(asks, self._iat()))
        except NatsBusError:
            self._ask_failed = (asks, now + 60.0)
            raise
        self._ask_failed = None
        self._ask_sent, self._ask_sent_at = asks, now
        return True

    async def publish_stats(self, body: bytes) -> None:
        """One STATS part on `<stats>.<name>` (RT-6.43 step 7), the body stats.nats_body made;
        raises NatsBusError when it was not sent."""
        await self._publish(f"{self._s.stats}.{self.name}", body)

    def _presence_body(self, status: str) -> bytes:
        """This agent's presence body, with the card's data card_source states now (RT-6.37):
        a change shows with the next heartbeat. A failing source sends no card, never no
        presence."""
        card = None
        if self.card_source is not None:
            try:
                card = self.card_source()
            except Exception as exc:  # noqa: BLE001 — a label, never a reason to look offline
                log.info("nats bus: the card's data was not read (%s)", type(exc).__name__)
        return encode_presence(status, self._bot, self._bot_name, card)

    def link_bot(self, bot_id: str, username: str = "") -> None:
        """The Mattermost bot this agent's presence names (RT-6.37), and its username for
        people (one that is no valid username is left out); before start."""
        if not BOT_RE.fullmatch(bot_id):
            raise ValueError("not a Mattermost user id")
        self._bot = bot_id
        self._bot_name = username if USERNAME_RE.fullmatch(username) else ""

    def bot_of(self, agent: str) -> str:
        """The bot the agent's presence names, '' for none."""
        return self._bots.get(agent, "")

    @property
    def synced(self) -> bool:
        """True once a presence delivery came with nothing pending: the stored presences
        and claims are read; False again at each (re)connect."""
        return self._synced

    def claimed(self, channel: str, agent: str) -> bool:
        """Whether the agent holds a crown claim in the channel (not its membership)."""
        return agent in self._claims.get(channel, {})

    def is_member(self, channel: str, agent: str) -> bool:
        """Whether the agent announced its membership of the channel within the last 24 h."""
        return agent in self._members.get(channel, {})

    def owner_of(self, agent: str) -> str:
        """The owner the agent's presence subject names (RT-6.37), '' for none. The login
        service pins it there by the credential's one presence permission, as it pins the
        sender: no agent can publish a presence under another owner."""
        anchor = self._owners.get(agent, "")
        att = self.attestation_of(agent)
        if att is not None and anchor and att.owner_anchor == anchor:
            return att.owner_name  # the anchor is the same one: the name it has now
        return anchor

    def anchor_of(self, agent: str) -> str:
        """The owner the agent's presence subject names, as the login service pinned it (the
        anchor), '' for none: what owner_of returns before any attestation."""
        return self._owners.get(agent, "")

    def records_on(self) -> bool:
        """A valid attestation (any agent's) was seen in this process: the login service
        writes them. It never turns off again before a restart, so an expired or missing
        record never brings back the comparison by name (fail closed, ADR-016)."""
        return self._records_on

    def attestation_of(self, agent: str) -> Attestation | None:
        """The login service's newest attestation of the agent, None when there is none or it
        has expired by the server's clock (never this host's)."""
        seen = self._attest.get(agent)
        if seen is None or seen[2].exp <= self._server_now:
            return None
        return seen[2]

    def presence_of(self, agent: str) -> str:
        """online or away while the agent's last presence is younger than
        PRESENCE_FRESH_SEC by the server's clock (never this host's) and no bye;
        else offline."""
        seen = self._presence.get(agent)
        if seen is None or seen[1] == "bye" or self._server_now - seen[0] >= PRESENCE_FRESH_SEC:
            return "offline"
        return seen[1]

    def presence_snapshot(self) -> dict[str, tuple[float, str]]:
        """agent -> (server time, status) of the last presence message of each."""
        return dict(self._presence)

    @property
    def server_now(self) -> float:
        """The newest server timestamp this host has seen: its "now" for every age."""
        return self._server_now

    def claim_info(self, channel: str) -> dict[str, tuple[int, float]]:
        """agent -> (stream sequence, server time) of its crown claim in the channel."""
        return {a: (seq, self._claim_at.get((channel, a), 0.0))
                for a, seq in self._claims.get(channel, {}).items()}

    def own_echo_age(self) -> float:
        """Seconds (monotonic clock) since this agent's own presence last came back over its
        presence consumer, or since the connection opened. A half-open connection delivers
        nothing: nats-py notices only after minutes (RT-6.38 step 2)."""
        return time.monotonic() - self._own_echo

    def claim_times(self, channel: str) -> dict[str, float]:
        return {a: self._claim_at.get((channel, a), 0.0) for a in self._claims.get(channel, {})}

    def candidates(self, channel: str, now: float | None = None) -> list[str]:
        """The agents that may hold the channel's crown, in claim order: fresh presence and
        a membership of the channel (RT-6.38); the standing-peer rule is the port's."""
        return crown_candidates(self._claims.get(channel, {}), self._presence,
                                self._server_now if now is None else now,
                                lambda a: self.is_member(channel, a), at=self.claim_times(channel))

    def holder(self, channel: str, now: float | None = None) -> str | None:
        """The crown of a channel. "Now" is the newest server timestamp this host has seen,
        never its own clock (which may be wrong)."""
        return crown_holder(self._claims.get(channel, {}), self._presence,
                            self._server_now if now is None else now, at=self.claim_times(channel))

    # --- people over NATS (RT-6.42) ------------------------------------------------------
    def people_switched(self, channel: str) -> bool:
        """A fresh signal of the bridge names the channel (by the server's clock): its people
        come over NATS. False otherwise: they are read on Mattermost, as without a bridge."""
        if not self.connected or self.own_echo_age() > OWN_ECHO_SEC:
            # The server's clock stands still while nothing arrives: a signal is not
            # fresh then, whatever its exp says (people on Mattermost, fail open).
            return False
        got = self._bridge_signal.get(channel)
        return got is not None and got[2] > self._server_now

    def anchor_of_account(self, account: str) -> str:
        """The person a Mattermost account is bound to by the login service's record, as the
        sender token the bridge publishes it under: the anchor, UNBOUND for none (no record,
        one that did not decode, or `anchor` null). A stale record still names the anchor."""
        got = self._accounts.get(account)
        if got is not None and got[2] is None:
            # Erased by a message that does not decode: the anchor last read, for the
            # keys only (the bridge may still hold it); it binds no trust (approver).
            return self._last_anchor.get(account, UNBOUND)
        rec = got[2] if got is not None else None
        return rec.anchor if rec is not None and rec.anchor else UNBOUND

    def approver(self, anchor: str) -> bool:
        """A fresh record (exp after the server's now) binds an account to `anchor` and says
        `approver` (RT-6.42 step 9, ADR-016 step 2 point 8); never for UNBOUND."""
        if anchor == UNBOUND:
            return False
        return any(rec is not None and rec.anchor == anchor and rec.approver and rec.exp > self._server_now
                   for _, _, rec in self._accounts.values())

    # --- receiving ----------------------------------------------------------------------
    def _handler(self, where: str) -> Callable[[Any], Awaitable[None]]:
        async def handle(msg: Any) -> None:
            await self._on_deliver(where, msg.subject, msg.reply, msg.data)
        return handle

    def _see(self, server_time: float) -> None:
        """Advance the host's server clock, and drop what the stream's max_age drops."""
        if server_time <= self._server_now:
            return
        self._server_now = server_time
        self._server_now_mono = time.monotonic()
        if server_time - self._last_prune < 60.0:
            return
        self._last_prune = server_time
        cut = server_time - MAX_AGE_SEC
        for agent, (t, _) in list(self._presence.items()):
            if t < cut:
                del self._presence[agent]
                self._owners.pop(agent, None)
                self._bots.pop(agent, None)
                self._presence_seq.pop(agent, None)
        for agent, (_, t, _a) in list(self._attest.items()):
            if t < cut:
                del self._attest[agent]
        for account, (_, t, _r) in list(self._accounts.items()):
            if t < cut:
                del self._accounts[account]
                self._last_anchor.pop(account, None)
        for ch, members in list(self._members.items()):
            for agent in list(members):
                if members[agent][1] < cut:
                    del members[agent]
            if not members:
                del self._members[ch]
        for ch, claims in list(self._claims.items()):
            for agent in list(claims):
                if self._claim_at.get((ch, agent), 0.0) < cut:
                    del claims[agent]
                    self._claim_at.pop((ch, agent), None)
            if not claims:
                del self._claims[ch]

    async def _on_deliver(self, where: str, subject: str, reply: str | None, data: bytes) -> None:
        consumer = f"{self.name}_dm" if where == DM_SINCE else f"{self.name}_ch_{where}"
        ack = parse_ack(reply, BUS_STREAM, consumer)
        if ack is None:
            log.warning("nats bus: dropped a message on %s without this consumer's JetStream metadata", where)
            return
        self._see(ack.time)
        gap = self._gaps.observe(where, ack.consumer_seq)  # every delivery counts, dropped or not
        tokens = subject.split(".")
        sender = tokens[-1]
        expect = [self._s.bus, "ch", where] if where != DM_SINCE else [self._s.bus, "dm", self.name]
        if tokens[:-1] != expect or not AGENT_RE.fullmatch(sender):
            log.warning("nats bus: dropped a message on %s with an unexpected subject", where)
            return
        if gap:
            self._gap(where)
        if where in self._stale:
            return
        if ack.stream_seq <= self._last_seq.get(where, 0):
            return  # a repeat after a restart from `since`
        self._last_seq[where] = ack.stream_seq
        try:
            message = decode(data, self._s.props_prefix)
        except MessageError as exc:
            log.warning("nats bus: dropped a malformed message on %s (seq %d): %s", where, ack.stream_seq, exc)
            return
        event = Event(where, sender, ack.stream_seq, ack.time, message)
        try:
            res = self._on_event(event)
            if inspect.isawaitable(res):
                await res
        except Exception as exc:
            log.warning("nats bus: event callback failed: %s", type(exc).__name__)

    def _person_handler(self, channel: str) -> Callable[[Any], Awaitable[None]]:
        async def handle(msg: Any) -> None:
            await self._on_person_deliver(channel, msg.subject, msg.reply, msg.data)
        return handle

    async def _on_person_deliver(self, channel: str, subject: str, reply: str | None, data: bytes) -> None:
        """A person's event (RT-6.42) from `<name>_hu_<channel>`: the subject is
        `<bus>.hu.<channel>.<anchor>`, the person its last token, never the body's."""
        key = f"hu.{channel}"  # its gaps and repeats, apart from the channel's agents
        ack = parse_ack(reply, BUS_STREAM, f"{self.name}_hu_{channel}")
        if ack is None:
            log.warning("nats bus: dropped a person's message on %s without this consumer's JetStream metadata",
                        channel)
            return
        self._see(ack.time)
        gap = self._gaps.observe(key, ack.consumer_seq)
        tokens = subject.split(".")
        if len(tokens) != 4 or tokens[:3] != [self._s.bus, "hu", channel]:
            log.warning("nats bus: dropped a person's message on %s with an unexpected subject", channel)
            return
        anchor = tokens[3] if tokens[3] == UNBOUND else owner_of_token(tokens[3])
        if not anchor:
            log.warning("nats bus: dropped a person's message on %s: the sender is no anchor", channel)
            return
        if gap:
            # The consumer starts with new messages and keeps its position (no `since`):
            # a reconnect brings nothing back, Mattermost has what was lost (RT-6.42 step 9).
            log.warning("nats bus: gap on the people of %s: read on Mattermost", channel)
            if self.on_person_gap is not None:
                self.on_person_gap(channel)
        if ack.stream_seq <= self._last_seq.get(key, 0):
            return  # a repeat
        self._last_seq[key] = ack.stream_seq
        try:
            message = decode_person(data)
        except MessageError as exc:
            log.warning("nats bus: dropped a malformed person's message on %s (seq %d): %s",
                        channel, ack.stream_seq, exc)
            return
        if self._on_person is None:
            return
        try:
            res = self._on_person(PersonEvent(channel, anchor, ack.stream_seq, ack.time, message))
            if inspect.isawaitable(res):
                await res
        except Exception as exc:
            log.warning("nats bus: person callback failed: %s", type(exc).__name__)

    def _gap(self, where: str) -> None:
        """A gap on `where`: it is dropped (stale) until a reconnect restarts it from
        `since`. At most one reconnect per gap interval: within it, the reconnect is
        scheduled for when the interval is over, so the gap is never lost for good."""
        self._stale.add(where)
        now = time.monotonic()
        wait = self._gap_sec - (now - self._last_gap_reconnect)
        if wait <= 0:
            log.warning("nats bus: gap on %s, reconnecting with since", where)
            self._last_gap_reconnect = now
            self._wake.set()
            return
        log.warning("nats bus: gap on %s, reconnect held back %.0f s", where, wait)
        if self._gap_task is None or self._gap_task.done():
            self._gap_task = asyncio.create_task(self._gap_reconnect_after(wait))

    async def _gap_reconnect_after(self, wait: float) -> None:
        await asyncio.sleep(wait)
        if not self._stopping:
            self._last_gap_reconnect = time.monotonic()
            self._wake.set()

    async def _on_presence_msg(self, msg: Any) -> None:
        ack = parse_ack(msg.reply, PRESENCE_STREAM, f"{self.name}_presence")
        if ack is None:
            log.warning("nats bus: dropped a presence message without this consumer's JetStream metadata")
            return
        self._see(ack.time)
        if ack.consumer_seq <= self._presence_cseq:
            # The consumer was made anew (the login service at a credential, a stream made
            # anew): what is delivered now is what the stream holds. Remembered claims may
            # not be in it: forget them, so that an own claim that is gone is claimed again.
            self._claims.clear()
            self._claim_at.clear()
            self._synced = False  # until a delivery with pending 0: the replay is not read yet
        self._presence_cseq = ack.consumer_seq
        if ack.pending == 0:
            self._synced = True
            self._presence_read = True
        t = msg.subject.split(".")
        p = self._s.presence
        try:
            if len(t) == 3 and t[0] == p and AGENT_RE.fullmatch(t[2]):
                # <presence>.<owner>.<name>: the owner as the login service pinned it.
                agent, status = t[2], decode_presence(msg.data)
                seq, at = self._presence_seq.get(agent, (0, 0.0))
                if (ack.time, ack.stream_seq) <= (at, seq):
                    # An older message never overrides a newer one: ordered by the server's
                    # time, then the sequence (a PRESENCE stream made anew starts at 1).
                    return
                self._presence_seq[agent] = (ack.stream_seq, ack.time)
                self._presence[agent] = (ack.time, status)
                if agent == self.name:
                    self._own_echo = time.monotonic()
                    self._echo_backoff = 0.0  # a real echo: the next outage starts afresh
                bot = decode_presence_bot(msg.data)
                if bot:
                    self._bots[agent] = bot
                else:
                    self._bots.pop(agent, None)
                owner = owner_of_token(t[1])
                if owner:
                    self._owners[agent] = owner
                else:
                    self._owners.pop(agent, None)
            elif len(t) == 4 and t[0] == p and t[1] == "crown" and CHANNEL_RE.fullmatch(t[2]) and AGENT_RE.fullmatch(t[3]):
                decode_claim(msg.data)
                claims = self._claims.setdefault(t[2], {})
                if (ack.time, ack.stream_seq) > (self._claim_at.get((t[2], t[3]), 0.0), claims.get(t[3], 0)):
                    claims[t[3]] = ack.stream_seq
                    self._claim_at[(t[2], t[3])] = ack.time
            elif len(t) == 4 and t[0] == p and t[1] == "member" and CHANNEL_RE.fullmatch(t[2]) and AGENT_RE.fullmatch(t[3]):
                decode_claim(msg.data)
                members = self._members.setdefault(t[2], {})
                seq0, at0 = members.get(t[3], (0, 0.0))
                if (ack.time, ack.stream_seq) > (at0, seq0):
                    members[t[3]] = (ack.stream_seq, ack.time)
            elif len(t) == 5 and t[0] == p and t[1:3] == ["account", "mattermost"] and t[4] == "v1" \
                    and BOT_RE.fullmatch(t[3]):
                # RT-6.42 step 3: the newest message counts; one that does not decode erases
                # the record (unbound, no approver) until the login service writes it again.
                seq0, at0, _rec = self._accounts.get(t[3], (0, 0.0, None))
                if (ack.time, ack.stream_seq) > (at0, seq0):
                    try:
                        rec: AccountRecord | None = decode_account(msg.data, t[3])
                        assert rec is not None
                        # Never longer than a credential lives (15 min) from its write.
                        rec = dataclasses.replace(rec, exp=min(rec.exp, ack.time + ACCOUNT_MAX_SEC))
                        if rec.anchor:
                            self._last_anchor[t[3]] = rec.anchor
                        else:
                            self._last_anchor.pop(t[3], None)
                    except MessageError:
                        rec = None
                        log.warning("nats bus: an account record that does not decode (seq %d): unbound",
                                    ack.stream_seq)
                    self._accounts[t[3]] = (ack.stream_seq, ack.time, rec)
            elif len(t) == 4 and t[0] == p and t[1] == "bridge" and t[3] == "v1" and CHANNEL_RE.fullmatch(t[2]):
                # RT-6.42 step 7: the bridge carries the channel's people while this is fresh.
                seq0, at0, _exp = self._bridge_signal.get(t[2], (0, 0.0, 0.0))
                if (ack.time, ack.stream_seq) > (at0, seq0):
                    try:
                        exp = min(decode_bridge_signal(msg.data, t[2]), ack.time + SIGNAL_MAX_SEC)
                    except MessageError:
                        exp = 0.0
                        log.warning("nats bus: a bridge signal that does not decode (seq %d): people on "
                                    "Mattermost", ack.stream_seq)
                    self._bridge_signal[t[2]] = (ack.stream_seq, ack.time, exp)
            elif len(t) == 4 and t[0] == p and t[1] == "attest" and t[3] == "v1" and AGENT_RE.fullmatch(t[2]):
                att = decode_attestation(msg.data, t[2])
                seq0, at0, _old = self._attest.get(t[2], (0, 0.0, att))
                if (ack.time, ack.stream_seq) > (at0, seq0):
                    self._attest[t[2]] = (ack.stream_seq, ack.time, att)
                self._records_on = True
            else:
                log.warning("nats bus: dropped a presence message with an unexpected subject")
        except MessageError as exc:
            log.warning("nats bus: dropped a malformed presence message (seq %d): %s", ack.stream_seq, exc)
