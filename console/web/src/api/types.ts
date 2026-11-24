// The console API as ADR-15 point 10 defines it (docs/arc42, section 9):
// the same shapes the agents' tools return, so a number means the same in
// both. The console server (Python, ADR-015's decided point) will serve
// them; this app is built against a mock (mock.ts) or a real server
// (http.ts) behind the same ConsoleApi interface.

/** An agent's state, as the presence lease keeps it (ADR-015 point 5,
 * quoting lease.py): `offline` when the lease ran out. WAITING, BLOCKED and
 * BYE are words of posts, not states. */
export type AgentState = "working" | "listening" | "bus" | "idle" | "silent" | "offline"

export const AGENT_STATES: readonly AgentState[] = [
  "working", "listening", "bus", "idle", "silent", "offline",
]

/** The card's harness decides the logo: these, else "other" (ADR-015 point 6). */
export type Harness =
  | "claude-code" | "opencode" | "cursor" | "codex" | "pi" | "other"

export const HARNESS_LABEL: Record<Harness, string> = {
  "claude-code": "Claude Code",
  opencode: "OpenCode",
  cursor: "Cursor",
  codex: "Codex",
  pi: "pi",
  other: "other",
}

/** The Kanban card (ADR-015 point 6): what sits on the card itself. */
export interface AgentCard {
  /** The agent's name on the bus. */
  name: string
  home: string
  state: AgentState
  /** The crown is a mark on the card, not a column (ADR-015 point 6). */
  crown: boolean
  harness: Harness
  model: string
  /** One line; customers as placeholders in production. */
  task: string
  role: "coordinator" | "member"
  /** The responsible human, with `autonomous` for a session without one. */
  for: string
  autonomous: boolean
  trusts: string[]
  capabilities: string[]
  project: string
  branch: string | null
  pr: { number: number; url: string } | null
  /** Percent, or null when the bus does not carry it yet: unknown, never 0. */
  ctx: number | null
  /** The limits or the cost, when the card carries it. */
  limits: string | null
  version: string
  /** Its open asks; `asksForOperator` of those are the operator's. */
  openAsks: number
  asksForOperator: number
  /** Since the HELLO, and the last post (both the servers's clock). */
  helloAt: number
  lastPostAt: number | null
  /** The task rows it holds, shown in the detail only (point 6): the card
   * carries them; the API has no endpoint of its own. */
  tasks: TaskRow[]
}

/** The task rows the agent holds, in the detail only (coordinator("tasks")). */
export interface TaskRow {
  id: string
  title: string
  by: string
  /** The agent that holds it, when one does. */
  agent: string | null
  status: string
  state: string
  spec: string | null
  pr: string | null
  branch: string | null
  at: number
}

/** What `GET /v1/fleet` streams after the cards: the changes. */
export type FleetEvent =
  | { type: "snapshot"; cards: AgentCard[] }
  | { type: "card"; card: AgentCard }
  | { type: "drop"; name: string }

/** An ask, as the rows of coordinator("tasks") hold them (RT-6.14 step 3). */
export interface Ask {
  id: string
  /** The agent that asked. */
  by: string
  /** The asked human, null when it waits on no listed human. */
  human: string | null
  need: string
  answers: string[]
  at: number
  end: "open" | "answered" | "resolved" | "expired"
  /** The strongest mark: its asked human is the operator (ADR-015 point 4). */
  forMe: boolean
  /** Weaker: an ask to someone else by an agent whose owner or trusts name
   * the operator. */
  mayAnswer: boolean
  /** The thread the ask sits in, for the detail (GET /v1/threads/<id>). */
  threadId: string
}

/** A thread from the server's memory, paged, when the detail opens. */
export interface Post {
  id: string
  /** The thread's root id when this is a reply. */
  rootId: string | null
  author: string
  at: number
  /** The bus word first when the post carries one. */
  message: string
}

export interface ThreadPage {
  rootId: string
  posts: Post[]
  /** The page that follows, null on the last. */
  next: boolean
}

/** The graph (ADR-015 point 7): people, channels (homes), agents. */
export type GraphNode = {
  id: string
  kind: "person" | "channel" | "agent"
  label: string
  /** For an agent: the crown mark on its node. */
  crown?: boolean
  /** For an agent: its harness logo on the node. */
  harness?: Harness
  state?: AgentState
}

export interface GraphEdge {
  id: string
  from: string
  to: string
  kind: "trusts" | "coordinates"
  /** Shown where the depth of two cuts an edge away. */
  cut?: boolean
}

export interface GraphData {
  nodes: GraphNode[]
  edges: GraphEdge[]
  /** True when the shown graph is cut at depth two (a mark says where). */
  cutDepth: 2 | null
}

/** A report field: counts and seconds only, no names, no text
 * (ADR-015 point 8). A field a line does not have shows as "n/a",
 * never as 0 — so a missing value is null. */
export interface ReportField {
  label: string
  value: number | null
  unit: "count" | "s" | "%"
}

export interface ReportBar {
  label: string
  value: number
}

export interface Report {
  id: string
  title: string
  window: "live" | "24 h" | "90 d"
  /** Which export: when a value should draw attention. */
  warn?: string
  fields: ReportField[]
  bars?: ReportBar[]
}

/** The whole API the app needs (ADR-015 point 10). */
export interface ConsoleApi {
  /** The cards once, then changes over SSE — one direction of push is
   * enough (ADR-015 point 10). watchFleet delivers snapshot and changes; it
   * returns the stop function. */
  watchFleet(onEvent: (event: FleetEvent) => void): () => void
  asks(): Promise<Ask[]>
  graph(): Promise<GraphData>
  stats(): Promise<Report[]>
  thread(rootId: string): Promise<ThreadPage>
}
