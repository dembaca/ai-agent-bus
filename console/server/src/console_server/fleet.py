"""The fleet, read from the PRESENCE stream's deliveries (RT-6.37/6.38).

The reader mirrors ``natsbus.NatsBus._on_presence_msg`` — the subject
shapes, the ordering by the server's clock, the consumer-made-anew reset,
the freshness by the server's timestamps — minus everything an agent's
bus does (echoes, claims, publication). Everything pure here: the live
connection (``nats_read``) feeds it subjects, bodies and ack metadata.

What a NATS home carries today is less than a Mattermost card: presence
knows ``online``/``away``, the crown claims and the memberships, the
attestation names the owner and the trusted humans — everything else on
the card is unknown, never guessed as empty string or zero: the app
shows ``n/a``.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from mattermost_ai_agent_bus import natsbus
from mattermost_ai_agent_bus.natsbus import (
    AGENT_RE, CHANNEL_RE, Attestation, decode_attestation,
    decode_claim, decode_presence, decode_presence_bot, owner_of_token,
    parse_ack,
)


@dataclass
class Fleet:
    """The whole tenant as one read-only view over presence deliveries."""

    presence_subject: str = "presence"

    # agent -> (server time, status) of its last presence message
    _presence: dict[str, tuple[float, str]] = field(default_factory=dict)
    _presence_seq: dict[str, tuple[int, float]] = field(default_factory=dict)
    _owners: dict[str, str] = field(default_factory=dict)
    _bots: dict[str, str] = field(default_factory=dict)
    # channel -> agent -> (stream sequence, server time) of its membership
    _members: dict[str, dict[str, tuple[int, float]]] = field(default_factory=dict)
    # channel -> agent -> stream sequence of its crown claim
    _claims: dict[str, dict[str, int]] = field(default_factory=dict)
    _claim_at: dict[tuple[str, str], float] = field(default_factory=dict)
    # agent -> Attestation (the login service's record, RT-6.41)
    _attests: dict[str, tuple[int, float, Attestation]] = field(default_factory=dict)
    _presence_cseq: int = 0
    _server_now: float = 0.0
    # the wall clock beside each server stamp: (server time, wall time)
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
        the dead stay "online" forever. The floor ages presence by the
        host clock only where the host's own age is the bigger of the two:
        the floor is the local clock's distance since the newest delivery,
        added to that delivery's server time, and never below it. A wrong
        host clock can only shorten lives that the server's clock would
        keep (both faces of wrongness are bounded by the delivery gap)."""
        import time
        if self.last_server_now_wall is None:
            return self._server_now
        at, wall = self.last_server_now_wall
        return round(max(self._server_now, at + (time.time() - wall)), 3)

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
            # remembered claims may not be in the replay. Forget them.
            self._claims.clear()
            self._claim_at.clear()
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
            if (len(t) == 4 and t[0] == p and t[1] == "attest"
                    and t[3] == "v1" and AGENT_RE.fullmatch(t[2])):
                att = decode_attestation(data, t[2])
                seq0, at0, _old = self._attests.get(t[2], (0, 0.0, att))
                if (ack.time, ack.stream_seq) > (at0, seq0):
                    self._attests[t[2]] = (ack.stream_seq, ack.time, att)
                    return True
                return False
        except Exception:
            # One unreadable message never ends the view (as NatsBus logs it).
            return False
        return False

    def _see(self, at: float) -> None:
        """The newest server timestamp this console has seen; prunes like
        NatsBus: nothing older than the stream's 24 h max age stays."""
        self._server_now = max(self._server_now, at)
        import time
        self.last_server_now_wall = (self._server_now, time.time())
        edge = self.server_now - natsbus.MAX_AGE_SEC
        for agent, (at_p, _s) in list(self._presence.items()):
            if at_p < edge:
                del self._presence[agent]
                self._presence_seq.pop(agent, None)
                self._owners.pop(agent, None)
                self._bots.pop(agent, None)
        self._attests = {a: row for a, row in self._attests.items()
                         if row[1] >= edge}
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

    def agents(self) -> list[str]:
        """Every agent of the tenant, sorted: one seen in presence."""
        return sorted(set(self._presence) | set(self._members_channels_agents()))

    def _members_channels_agents(self) -> list[str]:
        return [a for ch in self._members.values() for a in ch]

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
            record = att[2] if att and att[2].exp > self._server_now else None
            channels = sorted(ch for ch in self._members if agent in self._members[ch])
            presence = self._presence.get(agent, (0.0, "offline"))
            out.append({
                "name": agent,
                "displayName": short_name(agent),
                "home": crowns.get(agent) or (channels[0] if channels else ""),
                "state": self.state_of(agent),
                "crown": agent in crowns,
                "harness": "other",
                "model": "",
                "task": "",
                "role": "member",
                "for": record.owner_name if record else self._owners.get(agent, ""),
                "autonomous": False,
                "trusts": [name for _anchor, name in record.trusted] if record else [],
                "capabilities": [],
                "project": "",
                "branch": None,
                "pr": None,
                "ctx": None,
                "limits": None,
                "version": "",
                # unknown is null here, not 0: asks do not lie on NATS yet
                "openAsks": None,
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

    def graph(self) -> dict[str, Any]:
        """Nodes: people, channels, agents; edges: the owner (as attested)
        and each channel's crown and members — trusts and coordinates,
        cut at no depth yet (the pilot shows one tenant, whole)."""
        nodes = []
        edges = []
        people: dict[str, None] = {}
        for agent in self.agents():
            att = self._attests.get(agent)
            record = att[2] if att and att[2].exp > self._server_now else None
            owner = record.owner_name if record else self._owners.get(agent, "")
            if owner:
                people.setdefault(owner, None)
                edges.append({"id": f"e-{agent}-owner", "from": agent, "to": owner,
                              "kind": "trusts"})
        for who in people:
            nodes.append({"id": who, "kind": "person", "label": who})
        for channel in self.channels():
            nodes.append({"id": channel, "kind": "channel", "label": channel})
            for agent in self._members.get(channel, {}):
                edges.append({"id": f"e-{agent}-{channel}", "from": agent, "to": channel,
                              "kind": "coordinates"})
        for agent in self.agents():
            crown = any(self.holder_of(ch) == agent for ch in self._claims)
            node: dict[str, Any] = {"id": agent, "kind": "agent",
                                   "label": short_name(agent),
                                   "state": self.state_of(agent)}
            if crown:
                node["crown"] = True
            nodes.append(node)
        return {"nodes": nodes, "edges": [{"id": e["id"], "from": e["from"], "to": e["to"],
                                          "kind": e["kind"]} for e in edges],
                "cutDepth": None}


def short_name(name: str) -> str:
    """The list's short name: the id the harnesses use, without the common
    prefixes (`display_name` on the wire)."""
    for prefix in ("agent-mac-ai-agent-", "agent-mac-chat-next-", "agent-mac-"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name
