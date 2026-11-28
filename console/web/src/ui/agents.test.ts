import { describe, expect, it } from "vitest"
import type { AgentCard, AgentState, Ask } from "../api/types"
import { agentName, askerName, openAsksFirst, tokenCount } from "./agents"

describe("tokenCount (#201)", () => {
  it("shortens for a dense cell and says n/a for null, never 0", () => {
    expect([0, 950, 1000, 92_100, 640_000, 1_234_567, 12_000_000].map(tokenCount))
      .toEqual(["0", "950", "1k", "92.1k", "640k", "1.2M", "12M"])
    expect(tokenCount(null)).toBe("n/a")
  })

  it("picks the unit after rounding: 999,999 is 1M, never 1000k", () => {
    expect([999_499, 999_500, 999_999, 99_949, 99_950, 1_949_999, 1_999_999, 99_960_000].map(tokenCount))
      .toEqual(["999k", "1M", "1M", "99.9k", "100k", "1.9M", "2M", "100M"])
  })
})

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

describe("an agent's name (#199)", () => {
  const nats = "oudzj4bhpl62iahctur5s5sf4"

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
