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
  /** The agent's name on the bus: the stable key (on NATS a 25-character
   * hash without a prefix, the subject's last token, #274). */
  name: string
  /** What the card shows: the bot's username the agent states (the name
   * coordinator("who") shows), else the short bus name. */
  displayName: string
  /** The username the agent's presence states, even when another card
   * states it too (#199); "" when it states none. */
  statedName: string
  /** Another card states the same username: the name is not confirmed and
   * shows with a mark. */
  nameShared: boolean
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
  /** Since the HELLO (the server's clock), and the agent's last post in its
   * home, ms: its presence states it (its clock, never later than that
   * presence); `null` when not stated (an older MCP server, no post yet). */
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

/** How the agents list groups its rows (#283): the server groups and counts. */
export type GroupBy = "home" | "repo" | "owner" | "status" | "none"

/** Whose agents the list shows: the operator's own, those that name the
 * operator as owner or trusted ("trusting me"), or everyone's. */
export type Scope = "mine" | "trusting" | "everyone"

/** The numbers the server counts of a set of agents (`GET /v1/groups`, #283);
 * the browser never counts over the presence cards. A token sum is null when
 * no agent counted states one (n/a, never 0); `tokensAgents` of `total`
 * agents are the ones the operator may see per-agent numbers of (own or
 * trusted, ADR-015 point 15) and that state a count: the sum covers those,
 * and the list says "6 of 7 agents" only when it leaves agents out. */
export interface GroupNumbers {
  total: number
  /** Agents by status group: working, listening, idle, offline, other. */
  states: Record<string, number>
  reachable: number
  working: number
  asksOpen: number
  asksForYou: number
  asksMayAnswer: number
  tokens: number | null
  tokensAgents: number
}

/** One group of the list: its key and label, the names of its agents (the
 * rows come from the fleet stream's cards) and its numbers. */
export interface Group extends GroupNumbers {
  key: string
  label: string
  agents: string[]
}

/** The panel's ask tiles, whoever owns the asking agent: the open asks that
 * ask the operator, the open asks to another human by agents that trust the
 * operator, and all open asks in view. */
export interface GlanceAsks { forYou: number; mayAnswer: number; open: number }

/** `GET /v1/groups`: the groups, and the panel's numbers: the operator's own
 * agents, the whole flock and the asks. */
export interface GroupsAnswer {
  by: GroupBy
  scope: Scope
  groups: Group[]
  glance: { mine: GroupNumbers; flock: GroupNumbers; asks: GlanceAsks }
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

/** The time range of the Stats page (#284): `GET /v1/stats?range=`. */
export type RangeKey = "24h" | "7d" | "30d" | "90d" | "12m"

/** One bar of the activity chart: the agents' own posts of one bucket, in the four
 * groups and by every bus word (for the hover). `start` is the bucket's first second
 * (UTC, epoch seconds). */
export interface ActivityBar {
  start: number
  groups: Record<string, number>
  words: Record<string, number>
}

/** The first card (#284): one bar per hour, day, week or month. `unavailable` says
 * why there are no bars (hourly figures need agents that publish hourly). */
export interface Activity {
  bucket: "hour" | "day" | "week" | "month"
  groups: { id: string; label: string }[]
  bars: ActivityBar[]
  unavailable?: string
}

export interface Report {
  id: string
  title: string
  /** The range's label ("24 h", "7 d", "30 d", "90 d", "12 mo"), or "the 7 d before". */
  window: string
  /** Which export: when a value should draw attention. */
  warn?: string
  /** One line on how the numbers come about (an estimate's method). */
  note?: string
  fields: ReportField[]
  bars?: ReportBar[]
  stacks?: ReportStack[]
  activity?: Activity
}

/** One agent's token sums of a window (#201): `all` and the split by type
 * (the detail's "tokens/cache split"), the input's bus overhead and
 * coordination, summed over the same parts as `all`. A type no part states,
 * or an object stated empty, is null — n/a, never 0, the reports' rule; a
 * stated 0 stays 0; a window without any token counts at all is null as a
 * whole. */
export interface TokenSums {
  all: number | null
  in: number | null
  read: number | null
  w5m: number | null
  w1h: number | null
  out: number | null
  ovh: number | null
  coord: number | null
}

/** One row of `GET /v1/agent-stats` (#201): the canvas's per-agent numbers
 * from the STATS parts this session's own consumers deliver (RT-6.43 step 8)
 * — token sums of the last 24 hours and of the 7-day window, the posts,
 * wakes and tool calls of the last 24 hours. Only own or trusted agents have
 * a row: those whose attestation names the logged-in operator as owner or
 * trusted (ADR-015 point 15). "24 hours" are the parts the server received
 * in 24 hours; a long session's part may cover time before. A field no part
 * of the window carries is null, never 0. */
export interface AgentStatsRow {
  agent: string
  tok24h: TokenSums | null
  tok7d: TokenSums | null
  posts24h: number | null
  wakes24h: number | null
  tools24h: number | null
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
  /** `GET /v1/groups`: the groups of the list and the panel's numbers (#283). */
  groups(by: GroupBy, scope: Scope): Promise<GroupsAnswer>
  stats(range: RangeKey): Promise<Report[]>
  agentStats(): Promise<AgentStatsRow[]>
  thread(rootId: string, next?: string | null): Promise<ThreadPage>
}
