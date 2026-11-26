// The graph view's model (#145): data in, React Flow nodes and edges out, and
// live updates that keep the picture still where nothing moved.
import { afterEach, describe, expect, it, vi } from "vitest"
import type { Edge, Node } from "@xyflow/react"
import type { AgentCard, AgentState, Ask, GraphData } from "../../api/types"
import { layout, NODE_SIZE, reconcile, toFlow, type FlowData, type GraphInput } from "./model"
import { mockApi } from "../../api/mock"

const HOME = "~ai-agent-bus-dev"

const card = (name: string, over: Partial<AgentCard> = {}): AgentCard => ({
  name, displayName: name.slice(-6), home: HOME, guests: [], state: "working", crown: false,
  harness: "claude-code", model: "Opus 5.5", task: "a task", role: "member",
  for: "dembaca", autonomous: false, trusts: ["dembaca"], capabilities: [],
  project: "dembaca/ai-agent-bus", branch: null, pr: null, ctx: null,
  limits: null, version: "0.97.0", openAsks: 0, asksForOperator: 0,
  helloAt: 0, lastPostAt: null, helloPostId: `hello-${name}`, tasks: [],
  ...over,
})

const ask = (id: string, by: string, end: Ask["end"] = "open"): Ask => ({
  id, by, displayName: by.slice(-6), home: HOME.slice(1), kind: "question",
  human: "dembaca", need: "a question", at: 0, end, forMe: true,
  mayAnswer: true, agentGone: false, threadId: id,
})

/** People and channels only: the live agents come from the cards. */
const graph: GraphData = {
  nodes: [
    { id: "dembaca", kind: "person", label: "dembaca" },
    { id: HOME, kind: "channel", label: HOME },
    { id: "~chat-next", kind: "channel", label: "~chat-next" },
  ],
  edges: [],
  cutDepth: null,
}

const A = "agent-mac-ai-agent-aaaaaa"
const B = "agent-mac-ai-agent-bbbbbb"
const C = "agent-mac-ai-agent-cccccc"
const JOINER = "agent-mac-ai-agent-dddddd"
const ASK_ID = "8xqk3b1zwtrd5mfy6nqpe4hc1o"

const base = (): GraphInput => ({
  graph,
  cards: [
    card(A, { crown: true }),
    card(B, { harness: "opencode", state: "listening" }),
    card(C, { home: "~chat-next", state: "idle" }),
  ],
  asks: [],
})

/** What the view holds after the first load: laid out. */
const laidOut = (input: GraphInput) => {
  const flow = toFlow(input)
  return { nodes: layout(flow.nodes, flow.edges), edges: flow.edges }
}

const step = (prev: { nodes: Node[]; edges: Edge[] }, input: GraphInput) => {
  const next = toFlow(input)
  return reconcile(prev.nodes, prev.edges, next.nodes, next.edges)
}

const byId = (nodes: Node[]) => new Map(nodes.map((n) => [n.id, n]))
const ids = (items: { id: string }[]) => items.map((i) => i.id).sort()
const touching = (edges: Edge[], id: string) =>
  edges.filter((e) => e.source === id || e.target === id)

/** Every node of `prev` still in `next` sits where it sat. */
const expectPositionsKept = (prev: Node[], next: Node[]) => {
  const now = byId(next)
  for (const node of prev) {
    const kept = now.get(node.id)
    if (kept) expect(kept.position, node.id).toEqual(node.position)
  }
}

describe("toFlow", () => {
  it("takes a person id the server already prefixed as it is (#147)", () => {
    // The graph endpoint sends `person:<anchor>` since #147; an ask names its
    // human without the prefix. One node either way, never `person:person:`.
    const g: GraphData = {
      ...graph,
      nodes: [...graph.nodes, { id: A, kind: "agent", label: "aaaaaa", state: "working" },
        { id: "person:sub-alice", kind: "person", label: "alice" }],
      edges: [{ id: "t1", from: A, to: "person:sub-alice", kind: "trusts" }],
    }
    const { nodes, edges } = toFlow({ ...base(), graph: g })
    const all = ids(nodes)
    expect(all).toContain("person:sub-alice")
    expect(all.some((id) => id.startsWith("person:person:"))).toBe(false)
    const trust = edges.find((e) => (e.data as { kind: string }).kind === "trusts")
    // edges are drawn left to right, so the person is the source
    expect([trust?.source, trust?.target]).toEqual(["person:sub-alice", `agent:${A}`])
  })

  it("names nodes by kind and starts them at the origin", () => {
    const { nodes, edges } = toFlow({ ...base(), asks: [ask(ASK_ID, A)] })
    const all = ids(nodes)
    expect(all).toEqual(expect.arrayContaining([
      `agent:${A}`, `agent:${B}`, `agent:${C}`, `ask:${ASK_ID}`, "person:dembaca",
    ]))
    expect(all.some((id) => id.startsWith("channel:"))).toBe(true)
    for (const n of nodes) expect(n.position).toEqual({ x: 0, y: 0 })
    for (const n of nodes) {
      expect(n.data).toHaveProperty("kind")
      expect(n.data).toHaveProperty("label")
    }
    // Every edge joins two nodes that exist.
    const present = new Set(all)
    for (const e of edges) {
      expect(present.has(e.source), e.id).toBe(true)
      expect(present.has(e.target), e.id).toBe(true)
    }
  })

  it("carries an agent's state and harness on its node", () => {
    const nodes = byId(toFlow(base()).nodes)
    expect(nodes.get(`agent:${B}`)?.data.state).toBe("listening")
    expect(nodes.get(`agent:${B}`)?.data.harness).toBe("opencode")
    expect(nodes.get(`agent:${C}`)?.data.state).toBe("idle")
  })

  it("joins an agent to its home channel", () => {
    const { edges } = toFlow(base())
    const homes = touching(edges, `agent:${A}`)
      .flatMap((e) => [e.source, e.target])
      .filter((id) => id.startsWith("channel:"))
    expect(homes.length).toBeGreaterThan(0)
  })

  it("puts the crown on exactly the holder", () => {
    const crowned = toFlow(base()).nodes.filter((n) => n.data.crown === true)
    expect(crowned.map((n) => n.id)).toEqual([`agent:${A}`])
  })

  it("moves the crown with the cards", () => {
    const input = base()
    input.cards = input.cards.map((c) => ({ ...c, crown: c.name === B }))
    const crowned = toFlow(input).nodes.filter((n) => n.data.crown === true)
    expect(crowned.map((n) => n.id)).toEqual([`agent:${B}`])
  })

  it("shows open asks only, each joined to its agent", () => {
    const { nodes, edges } = toFlow({
      ...base(),
      asks: [ask(ASK_ID, A), ask("answeredaskid0000000000000", B, "answered"),
             ask("expiredaskid00000000000000", B, "expired")],
    })
    const asks = nodes.filter((n) => n.id.startsWith("ask:")).map((n) => n.id)
    expect(asks).toEqual([`ask:${ASK_ID}`])
    const ends = touching(edges, `ask:${ASK_ID}`).flatMap((e) => [e.source, e.target])
    expect(ends).toContain(`agent:${A}`)
  })

  it("draws the mock's data without dangling edges", async () => {
    const api = mockApi()
    let cards: AgentCard[] = []
    const stop = api.watchFleet((e) => { if (e.type === "snapshot") cards = e.cards })
    stop()
    const { nodes, edges } = toFlow({ graph: await api.graph(), cards, asks: await api.asks() })
    const present = new Set(nodes.map((n) => n.id))
    for (const c of cards) expect(present.has(`agent:${c.name}`), c.name).toBe(true)
    for (const e of edges) {
      expect(present.has(e.source), `${e.id} source ${e.source}`).toBe(true)
      expect(present.has(e.target), `${e.id} target ${e.target}`).toBe(true)
    }
    expect(new Set(nodes.map((n) => n.id)).size).toBe(nodes.length)
    expect(new Set(edges.map((e) => e.id)).size).toBe(edges.length)
  })
})

describe("layout", () => {
  it("is deterministic: the same input gives the same positions", () => {
    const input = { ...base(), asks: [ask(ASK_ID, A)] }
    const one = laidOut(input).nodes
    const two = laidOut(input).nodes
    expect(two.map((n) => [n.id, n.position])).toEqual(one.map((n) => [n.id, n.position]))
  })

  it("spreads the nodes out (no two on one spot)", () => {
    const nodes = laidOut({ ...base(), asks: [ask(ASK_ID, A)] }).nodes
    const spots = new Set(nodes.map((n) => `${n.position.x},${n.position.y}`))
    expect(spots.size).toBe(nodes.length)
    for (const n of nodes) {
      expect(Number.isFinite(n.position.x)).toBe(true)
      expect(Number.isFinite(n.position.y)).toBe(true)
    }
  })
})

describe("reconcile", () => {
  it("a status change recolors in place: same positions, no relayout", () => {
    const prev = laidOut(base())
    const input = base()
    input.cards = input.cards.map((c) => c.name === B ? { ...c, state: "working" as AgentState } : c)
    const out = step(prev, input)
    expect(out.relayout).toBe(false)
    expect(ids(out.nodes)).toEqual(ids(prev.nodes))
    expectPositionsKept(prev.nodes, out.nodes)
    expect(byId(out.nodes).get(`agent:${B}`)?.data.state).toBe("working")
    expect(byId(prev.nodes).get(`agent:${B}`)?.data.state).toBe("listening")
    expect(ids(out.edges)).toEqual(ids(prev.edges))
  })

  it("an agent joining adds its node, asks for a relayout and moves no old node", () => {
    const prev = laidOut(base())
    const input = base()
    input.cards = input.cards.concat(card(JOINER, { harness: "cursor" }))
    const out = step(prev, input)
    expect(out.relayout).toBe(true)
    expect(byId(out.nodes).has(`agent:${JOINER}`)).toBe(true)
    expect(out.nodes.length).toBe(prev.nodes.length + 1)
    expectPositionsKept(prev.nodes, out.nodes)
    expect(touching(out.edges, `agent:${JOINER}`).length).toBeGreaterThan(0)
  })

  it("an agent leaving removes its node and its edges, the rest stays put", () => {
    const prev = laidOut({ ...base(), asks: [ask(ASK_ID, A)] })
    expect(touching(prev.edges, `agent:${B}`).length).toBeGreaterThan(0)
    const input = { ...base(), asks: [ask(ASK_ID, A)] }
    input.cards = input.cards.filter((c) => c.name !== B)
    const out = step(prev, input)
    expect(byId(out.nodes).has(`agent:${B}`)).toBe(false)
    expect(touching(out.edges, `agent:${B}`)).toEqual([])
    expectPositionsKept(prev.nodes, out.nodes)
  })

  it("an agent leaving that the graph still lists is gone too (the cards are live)", async () => {
    const api = mockApi()
    let cards: AgentCard[] = []
    api.watchFleet((e) => { if (e.type === "snapshot") cards = e.cards })()
    const g = await api.graph()
    const gone = "agent-mac-ai-agent-bb8f09"
    const prev = laidOut({ graph: g, cards, asks: [] })
    expect(byId(prev.nodes).has(`agent:${gone}`)).toBe(true)
    const out = step(prev, { graph: g, cards: cards.filter((c) => c.name !== gone), asks: [] })
    expect(byId(out.nodes).has(`agent:${gone}`)).toBe(false)
    expect(touching(out.edges, `agent:${gone}`)).toEqual([])
  })

  it("an ask opening adds its node and edge; closing removes both", () => {
    const prev = laidOut(base())
    const opened = step(prev, { ...base(), asks: [ask(ASK_ID, A)] })
    expect(byId(opened.nodes).has(`ask:${ASK_ID}`)).toBe(true)
    expect(touching(opened.edges, `ask:${ASK_ID}`).length).toBeGreaterThan(0)
    expectPositionsKept(prev.nodes, opened.nodes)

    const held = { nodes: opened.nodes, edges: opened.edges }
    for (const closed of [[ask(ASK_ID, A, "answered")], []]) {
      const out = step(held, { ...base(), asks: closed })
      expect(byId(out.nodes).has(`ask:${ASK_ID}`)).toBe(false)
      expect(touching(out.edges, `ask:${ASK_ID}`)).toEqual([])
      expectPositionsKept(held.nodes, out.nodes)
    }
  })

  it("no change: no relayout, nothing moves", () => {
    const prev = laidOut(base())
    const out = step(prev, base())
    expect(out.relayout).toBe(false)
    expectPositionsKept(prev.nodes, out.nodes)
    expect(ids(out.nodes)).toEqual(ids(prev.nodes))
  })
})

/** No two nodes that share a column overlap (their x ranges meet and their
 * y ranges, with the layout's sizes, meet). */
const expectNoOverlap = (nodes: Node[], what = "") => {
  const size = (n: Node) => NODE_SIZE[(n.data as FlowData).kind]
  for (let i = 0; i < nodes.length; i++) {
    for (let j = i + 1; j < nodes.length; j++) {
      const [a, b] = [nodes[i], nodes[j]]
      const [sa, sb] = [size(a), size(b)]
      const xMeet = a.position.x < b.position.x + sb.width && b.position.x < a.position.x + sa.width
      const yMeet = a.position.y < b.position.y + sb.height && b.position.y < a.position.y + sa.height
      expect(xMeet && yMeet, `${what} ${a.id} overlaps ${b.id}`).toBe(false)
    }
  }
}

describe("reconcile: a joining node takes a free slot", () => {
  afterEach(() => { vi.useRealTimers() })

  it("agents a, c, e, then b joins: b is not stacked on a neighbour", () => {
    const name = (l: string) => `agent-mac-ai-agent-${l.repeat(6)}`
    const three = { graph, cards: ["a", "c", "e"].map((l) => card(name(l))), asks: [] }
    const prev = laidOut(three)
    expectNoOverlap(prev.nodes, "before")
    const out = step(prev, { ...three, cards: ["a", "b", "c", "e"].map((l) => card(name(l))) })
    expect(out.relayout).toBe(true)
    expectPositionsKept(prev.nodes, out.nodes)
    expectNoOverlap(out.nodes, "after b joined")
  })

  it("an ask opening between asks takes a free slot too", () => {
    const prev = laidOut({ ...base(), asks: [ask("a".repeat(26), A), ask("c".repeat(26), C)] })
    const out = step(prev, { ...base(), asks: ["a", "b", "c"].map((l, i) => ask(l.repeat(26), [A, B, C][i])) })
    expectPositionsKept(prev.nodes, out.nodes)
    expectNoOverlap(out.nodes, "after the ask opened")
  })

  it("the mock's live sequence (the visitor joins and leaves, an ask opens and closes) never overlaps", async () => {
    vi.useFakeTimers()
    const api = mockApi()
    const g = await api.graph()
    let cards: AgentCard[] = []
    const stop = api.watchFleet((e) => {
      if (e.type === "snapshot") cards = e.cards
      else if (e.type === "card") cards = cards.filter((c) => c.name !== e.card.name).concat(e.card)
      else cards = cards.filter((c) => c.name !== e.name)
    })
    let shown = laidOut({ graph: g, cards, asks: await api.asks(), fleetSeen: true })
    expectNoOverlap(shown.nodes, "first")
    let joined = false
    for (let i = 0; i < 40; i++) {
      vi.advanceTimersByTime(4000)
      const before = shown.nodes
      const out = step(shown, { graph: g, cards, asks: await api.asks(), fleetSeen: true })
      expectPositionsKept(before, out.nodes)
      expectNoOverlap(out.nodes, `step ${i}`)
      if (out.nodes.some((n) => n.id === "agent:agent-mac-ai-agent-e41a07")) joined = true
      shown = out
    }
    stop()
    expect(joined).toBe(true)
  })
})

describe("toFlow: the operator's own node (#148)", () => {
  it("carries `you` from the graph endpoint's person node", () => {
    const g: GraphData = {
      ...graph,
      nodes: [...graph.nodes.filter((n) => n.kind !== "person"),
        { id: "person:dembaca", kind: "person", label: "dembaca", you: true },
        { id: "person:sub-alice", kind: "person", label: "alice" }],
    }
    const { nodes } = toFlow({ ...base(), graph: g, cards: [card(A)], asks: [] })
    const people = nodes.filter((n) => (n.data as FlowData).kind === "person")
    expect(people.map((n) => [n.id, (n.data as FlowData).you === true])).toEqual([
      ["person:dembaca", true], ["person:sub-alice", false]])
  })

  it("marks no person from an ask: a human's name is no anchor", () => {
    const g: GraphData = { ...graph, nodes: graph.nodes.filter((n) => n.kind !== "person") }
    const { nodes } = toFlow({ ...base(), graph: g, asks: [ask(ASK_ID, A)] })
    const person = nodes.find((n) => n.id === "person:dembaca")
    expect((person?.data as FlowData).you).toBeUndefined()
  })
})

describe("toFlow: the fleet's first snapshot and gone agents", () => {
  const withAgent: GraphData = {
    ...graph,
    nodes: graph.nodes.concat({ id: A, kind: "agent", label: "aaaaaa", state: "working" }),
  }

  it("before the first snapshot the graph's agents stand in", () => {
    const ids = toFlow({ graph: withAgent, cards: [], asks: [] }).nodes.map((n) => n.id)
    expect(ids).toContain(`agent:${A}`)
  })

  it("after the first snapshot an empty fleet shows no agent", () => {
    const ids = toFlow({ graph: withAgent, cards: [], asks: [], fleetSeen: true }).nodes.map((n) => n.id)
    expect(ids.some((id) => id.startsWith("agent:"))).toBe(false)
  })

  it("an open ask whose agent is gone stays, joined to its human only", () => {
    const gone = "agent-mac-ai-agent-gonegone"
    const { nodes, edges } = toFlow({ ...base(), asks: [ask(ASK_ID, gone)], fleetSeen: true })
    expect(nodes.map((n) => n.id)).toContain(`ask:${ASK_ID}`)
    const ends = touching(edges, `ask:${ASK_ID}`).flatMap((e) => [e.source, e.target])
    expect(ends).toContain("person:dembaca")
    expect(ends).not.toContain(`agent:${gone}`)
  })
})
