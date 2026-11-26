import { describe, expect, it } from "vitest"
import type { AgentCard, AgentState, Ask } from "../api/types"
import { agentName, askerName, countByGroup, filterCards, openAsksFirst } from "./agents"

function card(name: string, state: AgentState | string, extra: Partial<AgentCard> = {}): AgentCard {
  return {
    name, displayName: name, statedName: "", nameShared: false, home: "~bus-dev", guests: [], state: state as AgentState,
    crown: false, harness: "claude-code", model: "opus", task: "", role: "member",
    for: "dembaca", autonomous: false, trusts: [], capabilities: [], project: "ai-agent-bus",
    branch: "main", pr: null, ctx: null, limits: null, version: "0.103.0",
    openAsks: 0, asksForOperator: 0, helloAt: 0, lastPostAt: null, helloPostId: "",
    tasks: [], ...extra,
  }
}

const FLEET: AgentCard[] = [
  card("agent-a", "working", { task: "Console design", branch: "feat/console-design" }),
  card("agent-b", "bus", { harness: "opencode", model: "glm" }),
  card("agent-c", "listening", { guests: ["~chat-next"] }),
  card("agent-d", "online"),             // format v1: counts as listening
  card("agent-e", "away"),               // format v1: counts as idle
  card("agent-f", "silent", { pr: { number: 182, url: "" } }),
  card("agent-g", "offline"),
  card("agent-h", "dreaming"),           // a state the console does not know
]

const names = (cards: AgentCard[]) => cards.map((c) => c.name)

describe("the agents list's filter", () => {
  it("shows every agent under All, the unknown state too", () => {
    expect(names(filterCards(FLEET, "all", ""))).toEqual(names(FLEET))
  })

  it("groups the state words, format v1's among them", () => {
    expect(names(filterCards(FLEET, "working", ""))).toEqual(["agent-a", "agent-b"])
    expect(names(filterCards(FLEET, "listening", ""))).toEqual(["agent-c", "agent-d"])
    expect(names(filterCards(FLEET, "idle", ""))).toEqual(["agent-e", "agent-f"])
    expect(names(filterCards(FLEET, "offline", ""))).toEqual(["agent-g"])
  })

  it("never guesses an unknown state into a group", () => {
    for (const f of ["working", "listening", "idle", "offline"] as const) {
      expect(names(filterCards(FLEET, f, ""))).not.toContain("agent-h")
    }
    expect(countByGroup(FLEET)).toEqual({ working: 2, listening: 2, idle: 2, offline: 1, other: 1 })
  })

  it("lists the states no group knows under Other", () => {
    expect(names(filterCards(FLEET, "other", ""))).toEqual(["agent-h"])
    expect(names(filterCards(FLEET.slice(0, 7), "other", ""))).toEqual([])
  })

  it("searches task, branch, harness, guests and PR, every word, any case", () => {
    expect(names(filterCards(FLEET, "all", "CONSOLE"))).toEqual(["agent-a"])
    expect(names(filterCards(FLEET, "all", "feat/console design"))).toEqual(["agent-a"])
    expect(names(filterCards(FLEET, "all", "opencode"))).toEqual(["agent-b"])
    expect(names(filterCards(FLEET, "all", "chat-next"))).toEqual(["agent-c"])
    expect(names(filterCards(FLEET, "all", "#182"))).toEqual(["agent-f"])
    expect(names(filterCards(FLEET, "all", "  "))).toEqual(names(FLEET))
  })

  it("combines the search with the status group", () => {
    expect(names(filterCards(FLEET, "idle", "opencode"))).toEqual([])
    expect(names(filterCards(FLEET, "working", "glm"))).toEqual(["agent-b"])
  })
})

describe("an agent's name (#199)", () => {
  const nats = "agent-oudzj4bhpl62iahctur5s5sf44"

  it("is the name its presence states, the bus id only for the title", () => {
    const c = card(nats, "working", { displayName: "agent-himalia-772844", statedName: "agent-himalia-772844" })
    expect(agentName(c)).toEqual({ text: "agent-himalia-772844", shared: false, id: nats })
  })

  it("shows a name another card states too, marked as shared", () => {
    // the server never confirms it: displayName stays the bus id
    const c = card(nats, "working", { displayName: nats, statedName: "agent-same", nameShared: true })
    expect(agentName(c)).toEqual({ text: "agent-same", shared: true, id: nats })
  })

  it("falls back to the server's name when none is stated", () => {
    expect(agentName(card(nats, "working", { displayName: nats })).text).toBe(nats)
  })

  it("names an ask's agent as its card does, else as the ask row says", () => {
    const fleet = [card(nats, "working", { displayName: nats, statedName: "agent-same", nameShared: true })]
    expect(askerName({ by: nats, displayName: nats }, fleet).text).toBe("agent-same")
    expect(askerName({ by: "agent-gone", displayName: "gone" }, fleet)).toEqual({ text: "gone", shared: false, id: "agent-gone" })
  })
})

describe("the open asks on top", () => {
  const ask = (id: string, at: number, extra: Partial<Ask> = {}): Ask => ({
    id, by: "agent-a", displayName: "agent-a", home: "~bus-dev", kind: "approval",
    human: null, need: id, at, end: "open", forMe: false, mayAnswer: false,
    agentGone: false, threadId: id, ...extra,
  })

  it("puts the operator's first, then those it may answer, then the oldest", () => {
    const rows = openAsksFirst([
      ask("old", 1), ask("mine", 5, { forMe: true }), ask("answered", 0, { end: "answered" }),
      ask("may", 4, { mayAnswer: true }), ask("expired", 2, { end: "expired" }),
    ])
    expect(rows.map((a) => a.id)).toEqual(["mine", "may", "old", "expired"])
  })
})
