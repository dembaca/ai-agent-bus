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
import contextlib
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
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from typing import AbstractSet, Any, Protocol

from nats.aio.client import Client as NatsClient
from nats.errors import AuthorizationError, InvalidUserCredentialsError

from . import a2a, planb
from .login import LoginError, NKey
from .model import StatusMark
from .session_file import NONCE_RE

log = logging.getLogger(__name__)

# How long closing a superseded connection may take: it runs under the send
# lock, and a close that never answers must not block the next post.
CLOSE_SEC = 5.0

MAX_BODY = 65536  # the BUS stream's max_msg_size
HEARTBEAT_SEC = 30.0
PRESENCE_SOON_SEC = 2.0  # plan B piece 9: the least time after an early presence (presence_soon)
PRESENCE_FRESH_SEC = 90.0  # a holder's last presence must be younger than this
OWN_ECHO_SEC = 75.0  # the echo of this agent's own presence must be younger than this (monotonic), else the view is unknown
GAP_RECONNECT_SEC = 30.0  # at most one reconnect for a gap within this time
# The wait between reconnect attempts doubles from a second, but a server that comes
# back must be noticed soon: with a cap of 30 s a restarted nats-server was unreachable
# for up to 20 s beyond its downtime (#222). 5 s keeps a long outage to ~16 attempts a
# minute, less than nats-py's own 2 s default (30).
RECONNECT_MAX_SEC = 5.0
# A login failure is not the server being away: the login service may need longer (a
# human, a restarted service), so that path keeps the slower growth.
LOGIN_RECONNECT_MAX_SEC = 30.0
# A flush that does not hear its PONG within this fails the send instead of holding the
# send lock for nats-py's 10 s default: a post in flight under a fault blocked every
# later post for 8 s, which is the floor the rig measured (#222). Above the rig's 2 s
# fault window, so a PONG delayed by a short outage does not turn a delivered post into
# a failed one.
SYNC_SEC = 5
# The opening handshake keeps nats-py's own bound: a slow line there is not a fault yet.
OPEN_SYNC_SEC = 10
# At most one reconnect per flush timeout within this interval: a server that answers
# every agent slowly must not make every agent reconnect at once.
FLUSH_RECONNECT_SEC = 30.0
PLANB_STALE_SEC = 7200.0  # a plan B record older than two republish periods (callout.PlanBRepublish, 1 h) counts as off
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
# #274: an agent's name is its hash alone (no prefix by default), 25 characters: never 26,
# the length of a Mattermost id, so a NATS name and a Mattermost id never read alike
# (nats_port.is_nats_user). Go callout.NameHashLen and the bridge's presence.LinkTag.
NAME_HASH_LEN = 25
# The subject tokens that stand where an agent's name stands in some subject, or beside it;
# a name equal to one (in any case) is no agent's (#274). Go callout.reservedNames.
RESERVED_NAMES = frozenset((
    "account", "accounts", "ask", "attest", "bot", "bridge", "bus", "ch", "console-metrics",
    "copied", "crown", "deliver", "directory", "dm", "hu", "import", "mattermost", "member",
    "metrics", "moved", "notice", "person", "planb", "presence", "req", "stats", "v1", "x",
))
# The login service refuses a name that starts with its person prefix while the console is on
# (CB_AUTH_PERSON_PREFIX, default "person-"); the session does not know that setting, so it
# refuses the default one always (a hash never starts with it, "-" is no base32 character).
PERSON_NAME_PREFIX = "person-"


def agent_ok(name: str, reserved: AbstractSet[str] = frozenset()) -> bool:
    """Whether `name` is an agent's name as a subject token (#274): AGENT_RE's form (no ".",
    "*", ">", whitespace, no "_", "$" or "-" first) and none of RESERVED_NAMES or `reserved`
    (the configured prefixes, lower case), and none that starts with the default person prefix."""
    if not isinstance(name, str) or not AGENT_RE.fullmatch(name):
        return False
    low = name.lower()
    return low not in RESERVED_NAMES and low not in reserved and not low.startswith(PERSON_NAME_PREFIX)
SESSION_RE = NONCE_RE  # #163: the one pattern of the session part (session_file)
PREFIX_RE = re.compile(r"^[A-Za-z0-9_-]+$")
NAME_PREFIX_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")  # Go callout.Config.check, #274
# An owner counts only in this form (RT-6.37): a Mattermost username's characters in lower
# case, the same string the bus compares with CB_TRUSTED_HUMANS and the owners of Mattermost
# bots. Nothing is normalised: an owner in any other form is no owner.
OWNER_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
NO_OWNER = "-"  # the owner token of an owner not in OWNER_RE's form
USERNAME_RE = re.compile(r"[a-z][a-z0-9._-]{0,63}")  # a human's name as the login service reads it
NOTICE_KINDS = ("session", "host_key")  # RT-6.34: what the login service watched
NOTICE_VERDICTS = ("moved", "copied")  # a report, or a copy (the DM)
NOTICE_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,200}")  # a Keycloak session id or a host key
NETWORK_RE = re.compile(r"[A-Za-z0-9._:/-]{1,64}")  # a prefix (203.0.113.0/24), never a host address
NOTICE_SEEN_MAX = 64  # notices told or dropped, remembered against a replay
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


_REFUSED_CREDENTIAL = re.compile(r"authorization violation|invalid user credentials", re.I)


def login_failure(exc: Exception) -> bool:
    """Whether a reconnect failure is the login's, not the server being away: the bus's
    own login errors, the authorization errors nats-py raises after a connect, and - a
    refused CONNECT during the handshake - its plain `Error` with the server's text
    (`_process_err` names the parent, the handshake path does not, nats-py 2.x)."""
    return (isinstance(exc, (LoginError, AuthorizationError, InvalidUserCredentialsError))
            or bool(_REFUSED_CREDENTIAL.search(str(exc))))


def reconnect_waits(jitter: Callable[[], float] | None = None, *,
                    cap: float = RECONNECT_MAX_SEC) -> Iterator[float]:
    """The wait before each reconnect attempt (RT-6.36 step 2): one second, doubling to
    `cap`, each stretched by 0.5 to 1.0 so a home's agents do not retry in step. The cap
    is what a server that comes back needs: with 30 s, a restarted nats-server stayed
    unreachable for up to 20 s beyond its downtime in the rig; with 5 s the sender was
    back within 1 to 5 s. A login failure keeps the slower growth (#222)."""
    jitter = jitter or (lambda: random.uniform(0.5, 1.0))
    delay = 1.0
    while True:
        yield delay * jitter()
        delay = min(delay * 2, cap)


class NatsBusError(Exception):
    """A bus operation failed. The text never holds a token or a seed."""


class RedirectRefused(NatsBusError):
    """The WebSocket server answered the upgrade with a redirect: refused, because the
    CONNECT carries the access token (Fable's review of #51). Its text is ours and fixed,
    so the log may name it (RT-6.36, #85)."""


class NameMismatch(NatsBusError):
    """The server refused the host's subscription to its own deliver subjects:
    the name the login service wrote into the credential is not the one the
    host computed. The host sends nothing under the computed name."""


class MessageError(ValueError):
    """A message body is not format v1. The text never holds content."""


# --- the agent's name --------------------------------------------------------

def agent_name(owner: str, host_nkey_public: str, prefix: str = "", session: str = "") -> str:
    """Same as Go `name()` (services/auth/internal/callout/service.go) plus the
    prefix (none by default, #274): SHA-256 over owner, a NUL byte, the host nkey and,
    when the host key is shared, a NUL byte and the session nonce, the first 16 bytes as
    lower-case base32 without padding, of which the first NAME_HASH_LEN (25) characters.
    The owner is used as the login service gets it (the token's `preferred_username`),
    not changed."""
    raw = owner.encode() + b"\x00" + host_nkey_public.encode()
    if session:
        raw += b"\x00" + session.encode()
    digest = hashlib.sha256(raw).digest()[:16]
    return prefix + base64.b32encode(digest).decode().lower().rstrip("=")[:NAME_HASH_LEN]


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
    # Plan B piece 7 (RT-6.42 step 13): a Mattermost post named by its id instead of a sequence,
    # a person's or the sender's own from before the switch. Sent only while the piece is on,
    # never beside `root` or `ref`; absent from the body otherwise.
    mm_root: str | None = None  # post: the Mattermost id of the post it replies to
    mm_ref: str | None = None  # reaction: the Mattermost id of the post it reacts to


_MM_POST = re.compile(r"[a-z0-9]{26}")  # a Mattermost post id


def _mm_post(v: object) -> bool:
    return isinstance(v, str) and bool(_MM_POST.fullmatch(v))


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
            raise MessageError("props keys start with the bus's props prefix (CB_PROPS_PREFIX)")
        if not _wellformed(k) or (isinstance(v, str) and not _wellformed(v)):
            raise MessageError("props are not valid Unicode")
    if m.mm_root is not None and (m.kind != "post" or m.root is not None or not _mm_post(m.mm_root)):
        raise MessageError("mm_root is a Mattermost post id, on a post without root")
    if m.mm_ref is not None and (m.kind == "post" or m.ref is not None or not _mm_post(m.mm_ref)):
        raise MessageError("mm_ref is a Mattermost post id, on a reaction without ref")
    if m.kind == "post":
        if m.ref is not None or m.emoji is not None:
            raise MessageError("a post has no ref or emoji")
    else:
        if not _seq(m.ref) and m.mm_ref is None:
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
    obj: dict[str, Any] = {"v": 1, "kind": m.kind, "text": m.text, "root": m.root, "ref": m.ref,
                           "emoji": m.emoji, "props": m.props}
    if m.mm_root is not None:  # plan B piece 7 only: without it the body is as before
        obj["mm_root"] = m.mm_root
    if m.mm_ref is not None:
        obj["mm_ref"] = m.mm_ref
    body = _dumps(obj, ensure_ascii=False).encode()
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
                ref=obj.get("ref"), emoji=obj.get("emoji"), props=props,
                mm_root=obj.get("mm_root"), mm_ref=obj.get("mm_ref"))
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
# The agent's last post as epoch seconds: an int from 2020 to 2100, else left out. The
# console also drops a time after the presence that states it (clock skew aside).
CARD_EPOCH_MIN = 1_577_836_800  # 2020-01-01T00:00:00Z
CARD_EPOCH_MAX = 4_102_444_800  # 2100-01-01T00:00:00Z
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
    still waiting (asks.waiting); last_post: when the agent last posted in its home, epoch
    seconds (ownpost.py's AT: a time only, never the post's text)."""

    harness: str | None = None
    model: str | None = None
    task: str | None = None
    branch: str | None = None
    asks: int | None = None
    last_post: int | None = None
    # The roster's numbers and version as the status text states them (RT-6.37 step 6,
    # plan B piece 2): `usage` the usage segments (`ctx 62% · 5h 40%`), `release` the
    # version segment (`v0.104.0 unchecked`), each read back by usage.from_status and
    # release.from_status, which drop what they do not know.
    usage: str | None = None
    release: str | None = None
    # The state word of the status text (`working`, `listening`, `idle`, `bus`), which the
    # coordinator routes by (RT-6.37 step 6); the key is `state`, since `status` is the
    # presence's own. None for an older server, which states none.
    state: str | None = None


CARD_USAGE_MAX = 80
CARD_RELEASE_MAX = 40


def card_text(v: object, limit: int) -> str | None:
    """A card line: control characters out, whitespace collapsed, at most `limit` characters;
    None for no string."""
    if not isinstance(v, str):
        return None
    return " ".join(_CARD_CONTROL.sub(" ", v).split())[:limit].rstrip()


def card_block(v: object, limit: int) -> str | None:
    """A card text of several lines (an ask's context, recommendation or decision): line
    breaks kept, control characters out, the spaces of each line collapsed, a run of blank
    lines made one, the ends trimmed, at most `limit` characters; None for no string."""
    if not isinstance(v, str):
        return None
    lines = (" ".join(_CARD_CONTROL.sub(" ", line).split())
             for line in v.replace("\r\n", "\n").replace("\r", "\n").split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return text[:limit].rstrip()


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


def card_epoch(v: object) -> int | None:
    """Epoch seconds as an int from CARD_EPOCH_MIN to CARD_EPOCH_MAX; None for anything else
    (a float, a bool, a string, a time out of that range: dropped, never repaired)."""
    if isinstance(v, bool) or not isinstance(v, int) or not CARD_EPOCH_MIN <= v <= CARD_EPOCH_MAX:
        return None
    return v


CARD_STATES = ("working", "listening", "idle", "bus")


def card_state(v: object) -> str | None:
    """One of CARD_STATES; None for anything else (a word a newer server knows, a non-string)."""
    return v if isinstance(v, str) and v in CARD_STATES else None


def clean_card(card: PresenceCard) -> PresenceCard:
    """The card with every field bounded (the encoder's and the decoder's rule)."""
    return PresenceCard(harness=card_harness(card.harness), model=card_text(card.model, CARD_MODEL_MAX),
                        task=card_text(card.task, CARD_TASK_MAX), branch=card_branch(card.branch),
                        asks=card_asks(card.asks), last_post=card_epoch(card.last_post),
                        usage=card_text(card.usage, CARD_USAGE_MAX) or None,
                        release=card_text(card.release, CARD_RELEASE_MAX) or None,
                        state=card_state(card.state))


_CARD_KEYS = ("harness", "model", "task", "branch", "asks", "last_post", "usage", "release", "state")


def encode_presence(status: str, bot: str = "", bot_name: str = "",
                    card: PresenceCard | None = None, dm: bool = False,
                    shown: Mapping[str, Any] | None = None) -> bytes:
    """The presence body; `bot` is the agent's Mattermost bot id (RT-6.37) and `name` that
    bot's username, the name people and coordinator("who") know the agent by (the operator
    console shows it, ADR-015); each left out when ''. `card`: the HELLO card's data
    (harness, model, task, branch, open asks) and the last post's time, bounded by clean_card;
    a field it does not state (None) is left out. `dm`: this session's home runs plan B's
    agent DMs over NATS (piece 3), so a DM addressed to the agent goes over NATS; left out when
    False, which every sender reads as "stay on Mattermost". `shown`: the status the bridge
    shows under the agent's bridge bot (plan B piece 9), bounded by clean_shown; left out when
    None or out of bounds, which the bridge reads as "not mine to show".
    """
    if status not in _STATUSES:
        raise MessageError("unknown status")
    body: dict[str, Any] = {"v": 1, "status": status}
    if bot:
        body["bot"] = bot
    if bot_name:
        body["name"] = bot_name
    if dm:
        body["dm"] = True
    if card is not None:
        clean = clean_card(card)
        for key in _CARD_KEYS:
            value = getattr(clean, key)
            if value is not None:
                body[key] = value
    if shown is not None:
        clean_status = clean_shown(shown)
        if clean_status is not None:
            body["shown"] = clean_status
    return _dumps(body).encode()


# Plan B piece 9: the emoji a `shown` status may carry, the states' own; never the crown's,
# which the bridge sets by the claims (RT-6.38).
SHOWN_EMOJIS = frozenset(a2a.status_emoji(m) for m in (StatusMark.WORKING, StatusMark.LISTENING,
                                                        StatusMark.IDLE, StatusMark.BUS))
SHOWN_TEXT_MAX = 100  # Mattermost's custom status text


def clean_shown(shown: Mapping[str, Any]) -> dict[str, Any] | None:
    """A `shown` status bounded as the bridge reads it (plan B piece 9): `home` the channel it
    is shown for (the session's home; the bridge shows it only there), `emoji` one of
    SHOWN_EMOJIS, `text` a card line of at most SHOWN_TEXT_MAX characters, `until` its end in
    epoch seconds, `crown` only when True (this session's mirror holds its home's crown).
    None when a field is out of bounds: dropped, never repaired."""
    home, emoji, until = shown.get("home"), shown.get("emoji"), card_epoch(shown.get("until"))
    text = card_text(shown.get("text"), SHOWN_TEXT_MAX)
    if (not isinstance(home, str) or not CHANNEL_RE.fullmatch(home) or emoji not in SHOWN_EMOJIS
            or not text or until is None):
        return None
    out: dict[str, Any] = {"home": home, "emoji": emoji, "text": text, "until": until}
    if shown.get("crown") is True:
        out["crown"] = True
    return out


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


def decode_presence_dm(data: bytes) -> bool:
    """Whether a presence body states that the agent's home runs agent DMs over NATS (plan B
    piece 3), True only for a literal `true`: an older MCP server states nothing and a
    malformed body states nothing, and both count as off (a DM to that agent stays on
    Mattermost). Self-stated by the agent's own server, and it decides only DMs to itself
    (ADR-014 phase 5, piece 3: a recipient that states it wrongly misroutes only DMs to it)."""
    try:
        return _obj(data).get("dm") is True
    except MessageError:
        return False



def link_tag(name: str) -> str:
    """The 16 characters of the hash part of an agent name that the roster status shows
    (RT-6.37); '' for a name shorter than the hash part (NAME_HASH_LEN characters)."""
    return name[-NAME_HASH_LEN:][:16] if len(name) >= NAME_HASH_LEN else ""


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
    # The hello's home as the login service granted it (RT-6.41, #255): a channel token, or ''
    # when the hello named none or the human is no member of it. None when the record states
    # none at all (an older login service): no word, which a reader must not read as ''.
    home: str | None = None


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
    home: str | None = None
    if "home" in obj:  # absent: an older login service states none, which is not "no home"
        home = obj["home"]
        if not isinstance(home, str) or (home and not CHANNEL_RE.fullmatch(home)):
            raise MessageError("home is no channel name")
    _int(obj, "iat")
    return Attestation(owner_anchor=owner[0], owner_name=owner[1],
                       trusted=tuple(_person(t, "a trusted entry") for t in trusted),
                       cut=cut, exp=float(_int(obj, "exp")), home=home)


def decode_notice(data: bytes) -> Notice:
    """Strict: anything that is not a well-formed v1 notice raises MessageError."""
    obj = _obj(data)
    if _int(obj, "v") != 1:
        raise MessageError("v is not 1")
    kind = obj.get("kind")
    if kind not in NOTICE_KINDS:
        raise MessageError("kind is not session or host_key")
    verdict = obj.get("verdict")
    if verdict not in NOTICE_VERDICTS:
        raise MessageError("verdict is not moved or copied")
    sid = obj.get("sid")
    if not isinstance(sid, str) or not NOTICE_ID_RE.fullmatch(sid):
        raise MessageError("sid is not a session id or host key")
    network = obj.get("network")
    if not isinstance(network, str) or not NETWORK_RE.fullmatch(network):
        raise MessageError("network is not a network prefix")
    agent = obj.get("agent", "")
    if not isinstance(agent, str) or (agent and not agent_ok(agent)):
        raise MessageError("agent is not an agent name")
    iat, exp = _int(obj, "iat"), _int(obj, "exp")
    if exp <= iat:
        raise MessageError("exp is not after iat")
    return Notice(kind=kind, verdict=verdict, sid=sid, network=network, agent=agent,
                  iat=float(iat), exp=float(exp))


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
# console. A title, and since the owner's decision of 2026-11-28 (ask 9s1x3af91fnp9bzbgwefpeer5c)
# optionally the ask's card; bounded so that no agent can stretch it. The console server
# hands the card only to the asked human and the people the asking agent trusts.
ASK_MAX = 20  # asks in one record; the rest is counted in `more`
ASK_TITLE_MAX = 200  # characters
ASK_RECORD_MAX = 4096  # bytes, the whole record: what a reader accepts (protocol.md)
# What the encoder makes at most: the PRESENCE stream's max_msg_size (4096) counts headers
# too; this bus sends none, the margin keeps a record valid if a client library adds one.
ASK_BODY_MAX = 3840
ASK_VALID_SEC = 1800  # `exp` - `iat`: a record not renewed runs out after 30 min
ASK_RENEW_SEC = 600.0  # renewed this often while an ask is open
ASK_KINDS = ("approval", "question")
# The ask's card, each part optional and sent only when the ask has it (protocol.md).
# Bounded so that one ask with a full card fits a record (about 3.5 KB in ASCII); a record
# that would not fit drops the newest cards first, then the newest asks (encode_ask_record).
ASK_CONTEXT_MAX = 900  # characters, line breaks kept
ASK_RECOMMENDATION_MAX = 900  # characters, line breaks kept
ASK_DECISION_MAX = 400  # characters, line breaks kept: what a yes approves
ASK_WHERE_MAX = 200  # characters, one line
ASK_ANSWER_MAX = 120  # characters of one answer, one line; an ask offers two to four
ASK_TEXTS = (("context", ASK_CONTEXT_MAX), ("recommendation", ASK_RECOMMENDATION_MAX),
             ("decision", ASK_DECISION_MAX))
# The asked human's anchor in subject form (owner_token's encoding: "." as "~"); "-": none.
HUMAN_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9~_-]{0,63}")
_ASK_RECORD_KEYS = frozenset({"v", "asks", "more", "iat", "exp"})
_ASK_KEYS = frozenset({"mm", "kind", "human", "title", "asked", "answered"})
# Optional, sent only when the ask has them (a reader before this change refuses a record
# with an unknown key, so an ask without a card keeps the old shape).
_ASK_CARD_KEYS = frozenset({"context", "recommendation", "decision", "where", "answers", "chosen"})


@dataclass(frozen=True)
class OpenAsk:
    """One open ask as the record carries it: `mm` the ask's Mattermost post id, `human` the
    asked human's anchor in subject form or NO_OWNER (in what the MCP server hands the bus:
    the asked human's username, which the bus maps through its attestation), `asked` server
    time in Unix seconds, `answered` a trusted human's first answer was seen. The card, ""
    or () for none: `context`, `recommendation` and `decision` (the wording a yes approves)
    as plain text with line breaks, `where` one line, `answers` the ask's own answers in the
    order it was posted with (two to four), `chosen` the one a trusted human's reaction
    picked ("" for a reply or none yet)."""

    mm: str
    kind: str
    human: str
    title: str
    asked: int
    answered: bool = False
    context: str = ""
    recommendation: str = ""
    decision: str = ""
    where: str = ""
    answers: tuple[str, ...] = ()
    chosen: str = ""


@dataclass(frozen=True)
class AskRecord:
    asks: tuple[OpenAsk, ...]
    more: int
    iat: int
    exp: int


def _ask_obj(a: OpenAsk) -> dict[str, Any]:
    obj: dict[str, Any] = {"mm": a.mm, "kind": a.kind, "human": a.human, "title": a.title,
                           "asked": a.asked, "answered": a.answered}
    for key, _limit in ASK_TEXTS:
        if getattr(a, key):
            obj[key] = getattr(a, key)
    if a.where:
        obj["where"] = a.where
    if a.answers:
        obj["answers"] = list(a.answers)
        if a.chosen:
            obj["chosen"] = a.chosen
    return obj


def _clean_answers(answers: Iterable[str], chosen: str) -> tuple[tuple[str, ...], str]:
    """The answers a record may carry: two to four non-empty one-line texts, each cut to
    ASK_ANSWER_MAX; otherwise none (and then no chosen). `chosen` must be one of them."""
    cut = tuple(card_text(a, ASK_ANSWER_MAX) or "" for a in answers)
    if not 2 <= len(cut) <= 4 or not all(cut):
        return (), ""
    pick = card_text(chosen, ASK_ANSWER_MAX) or ""
    return cut, pick if pick in cut else ""


def _shed_card(kept: list[OpenAsk]) -> bool:
    """One step of making a record fit: the newest ask's texts go, else the newest ask's
    answers; False when no ask carries a card any more."""
    for i in reversed(range(len(kept))):
        a = kept[i]
        if a.context or a.recommendation or a.decision or a.where:
            kept[i] = replace(a, context="", recommendation="", decision="", where="")
            return True
    for i in reversed(range(len(kept))):
        if kept[i].answers:
            kept[i] = replace(kept[i], answers=(), chosen="")
            return True
    return False


def encode_ask_record(asks: Iterable[OpenAsk], iat: int, more: int = 0) -> bytes:
    """The ask record (protocol.md): the open asks oldest first, at most ASK_MAX, each title
    cut to one line of ASK_TITLE_MAX characters, the whole within ASK_BODY_MAX bytes; the
    newest that do not fit are counted in `more`, as is an ask with a malformed id or a
    repeated one. A `human` off HUMAN_TOKEN_RE goes as NO_OWNER, an unknown kind as approval.
    The card is cut to its bounds (ASK_TEXTS, ASK_WHERE_MAX, _clean_answers); when the record
    would be too big, the newest asks' texts go first, then their answers, and only then the
    newest asks themselves: every ask keeps its title before any keeps its card."""
    seen: set[str] = set()
    clean: list[OpenAsk] = []
    for a in asks:
        if not isinstance(a.mm, str) or not BOT_RE.fullmatch(a.mm) or a.mm in seen:
            more += 1
            continue
        seen.add(a.mm)
        answers, chosen = _clean_answers(a.answers, a.chosen)
        clean.append(OpenAsk(
            mm=a.mm, kind=a.kind if a.kind in ASK_KINDS else "approval",
            human=a.human if a.human == NO_OWNER or HUMAN_TOKEN_RE.fullmatch(a.human or "") else NO_OWNER,
            title=card_text(a.title, ASK_TITLE_MAX) or "",
            asked=max(0, int(a.asked)), answered=bool(a.answered),
            context=card_block(a.context, ASK_CONTEXT_MAX) or "",
            recommendation=card_block(a.recommendation, ASK_RECOMMENDATION_MAX) or "",
            decision=card_block(a.decision, ASK_DECISION_MAX) or "",
            where=card_text(a.where, ASK_WHERE_MAX) or "", answers=answers, chosen=chosen))
    clean.sort(key=lambda a: a.asked)
    kept, more = clean[:ASK_MAX], more + max(0, len(clean) - ASK_MAX)
    iat = int(iat)
    while True:
        body = _dumps({"v": 1, "asks": [_ask_obj(a) for a in kept], "more": more,
                       "iat": iat, "exp": iat + ASK_VALID_SEC}).encode()
        if len(body) <= ASK_BODY_MAX or not kept:
            return body
        if not _shed_card(kept):
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
    repeated `mm`, a card part off its type or bound or with a control or direction character
    (the line break only in the texts), a `chosen` that is none of the two to four `answers`,
    or an `exp` not after `iat` or more than ASK_VALID_SEC after it raises
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
        if not isinstance(a, dict) or not _ASK_KEYS <= set(a) <= _ASK_KEYS | _ASK_CARD_KEYS:
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
        context, recommendation, decision = (_card_field(a, key, limit, lines=True)
                                             for key, limit in ASK_TEXTS)
        where = _card_field(a, "where", ASK_WHERE_MAX, lines=False)
        answers = a.get("answers", [])
        if "answers" in a and (not isinstance(answers, list) or not 2 <= len(answers) <= 4
                               or not all(_card_line(x, ASK_ANSWER_MAX) and x == card_text(x, ASK_ANSWER_MAX)
                                      for x in answers)):
            raise MessageError("ask answers")
        chosen = a.get("chosen", "")
        if "chosen" in a and (not isinstance(chosen, str) or chosen not in answers):
            raise MessageError("ask chosen")
        out.append(OpenAsk(mm=mm, kind=kind, human=human, title=title,
                           asked=_nonneg(a["asked"], "asked"), answered=answered,
                           context=context, recommendation=recommendation, decision=decision,
                           where=where, answers=tuple(answers), chosen=chosen))
    return AskRecord(asks=tuple(out), more=_nonneg(obj["more"], "more"), iat=iat, exp=exp)


def _card_line(v: object, limit: int) -> bool:
    return isinstance(v, str) and 0 < len(v) <= limit and not _CARD_CONTROL.search(v)


def _card_field(a: dict[str, Any], key: str, limit: int, *, lines: bool) -> str:
    """An optional card text of an ask: absent is "", present must be a non-empty string
    within `limit` characters with no control or direction character (`lines`: but the line
    break) and as the encoder's normalising leaves it; anything else drops the record."""
    if key not in a:
        return ""
    v = a[key]
    if not _card_line(v.replace("\n", " ") if lines and isinstance(v, str) else v, limit):
        raise MessageError(f"ask {key}")
    if v != (card_block if lines else card_text)(v, limit):
        raise MessageError(f"ask {key}")
    return str(v)


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
# The bridge's bot prefix (RT-6.36 step 4): set while it posts each agent's copies under the
# agent's own bot, `<bot_prefix><NATS agent name>`; a Mattermost username's first part.
BOT_PREFIX_RE = re.compile(r"[a-z][a-z0-9._-]{0,31}")


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


_CHANNEL_RECORD_KEYS = {"v", "agent", "channels", "iat", "exp"}
MAX_GUESTS = 16  # the login service's callout.MaxGuests (a test ties the two)
MAX_CHANNEL_RECORD = 1 + MAX_GUESTS + 1  # the home, the guests, the lobby: what the login service grants at most


def decode_channel_record(data: bytes, agent: str) -> dict[str, str]:
    """The channels of `decode_channel_record_full`."""
    return decode_channel_record_full(data, agent)[0]


def decode_channel_record_full(data: bytes, agent: str) -> tuple[dict[str, str], float]:
    """Strict (RT-6.39 step 6): the login service's v1 record of the channels `agent`'s
    credential grants, `<presence>.directory.<agent>.v1`; returns channel name -> the chat's
    own (Mattermost) id, "" for a channel the directory holds no id for, and the record's `exp`."""
    obj = _obj(data)
    _exact(obj, _CHANNEL_RECORD_KEYS)
    if obj["v"] != 1 or obj["agent"] != agent:
        raise MessageError("channel record of another agent or version")
    _int(obj, "iat")
    exp = float(_int(obj, "exp"))
    chans = obj["channels"]
    if not isinstance(chans, dict) or len(chans) > MAX_CHANNEL_RECORD:
        raise MessageError("channels is not a small object")
    out: dict[str, str] = {}
    for name, cid in chans.items():
        if not isinstance(name, str) or not CHANNEL_RE.fullmatch(name):
            raise MessageError("a channel name that is no token of the bus")
        if cid != "":
            cid = _mm_id(cid, "channel id")
        out[name] = cid
    return out, exp


_BOT_RECORD_KEYS = {"v", "agent", "bot", "username", "owner_account", "iat", "exp"}
BOT_RECORD_MAX_SEC = 180.0  # a bot record counts at most this long after it was published


@dataclass(frozen=True)
class BotRecord:
    """The bridge's record of the Mattermost bot it made for an agent
    (`<presence>.bot.<agent>.v1`, RT-6.44 step 3): the bot, its username and the Mattermost
    account that owns it; all three None once the bot is gone. `exp` is server time."""

    agent: str
    bot: str | None
    username: str | None
    owner_account: str | None
    exp: float


def decode_bot_record(data: bytes, agent: str) -> BotRecord:
    """Strict: exactly the keys of a v1 record of `agent`; `bot` and `owner_account` a
    Mattermost id, `username` a username that ends with the agent's name (the bridge's prefix
    plus the name; the prefix itself is compared where the record is used), or all three
    null; anything else raises."""
    obj = _obj(data)
    _exact(obj, _BOT_RECORD_KEYS)
    if obj["v"] != 1 or obj["agent"] != agent:
        raise MessageError("bot record of another agent or version")
    bot, user, owner = obj["bot"], obj["username"], obj["owner_account"]
    _int(obj, "iat")
    exp = float(_int(obj, "exp"))
    if bot is None and user is None and owner is None:
        return BotRecord(agent, None, None, None, exp)
    bot = _mm_id(bot, "bot")
    owner = _mm_id(owner, "owner_account")
    if not isinstance(user, str) or not USERNAME_RE.fullmatch(user) or not user.endswith(agent) \
            or not BOT_PREFIX_RE.fullmatch(user[:len(user) - len(agent)]):
        raise MessageError("username is not a prefix plus the agent")
    return BotRecord(agent, bot, user, owner, exp)


def decode_bridge_signal(data: bytes, channel: str) -> float:
    """Strict: the bridge's v1 signal for `channel`, `people` true; returns its `exp`."""
    return decode_bridge_signal_full(data, channel)[0]


def decode_bridge_signal_full(data: bytes, channel: str) -> tuple[float, str]:
    """Strict: the bridge's v1 signal for `channel`, `people` true; returns its `exp` and
    its optional `bot_prefix` ("" without one; one that is no username's start raises)."""
    exp, prefix, _ = decode_bridge_signal_pieces(data, channel)
    return exp, prefix


def decode_bridge_signal_pieces(data: bytes, channel: str) -> tuple[float, str, frozenset[int]]:
    """decode_bridge_signal_full and the signal's optional `pieces`: the plan B pieces whose
    bridge half runs for the channel (piece 6, asks, RT-6.11 step 7), each a plan B piece,
    each once; none without the key."""
    obj = _obj(data)
    _exact(obj, _BRIDGE_KEYS, ("bot_prefix", "pieces"))
    pieces = obj.get("pieces", [])
    if not isinstance(pieces, list) or len(pieces) > planb.MAX_PIECE or any(
            isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= planb.MAX_PIECE for n in pieces) \
            or len(set(pieces)) != len(pieces):
        raise MessageError("pieces is not a list of plan B pieces")
    if obj["home"] != channel:
        raise MessageError("signal of another channel")
    if obj["people"] is not True:
        raise MessageError("people is not true")
    _int(obj, "iat")
    prefix = obj.get("bot_prefix", "")
    if "bot_prefix" in obj and (not isinstance(prefix, str) or not BOT_PREFIX_RE.fullmatch(prefix)):
        raise MessageError("bot_prefix is no username's start")
    return float(_int(obj, "exp")), prefix, frozenset(pieces)


def decode_planb(data: bytes, channel: str) -> frozenset[int]:
    """Strict: the login service's v1 record of the plan B pieces switched on for `channel`."""
    obj = _obj(data)
    _exact(obj, {"v", "channel", "pieces"})
    if _int(obj, "v") != 1:
        raise MessageError("not a v1 record")
    if obj["channel"] != channel:
        raise MessageError("record of another channel")
    pieces = obj["pieces"]
    if not isinstance(pieces, list) or len(pieces) > planb.MAX_PIECE:
        raise MessageError("pieces is not a short list")
    for n in pieces:
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= planb.MAX_PIECE:
            raise MessageError("a piece is not a plan B piece")
    out = frozenset(pieces)
    if len(out) != len(pieces):
        raise MessageError("a piece twice")
    return out


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
                since: Mapping[str, int | tuple[int, int]], *, fresh_presence: bool = False,
                trusted: tuple[str, ...] = (), trusted_cut: bool = False,
                session: str = "") -> str:
    """The CONNECT `auth_token` the login service reads (callout.Hello). It holds the
    access token: never log it. A `since` value is a sequence, or the bus's
    (sequence, server time) pair (#213): the hello carries the sequence only."""
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
    if session:
        hello["session"] = session  # #163: the session's own part of the name, when the key is shared
    known = {}
    for ch, value in since.items():
        seq = value[0] if isinstance(value, tuple) else value
        if seq > 0:
            known[ch] = seq
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
        if isinstance(e, RedirectRefused):
            # Our own text, fixed above: the operator sees why, not a bare timeout (#85).
            return str(e)
        if isinstance(e, ssl.SSLCertVerificationError):
            # Both set by OpenSSL, absent on one raised by hand: this runs in the retry loops.
            code = getattr(e, "verify_code", None)
            msg = getattr(e, "verify_message", None) or "no reason given"
            if code in _NAME_MISMATCH:
                return f"TLS: the certificate does not name this host ({msg})"
            if code in _UNKNOWN_ISSUER:
                return (f"TLS: the server's certificate is not trusted ({msg}): "
                        "if that CA is your NATS server's, set CB_NATS_CA_FILE to it")
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
                raise RedirectRefused("the WebSocket server redirected the connection: refused, "
                                      "so the access token never reaches another address")

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
    req: str = "req"  # the login service's AUTH_REQ_PREFIX: requests on <req>.<kind>.<name> (ADR-014 phase 5)
    name_prefix: str = ""  # the login service's CB_AUTH_NAME_PREFIX: none by default (#274)
    props_prefix: str = a2a.DEFAULT_PREFIX  # CB_PROPS_PREFIX, one setting of the whole bus (RT-6.16)
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
        # The login service's rule (callout.Config.check): a prefix of one character plus the
        # 25-character hash is 26 of [a-z0-9], a Mattermost id's form (#274, RT-6.36 step 8).
        if self.name_prefix and (len(self.name_prefix) < 2 or not NAME_PREFIX_RE.fullmatch(self.name_prefix)):
            raise ValueError("invalid name prefix: two or more of letters, digits, - and _, "
                             "a letter or digit first")
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


@dataclass(frozen=True)
class Notice:
    """The login service's notice that one id was used from alternating networks
    (`<presence>.notice.<owner anchor>.<verdict>.v1`, RT-6.34): what was watched (`session` or
    `host_key`), its id, the network prefix and the agent's name ("" when the token
    named none), with its lifetime in server time. The subject is the proof - only the
    login service may publish there (RT-6.41) - so nothing in the body decides."""

    kind: str
    verdict: str
    sid: str
    network: str
    agent: str
    iat: float
    exp: float


EventCallback = Callable[[Event], Awaitable[None] | None]
PersonCallback = Callable[[PersonEvent], Awaitable[None] | None]
NoticeCallback = Callable[[Notice], Awaitable[None] | None]
FatalCallback = Callable[[NatsBusError], Awaitable[None] | None]
DM_SINCE = "@dm"  # the key of `since` for direct messages (no channel name can look like it)


def _drop_subscriptions(nc: Any) -> None:
    """Cancel the message tasks of a closed client's subscriptions (#221): a client nats-py
    closed without its own cleanup (the silent close of `_watch_read_loop`, or a flush that
    timed out) keeps one pending `_wait_for_msgs` task per subscription, and its `close()`
    returns at once for a closed client, so nothing else ends them."""
    subs = getattr(nc, "_subs", None)
    if not isinstance(subs, dict):
        return
    for sub in list(subs.values()):
        task = getattr(sub, "_wait_for_msgs_task", None)
        if isinstance(task, asyncio.Task) and not task.done():
            task.cancel()
    subs.clear()


class NatsBus:
    def __init__(self, settings: NatsSettings, login: LoginLike, owner: str, on_event: EventCallback,
                 *, heartbeat_sec: float = HEARTBEAT_SEC, gap_reconnect_sec: float = GAP_RECONNECT_SEC,
                 connect_timeout: float = 5.0, renew_sec: float = RENEW_SEC,
                 send_wait_sec: float = SEND_WAIT_SEC, member_renew_sec: float = MEMBER_RENEW_SEC,
                 on_fatal: FatalCallback | None = None, on_person: PersonCallback | None = None,
                 on_notice: NoticeCallback | None = None, session: str = "",
                 since: Mapping[str, tuple[int, int]] | None = None) -> None:
        """`since` (#213, RT-6.35 step 3) is the hand-over cursor the host derives from its
        read mark: channel -> (last stream sequence it HANDED to the agent, that post's
        server time in ms). It goes into the first CONNECT beside `@presence`, so the login
        service restarts that consumer one message later and a post still queued when the
        process was killed is delivered again rather than lost. The time is the stream-reset's
        second sign: a recreated BUS stream delivers newer messages, a start by time replays
        older ones. This class keeps no file; the read mark is the host's record."""
        for what, v in (("bus", settings.bus), ("presence", settings.presence),
                        ("stats", settings.stats), ("request", settings.req)):
            if not PREFIX_RE.fullmatch(v) or v == DELIVER:
                raise ValueError(f"invalid {what} prefix")
        if len({settings.bus, settings.presence, settings.stats, settings.req}) != 4:
            raise ValueError("the bus, presence, stats and request prefixes must differ")
        for url in settings.servers:
            _check_server(url)
        _schemes(settings.servers)  # one scheme per pool, also for settings built around the check
        self._s = settings
        self._login = login
        self._owner = owner
        self._on_event = on_event
        self._on_fatal = on_fatal
        self._on_person = on_person
        if session and not SESSION_RE.fullmatch(session):
            raise ValueError("the session part is not 26 lower-case base32 characters")
        self._session = session  # #163: the session's own part of the name, when the key is shared
        self._on_notice = on_notice
        # Notices already told (replay, redelivery): (anchor, kind, sid, iat) -> monotonic.
        self._notice_seen: dict[tuple[str, str, str, float], float] = {}
        self.on_person_gap: Callable[[str], None] | None = None  # a gap on a person's consumer (RT-6.42)
        # A person's direct message to this agent (plan B piece 10, RT-6.13): `where` is DM_SINCE.
        self.on_person_dm: PersonCallback | None = None
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
        self._last_flush_reconnect = -1e18  # storm control for flush-triggered reconnects
        self._flush_task: asyncio.Task[None] | None = None
        self._last_echo_reconnect = -1e18
        self._echo_backoff = 0.0  # the wait after an echo reconnect that brought no echo back; 0: none yet
        self._stale: set[str] = set()  # channels with a gap, dropped until the reconnect
        # channel (or "@dm") -> last stream sequence handed to the agent: the host's read mark
        # at the start (#213), and this process's own hand-overs since (a reconnect)
        self._last_seq: dict[str, int] = {}
        # channel (or `@dm`) -> the server time of the message whose sequence is in
        # `_last_seq`, in integer milliseconds (as the read mark and a post's create_at hold
        # it): the stream-reset's second sign (#213)
        self._last_time: dict[str, int] = {}
        for key, pair in (since or {}).items():
            # Only a channel name or the DMs can be a `since` key: anything else (a person's
            # `hu.<channel>`, `@presence`, junk) is never sent. The port's cursor names channels
            # only (it drops DMs, `_on_event`); `@dm` reaches this from a reconnect within the
            # process, where the bus has seen the DMs itself.
            if not (isinstance(pair, tuple) and len(pair) == 2):
                continue
            seq, at = pair
            if ((key == DM_SINCE or CHANNEL_RE.fullmatch(key))
                    and isinstance(seq, int) and 0 < seq < 1 << 63
                    and isinstance(at, int) and at > 0):
                self._last_seq[key] = seq
                self._last_time[key] = at
        self._server_now = 0.0  # the newest server timestamp seen: the host's "now"
        self._server_now_mono = 0.0  # the monotonic clock when it was seen (for _iat)
        self._last_prune = 0.0
        self._presence: dict[str, tuple[float, str]] = {}
        self._owners: dict[str, str] = {}  # agent -> owner its presence subject names (RT-6.37)
        self._presence_seq: dict[str, tuple[int, float]] = {}  # agent -> (stream sequence, server time)
        self._bots: dict[str, str] = {}  # agent -> Mattermost bot its presence names (RT-6.37)
        # agent -> (stream sequence, server time, record): the newest bot record of the bridge
        self._bot_records: dict[str, tuple[int, float, BotRecord]] = {}
        # agent -> the card its newest presence states (the roster's usage and version)
        self._cards: dict[str, PresenceCard] = {}
        # agent -> (stream sequence, server time, record): the newest attestation of the login service
        self._attest: dict[str, tuple[int, float, Attestation]] = {}
        # A valid attestation was seen in this process: the login service writes them, and
        # from then on no NATS sender is judged by name again until a restart (ADR-016).
        self._records_on = False
        self._bot = ""
        self._bot_name = ""  # the bot's username, stated in the presence for people (ADR-015)
        # the HELLO card's data for the presence (RT-6.37, ADR-015); None sends none
        self.card_source: Callable[[], PresenceCard | None] | None = None
        # Plan B piece 3 (#244): whether this session's home runs agent DMs over NATS, stated
        # in every presence the bus sends; the MCP server sets it. None or a failing source
        # states nothing, which every sender reads as "stay on Mattermost".
        self.dm_source: Callable[[], bool] | None = None
        # Plan B piece 9: the status the bridge shows for this agent, stated in every presence
        # (`shown`); None or a failing source states none, and the bridge shows nothing of it.
        self.shown_source: Callable[[], Mapping[str, Any] | None] | None = None
        self._presence_soon = asyncio.Event()  # presence_soon: the heartbeat sends now
        self._dm: dict[str, bool] = {}  # agent -> its newest presence states DMs over NATS
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
        # channel -> the bot prefix its newest signal that had one named (RT-6.36 step 4); kept
        # when a later signal has none or the signal runs out: the copies made stay in the channel
        self._bridge_prefix: dict[str, str] = {}
        self._bridge_pieces: dict[str, frozenset[int]] = {}  # channel -> plan B pieces its signal names
        # channel -> (stream sequence, server time, pieces) of the plan B switches (RT-6.39 step 5)
        self._planb: dict[str, tuple[int, float, frozenset[int]]] = {}
        # the map before the presence consumer was made anew: read while the replay runs (#244)
        self._planb_prev: dict[str, tuple[int, float, frozenset[int]]] = {}
        # RT-6.39 step 6: the channel record of this credential (stream sequence, server time,
        # channel -> Mattermost id); None until one was read.
        self._chan_record: tuple[int, float, dict[str, str]] | None = None
        self._chan_event = asyncio.Event()
        self._last_anchor: dict[str, str] = {}  # account -> the anchor of its last record that decoded

    def __repr__(self) -> str:
        return f"NatsBus(name={self._name!r}, connected={self._connected})"

    def agent_ok(self, name: str) -> bool:
        """Whether `name` is an agent's name on this bus (#274): agent_ok with the
        configured prefixes reserved too."""
        s = self._s
        return agent_ok(name, frozenset(x.lower() for x in (s.bus, s.presence, s.stats, s.req)))

    @property
    def name(self) -> str:
        if self._name is None:
            self._name = agent_name(self._owner, self._login.nkey().public_key, self._s.name_prefix,
                                    session=self._session)
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

    def since(self) -> dict[str, tuple[int, int]]:
        """The hand-over cursor: channel (and `@dm`) -> (last stream sequence handed to the
        agent, that message's server time in ms). The first CONNECT sends the sequences so the
        login service restarts each consumer one message later; the time stays here for the
        stream-reset check (RT-6.35 step 3, #213)."""
        out: dict[str, tuple[int, int]] = {}
        for ch in [*self.channels, DM_SINCE]:
            if self._last_seq.get(ch):
                out[ch] = (self._last_seq[ch], self._last_time.get(ch, 0))
        return out

    def _handed(self, where: str, stream_seq: int, at: int) -> None:
        """The message at `stream_seq` (server time `at`, ms) was handed over: the cursor
        follows it (#213). Called only once the delivered event's callback returned, so a
        message still in the callback, a failed one, or a cancelled one leaves the cursor
        before it: a reconnect or the host's read mark brings it again."""
        self._last_seq[where] = stream_seq
        self._last_time[where] = at

    def _stream_reset(self, where: str, consumer_seq: int, stream_seq: int, at: int) -> None:
        """A BUS stream made anew restarts its sequences, as a presence consumer made anew does
        (RT-6.38 step 1): its first delivery (number 1) at or below the remembered cursor says
        the cursor is from before the reset, and its server time is NEWER than the cursor's
        message (a start by time instead replays older messages and is no reset, #213). Forget
        the cursor then, so the new stream's low sequences are not dropped."""
        if (consumer_seq == 1 and stream_seq <= self._last_seq.get(where, 0)
                and at > self._last_time.get(where, 0)):
            self._last_seq.pop(where, None)
            self._last_time.pop(where, None)

    # --- lifecycle ---------------------------------------------------------------
    async def start(self) -> None:
        for ch in self.channels:
            if not CHANNEL_RE.fullmatch(ch):
                raise ValueError("invalid channel name")
        planb.attach(self.planb_pieces, lambda: self._synced)  # known once the presence replay is read
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
        planb.detach(self.planb_pieces)
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
        for t in (self._renew_task, self._gap_task, self._flush_task):
            if t is not None and t is not asyncio.current_task():
                t.cancel()
        self._renew_task = self._gap_task = self._flush_task = None

    # --- connection ----------------------------------------------------------------
    async def _open(self) -> None:
        """One connection with a fresh access token, `since` and the literal subscriptions."""
        self._synced = False  # the stored presences are read again from this connection
        self._own_echo = time.monotonic()  # a new connection gets the window to see its own presence
        token = await self._login.access_token()
        key = self._login.nkey()
        hello = build_hello(token, self._s.home, self._s.guests, self._s.lobby, self.since(),
                            fresh_presence=not self._presence_read, trusted=self._s.trusted,
                            trusted_cut=self._s.trusted_cut, session=self._session)
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
        self._watch_read_loop(nc)
        self._gaps.reset()
        self._stale.clear()
        self._cancel_gap_timer()  # this connection starts from `since`: the held-back gap is covered
        self._cancel_flush_timer()  # a held-back flush reconnect was for the old connection
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
            await self._sync(nc, timeout=OPEN_SYNC_SEC)
            if refused:
                raise NameMismatch("the credential's agent name is not the computed one; nothing sent")
            await nc.publish(self._presence_subject, self._presence_body("online"))
            await self._sync(nc, timeout=OPEN_SYNC_SEC)
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

    def _watch_read_loop(self, nc: NatsClient) -> None:
        """Wake the supervisor when `nc`'s read loop ends with the client closed, also
        when nats-py skipped its callbacks (RT-6.36 step 2, #221): nats-py 2.16 writes a
        publish still pending at a reset to the dead socket inside `_close`, and that
        write's error ends `_close` before `disconnected_cb`/`closed_cb`. The client is
        closed all the same; without this the bus stayed on it until a flush timed out
        or the credential was renewed. A loop that ends while the client lives (a
        reconnect of nats-py's own, which this bus turns off) wakes nobody."""
        task = getattr(nc, "_reading_task", None)
        if not isinstance(task, asyncio.Task):
            return

        def ended(done: asyncio.Task[Any]) -> None:
            if not done.cancelled() and done.exception() is not None:
                # That write's error, which the loop let escape: retrieved here, so
                # asyncio does not print it as "never retrieved"; the reconnect is the answer.
                log.debug("nats bus: the read loop ended: %s", type(done.exception()).__name__)
            if self._nc is nc and not self._stopping and nc.is_closed:
                self._connected = False
                self._wake.set()

        task.add_done_callback(ended)

    @staticmethod
    async def _sync(nc: NatsClient, timeout: int = SYNC_SEC) -> None:
        """Two round trips, so that every -ERR the server sent for what came before has
        been handled when this returns. One flush is not enough: its PING is queued behind
        the client's own buffered writes, and the PONG that resolves it may answer a PING
        that was already on the wire before them (nats-py matches PONGs to flushes in
        order), so an -ERR for the writes may still be on its way. The second PING is
        sent only after the first PONG, and the server answers in order."""
        await nc.flush(timeout=timeout)
        await nc.flush(timeout=timeout)

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

    def _cancel_flush_timer(self) -> None:
        if self._flush_task is not None and self._flush_task is not asyncio.current_task():
            self._flush_task.cancel()
        self._flush_task = None

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
            # The server being away is retried soon; a login failure, which may need a
            # human or a restarted login service, keeps growing to 30 s (#222 review).
            server_waits = reconnect_waits()
            login_waits = reconnect_waits(cap=LOGIN_RECONNECT_MAX_SEC)
            failures = 0
            if not planned:
                # A jittered moment before the first attempt: a home whose agents lost
                # the same server must not knock in step (#222 review).
                await asyncio.sleep(next(server_waits))
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
                        failures += 1
                        login_failed = login_failure(exc)
                        wait = next(login_waits if login_failed else server_waits)
                        # The first tries are news; a long outage is not a warning a turn.
                        if failures <= 3 or failures % 10 == 0:
                            log.warning("nats bus: reconnect failed (%s), next in %.1f s: %s",
                                        "login" if login_failed else "server", wait,
                                        connect_reason(exc))
                        else:
                            log.debug("nats bus: reconnect failed (%s), next in %.1f s",
                                      "login" if login_failed else "server", wait)
                        await asyncio.sleep(wait)
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
            if getattr(old, "is_closed", False):
                _drop_subscriptions(old)

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
            try:
                await asyncio.wait_for(self._presence_soon.wait(), self._heartbeat_sec)
            except asyncio.TimeoutError:
                pass
            soon = self._presence_soon.is_set()
            self._presence_soon.clear()
            nc = self._nc
            if nc is not None and self._connected:
                try:
                    await nc.publish(self._presence_subject, self._presence_body("online"))
                except Exception as exc:
                    log.warning("nats bus: heartbeat: %s", type(exc).__name__)
            self._check_own_echo()
            if soon:
                await asyncio.sleep(PRESENCE_SOON_SEC)  # a burst of changes sends one a while

    def presence_soon(self) -> None:
        """Plan B piece 9: the status this presence states changed, so the next presence goes
        out now (PRESENCE_SOON_SEC after the last early one at most), not at the next
        heartbeat. Safe from any thread: the event is set in the bus's own loop."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return  # not started: the first presence states it
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._presence_soon.set()
        else:
            loop.call_soon_threadsafe(self._presence_soon.set)

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
        except TimeoutError:
            # The line stopped answering: nats-py matches PONGs to flushes in order, so a
            # timed-out flush would misfit every later one. Give the connection up and
            # let the supervisor make a new one (RT-6.36 step 2) - at most once per
            # interval, so a server that answers everybody slowly is not stampeded.
            # The publish was written before the PONG went missing: it may have arrived.
            if self._nc is nc:
                self._connected = False
                now = time.monotonic()
                wait = FLUSH_RECONNECT_SEC - (now - self._last_flush_reconnect)
                if wait <= 0:
                    self._last_flush_reconnect = now
                    self._wake.set()
                elif self._flush_task is None or self._flush_task.done():
                    # Held back: arm the reconnect for the gate's end. Without it every send
                    # would raise "not connected" until the next credential renewal (#228).
                    self._flush_task = asyncio.create_task(
                        self._flush_reconnect_after(wait, nc))
            raise NatsBusError("the connection stopped answering; the post may have arrived") from None
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

    async def post_mm(self, channel: str, text: str, mm_root: str,
                      props: dict[str, Prop] | None = None) -> None:
        """Plan B piece 7 (RT-6.42 step 13): a reply in the thread of the Mattermost post
        `mm_root` (a person's), which the bridge posts there once it checked the post is in
        `channel`."""
        body = encode(Message("post", text, props=props or {}, mm_root=mm_root), self._s.props_prefix)
        await self._publish(f"{self._s.bus}.ch.{self._asked(channel)}.{self.name}", body)

    async def react_mm(self, channel: str, mm_ref: str, emoji: str, remove: bool = False) -> None:
        """Plan B piece 7: a reaction, or its removal, on the Mattermost post `mm_ref`."""
        body = encode(Message("unreaction" if remove else "reaction", emoji=emoji, mm_ref=mm_ref),
                      self._s.props_prefix)
        await self._publish(f"{self._s.bus}.ch.{self._asked(channel)}.{self.name}", body)

    def _dm_subject(self, recipient: str) -> str:
        if not self.agent_ok(recipient):
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
        presence. The DM flag (plan B piece 3) is read the same way: a failing source states
        none, so a DM to this agent stays on Mattermost."""
        card = None
        if self.card_source is not None:
            try:
                card = self.card_source()
            except Exception as exc:  # noqa: BLE001 — a label, never a reason to look offline
                log.info("nats bus: the card's data was not read (%s)", type(exc).__name__)
        dm = False
        if self.dm_source is not None:
            try:
                dm = bool(self.dm_source())
            except Exception as exc:  # noqa: BLE001 — in doubt off: the safe side for a DM
                log.info("nats bus: the DM flag was not read (%s): not stated", type(exc).__name__)
        shown = None
        if self.shown_source is not None:
            try:
                shown = self.shown_source()
            except Exception as exc:  # noqa: BLE001 — a display, never a reason to look offline
                log.info("nats bus: the shown status was not read (%s): not stated", type(exc).__name__)
        return encode_presence(status, self._bot, self._bot_name, card, dm=dm, shown=shown)

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

    def dm_of(self, agent: str) -> bool:
        """Whether the agent's newest presence says its home runs agent DMs over NATS (plan B
        piece 3): True only while that presence is fresh. No presence, an offline one and a
        presence without the flag (an older MCP server) all count as off, so a DM to the agent
        stays on Mattermost."""
        return bool(self._dm.get(agent)) and self.presence_of(agent) != "offline"

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

    def home_of(self, agent: str) -> str | None:
        """The home the agent's attestation states (RT-6.41, #255): a channel token, '' when the
        record states no home (a hello that named none), None when the record states none at all
        (an older login service) or there is no record this agent's presence backs - fresh and
        naming the anchor of the presence subject, the same gate as the circle. So a reader tells
        "the service says this is no home agent" from "no word yet". It grants nothing."""
        anchor = self._owners.get(agent, "")
        att = self.attestation_of(agent)
        if att is None or not anchor or att.owner_anchor != anchor:
            return None
        return att.home

    def presence_of(self, agent: str) -> str:
        """online or away while the agent's last presence is younger than
        PRESENCE_FRESH_SEC by the server's clock (never this host's) and no bye;
        else offline."""
        seen = self._presence.get(agent)
        if seen is None or seen[1] == "bye" or self._server_now - seen[0] >= PRESENCE_FRESH_SEC:
            return "offline"
        return seen[1]

    def notice_teller(self, agent: str) -> str:
        """Which live agent of this owner tells a notice (RT-6.34): the named agent if it is
        live, else the live agent with the smallest name, this one included. Every server
        answers from presence alone, so one DM goes out, not one per server - and only the
        owner's own agents count, never another owner's."""
        owner = self._owners.get(self.name) or self._owner
        live = {a for a in self._presence
                if self.presence_of(a) != "offline" and self._owners.get(a, "") == owner}
        live.add(self.name)
        return agent if agent in live else min(live)

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

    def channel_ids(self) -> dict[str, str] | None:
        """The channels the credential grants with the chat's ids, from the login service's
        record (RT-6.39 step 6): channel name -> Mattermost id ("" for one the directory has no
        id for); None while no record was read."""
        return dict(self._chan_record[2]) if self._chan_record is not None else None

    async def wait_channel_ids(self, timeout: float) -> dict[str, str] | None:
        """channel_ids, waiting up to `timeout` s for the first record; None when none came."""
        if self._chan_record is None:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._chan_event.wait(), timeout)
        return self.channel_ids()

    def bridge_pieces(self, channel: str) -> frozenset[int]:
        """The plan B pieces the bridge's fresh signal for the channel names (RT-6.42 step 7):
        those whose bridge half runs there, so far piece 6 (asks, RT-6.11 step 7); none while
        no fresh signal names the channel (people_switched)."""
        if not self.people_switched(channel):
            return frozenset()
        return self._bridge_pieces.get(channel, frozenset())

    def bridge_prefix(self, channel: str) -> str:
        """The bot prefix of the bridge's signal for the channel (RT-6.36 step 4): the bridge
        posts each agent's copies there under `<prefix><agent name>`; "" while no signal
        named one. Not bound to the signal's freshness: the copies stay in the channel."""
        return self._bridge_prefix.get(channel, "")

    def bot_record(self, agent: str) -> BotRecord | None:
        """The bridge's bot record of `agent` while it is fresh (its `exp` after the server's
        now) and names a bot; None for none, a stale one or one that says the bot is gone
        (RT-6.44 step 3)."""
        got = self._bot_records.get(agent)
        if got is None or got[2].bot is None or got[2].exp <= self._server_now:
            return None
        return got[2]

    def bot_records(self) -> dict[str, BotRecord]:
        """Every fresh bot record that names a bot, by agent name."""
        return {a: r for a in list(self._bot_records) if (r := self.bot_record(a)) is not None}

    def card_of(self, agent: str) -> PresenceCard:
        """The card the agent's newest presence states (self-stated, bounded), empty for none."""
        return self._cards.get(agent, PresenceCard())

    def member_names(self, channel: str) -> list[str]:
        """The agents that announced their membership of `channel` within 24 h, sorted."""
        return sorted(self._members.get(channel, {}))

    def account_person(self, account: str) -> tuple[str, str] | None:
        """(anchor, name) of a Mattermost account the login service's FRESH record binds to a
        person with a name (RT-6.42 step 3); None for no record, a stale one, an unbound one
        or one without a name."""
        got = self._accounts.get(account)
        rec = got[2] if got is not None else None
        if rec is None or not rec.anchor or not rec.name or rec.exp <= self._server_now:
            return None
        return rec.anchor, rec.name

    def accounts_of(self, anchor: str) -> list[str]:
        """The accounts whose fresh record binds `anchor`, sorted (RT-6.44 step 2)."""
        if not anchor or anchor == UNBOUND:
            return []
        return sorted(a for a, (_, _, rec) in self._accounts.items()
                      if rec is not None and rec.anchor == anchor and rec.exp > self._server_now)

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

    def bound_accounts(self) -> dict[str, str]:
        """Mattermost account -> anchor, for each account a record of the login service that
        decodes binds to a person (RT-6.42 step 3), a stale one too: a stale record still names
        the person, only its `approver` needs it fresh (as the bridge reads it). A record erased
        by a message that does not decode, and `anchor` null, bind none. Plan B piece 4 judges a
        Mattermost account's trust and approval by it (RT-6.41 step 6): the login service writes a
        record only for a binding it proved (OpenID or LDAP) and keeps it, from its directory,
        only while Mattermost shows the account so (#266)."""
        return {account: rec.anchor for account, (_, _, rec) in self._accounts.items()
                if rec is not None and rec.anchor}

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
                self._dm.pop(agent, None)
                self._cards.pop(agent, None)
                self._presence_seq.pop(agent, None)
        for agent, (_, t, _b) in list(self._bot_records.items()):
            if t < cut:
                del self._bot_records[agent]
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
        if where == DM_SINCE and tokens[:4] == [self._s.bus, "hu", "dm", self.name]:
            # Plan B piece 10 (RT-6.13): a person's direct message to this very agent, which the
            # bridge carries on the DM consumer's second filter, `<bus>.hu.dm.<name>.<anchor>`.
            await self._on_person_dm_deliver(tokens, ack, data, gap)
            return
        sender = tokens[-1]
        expect = [self._s.bus, "ch", where] if where != DM_SINCE else [self._s.bus, "dm", self.name]
        if tokens[:-1] != expect or not self.agent_ok(sender):
            log.warning("nats bus: dropped a message on %s with an unexpected subject", where)
            return
        if gap:
            self._gap(where)
        if where in self._stale:
            return
        at = int(ack.time * 1000)  # ms, as the read mark and a post's create_at hold server time
        # before the repeat check: a new stream's seq 1 may follow the old cursor
        self._stream_reset(where, ack.consumer_seq, ack.stream_seq, at)
        if ack.stream_seq <= self._last_seq.get(where, 0):
            return  # a repeat after a restart from `since`
        try:
            message = decode(data, self._s.props_prefix)
        except MessageError as exc:
            log.warning("nats bus: dropped a malformed message on %s (seq %d): %s", where, ack.stream_seq, exc)
            self._handed(where, ack.stream_seq, at)  # never readable: no replay on every reconnect
            return
        event = Event(where, sender, ack.stream_seq, ack.time, message)
        try:
            res = self._on_event(event)
            if inspect.isawaitable(res):
                await res
        except asyncio.CancelledError:
            # Killed while handing it: no cursor, so the host's read mark (which never passed
            # this post) asks for it again at the next start (at least once for this one).
            raise
        except Exception as exc:
            log.warning("nats bus: event callback failed: %s", type(exc).__name__)
            return  # not handed: the cursor stays before it
        self._handed(where, ack.stream_seq, at)

    async def _on_person_dm_deliver(self, tokens: list[str], ack: Any, data: bytes, gap: bool) -> None:
        """A person's direct message to this agent (plan B piece 10, RT-6.13) from `<name>_dm`:
        the subject is `<bus>.hu.dm.<name>.<anchor>`, the person its last token, never the
        body's; the body is a person's event of format v1 (decode_person, strict). It shares
        the DM consumer's sequence: a gap is the DMs' gap, a repeat after a restart from `since`
        is dropped, and it is handed like a DM so the cursor passes it."""
        at = int(ack.time * 1000)
        if len(tokens) != 5:
            log.warning("nats bus: dropped a person's direct message with an unexpected subject")
            return
        anchor = tokens[4] if tokens[4] == UNBOUND else owner_of_token(tokens[4])
        if not anchor:
            log.warning("nats bus: dropped a person's direct message: the sender is no anchor")
            return
        if gap:
            self._gap(DM_SINCE)
        if DM_SINCE in self._stale:
            return
        self._stream_reset(DM_SINCE, ack.consumer_seq, ack.stream_seq, at)
        if ack.stream_seq <= self._last_seq.get(DM_SINCE, 0):
            return  # a repeat after a restart from `since`
        try:
            message = decode_person(data)
        except MessageError as exc:
            log.warning("nats bus: dropped a malformed person's direct message (seq %d): %s", ack.stream_seq, exc)
            self._handed(DM_SINCE, ack.stream_seq, at)
            return
        callback = self.on_person_dm
        if callback is not None:
            try:
                res = callback(PersonEvent(DM_SINCE, anchor, ack.stream_seq, ack.time, message))
                if inspect.isawaitable(res):
                    await res
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("nats bus: person's direct message callback failed: %s", type(exc).__name__)
                return  # not handed: the cursor stays before it
        self._handed(DM_SINCE, ack.stream_seq, at)

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
        at = int(ack.time * 1000)  # ms, as `_last_time` holds server time
        if ack.stream_seq <= self._last_seq.get(key, 0):
            # A BUS stream made anew restarts its sequences: the consumer's first delivery (1)
            # with a NEWER time is not a repeat then. A person's key (`hu.<channel>`) is no
            # `since` key and the host keeps no mark for it: a restart starts their consumer
            # with new messages (RT-6.42 step 9).
            if ack.consumer_seq == 1 and at > self._last_time.get(key, 0):
                self._last_seq.pop(key, None)
                self._last_time.pop(key, None)
            else:
                return  # a repeat
        try:
            message = decode_person(data)
        except MessageError as exc:
            log.warning("nats bus: dropped a malformed person's message on %s (seq %d): %s",
                        channel, ack.stream_seq, exc)
            self._handed(key, ack.stream_seq, at)  # never readable: no replay on every reconnect
            return
        if self._on_person is None:
            self._handed(key, ack.stream_seq, at)
            return
        try:
            res = self._on_person(PersonEvent(channel, anchor, ack.stream_seq, ack.time, message))
            if inspect.isawaitable(res):
                await res
        except Exception as exc:
            log.warning("nats bus: person callback failed: %s", type(exc).__name__)
            return  # not handed: a repeat is handed again
        self._handed(key, ack.stream_seq, at)

    async def _on_notice_msg(self, anchor: str, verdict: str, msg: Any, ack: Any) -> None:
        """A theft notice (RT-6.34): dropped once expired, told once per record."""
        notice = decode_notice(msg.data)
        if notice.verdict != verdict:
            raise MessageError("the notice's verdict is not the subject's")
        if notice.exp < ack.time:
            log.info("nats bus: a login notice that expired (seq %d) is dropped", ack.stream_seq)
            return
        key = (owner_of_token(anchor), notice.kind, notice.sid, notice.iat)
        if key in self._notice_seen:
            return
        self._notice_seen[key] = time.monotonic()
        while len(self._notice_seen) > NOTICE_SEEN_MAX:
            self._notice_seen.pop(next(iter(self._notice_seen)))
        if self._on_notice is None:
            return
        try:
            res = self._on_notice(notice)
            if inspect.isawaitable(res):
                await res
        except Exception as exc:  # noqa: BLE001 — a failed DM is logged, the bus goes on
            log.warning("nats bus: the notice callback failed: %s", type(exc).__name__)

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

    async def _flush_reconnect_after(self, wait: float, nc: NatsClient) -> None:
        await asyncio.sleep(wait)
        if self._stopping or self._nc is not nc:
            return  # another connection came first (a drop, a gap, a renewal): it is healthy
        self._last_flush_reconnect = time.monotonic()
        self._wake.set()

    async def _gap_reconnect_after(self, wait: float) -> None:
        await asyncio.sleep(wait)
        if not self._stopping:
            self._last_gap_reconnect = time.monotonic()
            self._wake.set()

    def planb_pieces(self, channel: str) -> frozenset[int]:
        """The plan B pieces switched on for one of this bus's channels (RT-6.39 step 5)."""
        if channel not in self.channels:
            return frozenset()
        got = self._planb.get(channel)
        if got is None and not self._synced:
            got = self._planb_prev.get(channel)  # the replay has not reached this channel yet (#244)
        if got is None or self._iat() - got[1] >= PLANB_STALE_SEC:  # server time, carried on by the monotonic clock
            return frozenset()  # off is fail-safe: a record no credential renewed is no longer read
        return got[2]

    def planb_held(self, channel: str) -> frozenset[int]:
        """The pieces of the plan B record this bus holds for `channel`, stale or not (plan B
        piece 10, RT-6.13 step 7): a piece in a record that went stale is not known off."""
        got = self._planb.get(channel) or self._planb_prev.get(channel)
        return got[2] if got is not None else frozenset()

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
            # the consumer delivers the last record per subject again: until it has, the old
            # records stay readable, or every piece would flap to Mattermost (#244)
            # (a second recreation before the replay is complete keeps what the first left)
            self._planb_prev = {**self._planb_prev, **self._planb}
            self._planb = {}
            self._synced = False  # until a delivery with pending 0: the replay is not read yet
        self._presence_cseq = ack.consumer_seq
        if ack.pending == 0:
            self._synced = True
            self._presence_read = True
            self._planb_prev = {}  # the replay is complete: a channel without a record reads off
        t = msg.subject.split(".")
        p = self._s.presence
        try:
            if len(t) == 3 and t[0] == p and self.agent_ok(t[2]):
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
                self._cards[agent] = decode_presence_card(msg.data)
                bot = decode_presence_bot(msg.data)
                if bot:
                    self._bots[agent] = bot
                else:
                    self._bots.pop(agent, None)
                self._dm[agent] = decode_presence_dm(msg.data)  # plan B piece 3: DMs over NATS
                owner = owner_of_token(t[1])
                if owner:
                    self._owners[agent] = owner
                else:
                    self._owners.pop(agent, None)
            elif len(t) == 4 and t[0] == p and t[1] == "crown" and CHANNEL_RE.fullmatch(t[2]) and self.agent_ok(t[3]):
                decode_claim(msg.data)
                claims = self._claims.setdefault(t[2], {})
                if (ack.time, ack.stream_seq) > (self._claim_at.get((t[2], t[3]), 0.0), claims.get(t[3], 0)):
                    claims[t[3]] = ack.stream_seq
                    self._claim_at[(t[2], t[3])] = ack.time
            elif len(t) == 4 and t[0] == p and t[1] == "member" and CHANNEL_RE.fullmatch(t[2]) and self.agent_ok(t[3]):
                decode_claim(msg.data)
                members = self._members.setdefault(t[2], {})
                seq0, at0 = members.get(t[3], (0, 0.0))
                if (ack.time, ack.stream_seq) > (at0, seq0):
                    members[t[3]] = (ack.stream_seq, ack.time)
            elif len(t) == 5 and t[0] == p and t[1] == "notice" and t[3] in NOTICE_VERDICTS \
                    and t[4] == "v1" and owner_of_token(t[2]):
                await self._on_notice_msg(t[2], t[3], msg, ack)
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
                        exp, prefix, pieces = decode_bridge_signal_pieces(msg.data, t[2])
                        exp = min(exp, ack.time + SIGNAL_MAX_SEC)
                        if prefix:
                            self._bridge_prefix[t[2]] = prefix
                        self._bridge_pieces[t[2]] = pieces
                    except MessageError:
                        exp = 0.0
                        self._bridge_pieces.pop(t[2], None)
                        log.warning("nats bus: a bridge signal that does not decode (seq %d): people on "
                                    "Mattermost", ack.stream_seq)
                    self._bridge_signal[t[2]] = (ack.stream_seq, ack.time, exp)
            elif len(t) == 4 and t[0] == p and t[1] == "planb" and t[3] == "v1" and CHANNEL_RE.fullmatch(t[2]):
                seq0, at0, _pb = self._planb.get(t[2], (0, 0.0, frozenset()))
                if (ack.time, ack.stream_seq) > (at0, seq0):
                    try:
                        pieces = decode_planb(msg.data, t[2])
                    except MessageError:
                        pieces = frozenset()
                        log.warning("nats bus: a plan B record that does not decode (seq %d): its pieces off",
                                    ack.stream_seq)
                    self._planb[t[2]] = (ack.stream_seq, ack.time, pieces)
            elif len(t) == 4 and t[0] == p and t[1] == "directory" and t[2] == self.name and t[3] == "v1":
                # RT-6.39 step 6: this credential's channels with the chat's ids.
                seq0, at0 = (self._chan_record[0], self._chan_record[1]) if self._chan_record else (0, 0.0)
                if (ack.time, ack.stream_seq) > (at0, seq0):
                    chans, exp = decode_channel_record_full(msg.data, t[2])
                    if exp <= max(self._server_now, ack.time):  # past its time: as if it never came
                        log.warning("nats bus: ignored an expired channel record (seq %d)", ack.stream_seq)
                    else:
                        self._chan_record = (ack.stream_seq, ack.time, chans)
                        self._chan_event.set()
            elif len(t) == 4 and t[0] == p and t[1] == "bot" and t[3] == "v1" and self.agent_ok(t[2]):
                # RT-6.44 step 3: the newest valid record counts; one that does not decode is
                # dropped (a warning below) and erases nothing, as an attestation.
                brec = decode_bot_record(msg.data, t[2])
                seq0, at0, _oldb = self._bot_records.get(t[2], (0, 0.0, brec))
                if (ack.time, ack.stream_seq) > (at0, seq0):
                    brec = dataclasses.replace(brec, exp=min(brec.exp, ack.time + BOT_RECORD_MAX_SEC))
                    self._bot_records[t[2]] = (ack.stream_seq, ack.time, brec)
            elif len(t) == 4 and t[0] == p and t[1] == "attest" and t[3] == "v1" and self.agent_ok(t[2]):
                att = decode_attestation(msg.data, t[2])
                seq0, at0, _old = self._attest.get(t[2], (0, 0.0, att))
                if (ack.time, ack.stream_seq) > (at0, seq0):
                    self._attest[t[2]] = (ack.stream_seq, ack.time, att)
                self._records_on = True
            else:
                log.warning("nats bus: dropped a presence message with an unexpected subject")
        except MessageError as exc:
            log.warning("nats bus: dropped a malformed presence message (seq %d): %s", ack.stream_seq, exc)
