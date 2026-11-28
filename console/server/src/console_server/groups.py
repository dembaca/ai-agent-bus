"""The agents list's groups and the "At a glance" panel (#283), counted here, so the
browser no longer counts over every presence card.

``/v1/groups?by=home|repo|owner|status|none&scope=mine|trusting|everyone`` (RT-6.43 step 11): the
agents of the scope grouped by one key, each group with its members' names (the
browser draws the rows from the fleet stream's cards), the state counts, how many
are reachable and working, the open asks for the operator, and the sum of today's
tokens. Per-agent numbers stay visible only for own or trusted agents (ADR-015
point 15): a group's token sum covers those of its agents the operator may see
that state a count and says how many that are (``tokensAgents`` of ``total``). Without the
operator's anchor nothing is "mine", no ask is "for you" and no tokens are summed.
"""
from __future__ import annotations

from typing import Any, Collection, Mapping

BY = ("home", "repo", "owner", "status", "none")
SCOPES = ("mine", "trusting", "everyone")

# The state words by status group, as the list's filter held them (agents.ts):
# presence v1 says online/away until the state words arrive.
STATUS_GROUPS: dict[str, tuple[str, ...]] = {
    "working": ("working", "bus"),
    "listening": ("listening", "online"),
    "idle": ("idle", "away", "silent"),
    "offline": ("offline",),
}
STATUS_ORDER = ("working", "listening", "idle", "offline", "other")


def status_of(state: str) -> str:
    """The status group of a state word; a word no group holds is "other", never guessed."""
    for group, words in STATUS_GROUPS.items():
        if state in words:
            return group
    return "other"


def _counts(cards: list[dict[str, Any]]) -> dict[str, int]:
    out = {g: 0 for g in STATUS_ORDER}
    for card in cards:
        out[status_of(card["state"])] += 1
    return out


def _numbers(cards: list[dict[str, Any]], open_asks: list[dict[str, Any]],
             tokens: Mapping[str, int | None], seen: Collection[str]) -> dict[str, Any]:
    states = _counts(cards)
    names = {c["name"] for c in cards}
    mine = [a for a in open_asks if a["by"] in names]
    summed = [tokens[n] for n in names if n in seen and tokens.get(n) is not None]
    return {
        "total": len(cards),
        "states": states,
        "reachable": len(cards) - states["offline"],
        "working": states["working"],
        "asksOpen": len(mine),
        "asksForYou": sum(1 for a in mine if a.get("for_me") is True),
        "asksMayAnswer": sum(1 for a in mine if a.get("may_answer") is True),
        # the sum of the agents the operator may see numbers of and that state a token count;
        # `tokensAgents` says how many that are (the list says "6 of 7 agents" only when it
        # is fewer than `total`); null (n/a) when none, never 0
        "tokens": sum(summed) if summed else None,  # type: ignore[arg-type]
        "tokensAgents": len(summed),
    }


def _glance_asks(open_asks: list[dict[str, Any]]) -> dict[str, int]:
    """The panel's ask tiles, whoever owns the asking agent: `forYou` the open asks that ask
    the operator, `mayAnswer` the open asks to another human by agents that trust the
    operator (as owner or trusted), `open` all open asks in view (the small number)."""
    return {
        "forYou": sum(1 for a in open_asks if a.get("for_me") is True),
        "mayAnswer": sum(1 for a in open_asks
                         if a.get("may_answer") is True and a.get("for_me") is not True),
        "open": len(open_asks),
    }


def _key(card: dict[str, Any], by: str) -> tuple[str, str]:
    """(key, label) of a card's group."""
    if by == "home":
        return card["home"], card["home"] or "no home"
    if by == "repo":
        return card["project"], card["project"] or "no repo"
    if by == "owner":
        who = card["for"]
        return who, who or "no owner"
    if by == "status":
        g = status_of(card["state"])
        return g, g.capitalize()
    return "all", "All agents"


def groups(cards: list[dict[str, Any]], asks: list[dict[str, Any]],
           agent_rows: list[dict[str, Any]], *, by: str, scope: str,
           own: Collection[str], own_or_trusted: Collection[str]) -> dict[str, Any]:
    """The answer of ``/v1/groups``: the groups of `by` over the agents of `scope`, and the
    panel's numbers (``glance``): the operator's own agents', the flock's and the asks'. Each
    scope widens the one before: "mine" (own) lies inside "trusting" (own or trusted), which
    lies inside "everyone". `own` and
    `own_or_trusted` are the agent names of the two scopes (Fleet.own,
    Fleet.own_or_trusted); `agent_rows` the rows of ``/v1/agent-stats``, which exist only
    for `own_or_trusted` agents."""
    if by not in BY:
        raise ValueError(f"by is one of {', '.join(BY)}")
    if scope not in SCOPES:
        raise ValueError(f"scope is one of {', '.join(SCOPES)}")
    open_asks = [a for a in asks if a["end"] != "answered"]
    tokens: dict[str, int | None] = {
        r["agent"]: (r["tok24h"] or {}).get("all") for r in agent_rows}
    seen = set(own_or_trusted)
    allowed = {"mine": set(own), "trusting": set(own_or_trusted)}.get(scope)
    shown = [c for c in cards if allowed is None or c["name"] in allowed]
    keyed: dict[str, tuple[str, list[dict[str, Any]]]] = {}
    for card in shown:
        key, label = _key(card, by)
        keyed.setdefault(key, (label, []))[1].append(card)
    out = []
    for key, (label, members) in keyed.items():
        out.append({"key": key, "label": label,
                    "agents": sorted(c["name"] for c in members),
                    **_numbers(members, open_asks, tokens, seen)})
    if by == "status":
        out.sort(key=lambda g: STATUS_ORDER.index(g["key"]))
    else:
        # the groups with an ask for the operator first, then by name
        out.sort(key=lambda g: (g["asksForYou"] == 0, g["label"].lower()))
    mine_cards = [c for c in cards if c["name"] in set(own)]
    return {"by": by, "scope": scope, "groups": out,
            "glance": {"mine": _numbers(mine_cards, open_asks, tokens, seen),
                       "flock": _numbers(cards, open_asks, tokens, seen),
                       "asks": _glance_asks(open_asks)}}
