// The mock of the console API: the same ConsoleApi the real server will
// serve (ADR-015 point 10), on data shaped like today's bus, so the app can
// be built and shown before the server exists. It changes on its own —
// presence beats and turn ends are seconds apart at this scale — so the
// agents list moves without a backend: states change, an agent
// joins and leaves, an ask opens and closes.
//
// The operator of this console is "dembaca" (Ask.forMe / mayAnswer are
// relative to that name).
import type {
  AgentCard, AgentState, AgentStatsRow, Ask, ConnState, ConsoleApi, FleetEvent, Group, GroupBy, GroupNumbers,
  GroupsAnswer, Operator, Post, Report, ReportStack, Scope, TaskRow, ThreadPage,
} from "./types"

const now = () => Date.now()
const minute = 60_000

const cards = (): CardWire[] => [

  {
    name: "agent-mac-ai-agent-164534", home: "~ai-agent-bus-dev", guests: ["~chat-next"], state: "working",
    crown: true, harness: "claude-code", model: "Opus 5.5",
    task: "coordinate the merge window, the bridge and the console plan",
    role: "coordinator", for: "dembaca", autonomous: false, trusts: ["dembaca"],
    capabilities: ["git", "gh", "pytest", "subagents"], project: "dembaca/ai-agent-bus",
    branch: null, pr: null, ctx: 88, limits: null, version: "0.97.0",
    openAsks: 1, asksForOperator: 1, helloAt: now() - 86 * minute, lastPostAt: now() - 2 * minute,
    tasks: [],
  },
  {
    name: "agent-mac-ai-agent-bb8f09", home: "~ai-agent-bus-dev", guests: ["~bgl-infra"], state: "working",
    crown: false, harness: "opencode", model: "GLM 5.3",
    task: "the console's browser app against a mock of the API",
    role: "member", for: "dembaca", autonomous: false, trusts: ["dembaca"],
    capabilities: ["git", "gh", "pytest", "subagents", "web research"],
    project: "dembaca/ai-agent-bus", branch: "console-web-app",
    pr: { number: 84, url: "https://github.com/dembaca/ai-agent-bus/pull/84" },
    ctx: 47, limits: null, version: "0.97.0",
    openAsks: 0, asksForOperator: 0, helloAt: now() - 165 * minute, lastPostAt: now() - 5 * minute,
    tasks: [],
  },
  {
    name: "agent-mac-ai-agent-d2f798", home: "~ai-agent-bus-dev", guests: [], state: "bus",
    crown: false, harness: "opencode", model: "DeepSeek V4.1 Flash",
    task: "the interim bridge: mirror NATS posts into Mattermost (ADR-014 point 14)",
    role: "member", for: "dembaca", autonomous: false, trusts: ["dembaca"],
    capabilities: ["git", "go", "python", "nats"], project: "dembaca/ai-agent-bus",
    branch: "feat/nats-bridge", pr: null, ctx: 63, limits: null, version: "0.97.0",
    openAsks: 0, asksForOperator: 0, helloAt: now() - 300 * minute, lastPostAt: now() - minute,
    tasks: [],
  },
  {
    name: "agent-mac-ai-agent-6d2afc", home: "~ai-agent-bus-dev", guests: [], state: "working",
    crown: false, harness: "opencode", model: "GLM 5.3",
    task: "an end-to-end run of a NATS home against a local nats-server",
    role: "member", for: "dembaca", autonomous: false, trusts: ["dembaca"],
    capabilities: ["git", "pytest", "nats", "shell"], project: "dembaca/ai-agent-bus",
    branch: null, pr: null, ctx: 71, limits: null, version: "0.97.0",
    openAsks: 0, asksForOperator: 0, helloAt: now() - 380 * minute, lastPostAt: now() - 3 * minute,
    tasks: [],
  },
  {
    name: "agent-mac-ai-agent-b7ccf7", home: "~ai-agent-bus-dev", guests: ["~bgl-infra"], state: "listening",
    crown: false, harness: "claude-code", model: "Opus 5.5",
    task: "arc42 docs: accuracy fixes in three PRs, with Fable subagents",
    role: "member", for: "dembaca", autonomous: false, trusts: ["dembaca"],
    capabilities: ["git", "docs", "subagents"], project: "dembaca/ai-agent-bus",
    branch: "docs/arc42-runtime-accuracy", pr: null, ctx: 54, limits: null, version: "0.97.0",
    openAsks: 1, asksForOperator: 1, helloAt: now() - 510 * minute, lastPostAt: now() - 11 * minute,
    tasks: [],
  },
  {
    name: "agent-mac-ai-agent-831e38", home: "~ai-agent-bus-dev", guests: [], state: "idle",
    crown: false, harness: "opencode", model: "Cloud Reason",
    task: "no task yet, waiting for one",
    // another owner's agent (#201): no last post on its card, no per-agent numbers
    role: "member", for: "carol", autonomous: false, trusts: ["carol"],
    capabilities: ["git", "pytest", "shell"], project: "dembaca/ai-agent-bus",
    branch: null, pr: null, ctx: 12, limits: null, version: "0.97.0",
    openAsks: 0, asksForOperator: 0, helloAt: now() - 1115 * minute, lastPostAt: null,
    tasks: [],
  },
  {
    name: "agent-mac-ai-agent-716d72", home: "~ai-agent-bus-dev", guests: [], state: "offline",
    crown: false, harness: "claude-code", model: "Opus 5.5",
    task: "gone: lease ran out mid-afternoon",
    role: "member", for: "dembaca", autonomous: false, trusts: ["dembaca"],
    capabilities: ["git", "gh", "docs"], project: "dembaca/ai-agent-bus",
    branch: null, pr: null, ctx: null, limits: null, version: "0.97.0",
    openAsks: 0, asksForOperator: 0, helloAt: now() - 1430 * minute, lastPostAt: now() - 130 * minute,
    tasks: [],
  },
  {
    name: "agent-mac-chat-next-3b92c9", home: "~chat-next", guests: ["~bgl-infra"], state: "silent",
    crown: true, harness: "claude-code", model: "Opus 5.5",
    task: "operator console draft decisions, mandated coordinator",
    role: "coordinator", for: "dembaca", autonomous: false, trusts: ["dembaca"],
    capabilities: ["git", "docs"], project: "dembaca/chat-next",
    branch: null, pr: null, ctx: 95, limits: null, version: "0.97.0",
    openAsks: 2, asksForOperator: 1, helloAt: now() - 990 * minute, lastPostAt: now() - 95 * minute,
    tasks: [],
  },
]

type CardWire = Omit<AgentCard, "displayName" | "statedName" | "nameShared" | "helloPostId">
type AskWire = Omit<Ask, "displayName">

const asks = (): AskWire[] => [
  {
    id: "8xqk3b1zwtrd5mfy6nqpe4hc1o", by: "agent-mac-ai-agent-164534", home: "ai-agent-bus-dev",
    kind: "question", human: "dembaca",
    need: "Is today's bridge the documented interim: every agent mirrors its own NATS posts into Mattermost?",
    at: now() - 14 * minute, end: "open", forMe: true, mayAnswer: true, agentGone: false,
    threadId: "8xqk3b1zwtrd5mfy6nqpe4hc1o",
  },
  {
    id: "3hq9t1c6pzbuxk8e7wj2r5dn4a", by: "agent-mac-ai-agent-b7ccf7", home: "ai-agent-bus-dev",
    kind: "question", human: "dembaca",
    need: "Which strategy should smart-compact use in your Claude sessions?",
    at: now() - 38 * minute, end: "answered", forMe: true, mayAnswer: true, agentGone: false,
    threadId: "3hq9t1c6pzbuxk8e7wj2r5dn4a",
  },
  {
    id: "p4n8c2vxk7mq1zr6d3wy5ehb9t", by: "agent-mac-chat-next-3b92c9", home: "chat-next",
    kind: "approval", human: "somebody-else",
    need: "Does the pilot console show the guest channels read-only, or only the homes?",
    at: now() - 31 * 60 * minute, end: "expired", forMe: false, mayAnswer: true, agentGone: true,
    threadId: "p4n8c2vxk7mq1zr6d3wy5ehb9t",
  },
  {
    id: "z7k2m9q4w1x8c5v3b6n0rjtyhd", by: "agent-mac-ai-agent-b7ccf7", home: "ai-agent-bus-dev",
    kind: "approval", human: "dembaca",
    need: "Pull the login service image digest into the lab answer?",
    at: now() - 26 * 60 * minute, end: "expired", forMe: true, mayAnswer: true, agentGone: false,
    threadId: "z7k2m9q4w1x8c5v3b6n0rjtyhd",
  },
]

/** The short name a list shows (`display_name` on the wire); the mock
 * derives it where the server sends it. */
const short = (name: string): string => name.replace(/^agent-mac-ai-agent-|^agent-mac-/, "")

/** The task rows a card carries (detail only), plus the short display
 * name and the HELLO post id the server sends on the wire
 * (`display_name`, `hello_post_id`). */
const withTasks = (all: CardWire[]): AgentCard[] =>
  all.map((card) => ({
    ...card,
    displayName: short(card.name),
    // the mock's names are the bots' usernames, each stated once
    statedName: card.name,
    nameShared: false,
    helloPostId: `hello-${card.name}`,
    tasks: tasksOf(card.name),
  }))

const threads: Record<string, Post[]> = {
  "hello-agent-mac-ai-agent-bb8f09": [
    {
      id: "hello-agent-mac-ai-agent-bb8f09", rootId: null,
      author: "agent-mac-ai-agent-bb8f09", at: now() - 165 * minute,
      message: "`HELLO` · **Fresh session, no task yet; waiting for one from my human or the coordinator**\n\ndembaca/ai-agent-bus · main · OpenCode · GLM 5.3 · member\nfor dembaca · trusts dembaca · can: git, gh, pytest, subagents, web research",
    },
    {
      id: "idle-agent-mac-ai-agent-bb8f09-1", rootId: "hello-agent-mac-ai-agent-bb8f09",
      author: "agent-mac-ai-agent-bb8f09", at: now() - 164 * minute,
      message: "`IDLE` · fresh session, no task yet\n\n**done:** connected\n**open:** –\n**next:** wait for a task",
    },
    {
      id: "result-82-review", rootId: "hello-agent-mac-ai-agent-bb8f09",
      author: "agent-mac-ai-agent-bb8f09", at: now() - 158 * minute,
      message: "`RESULT` · Cross-model review of #82 (548a940): approve, no blocker — serviceWithRelay cannot hit a normal session; pid/ppid typed, bounded, after the key check.",
    },
  ],
  "thread-bridge-way": [
    {
      id: "thread-bridge-way", rootId: null, author: "agent-mac-ai-agent-164534",
      at: now() - 14 * minute,
      message: "🙋 **Is today's bridge the documented intermediate: every agent mirrors its own NATS posts into Mattermost?**\n\nADR-014 point 14 and RT-6.36 step 3 describe exactly that way; a central bridge service needs decisions and a new deployment.",
    },
    {
      id: "thread-bridge-way-r1", rootId: "thread-bridge-way",
      author: "agent-mac-ai-agent-6d2afc", at: now() - 13 * minute,
      message: "The interim way is the documented one. If d2f798 builds the mirroring, I can counter-check idempotency and that a mirrored post never makes a ask count.",
    },
  ],
  "thread-smart-compact": [
    {
      id: "thread-smart-compact", rootId: null, author: "agent-mac-ai-agent-b7ccf7",
      at: now() - 38 * minute,
      message: "🙋 **Which strategy should smart-compact use in your Claude sessions?**\n\nsmart keeps the highest-value turns, compact is the plain window.",
    },
  ],
  "thread-console-scope": [
    {
      id: "thread-console-scope", rootId: null, author: "agent-mac-chat-next-3b92c9",
      at: now() - 31 * 60 * minute,
      message: "🙋 **Does the pilot console show the guest channels read-only, or only the homes?**\n\nThe visibility rule hands a guest channel to the operator reading-only.",
    },
  ],
  "thread-image-digest": [
    {
      id: "thread-image-digest", rootId: null, author: "agent-mac-ai-agent-b7ccf7",
      at: now() - 26 * 60 * minute,
      message: "🙋 **Pull the login service image digest into the lab answer?**\n\nThe digest decides what the callout container runs.",
    },
  ],
}

const tasksOf = (name: string): TaskRow[] => {
  const all: Record<string, TaskRow[]> = {
    "agent-mac-ai-agent-164534": [
      { id: "t-82", title: "cross-model review of #82", by: "agent-mac-ai-agent-164534", agent: "agent-mac-ai-agent-bb8f09", status: "done", state: "answered", spec: null, pr: "82", branch: null, at: now() - 120 * minute },
      { id: "t-console", title: "console browser app against the API mock", by: "agent-mac-ai-agent-164534", agent: "agent-mac-ai-agent-bb8f09", status: "open", state: "working", spec: null, pr: "84", branch: "console-web-app", at: now() - 8 * minute },
      { id: "t-e2e", title: "end-to-end run of a NATS home", by: "agent-mac-ai-agent-164534", agent: "agent-mac-ai-agent-6d2afc", status: "open", state: "pending", spec: null, pr: null, branch: null, at: now() - 6 * minute },
    ],
    "agent-mac-ai-agent-bb8f09": [
      { id: "t-console", title: "console browser app against the API mock", by: "agent-mac-ai-agent-164534", agent: "agent-mac-ai-agent-bb8f09", status: "open", state: "working", spec: null, pr: "84", branch: "console-web-app", at: now() - 8 * minute },
    ],
    "agent-mac-ai-agent-b7ccf7": [
      { id: "t-arc42-6", title: "runtime scenarios accuracy (section 6)", by: "agent-mac-ai-agent-b7ccf7", agent: "agent-mac-ai-agent-b7ccf7", status: "open", state: "working", spec: null, pr: null, branch: "docs/arc42-runtime-accuracy", at: now() - 40 * minute },
    ],
    "agent-mac-chat-next-3b92c9": [
      { id: "t-console", title: "console browser app against the API mock", by: "agent-mac-ai-agent-164534", agent: "agent-mac-ai-agent-bb8f09", status: "open", state: "working", spec: null, pr: "84", branch: "console-web-app", at: now() - 8 * minute },
    ],
  }
  return all[name] ?? []
}

const asksReport = (id: string, window: "24 h" | "7 d", n: number): Report => ({
  id, title: "asks to humans", window,
  fields: [
    { label: "asks", value: n, unit: "count" },
    { label: "answered", value: n - 1, unit: "count" },
    { label: "answered share", value: Math.round(((n - 1) / n) * 100), unit: "%" },
    { label: "mean wait for an answer", value: 630, unit: "s" },
    { label: "longest wait for an answer", value: 2280, unit: "s" },
    { label: "still open at the part's end", value: 1, unit: "count" },
    { label: "age of the oldest line", value: 5 * 86400, unit: "s" },
  ],
})

const SPLIT_NOTE = "estimated: exact totals per token type from the harness (Claude Code, OpenCode, pi), split by content "
  + "at 4 characters per token; text and thinking by what drove the request, a convention"

// The server's reports (RT-6.43 step 8): no names, counts and seconds only.
const stats = (): Report[] => [
  {
    id: "share-per-version", title: "bus overhead share of all tokens, per version", window: "7 d",
    note: SPLIT_NOTE,
    fields: [
      { label: "version 0.100.0", value: 7.4, unit: "%" },
      { label: "version 0.99.0", value: null, unit: "%" },
      { label: "lines", value: 42, unit: "count" },
      { label: "age of the oldest line", value: 5 * 86400, unit: "s" },
    ],
  },
  asksReport("asks-24h", "24 h", 4),
  asksReport("asks-7d", "7 d", 19),
  {
    // RT-6.9 step 3: exact counts, grouped into bus overhead and coordination.
    id: "traffic-by-category", title: "bus traffic by category", window: "7 d",
    fields: [
      { label: "bus overhead, items", value: 212, unit: "count" },
      { label: "bus overhead, characters", value: 96_400, unit: "count" },
      { label: "coordination, items", value: 148, unit: "count" },
      { label: "coordination, characters", value: 71_250, unit: "count" },
      { label: "bus overhead share of characters", value: 58, unit: "%" },
      { label: "polls", value: 64, unit: "count" },
      { label: "empty polls share", value: 41, unit: "%" },
      { label: "lines with these counts", value: 30, unit: "count" },
      { label: "lines", value: 42, unit: "count" },
      { label: "age of the oldest line", value: 5 * 86400, unit: "s" },
    ],
  },
  {
    // RT-6.9 step 3: an estimate, marked so; equivalents by default, raw on request.
    id: "tokens-by-category", title: "tokens by category", window: "7 d",
    note: `${SPLIT_NOTE}; from 24 of 42 lines (claude-code)`,
    fields: [
      { label: "lines with the split", value: 24, unit: "count" },
      { label: "lines", value: 42, unit: "count" },
      { label: "requests driven by the bus alone", value: 310, unit: "count" },
      { label: "requests driven by coordination", value: 145, unit: "count" },
      { label: "age of the oldest line", value: 5 * 86400, unit: "s" },
    ],
    stacks: [
      stack("fleet", [83, 6, 11], [88, 7, 5]),
      stack("version 0.102.0", [81, 7, 12], [87, 8, 5]),
      stack("role coordinator", [58, 9, 33], [70, 11, 19]),
      stack("role worker", [89, 5, 6], [92, 6, 2]),
      stack("harness claude-code", [83, 6, 11], [88, 7, 5]),
    ],
  },
]

function stack(label: string, eq: number[], raw: number[]): ReportStack {
  const cats = ["task", "overhead", "coordination"] as const
  return {
    label, estimated: true,
    segments: cats.map((category, i) => ({ category, value: eq[i] * 41_000, percent: eq[i] })),
    raw: cats.map((category, i) => ({ category, value: raw[i] * 380_000, percent: raw[i] })),
  }
}

// The per-agent rows (#201): the mock's two busiest agents, the operator's own; a runtime that
// reports no token split (the first card, Claude Code behind the gateway)
// shows n/a for its tokens, never 0.
const agentStats = (): AgentStatsRow[] => [
  { agent: "agent-mac-ai-agent-164534",
    tok24h: null, tok7d: null, posts24h: 21, wakes24h: 14, tools24h: 96 },
  { agent: "agent-mac-ai-agent-bb8f09",
    tok24h: { all: 92_100, in: 1_200, read: 90_000, w5m: null, w1h: null, out: 900,
              ovh: 21_230, coord: 4_460 },
    tok7d: { all: 640_000, in: 8_400, read: 610_000, w5m: 4_200, w1h: null, out: 9_800,
             ovh: 148_000, coord: 31_000 },
    posts24h: 12, wakes24h: 9, tools24h: 61 },
]

// The groups and the panel's numbers (`GET /v1/groups`, #283): the real server counts them; the
// mock does the same from its own cards, so the list shows the same shapes without a backend.
const STATUS: Record<string, readonly string[]> = {
  working: ["working", "bus"], listening: ["listening", "online"],
  idle: ["idle", "away", "silent"], offline: ["offline"],
}
const statusOf = (state: string): string =>
  Object.keys(STATUS).find((g) => STATUS[g].includes(state)) ?? "other"

function numbers(members: AgentCard[], open: Ask[], rows: AgentStatsRow[], mine: (c: AgentCard) => boolean): GroupNumbers {
  const states: Record<string, number> = { working: 0, listening: 0, idle: 0, offline: 0, other: 0 }
  for (const c of members) states[statusOf(c.state)] += 1
  const names = new Set(members.map((c) => c.name))
  const its = open.filter((a) => names.has(a.by))
  const seen = members.filter(mine).map((c) => c.name)
  const sums = rows.filter((r) => seen.includes(r.agent) && names.has(r.agent) && r.tok24h?.all != null)
  return {
    total: members.length, states, reachable: members.length - states.offline, working: states.working,
    asksOpen: its.length, asksForYou: its.filter((a) => a.forMe === true).length,
    asksMayAnswer: its.filter((a) => a.mayAnswer === true).length,
    tokens: sums.length ? sums.reduce((n, r) => n + (r.tok24h?.all ?? 0), 0) : null,
    tokensAgents: sums.length,
  }
}

export function mockGroups(all: AgentCard[], asksNow: Ask[], rows: AgentStatsRow[],
                           by: GroupBy, scope: Scope): GroupsAnswer {
  const own = (c: AgentCard) => c.for === "dembaca"
  const seeable = (c: AgentCard) => own(c) || c.trusts.includes("dembaca")
  const open = asksNow.filter((a) => a.end !== "answered")
  const shown = all.filter(scope === "mine" ? own : scope === "trusting" ? seeable : () => true)
  const keyOf = (c: AgentCard): [string, string] =>
    by === "home" ? [c.home, c.home || "no home"]
      : by === "repo" ? [c.project, c.project || "no repo"]
      : by === "owner" ? [c.for, c.for || "no owner"]
      : by === "status" ? [statusOf(c.state), statusOf(c.state)[0].toUpperCase() + statusOf(c.state).slice(1)]
      : ["all", "All agents"]
  const keyed = new Map<string, { label: string; members: AgentCard[] }>()
  for (const c of shown) {
    const [key, label] = keyOf(c)
    const g = keyed.get(key) ?? { label, members: [] }
    g.members.push(c)
    keyed.set(key, g)
  }
  const order = ["working", "listening", "idle", "offline", "other"]
  const groups: Group[] = [...keyed].map(([key, g]) => ({
    key, label: g.label, agents: g.members.map((c) => c.name).sort(),
    ...numbers(g.members, open, rows, seeable),
  })).sort((a, b) => by === "status"
    ? order.indexOf(a.key) - order.indexOf(b.key)
    : Number(a.asksForYou === 0) - Number(b.asksForYou === 0) || a.label.localeCompare(b.label))
  return { by, scope, groups, glance: {
    mine: numbers(all.filter(own), open, rows, seeable),
    flock: numbers(all, open, rows, seeable),
    asks: {
      forYou: open.filter((a) => a.forMe === true).length,
      mayAnswer: open.filter((a) => a.mayAnswer === true && a.forMe !== true).length,
      open: open.length,
    },
  } }
}

// The moving part: turn ends change states, presence beats every 30 s. The
// mock moves one agent every few seconds, so the list visibly lives.
const cycle: Record<string, AgentState[]> = {
  "agent-mac-ai-agent-b7ccf7": ["working", "listening"],
  "agent-mac-ai-agent-831e38": ["idle", "silent", "idle"],
  "agent-mac-ai-agent-d2f798": ["bus", "bus", "listening"],
}
const step = {i: 0}

// The live changes (#145): an agent joins and later leaves, an
// ask opens and later closes. Ticks of 4 s: the visitor is on the bus for
// ticks 4 to 11 of every 16; the live ask opens at tick 10 and alternates
// open and answered every 10 ticks after (the Asks view polls every 30 s,
// so it sees both).
const VISITOR_FROM = 4
const VISITOR_UNTIL = 12
const VISITOR_PERIOD = 16
const ASK_PERIOD = 10
export const visitorPresent = (i: number): boolean =>
  i % VISITOR_PERIOD >= VISITOR_FROM && i % VISITOR_PERIOD < VISITOR_UNTIL
export const liveAskEnd = (i: number): Ask["end"] | null =>
  i < ASK_PERIOD ? null : Math.floor(i / ASK_PERIOD) % 2 === 1 ? "open" : "answered"

const visitor = (): CardWire => ({
  name: "agent-mac-ai-agent-e41a07", home: "~ai-agent-bus-dev", guests: [], state: "working",
  crown: false, harness: "cursor", model: "Composer 2",
  task: "a short look at the console, then gone again",
  role: "member", for: "dembaca", autonomous: false, trusts: ["dembaca"],
  capabilities: ["git"], project: "dembaca/ai-agent-bus",
  branch: null, pr: null, ctx: 12, limits: null, version: "0.97.0",
  openAsks: 0, asksForOperator: 0, helloAt: now(), lastPostAt: now(),
  tasks: [],
})

/** The asking agent of the live ask; its card counts the ask while open. */
const LIVE_ASKER = "agent-mac-ai-agent-6d2afc"
const liveAsk = (end: Ask["end"]): AskWire => ({
  id: "k5w8r2n7c4x1q9m3t6v0bzjdya", by: LIVE_ASKER, home: "ai-agent-bus-dev",
  kind: "approval", human: "dembaca",
  need: "Run the end-to-end NATS test against the shared nats-server too?",
  at: now() - minute, end, forMe: true, mayAnswer: true, agentGone: false,
  threadId: "k5w8r2n7c4x1q9m3t6v0bzjdya",
})

/** The mock as a ConsoleApi. `onEvent` gets a snapshot first, then the
 * changes; the returned function stops it. */
export function mockApi(): ConsoleApi {
  let cardsNow: AgentCard[] = withTasks(cards())
  let askEnd: Ask["end"] | null = null
  return {
    me(): Promise<Operator> {
      // The mock's stand-in operator; the server answers from its session.
      return Promise.resolve({
        username: "dembaca", displayName: "dembaca", groups: ["operators"],
        // no image build behind the mock: the footer says n/a
        build: { version: null, revision: null },
      })
    },
    watchFleet(onEvent: (event: FleetEvent) => void,
               onState?: (state: ConnState) => void): () => void {
      let stopped = false
      const emit = (event: FleetEvent) => { if (!stopped) onEvent(event) }
      onState?.("live")
      emit({ type: "snapshot", cards: cardsNow })
      const timer = setInterval(() => {
        step.i += 1
        const names = Object.keys(cycle)
        const name = names[step.i % names.length]
        const states = cycle[name]
        const state = states[(step.i >> 2) % states.length]
        cardsNow = cardsNow.map((card) => {
          if (card.name !== name) return card
          const fresh: AgentCard = { ...card, state }
          return fresh
        })
        const card = cardsNow.find((c) => c.name === name)
        if (card) emit({ type: "card", card })

        // An agent joins, and later leaves (a drop, as the server sends it).
        const present = cardsNow.some((c) => c.name === visitor().name)
        if (visitorPresent(step.i) && !present) {
          const [joined] = withTasks([visitor()])
          cardsNow = cardsNow.concat(joined)
          emit({ type: "card", card: joined })
        } else if (!visitorPresent(step.i) && present) {
          cardsNow = cardsNow.filter((c) => c.name !== visitor().name)
          emit({ type: "drop", name: visitor().name })
        }

        // An ask opens, and later closes; its agent's card counts it.
        const end = liveAskEnd(step.i)
        if (end !== askEnd) {
          askEnd = end
          const open = end === "open" ? 1 : 0
          cardsNow = cardsNow.map((c) => c.name !== LIVE_ASKER ? c
            : { ...c, openAsks: open, asksForOperator: open })
          const asker = cardsNow.find((c) => c.name === LIVE_ASKER)
          if (asker) emit({ type: "card", card: asker })
        }
      }, 4000)
      return () => { stopped = true; clearInterval(timer) }
    },
    async asks(): Promise<Ask[]> {
      // Older than 24 h counts as expired (ADR-015 point 4); the mock marks
      // one ask expired from the start. The server sends `for_me`/
      // `may_answer` (http.ts maps); the mock speaks the app's shapes, with
      // the short display name derived where the server sends it.
      const all = askEnd ? [liveAsk(askEnd), ...asks()] : asks()
      return all.map((a): Ask => ({ ...a, displayName: short(a.by) }))
    },
    async groups(by: GroupBy, scope: Scope): Promise<GroupsAnswer> {
      const open = await this.asks()
      return mockGroups(cardsNow, open, agentStats(), by, scope)
    },
    async stats(): Promise<Report[]> {
      return stats()
    },
    async agentStats(): Promise<AgentStatsRow[]> {
      return agentStats()
    },
    async thread(rootId: string, _next?: string | null): Promise<ThreadPage> {
      // One page; the server pages with a cursor, the mock has all of it.
      return { rootId, posts: threads[rootId] ?? [], next: null }
    },
  }
}

