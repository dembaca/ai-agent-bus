"""The fleet, read from the PRESENCE stream's deliveries (RT-6.37/6.38).

The reader mirrors ``natsbus.NatsBus._on_presence_msg`` — the subject
shapes, the ordering by the server's clock, the consumer-made-anew reset,
the freshness by the server's timestamps — minus everything an agent's
bus does (echoes, claims, publication). Everything pure here: the live
connection (``nats_read``) feeds it subjects, bodies and ack metadata.

What a NATS home carries today is less than a Mattermost card: presence
knows ``online``/``away`` and the agent's bot (its id and, since the
presence states it, its username: the name people and coordinator("who")
know the agent by), the crown claims and the memberships, the attestation
names the owner and the trusted humans. The presence also states the
HELLO card's harness, model, task, branch and the number of the agent's
open asks (RT-6.37, ``natsbus.decode_presence_card``, bounded there);
self-stated like the name, they are shown as text and decide nothing. An
older MCP server states none of them, and everything else on the card is
unknown too: never guessed as empty string, zero or "other", the app
shows ``n/a``.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from mattermost_ai_agent_bus import natsbus
from mattermost_ai_agent_bus.natsbus import (
    AGENT_RE, CHANNEL_RE, Attestation, decode_attestation,
    AskRecord, PresenceCard, card_text, decode_ask_record, decode_claim, decode_presence,
    decode_presence_bot, decode_presence_card, decode_presence_name,
    owner_of_token, owner_token,
    parse_ack,
)

log = logging.getLogger(__name__)
# An ask older than this is shown as expired: a hint, a yes approves nothing then (RT-6.11);
# whether it does stays the asking server's check.
ASK_EXPIRED_SEC = 24 * 3600


@dataclass
class Fleet:
    """The whole tenant as one read-only view over presence deliveries."""

    presence_subject: str = "presence"

    # agent -> (server time, status) of its last presence message
    _presence: dict[str, tuple[float, str]] = field(default_factory=dict)
    _presence_seq: dict[str, tuple[int, float]] = field(default_factory=dict)
    _owners: dict[str, str] = field(default_factory=dict)
    _bots: dict[str, str] = field(default_factory=dict)
    # agent -> the bot's username its presence states (a label, not an identity)
    _names: dict[str, str] = field(default_factory=dict)
    # agent -> the HELLO card's data its presence states (self-stated, bounded)
    _cards: dict[str, PresenceCard] = field(default_factory=dict)
    # channel -> agent -> (stream sequence, server time) of its membership
    _members: dict[str, dict[str, tuple[int, float]]] = field(default_factory=dict)
    # channel -> agent -> stream sequence of its crown claim
    _claims: dict[str, dict[str, int]] = field(default_factory=dict)
    _claim_at: dict[tuple[str, str], float] = field(default_factory=dict)
    # agent -> (stream sequence, server time, home, record) of its open asks (RT-6.43 step 6)
    _asks: dict[str, tuple[int, float, str, AskRecord]] = field(default_factory=dict)
    # agent -> Attestation (the login service's record, RT-6.41)
    _attests: dict[str, tuple[int, float, Attestation]] = field(default_factory=dict)
    refusals: int = 0   # records the strict decoders refused, dropped whole
    count: Callable[[], None] | None = None   # the process-wide counter (#158)
    _presence_cseq: int = 0
    _server_now: float = 0.0
    # the clocks beside each server stamp: (server time, monotonic time)
    # — the floor's elapsed time is monotonic, so a jump of the host's
    # wall clock neither revives dead agents nor kills live ones
    last_server_now_wall: tuple[float, float] | None = None
    synced: bool = False
    # monotonic stamps, or None before the first: the last jetstream
    # replay seen (a delivery with pending 0, or a consumer that answered
    # with nothing left to deliver), and the last delivery of any kind —
    # /healthz reads them; None says "never synced" and reads red
    last_sync_at: float | None = None
    last_delivery_at: float = 0.0

    @property
    def server_now(self) -> float:
        """The newest server timestamp this console has seen (never its own
        clock, which may be wrong — as NatsBus, every age is by this).

        The wall-clock floor (#108): with no deliveries at all — a killed
        lone agent, a silent stream — `server_now` would stand still and
        the dead stay "online" forever. The floor is the **monotonic**
        clock's distance since the newest delivery, added to that
        delivery's server time, and never below the raw stamp: a jump of
        the host's wall clock changes nothing (the elapsed time is
        monotonic), and the floor is only wrong by the drift of the two
        clocks since the last delivery — bounded by the delivery gap."""
        import time
        if self.last_server_now_wall is None:
            return self._server_now
        at, mono = self.last_server_now_wall
        return max(self._server_now, at + (time.monotonic() - mono))

    def on_delivery(self, subject: str, data: bytes, reply: str | None,
                    stream: str, consumer: str) -> bool:
        """One delivery of the presence consumer: ``True`` when something in
        the fleet changed. Anything without this consumer's JetStream
        metadata is dropped, as NatsBus drops it."""
        ack = parse_ack(reply, stream, consumer)
        if ack is None:
            return False
        self.last_delivery_at = time.monotonic()
        self._see(ack.time)
        if ack.consumer_seq <= self._presence_cseq:
            # The consumer was made anew (here: the console reconnected):
            # remembered claims may not be in the replay. Forget them; the asks
            # too: the new consumer may no longer cover a channel the person
            # lost, and its record would linger until it ran out (RT-6.43 step 6).
            self._claims.clear()
            self._claim_at.clear()
            self._asks.clear()
            self.synced = False
        self._presence_cseq = ack.consumer_seq
        if ack.pending == 0:
            self.synced = True
            self.last_sync_at = time.monotonic()
        t = subject.split(".")
        p = self.presence_subject
        try:
            if len(t) == 3 and t[0] == p and AGENT_RE.fullmatch(t[2]):
                agent, status = t[2], decode_presence(data)
                seq, at = self._presence_seq.get(agent, (0, 0.0))
                if (ack.time, ack.stream_seq) <= (at, seq):
                    # An older message never overrides a newer one.
                    return False
                self._presence_seq[agent] = (ack.stream_seq, ack.time)
                self._presence[agent] = (ack.time, status)
                bot = decode_presence_bot(data)
                if bot:
                    self._bots[agent] = bot
                else:
                    self._bots.pop(agent, None)
                self._cards[agent] = decode_presence_card(data)
                label = decode_presence_name(data)
                if label:
                    self._names[agent] = label
                else:
                    self._names.pop(agent, None)
                owner = owner_of_token(t[1])
                if owner:
                    self._owners[agent] = owner
                else:
                    self._owners.pop(agent, None)
                return True
            if (len(t) == 4 and t[0] == p and t[1] == "crown"
                    and CHANNEL_RE.fullmatch(t[2]) and AGENT_RE.fullmatch(t[3])):
                decode_claim(data)
                claims = self._claims.setdefault(t[2], {})
                if (ack.time, ack.stream_seq) > (self._claim_at.get((t[2], t[3]), 0.0),
                                                 claims.get(t[3], 0)):
                    claims[t[3]] = ack.stream_seq
                    self._claim_at[(t[2], t[3])] = ack.time
                    return True
                return False
            if (len(t) == 4 and t[0] == p and t[1] == "member"
                    and CHANNEL_RE.fullmatch(t[2]) and AGENT_RE.fullmatch(t[3])):
                decode_claim(data)
                members = self._members.setdefault(t[2], {})
                seq0, at0 = members.get(t[3], (0, 0.0))
                if (ack.time, ack.stream_seq) > (at0, seq0):
                    members[t[3]] = (ack.stream_seq, ack.time)
                    return True
                return False
            if (len(t) == 4 and t[0] == p and t[1] == "ask"
                    and CHANNEL_RE.fullmatch(t[2]) and AGENT_RE.fullmatch(t[3])):
                try:
                    record = decode_ask_record(data)
                except Exception:
                    # dropped whole (RT-6.43 step 6), the warning carries none of it
                    self.refusals += 1
                    if self.count is not None:
                        self.count()
                    log.warning("console: an ask record of %s dropped: not format v1 or past its bounds", t[3])
                    return False
                seq0, at0, _h, _r = self._asks.get(t[3], (0, 0.0, "", record))
                if (ack.time, ack.stream_seq) > (at0, seq0):
                    self._asks[t[3]] = (ack.stream_seq, ack.time, t[2], record)
                    return True
                return False
            if (len(t) == 4 and t[0] == p and t[1] == "attest"
                    and t[3] == "v1" and AGENT_RE.fullmatch(t[2])):
                att = decode_attestation(data, t[2])
                seq0, at0, _old = self._attests.get(t[2], (0, 0.0, att))
                if (ack.time, ack.stream_seq) > (at0, seq0):
                    self._attests[t[2]] = (ack.stream_seq, ack.time, att)
                    return True
                return False
        except Exception:
            # One unreadable message never ends the view (as NatsBus logs it);
            # a record outside the rules is dropped whole, counted, not shown.
            self.refusals += 1
            if self.count is not None:
                self.count()
            return False
        return False

    def _see(self, at: float) -> None:
        """The newest server timestamp this console has seen; prunes like
        NatsBus: nothing older than the stream's 24 h max age stays.

        The floor re-anchors only when a delivery's stamp is genuinely
        newer: a replay after a reconnect re-delivers the same old stamps,
        and re-anchoring on them would revive a presence that the silent
        window had already aged out (Fable's replay probe)."""
        if at > self._server_now:
            self._server_now = at
            import time
            self.last_server_now_wall = (self._server_now, time.monotonic())
        edge = self.server_now - natsbus.MAX_AGE_SEC
        for agent, (at_p, _s) in list(self._presence.items()):
            if at_p < edge:
                del self._presence[agent]
                self._presence_seq.pop(agent, None)
                self._owners.pop(agent, None)
                self._bots.pop(agent, None)
                self._names.pop(agent, None)
                self._cards.pop(agent, None)
        self._attests = {a: row for a, row in self._attests.items()
                         if row[1] >= edge}
        self._asks = {a: row for a, row in self._asks.items() if self._ask_exp(row) > self.server_now}
        for ch in list(self._members):
            self._members[ch] = {a: m for a, m in self._members[ch].items() if m[1] >= edge}
            if not self._members[ch]:
                del self._members[ch]

    # --- the view ----------------------------------------------------------------------

    def state_of(self, agent: str) -> str:
        """The state word for the card: presence v1 carries `online`/`away`
        until it carries the state words; `offline` at a bye or none fresh
        (ADR-015 point 5: the columns until presence carries them)."""
        seen = self._presence.get(agent)
        if (seen is None or seen[1] == "bye"
                or self.server_now - seen[0] >= natsbus.PRESENCE_FRESH_SEC):
            return "offline"
        return seen[1]

    def holder_of(self, channel: str) -> str | None:
        """The crown of a channel, by the NATS rule (RT-6.38): the lowest
        fresh claim in claim order, a member of the channel."""
        claims = self._claims.get(channel, {})
        return natsbus.crown_holder(claims, self._presence, self.server_now,
                                    at=self.claim_times(channel)) or None

    def claim_times(self, channel: str) -> dict[str, float]:
        return {a: self._claim_at.get((channel, a), 0.0) for a in self._claims.get(channel, {})}

    def is_member(self, channel: str, agent: str) -> bool:
        return agent in self._members.get(channel, {})

    def channels(self) -> list[str]:
        """Every channel of the tenant, by membership (a member subject is
        the login service's own mark, so no publishing judge)."""
        return sorted(self._members)

    def channels_of(self, agent: str) -> list[str]:
        """The channels its memberships name, sorted (every granted channel
        gets one, home and guest alike, RT-6.37)."""
        return sorted(ch for ch in self._members if agent in self._members[ch])

    def home_of(self, agent: str) -> str:
        """Its home channel, as the bus itself states it: the channel of its
        ask record's subject, `<presence>.ask.<home>.<name>` (RT-6.43 step 5),
        sent by this agent's own bus; else a channel it claimed the crown of,
        any claim of its own and not only the current holder's (the login
        service grants a claim in the home alone, RT-6.38); else the first
        channel it is a member of. "" without any of them — never guessed from
        a name. A statement that does not sit beside a membership is no home
        (a record can outlive a lost channel): the next step answers then."""
        row = self._asks.get(agent)
        if row is not None and row[2] and self.is_member(row[2], agent):
            return row[2]
        # A claim the agent is no member of any more is no home either: claims are
        # kept for the stream's day and the pruning takes only members, asks and
        # attestations (Fable's delta review).
        claimed = sorted(ch for ch in self._claims
                         if agent in self._claims[ch] and self.is_member(ch, agent))
        if claimed:
            return claimed[0]
        channels = self.channels_of(agent)
        return channels[0] if channels else ""

    def guests_of(self, agent: str) -> list[str]:
        """The channels it is a member of beside its home: a guest is a
        channel an agent may visit, never claim (RT-6.16, ADR-016 point 4)."""
        home = self.home_of(agent)
        return [ch for ch in self.channels_of(agent) if ch != home]

    def agents(self) -> list[str]:
        """Every agent of the tenant, sorted: one seen in presence."""
        return sorted(set(self._presence) | set(self._members_channels_agents()))

    def _members_channels_agents(self) -> list[str]:
        return [a for ch in self._members.values() for a in ch]

    def shown_name(self, agent: str) -> str:
        """The name a card shows: the bot's username the agent's presence states,
        the name coordinator("who") shows. The agent states it of itself, so it is
        a label, never the key (the key is the agent's name on the bus, the
        subject's last token). A username any other known card states too
        (offline ones included, while the stream keeps their presence, 24 h)
        is shown on neither, so no agent takes over the name of one that went
        away; an absent one is not shown either — the card shows the short
        bus name then. An agent may still state a name falsely that no other
        card holds: the owner beside it is the attested one."""
        label = self._names.get(agent, "")
        if label and not any(other != agent and named == label
                             for other, named in self._names.items()):
            return label
        return short_name(agent)

    # --- the wire (ADR-015 point 10, the app's contract) -------------------------------

    def cards(self) -> list[dict[str, Any]]:
        """One wire card per agent, unknown fields marked as unknown
        (``None`` or empty), never guessed as 0 (ADR-015 point 8)."""
        out = []
        crowns: dict[str, str] = {}
        for channel in self._claims:
            holder = self.holder_of(channel)
            if holder:
                crowns[holder] = channel
        for agent in self.agents():
            att = self._attests.get(agent)
            record = att[2] if att and att[2].exp > self.server_now else None
            presence = self._presence.get(agent, (0.0, "offline"))
            stated = self._cards.get(agent, PresenceCard())
            out.append({
                "name": agent,
                "displayName": self.shown_name(agent),
                "home": self.home_of(agent),
                # the channels beside the home (RT-6.16): the graph draws them apart
                "guests": self.guests_of(agent),
                "state": self.state_of(agent),
                "crown": agent in crowns,
                # the HELLO card's data the presence states (RT-6.37); n/a when an
                # older MCP server states none
                "harness": stated.harness,
                "model": stated.model or "",
                "task": stated.task or "",
                "role": "member",
                "for": record.owner_name if record else self._owners.get(agent, ""),
                "autonomous": False,
                "trusts": [name for _anchor, name in record.trusted] if record else [],
                "capabilities": [],
                "project": "",
                "branch": stated.branch,
                "pr": None,
                "ctx": None,
                "limits": None,
                "version": "",
                # unknown is null here, not 0: an older MCP server states no asks
                "openAsks": stated.asks,
                "asksForOperator": None,
                "helloAt": int(presence[0] * 1000),
                "lastPostAt": None,
                "helloPostId": "",
                "tasks": [],
            })
        return out

    def card(self, agent: str) -> dict[str, Any] | None:
        for card in self.cards():
            if card["name"] == agent:
                return card
        return None

    def graph(self, anchor: str | None = None) -> dict[str, Any]:
        """Nodes: people, channels, agents; edges: the owner (as attested), each agent's
        home channel and its guest channels — trusts, coordinates and guest, cut at no
        depth yet (the pilot shows one tenant, whole).

        `anchor`: the operator's, from the login's own record (RT-6.43 step 2), never a
        value the browser sends. The person node it names is marked `you` (ADR-015
        point 7), matched by anchor, so two people of one name stay apart.

        Node ids are unique across kinds: a person's is `person:<anchor token>`, a
        channel's its name, an agent's its name — an owner named like a channel is
        two nodes with two ids."""
        nodes = []
        edges = []
        # anchor token -> the name it is shown under: the anchor is the identity
        # (RT-6.41), the name only labels the node, so two people of one name stay
        # two nodes. An owner without an attestation is the presence subject's
        # owner token, its name read back (RT-6.37 step 1).
        people: dict[str, str] = {}
        for agent in self.agents():
            att = self._attests.get(agent)
            record = att[2] if att and att[2].exp > self.server_now else None
            if record:
                # an attestation's anchor is in name form ("." kept), the login's own
                # record's in subject form ("." as "~"): the token is what both share
                token, name = owner_token(record.owner_anchor), record.owner_name
            else:
                name = self._owners.get(agent, "")
                token = owner_token(name)
            if token != natsbus.NO_OWNER:
                people.setdefault(token, name)
                edges.append({"id": f"e-{agent}-owner", "from": agent,
                              "to": f"person:{token}", "kind": "trusts"})
        me = anchor or ""
        for token, name in people.items():
            # `person:` keeps a person's id apart from a channel's or an agent's of the
            # same name; the card's `home` stays the plain channel name
            person: dict[str, Any] = {"id": f"person:{token}", "kind": "person", "label": name}
            if me and token == me:
                person["you"] = True
            nodes.append(person)
        for channel in self.channels():
            nodes.append({"id": channel, "kind": "channel", "label": channel})
            for agent in sorted(self._members.get(channel, {})):
                edges.append({"id": f"e-{agent}-{channel}", "from": agent, "to": channel,
                              "kind": ("coordinates" if self.home_of(agent) == channel
                                       else "guest")})
        for agent in self.agents():
            crown = any(self.holder_of(ch) == agent for ch in self._claims)
            node: dict[str, Any] = {"id": agent, "kind": "agent",
                                   "label": self.shown_name(agent),
                                   "state": self.state_of(agent)}
            if crown:
                node["crown"] = True
            nodes.append(node)
        return {"nodes": nodes, "edges": [{"id": e["id"], "from": e["from"], "to": e["to"],
                                          "kind": e["kind"]} for e in edges],
                "cutDepth": None}


    # --- the asks (RT-6.43 step 6; ADR-015 addition of 2026-11-25, points 8 to 11) --------

    @staticmethod
    def _ask_exp(row: tuple[int, float, str, AskRecord]) -> float:
        """When a record runs out: its `exp`, but never later than ASK_VALID_SEC after the
        server stored it (the agent's own clock is not trusted)."""
        return min(row[3].exp, row[1] + natsbus.ASK_VALID_SEC)

    def _fresh_attestation(self, agent: str) -> Attestation | None:
        att = self._attests.get(agent)
        return att[2] if att and att[2].exp > self.server_now else None

    def asks(self, anchor: str | None = None) -> list[dict[str, Any]]:
        """The open asks of every fresh record, as `/v1/asks` rows (the ADR's spellings).
        `anchor`: the operator's anchor in subject form, as the login's own record of the
        operator's NATS credential gives it (points 1 to 6 of the addition); never derived from a login name
        (point 4). Without it "for me" and "may answer" are None (n/a) and mark nothing. With
        it: "for me" when the asked human is that anchor, "may answer" when the asking
        agent's attestation names it as owner or trusted; `expired` after
        24 h (a hint: the asking server decides); `agent_gone` when its presence is not fresh.
        Read-only: no answer, no thread text. The title is plain text, control and direction
        characters stripped."""
        me = anchor
        now = self.server_now
        rows = []
        for agent, row in sorted(self._asks.items()):
            _seq, _at, home, record = row
            if self._ask_exp(row) <= now:
                continue
            att = self._fresh_attestation(agent)
            people = ([(att.owner_anchor, att.owner_name), *att.trusted] if att else [])
            names = {owner_token(anchor): named for anchor, named in people}
            gone = self.state_of(agent) == "offline"
            for ask in record.asks:
                human = None if ask.human == natsbus.NO_OWNER else ask.human
                rows.append({
                    "id": ask.mm,
                    "by": agent,
                    "display_name": self.shown_name(agent),
                    "home": home,
                    "kind": ask.kind,
                    "human": names.get(human, human) if human else None,
                    "need": card_text(ask.title, natsbus.ASK_TITLE_MAX) or "",
                    "at": ask.asked * 1000,
                    "end": ("expired" if now - ask.asked >= ASK_EXPIRED_SEC
                            else "answered" if ask.answered else "open"),
                    # the anchor must also be one the asking agent's attestation names
                    "for_me": (human == me and me in names) if me else None,
                    "may_answer": (me in names) if me else None,
                    "agent_gone": gone,
                    "thread_id": ask.mm,
                })
        return sorted(rows, key=lambda r: (not r["for_me"], r["at"]))


def short_name(name: str) -> str:
    """The list's short name: the id the harnesses use, without the common
    prefixes (`display_name` on the wire)."""
    for prefix in ("agent-mac-ai-agent-", "agent-mac-chat-next-", "agent-mac-"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name
