// The console API as ADR-15 point 10 defines it (docs/arc42, section 9):
// the same shapes the agents' tools return, so a number means the same in
// both. The console server (Python, ADR-015's decided point) will serve
// them; this app is built against a mock (mock.ts) or a real server
// (http.ts) behind the same ConsoleApi interface.
//
// The wire contract (what the server sends) is snake_case where the ADR
// spells it that way (`for_me`, `may_answer`, `hello_post_id`,
// `display_name`); `http.ts` maps the wire to the app's camelCase types —
// the mock speaks the app's shapes directly.

/** An agent's state, as the presence lease keeps it (ADR-015 point 5,
 * quoting lease.py): `offline` when the lease ran out, no fresh presence.
 * Until presence carries the state words it carries format v1's
 * `online`/`away` instead — so both spellings are heard, and a state the
 * console does not know lands in the Kanban's catch-all (point 6).
 * WAITING, BLOCKED and BYE are words of posts, not states. */
export type AgentState =
  | "working" | "listening" | "bus" | "idle" | "silent" | "offline"
  | "online" | "away"

/** The Kanban's column order; anything else falls into the "other" column. */
export const AGENT_STATES: readonly AgentState[] = [
  "working", "listening", "bus", "online", "away", "idle", "silent", "offline",
]

/** The card's harness decides the logo: these, else "other" (ADR-015 point 6). */
// Held equal to natsbus.HARNESSES and stats.RUNTIMES by mcp/tests/test_console_records.py.
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

/** How the fleet stream stands — one state for the whole console, shown in
 * the top bar; every view's failed load shows in the notice under it. */
export type ConnState = "connecting" | "live" | "reconnecting" | "syncing" | "error"

/** The operator the console is logged in as (`GET /v1/me`; the server runs
 * the Keycloak authorization-code flow with PKCE, the browser holds a
 * same-origin session cookie). */
export interface Operator {
  username: string
  displayName: string
  groups: string[]
  /** The console's own build, for the footer (#182). */
  build: BuildInfo
}

/** The console's release version and source commit, written at image build
 * (#182); null when the server has none (a development run). */
export interface BuildInfo {
  version: string | null
  revision: string | null
}

/** An agent's card (ADR-015 point 6): what the agents list and the detail
 * page show of it. */
export interface AgentCard {
  /** The agent's name on the bus: the stable key (on NATS `agent-` and a
   * 26-character hash, the subject's last token). */
  name: string
  /** What the card shows: the bot's username the agent states (the name
   * coordinator("who") shows), else the short bus name. */
  displayName: string
  home: string
  /** The channels it visits beside its home (RT-6.16): the graph draws them
   * apart from the home edge. Empty when it has none. */
  guests: string[]
  state: AgentState
  /** The crown is a mark on the card, not a column (ADR-015 point 6). */
  crown: boolean
  /** null: not on the wire (an older MCP server states no card's data in its
   * presence). Harness, model, task, branch and open asks are self-stated
   * (RT-6.37): shown as text, never trusted. */
  harness: Harness | null
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
  /** null: not stated (n/a); "": the agent stated no branch. */
  branch: string | null
  pr: { number: number; url: string } | null
  /** Percent, or null when the bus does not carry it yet: unknown, never 0. */
  ctx: number | null
  /** The limits or the cost, when the card carries it. */
  limits: string | null
  version: string
  /** Its open asks (its own asks to a human still waiting, as its presence
   * states them), `asksForOperator` of those the operator's; `null` when not
   * stated (unknown is never guessed as 0). */
  openAsks: number | null
  asksForOperator: number | null
  /** Since the HELLO, and the last post (both the servers's clock). */
  helloAt: number
  lastPostAt: number | null
  /** The post id of its HELLO: opens the HELLO thread in the detail. */
  helloPostId: string
  /** The task rows it holds, shown in the detail only (point 6): the card
   * carries them; the API has no endpoint of its own. */
  tasks: TaskRow[]
}

/** The task rows the agent holds, in the detail only (coordinator("tasks")). */
export interface TaskRow {
  id: string
  title: string
  by: string
  /** The holder's short name, where the server sends one. */
  displayName?: string
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

/** An open ask, from the record the asking agent publishes on NATS
 * (RT-6.43 step 6): a title, never its body. The wire sends `for_me`/
 * `may_answer`/`agent_gone`; the app speaks `forMe`/`mayAnswer`/`agentGone`. */
export interface Ask {
  /** The ask's Mattermost post id. */
  id: string
  /** The agent that asked, long and short. */
  by: string
  displayName: string
  /** The asking agent's home channel. */
  home: string
  kind: "approval" | "question"
  /** The asked human by name, null when it waits on no listed human. */
  human: string | null
  /** The ask's title line, plain text (at most 200 characters). */
  need: string
  at: number
  /** `expired` after 24 h is a hint: the asking server decides. */
  end: "open" | "answered" | "expired"
  /** The strongest mark: its asked human is the operator's anchor (ADR-015
   * point 4); null (n/a) until the login's own record, which gives the anchor,
   * has arrived. */
  forMe: boolean | null
  /** Weaker: an ask to someone else by an agent whose owner or trusts name
   * the operator. */
  mayAnswer: boolean | null
  /** The asking agent's presence is not fresh. */
  agentGone: boolean
  /** The thread the ask sits in, for the detail (GET /v1/threads/<id>). */
  threadId: string
}

/** A thread from the server's memory, paged, when the detail opens
 * (GET /v1/threads/<id>): `next` is the next cursor, null on the last page. */
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
  /** The next page's cursor, null on the last page. */
  next: string | null
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
  /** For a person: the logged-in operator's own node (ADR-015 point 7),
   * marked from the login's own record's anchor — never from the browser. */
  you?: boolean
}

export interface GraphEdge {
  id: string
  from: string
  to: string
  /** `coordinates`: an agent's home channel; `guest`: a channel it only
   * visits (RT-6.16). */
  kind: "trusts" | "coordinates" | "guest"
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

/** The three categories of the token split (RT-6.9 step 3). */
export type TokenCategory = "task" | "overhead" | "coordination"

/** One segment of a stacked bar: its value and its share of the bar,
 * within 0 to 100 (null when the bar has no tokens at all). */
export interface ReportSegment {
  category: TokenCategory
  value: number
  percent: number | null
}

/** One stacked bar (the tokens by category): the three categories in
 * input-token equivalents (`segments`) and in raw tokens (`raw`).
 * `estimated`: the split is an estimate, the bar says so. */
export interface ReportStack {
  label: string
  estimated: boolean
  segments: ReportSegment[]
  raw: ReportSegment[]
}

export interface Report {
  id: string
  title: string
  window: "live" | "24 h" | "7 d" | "90 d"
  /** Which export: when a value should draw attention. */
  warn?: string
  /** One line on how the numbers come about (an estimate's method). */
  note?: string
  fields: ReportField[]
  bars?: ReportBar[]
  stacks?: ReportStack[]
}

/** The whole API the app needs (ADR-015 point 10). */
export interface ConsoleApi {
  /** `GET /v1/me`: the logged-in operator. */
  me(): Promise<Operator>
  /** The cards once, then changes over SSE — one direction of push is
   * enough (ADR-015 point 10). watchFleet delivers snapshot and changes;
   * `onState` reports the stream itself when it changes; it returns the
   * stop function. */
  watchFleet(onEvent: (event: FleetEvent) => void,
             onState?: (state: ConnState) => void): () => void
  asks(): Promise<Ask[]>
  graph(): Promise<GraphData>
  stats(): Promise<Report[]>
  thread(rootId: string, next?: string | null): Promise<ThreadPage>
}
