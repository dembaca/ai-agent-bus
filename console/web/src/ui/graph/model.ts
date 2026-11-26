import dagre from "@dagrejs/dagre"
import type { Edge, Node } from "@xyflow/react"
import type { AgentCard, AgentState, Ask, GraphData, Harness } from "../../api/types"

/** What the graph view is drawn from: the graph endpoint (people, channels,
 * agents, trusts and coordinates), the fleet's live cards and the open asks.
 * No field of its own on the wire (issue #145). */
export type GraphInput = {
  graph: GraphData | null
  cards: AgentCard[]
  asks: Ask[]
  /** True once the fleet stream's first snapshot came: from then on the
   * cards alone say which agents there are, also when there are none.
   * Left out it counts as true once any card is there. */
  fleetSeen?: boolean
}

export type FlowKind = "person" | "ask" | "agent" | "channel"

/** What a custom node renders. `kind` doubles as the React Flow node type. */
export type FlowData = {
  kind: FlowKind
  label: string
  /** agent */
  state?: AgentState
  harness?: Harness | null
  crown?: boolean
  /** channel: some agent's home, else a guest channel */
  role?: "home" | "guest"
  /** ask */
  askKind?: Ask["kind"]
  forMe?: boolean | null
  by?: string
}

export type FlowEdgeKind = "member" | "guest" | "ask" | "asked" | "trusts" | "coordinates"

/** The column a kind stands in, left to right: you, asks, agents, homes. */
const COLUMN: Record<FlowKind, number> = { person: 0, ask: 1, agent: 2, channel: 3 }
const COLUMN_GAP = 260

/** Node sizes for the layout; the CSS gives the nodes the same width. */
export const NODE_SIZE: Record<FlowKind, { width: number; height: number }> = {
  person: { width: 150, height: 40 },
  ask: { width: 190, height: 64 },
  agent: { width: 170, height: 56 },
  channel: { width: 170, height: 36 },
}

/** Channels arrive with and without the `~` (cards: `~home`, asks: `home`). */
const channelName = (name: string): string => name.replace(/^~/, "")
const channelId = (name: string): string => `channel:${channelName(name)}`
const agentId = (name: string): string => `agent:${name}`
const personId = (name: string): string => `person:${name}`
const askId = (id: string): string => `ask:${id}`

const kindOf = (node: Node): FlowKind => (node.data as FlowData).kind

/** The space between two nodes of one column, as dagre's `nodesep`. */
const NODE_GAP = 18

/** The graph's nodes and edges from the three sources. Every id is derived
 * from a name on the wire, so the same input gives the same ids. Positions
 * are all 0,0: `layout` or `reconcile` places them. */
export function toFlow(input: GraphInput): { nodes: Node[]; edges: Edge[] } {
  const { graph, cards, asks } = input
  // Before the first snapshot the graph's agent nodes stand in; after it
  // the cards are the live truth, an empty fleet included.
  const live = input.fleetSeen === true || cards.length > 0
  const nodes = new Map<string, Node>()
  const edges = new Map<string, Edge>()
  const add = (id: string, data: FlowData) => {
    if (!nodes.has(id)) nodes.set(id, { id, type: data.kind, position: { x: 0, y: 0 }, data, connectable: false })
  }
  const link = (kind: FlowEdgeKind, from: string, to: string, extra: Partial<Edge> = {}) => {
    if (from === to || !nodes.has(from) || !nodes.has(to)) return
    // Drawn left to right whatever the edge's direction, so the line runs
    // from a column to the next and not back across the node.
    const [source, target] = COLUMN[kindOf(nodes.get(from)!)] <= COLUMN[kindOf(nodes.get(to)!)]
      ? [from, to] : [to, from]
    const id = `${kind}:${from}->${to}`
    if (edges.has(id)) return
    edges.set(id, {
      id, source, target, type: "default", className: `graph__edge graph__edge--${kind}`,
      data: { kind, from, to }, ...extra,
    })
  }

  // The graph endpoint's raw ids, mapped to the prefixed ones.
  const raw = new Map<string, string>()
  const homes = new Set(cards.map((c) => channelName(c.home)))
  for (const n of graph?.nodes ?? []) {
    if (n.kind === "person") {
      raw.set(n.id, personId(n.id))
      add(personId(n.id), { kind: "person", label: n.label })
    } else if (n.kind === "channel") {
      raw.set(n.id, channelId(n.id))
    } else {
      raw.set(n.id, agentId(n.id))
    }
  }

  // Agents: the fleet's cards are the live truth; before the first snapshot
  // the graph's agent nodes stand in, so the view is not empty meanwhile.
  if (live) {
    for (const c of cards) {
      add(agentId(c.name), {
        kind: "agent", label: c.displayName || c.name, state: c.state, harness: c.harness, crown: c.crown,
      })
    }
  } else {
    for (const n of graph?.nodes ?? []) {
      if (n.kind !== "agent") continue
      add(agentId(n.id), {
        kind: "agent", label: n.label, state: n.state, harness: n.harness ?? null, crown: n.crown ?? false,
      })
    }
  }

  // Channels: every home of a shown agent, and the graph's channels.
  for (const c of cards) add(channelId(c.home), { kind: "channel", label: `~${channelName(c.home)}`, role: "home" })
  for (const n of graph?.nodes ?? []) {
    if (n.kind !== "channel") continue
    const name = channelName(n.id)
    // A guest channel only once an agent is a guest there (below); the
    // graph endpoint's channels are homes (of agents maybe not in the fleet).
    add(channelId(n.id), { kind: "channel", label: `~${name}`, role: "home" })
  }

  // Open asks, and the human each waits on. An ask whose agent is gone
  // still waits on its human: it stays, joined to the human only.
  const open = asks.filter((a) => a.end === "open")
  for (const a of open) {
    if (a.human) add(personId(a.human), { kind: "person", label: a.human })
    add(askId(a.id), {
      kind: "ask", label: a.need, askKind: a.kind, forMe: a.forMe, by: a.displayName || a.by,
    })
  }

  // Edges: membership in the home, the ask to its agent and its human.
  for (const c of cards) link("member", agentId(c.name), channelId(c.home))
  for (const a of open) {
    link("ask", askId(a.id), agentId(a.by), { animated: true })
    if (a.human) link("asked", personId(a.human), askId(a.id))
  }
  // The graph's own edges. An agent to a channel is membership: its home is
  // already drawn from the card, another channel is a guest channel (the wire
  // has no guest list of its own).
  const homeOf = new Map(cards.map((c) => [agentId(c.name), channelId(c.home)]))
  for (const e of graph?.edges ?? []) {
    const from = raw.get(e.from)
    const to = raw.get(e.to)
    if (!from || !to || !nodes.has(from) || !nodes.has(to)) continue
    const kinds = [kindOf(nodes.get(from)!), kindOf(nodes.get(to)!)]
    if (kinds.includes("agent") && kinds.includes("channel")) {
      const [agent, channel] = kinds[0] === "agent" ? [from, to] : [to, from]
      const home = homeOf.get(agent)
      if (home === channel || (home === undefined && !live)) link("member", agent, channel)
      else {
        link("guest", agent, channel)
        const node = nodes.get(channel)!
        if (!homes.has(channelName(channel.slice("channel:".length)))) {
          nodes.set(channel, { ...node, data: { ...(node.data as FlowData), role: "guest" } })
        }
      }
      continue
    }
    link(e.kind, from, to, e.cut ? { className: `graph__edge graph__edge--${e.kind} graph__edge--cut` } : {})
  }

  const order = (a: { id: string }, b: { id: string }) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0)
  return { nodes: [...nodes.values()].sort(order), edges: [...edges.values()].sort(order) }
}

/** Places the nodes in four columns, left to right: you, asks, agents,
 * homes. Dagre (LR) orders the nodes inside each column to keep edges from
 * crossing; the column itself is fixed by the node's kind: every edge gets
 * the column distance as its minimum length, and a hidden root ties each
 * node to its column, so a rank is a column. Edges inside one column (agent
 * coordinates agent) do not take part. */
export function layout(nodes: Node[], edges: Edge[]): Node[] {
  const g = new dagre.graphlib.Graph()
  g.setGraph({ rankdir: "LR", nodesep: 18, ranksep: 80, marginx: 0, marginy: 0 })
  g.setDefaultEdgeLabel(() => ({}))
  const ROOT = "\u0000root"
  g.setNode(ROOT, { width: 1, height: 1 })
  const byId = new Map(nodes.map((n) => [n.id, n]))
  for (const n of nodes) {
    const kind = kindOf(n)
    g.setNode(n.id, { ...NODE_SIZE[kind] })
    g.setEdge(ROOT, n.id, { minlen: COLUMN[kind] + 1, weight: 1 })
  }
  for (const e of edges) {
    const s = byId.get(e.source)
    const t = byId.get(e.target)
    if (!s || !t) continue
    const span = COLUMN[kindOf(t)] - COLUMN[kindOf(s)]
    if (span <= 0) continue
    g.setEdge(e.source, e.target, { minlen: span, weight: 2 })
  }
  dagre.layout(g)
  return nodes.map((n) => {
    const kind = kindOf(n)
    const p = g.node(n.id)
    return {
      ...n,
      position: { x: COLUMN[kind] * COLUMN_GAP, y: Math.round(p.y - NODE_SIZE[kind].height / 2) },
    }
  })
}

type Box = { x: number; y: number; width: number; height: number }

/** Where a node stands and how big it is: React Flow's measure, else the
 * layout's size. */
const box = (n: Node): Box => ({
  x: n.position.x,
  y: n.position.y,
  width: n.measured?.width ?? NODE_SIZE[kindOf(n)].width,
  height: n.measured?.height ?? NODE_SIZE[kindOf(n)].height,
})

/** The y nearest to `want.y` at which `want` overlaps none of `taken` (with
 * NODE_GAP around it): dagre's own place if free, else just above or below
 * a node in the way, else below the lowest. Only nodes whose x range meets
 * want's take part, so a dragged-away node frees its slot. */
const freeY = (want: Box, taken: Box[]): number => {
  const inColumn = taken.filter((t) => t.x < want.x + want.width && want.x < t.x + t.width)
  const fits = (y: number) => inColumn.every((t) =>
    y + want.height + NODE_GAP <= t.y || t.y + t.height + NODE_GAP <= y)
  const candidates = [want.y]
  for (const t of inColumn) {
    candidates.push(t.y - want.height - NODE_GAP, t.y + t.height + NODE_GAP)
  }
  const free = candidates.filter(fits).sort((a, b) => Math.abs(a - want.y) - Math.abs(b - want.y) || a - b)
  // Below every node is always free; the candidates include it.
  return free[0]
}

const sameIds = (a: { id: string }[], b: { id: string }[]): boolean => {
  if (a.length !== b.length) return false
  const ids = new Set(a.map((x) => x.id))
  return b.every((x) => ids.has(x.id))
}

/** Folds a new model into the shown one. The same nodes and edges: next's
 * data (a status recolors in place) at prev's positions, no layout. A node
 * or edge came or went: a new layout, in which the nodes that were there
 * keep where they stood (also where the operator dragged them), so only the
 * new ones take dagre's place and nothing jumps. */
export function reconcile(
  prev: Node[], prevEdges: Edge[], next: Node[], nextEdges: Edge[],
): { nodes: Node[]; edges: Edge[]; relayout: boolean } {
  // A node that stays keeps what React Flow put on it (measured size,
  // selection) besides its position; the data is next's.
  const before = new Map(prev.map((n) => [n.id, n]))
  const keep = (n: Node): Node => {
    const old = before.get(n.id)
    return old ? { ...old, ...n, position: old.position } : n
  }
  if (sameIds(prev, next) && sameIds(prevEdges, nextEdges)) {
    return {
      nodes: next.map(keep),
      edges: nextEdges,
      relayout: false,
    }
  }
  const placed = layout(next, nextEdges)
  // The nodes that stay are held where they were; dagre's places for the new
  // ones assumed their neighbours moved aside, which they did not. So a new
  // node takes the free slot of its column nearest to dagre's place.
  const held = placed.filter((n) => before.has(n.id)).map(keep)
  const fresh = placed.filter((n) => !before.has(n.id))
  const taken = held.map(box)
  for (const n of fresh) {
    const b = box(n)
    const y = freeY(b, taken)
    taken.push({ ...b, y })
    held.push({ ...n, position: { x: b.x, y } })
  }
  const order = new Map(next.map((n, i) => [n.id, i]))
  held.sort((a, b) => order.get(a.id)! - order.get(b.id)!)
  return {
    nodes: held,
    edges: nextEdges,
    relayout: true,
  }
}
