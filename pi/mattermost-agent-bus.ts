// pi extension: the AI agent bus in pi sessions (RT-6.27 in
// docs/arc42/06-runtime-view.md; install: adapters/pi.md; ADR-009 phase 3).
// Written against pi-coding-agent 1.3.0: its docs/extensions.md and the
// declarations of `ExtensionAPI` and its events (dist/core/extensions/types.d.ts).
//
// Thin glue, as the OpenCode plugin (RT-6.20): the session's MCP server decides
// and words everything; this file turns pi's events into calls to the server's
// hook API (RT-6.17) and the server's answers into session input:
//   session_start       the MCP server (pi.registerMcpServer) and the long poll
//   resources_discover  the skill
//   input               a human's prompt (any source but "extension") starts a
//                       turn: the counter, drain rounds and backoff afresh
//   agent_start         presence working (not awaited; the presence calls go
//                       one at a time, presenceQueue)
//   agent_settled       the final idle signal, not agent_end (which pi may
//                       follow with a retry, a compaction or queued input):
//                       GET /v1/turn-end → drain, answer (a bus post without
//                       a reply in its thread) or sign-off as a user message
//                       that starts the next run, else presence "on the bus"
//                       (the server's gates hold) or idle; aborted (Escape):
//                       idle, no checks, no wake-up until the human's next
//                       prompt (RT-6.9)
//   turn_end, agent_settled
//                       the context use (ctx.getContextUsage) → POST /v1/usage
//                       for the status (ADR-008), not awaited
//   session_compact     GET /v1/handoff → the own posts and the bus skill as
//                       context that starts no run, not shown (RT-6.7)
//   context, message_end
//                       the token split (RT-6.9 step 2): each request's
//                       messages as segments by category, its usage laid over
//                       them; POST /v1/stat {k: usage} at agent_settled, numbers
//                       only, changing nothing the model sees
//   one long poll       GET /v1/wake?wait=50 (ADR-009) from session_start to
//                       session_shutdown: a wake → pi.sendUserMessage, at once
//                       when idle, deliverAs "followUp" while a human's run
//                       goes (after its tools, never in a tool call); none
//                       while a run this extension started goes, nor while
//                       pi compacts (it refuses a prompt then)
//   session_shutdown    ends the poll (idempotent)
// It holds no token and no secret, writes no file, and fails open: without a
// server or a bus session every call comes back empty and nothing happens. It
// finds the server as the OpenCode plugin does: the helpers between the
// keep-in-step markers are that plugin's (opencode/plugin/mattermost-agent-bus.ts),
// copied, because pi loads a symlinked extension from the link's directory,
// where no relative import of the checkout resolves; pi/test/ compares the two.

import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent"
import { createHash } from "node:crypto"
import { existsSync, readFileSync, realpathSync } from "node:fs"
import { homedir } from "node:os"
import { basename, dirname, join } from "node:path"
import { fileURLToPath } from "node:url"

export const SERVER = "ai-agent-bus"
export const SKILL = "ai-agent-bus"
const QUICK_MS = 3000
// /v1/turn-end reads several channels (the Stop hook's 8 s).
const SLOW_MS = 8000
// The long poll of /v1/wake: the server holds it WAIT_SEC (at most 55).
export const WAIT_SEC = 50
// After a failed or empty-handed poll, and the base of the pause after "replaced".
export const RETRY_SEC = 15
// The same unread counter, stall or reaction again after the turn it woke:
// wait 1, 2, 4 … min, at most 30.
export const WAKE_BACKOFF_MS = 60_000
export const WAKE_BACKOFF_MAX_MS = 30 * 60_000
// The long poll's read timeout is a margin above its wait (RT-6.17 step 7).
const READ_MARGIN_MS = 10_000
const REPLACED_MAX_MS = 60_000
// Nothing to ask for (aborted, a run of ours): asked again when something
// changes, at the latest after this.
const IDLE_PAUSE_MS = 60_000
// A message of ours whose run never came (pi refused it, which it reports as an
// extension error, not to the caller): poll again after this.
const OURS_MS = 60_000
// A compaction whose end never came (another extension cancelled it): poll
// again after this.
const COMPACT_MAX_MS = 10 * 60_000
// The texts of ours the token split recognizes in the context: the latest.
const OURS_KEPT = 256
// pi's per-request timeout for the bus tools (s; its default is 60):
// wait_for_events waits 60 s by default and may be asked for more.
export const MCP_TIMEOUT_SEC = 180
// The context use is reported when it moved a point, else at most once a
// minute, as the status line helper does (RT-6.10 step 3).
export const USAGE_EVERY_MS = 60_000

// The tests' seams: the environment, where this file lies, the poll's timing.
export type Deps = { env?: Env; here?: string; waitSec?: number; retrySec?: number }

type Env = Record<string, string | undefined>
type Json = Record<string, any>

// keep-in-step: from here to the end marker, the same text in
// opencode/plugin/mattermost-agent-bus.ts and pi/mattermost-agent-bus.ts
// (pi/test/ compares them; RT-6.20 step 2, RT-6.27 step 2).
function sha16(text: string): string {
  return createHash("sha1").update(text).digest("hex").slice(0, 16)
}

function real(dir: string): string | null {
  try {
    return realpathSync(dir)
  } catch {
    return null
  }
}

// The endpoint file of the workspace's MCP server (an OpenCode location, a pi
// session's directory), whose MM_WORKSPACE names that real directory (RT-6.17
// step 2; the name as mm_bus_file in bin/mm-agent-lib.sh writes it). No session id, no other
// variable, no walk-up: inherited from a Claude Code shell, or a parent
// directory's file, they may name another session's server.
export function busFile(env: Env, directory: string, prefix = "endpoint"): string | null {
  const state = join(env.XDG_STATE_HOME || join(env.HOME || homedir(), ".local", "state"), "mm-agent-bus")
  const dir = real(directory)
  const f = dir && join(state, `${prefix}-${sha16(dir)}.env`)
  return f && existsSync(f) ? f : null
}

function parseEnvFile(text: string): Record<string, string> {
  const out: Record<string, string> = {}
  for (const line of text.split("\n")) {
    const i = line.indexOf("=")
    if (i > 0) out[line.slice(0, i)] = line.slice(i + 1)
  }
  return out
}

// One call to the hook API; null for no server, no bus session or any failure.
// missing: called when no endpoint file is there. client: the harness session
// the caller speaks for, sent as CLIENT_HEADER (the host refuses another one's
// with 403 and client_session "refused", RT-6.18 step 6); forbidden: called then.
export const CLIENT_HEADER = "X-Bus-Client-Session"
export function hookApi(env: Env, directory: string, missing: () => void = () => {},
                        client: { session?: () => string; forbidden?: () => void } = {}) {
  // signal: ends the call when it aborts (an unload, a shutdown), as the timeout does.
  return async (method: "GET" | "POST", path: string, body?: Json, ms = QUICK_MS, signal?: AbortSignal): Promise<Json | null> => {
    let endpoint = env.MM_BUS_ENDPOINT || ""
    let key = env.MM_BUS_KEY || ""
    if (!endpoint || !key) {
      const f = busFile(env, directory)
      if (!f) {
        missing()
        return null
      }
      try {
        const vars = parseEnvFile(readFileSync(f, "utf8"))
        endpoint = vars.MM_BUS_ENDPOINT || ""
        key = vars.MM_BUS_KEY || ""
      } catch {
        return null
      }
      if (!endpoint || !key) return null
    }
    const session = client.session?.() || ""
    try {
      const res = await fetch(endpoint.replace(/\/$/, "") + path, {
        method,
        headers: { Authorization: `Bearer ${key}`, ...(body ? { "Content-Type": "application/json" } : {}),
                   ...(session ? { [CLIENT_HEADER]: session } : {}) },
        body: body ? JSON.stringify(body) : undefined,
        signal: signal ? AbortSignal.any([AbortSignal.timeout(ms), signal]) : AbortSignal.timeout(ms),
      })
      // Only the host's refusal of another session, not a route's own 403.
      if (res.status === 403 && (await res.json().catch(() => null))?.client_session === "refused") client.forbidden?.()
      if (res.status !== 200) return null
      return (await res.json()) as Json
    } catch {
      return null
    }
  }
}

// The presence, one call at a time (RT-6.20 step 3, RT-6.27 step 3): nothing
// waits for it unless it awaits the promise, and a state goes out only once the
// call before it has settled, so the server never sees a `working` after the
// `idle` that followed it. A state superseded while it waited is not sent.
export function presenceQueue(send: (state: string) => Promise<unknown>): (state: string) => Promise<void> {
  let chain: Promise<void> = Promise.resolve()
  let wanted = ""
  return (state: string) => {
    wanted = state
    chain = chain.then(async () => {
      if (wanted === state) await send(state)
    }).catch(() => {})
    return chain
  }
}

// The MCP server's Python environment (RT-6.20 step 1, RT-6.27 step 1): uv
// keeps a project's in <project>/.venv, which a read-only tree refuses (a
// corral read grant on a checkout, or the host copy, RT-6.28), so it goes into
// the user's cache, one per tree (by its real path: one per release of the
// host copy). The tree stays untouched: with --locked uv only reads uv.lock,
// and the project goes in as an editable install that points at its sources.
export function venvDir(env: Env, root: string): string {
  const cache = env.XDG_CACHE_HOME || join(env.HOME || homedir(), ".cache")
  return join(cache, "mm-agent-bus", `venv-${sha16(real(root) || root)}`)
}
// keep-in-step: end

// The long poll's wait in seconds, 10 to 55.
export function waitSec(sec: number = WAIT_SEC): number {
  return Math.min(55, Math.max(10, Number.isFinite(sec) && sec > 0 ? Math.floor(sec) : WAIT_SEC))
}

// The least pause after a failed poll: a server that is gone must not be
// asked in a tight loop. `timing` is the tests' to lower.
export const timing = { minRetrySec: 5 }

export function retryMs(sec: number = RETRY_SEC): number {
  return Math.max(timing.minRetrySec, Number.isFinite(sec) && sec > 0 ? sec : RETRY_SEC) * 1000
}

export function wakeBackoffMs(repeats: number): number {
  return Math.min(WAKE_BACKOFF_MS * 2 ** repeats, WAKE_BACKOFF_MAX_MS)
}

// The kinds of text put into the context the stats know (stats.INJECT_KINDS, RT-6.9 step 1);
// any other counts as "other". Only the kind and the length go to the server, never the text.
const INJECT_KINDS = new Set(["start", "check", "digest", "drain", "signoff", "listen", "idle", "compact", "wake"])
export function injectKind(name: string): string {
  return INJECT_KINDS.has(name) ? name : "other"
}

// A text's length in Unicode code points, as the server (Python's len) and the hooks
// (jq's length) count it; String.length would count UTF-16 units (RT-6.9).
// Counted without building an array: it runs over each request's whole context.
export function chars(text: string): number {
  let n = text.length
  for (let i = 0; i < text.length - 1; i++) {
    const hi = text.charCodeAt(i)
    if (hi >= 0xd800 && hi <= 0xdbff) {
      const lo = text.charCodeAt(i + 1)
      if (lo >= 0xdc00 && lo <= 0xdfff) {
        n -= 1 // a surrogate pair: one code point
        i += 1
      }
    }
  }
  return n
}

// ---- The token split (RT-6.9 step 2, ADR-017 method 1; issue #139 stage 2b) ----
// The rules of mcp/src/…/overhead.py over what pi shows an extension: the
// `context` event (the messages of each model request, without the system
// prompt and the tool docs) and the assistant message at `message_end` (its
// usage). In process and silent: nothing here adds text to the context, and
// only numbers go to the server (`usage`, once per settled run).
export const TASK = 0
export const OVERHEAD = 1
export const COORDINATION = 2
export const CHARS_PER_TOKEN = 4
export type Shares = [number, number, number]
const CATS = [TASK, OVERHEAD, COORDINATION]
const BUS_KEYS = ["ai-agent-bus", "mattermost-agent-bus"]
// a2a.WORDS; HELLO, IDLE and BYE are overhead (overhead.OVERHEAD_WORDS).
const BUS_WORDS = new Set(["TASK", "DELEGATE", "ACK", "PROGRESS", "BLOCKED", "WAITING", "RESULT", "DONE", "NO",
  "CANCEL", "HELLO", "IDLE", "BYE", "HANDOVER", "ASSIGNMENTS"])
const OVERHEAD_WORDS = new Set(["HELLO", "IDLE", "BYE"])
const POSTING = new Set(["post_message", "reply_in_thread"])
const COORDINATING = new Set(["ask_human", "resolve_ask", "coordinator", "get_file", "attach", "get_thread",
  "list_recent"])
// a2a.word: the convention word a message starts with ("PR #12" is a RESULT).
const PR_FIRST = /^[^\p{L}\p{N}_@#]*PR\s*#\d+/iu
const FIRST = /^[^\p{L}\p{N}_@#]*([A-Z]{2,11})(?![A-Za-z])/u

export function busWord(message: string): string | null {
  if (PR_FIRST.test(message)) return "RESULT"
  const m = FIRST.exec(message)
  return m && BUS_WORDS.has(m[1]) ? m[1] : null
}

// pi names an MCP tool `mcp__<server>__<tool>`, everything but [A-Za-z0-9_]
// turned into `_` (pi-mcp createMcpToolName): the bus tool, or null.
const BUS_NAMES = new Set(BUS_KEYS.flatMap((k) => [k, k.replace(/[^A-Za-z0-9_]/g, "_")]))
export function busTool(name: string): string | null {
  const m = /^mcp__(.+?)__(.+)$/.exec(name)
  return m && BUS_NAMES.has(m[1]) ? m[2] : null
}
const isBus = (name: string) => busTool(name) !== null
// pi's read tool on the bus skill's folder (relative or absolute).
const SKILL_PATH = new RegExp(`(^|[/\\\\])skills[/\\\\]${SKILL}[/\\\\]`)
export const isWait = (name: string) => busTool(name) === "wait_for_events"

// A tool call and its result: a bus tool's category, reading the bus skill
// overhead, anything else task work.
export function toolCategory(name: string, args: unknown): number {
  if (!isBus(name)) {
    const path = (args as Json | null)?.path
    return name === "read" && typeof path === "string" && SKILL_PATH.test(path) ? OVERHEAD : TASK
  }
  const tool = busTool(name) ?? ""
  const a = (args && typeof args === "object" ? args : {}) as Json
  if (POSTING.has(tool)) {
    // The word as the agent passed it (`kind`), else the one the message starts with.
    let word = String(a.kind ?? "").trim().toUpperCase()
    if (!BUS_WORDS.has(word)) word = busWord(String(a.message ?? "")) ?? ""
    return OVERHEAD_WORDS.has(word) ? OVERHEAD : COORDINATION
  }
  return COORDINATING.has(tool) ? COORDINATION : OVERHEAD
}

const textOf = (content: unknown): string =>
  typeof content === "string" ? content
    : Array.isArray(content) ? content.map((c) => (c?.type === "text" ? String(c.text ?? "") : "")).join("")
      : ""
// JSON as Python's json.dumps(ensure_ascii=False) writes it (", " and ": "):
// the size overhead.py gives a tool call's input.
export function pyJson(v: unknown): string {
  if (v === null || v === undefined) return "null"
  if (Array.isArray(v)) return "[" + v.map(pyJson).join(", ") + "]"
  if (typeof v === "object") {
    return "{" + Object.entries(v as Json).map(([k, x]) => JSON.stringify(k) + ": " + pyJson(x)).join(", ") + "}"
  }
  if (typeof v === "boolean") return v ? "true" : "false"
  return JSON.stringify(v) ?? "null"
}
const callChars = (c: any) => chars(pyJson(c?.arguments ?? {}))

// overhead._events: a wait_for_events result (or one of its content blocks,
// an item each) as [overhead, task, coordination] characters. Each delivered
// post's body is coordination, a trusted human's task work; the server's own
// items, the JSON around the bodies, an empty result and what does not parse
// are overhead.
export function waitResult(body: string): Shares {
  const whole: Shares = [chars(body), 0, 0]
  let items: any
  try {
    items = JSON.parse(body)
    if (items && typeof items === "object" && !Array.isArray(items) && typeof items.result === "string") {
      items = JSON.parse(items.result)
    }
  } catch {
    return whole
  }
  if (items && typeof items === "object" && !Array.isArray(items) && "result" in items) items = items.result
  if (items && typeof items === "object" && !Array.isArray(items)) items = [items]
  if (!Array.isArray(items)) return whole
  let task = 0
  let coord = 0
  for (const item of items) {
    const post = item && typeof item === "object" ? item.post : null
    if (!post || typeof post !== "object" || Array.isArray(post) || (!post.id && item.kind !== "reaction")) continue
    const n = chars(String(post.message ?? ""))
    if (item.from_trusted_human === true) task += n
    else coord += n
  }
  return [Math.max(0, whole[0] - task - coord), task, coord]
}

// The context's segments in order (category, characters), as running sums.
export class Segments {
  ends = [0]
  cats = [TASK]
  sums: Shares[] = [[0, 0, 0]]
  fresh: Shares = [0, 0, 0] // the input since the last request, by category

  add(cat: number, n: number, isNew = true) {
    if (!(n > 0)) return
    const s = [...this.sums.at(-1)!] as Shares
    s[cat] += n
    this.ends.push(this.ends.at(-1)! + n)
    this.cats.push(cat)
    this.sums.push(s)
    if (isNew) this.fresh[cat] += n
  }
  addSplit(n: number, shares: Shares, isNew = true) {
    for (const cat of CATS) this.add(cat, n * shares[cat], isNew)
  }
  get total(): number {
    return this.ends.at(-1)!
  }
  // The characters of each category in the first q characters.
  before(q: number): Shares {
    if (q >= this.total) return [...this.sums.at(-1)!] as Shares
    let lo = 1
    let hi = this.ends.length - 1
    while (lo < hi) {
      const mid = (lo + hi) >> 1
      if (this.ends[mid] < q) lo = mid + 1
      else hi = mid
    }
    const out = [...this.sums[lo - 1]] as Shares
    out[this.cats[lo]] += q - this.ends[lo - 1]
    return out
  }
  shares(): Shares {
    const t = this.total
    return t ? this.sums.at(-1)!.map((x) => x / t) as Shares : [1, 0, 0]
  }
  driver(): Shares {
    const t = this.fresh[0] + this.fresh[1] + this.fresh[2]
    return t ? this.fresh.map((x) => x / t) as Shares : [1, 0, 0]
  }
}

const anyUsage = (u: any) => !!u && [u.input, u.output, u.cacheRead, u.cacheWrite].some((x) => Number(x) > 0)

// One request's context (the `context` event's messages) as segments: the
// texts the extension put in (`ours`, by text; its handoff is a custom message
// of SERVER) are overhead, bus tool calls and their results by the tool's
// category (a wait_for_events result by its items, waitResult), a compaction
// summary by the shares the context had before it, everything else task work.
export function contextSegments(messages: readonly any[], ours: (text: string) => number | undefined,
                                summary: Shares | null = null): Segments {
  const seg = new Segments()
  const tools = new Map<string, number>()
  for (const m of messages ?? []) {
    switch (m?.role) {
      case "user": {
        const text = textOf(m.content)
        seg.add(ours(text) ?? TASK, chars(text))
        break
      }
      case "custom":
        seg.add(m.customType === SERVER ? OVERHEAD : TASK, chars(textOf(m.content)))
        break
      case "toolResult":
        if (isWait(String(m.toolName ?? ""))) {
          // One block, or one text block per item: each on its own.
          const blocks = Array.isArray(m.content) && m.content.length > 1 ? m.content.map((b: any) => textOf([b]))
            : [textOf(m.content)]
          for (const block of blocks) {
            const [o, t, c] = waitResult(block)
            seg.add(OVERHEAD, o)
            seg.add(TASK, t)
            seg.add(COORDINATION, c)
          }
        } else {
          seg.add(tools.get(m.toolCallId) ?? TASK, chars(textOf(m.content)))
        }
        break
      case "compactionSummary":
        seg.addSplit(chars(String(m.summary ?? "")), summary ?? [1, 0, 0])
        break
      case "branchSummary":
        seg.add(TASK, chars(String(m.summary ?? "")))
        break
      case "bashExecution":
        seg.add(TASK, chars(String(m.command ?? "")) + chars(String(m.output ?? "")))
        break
      case "assistant": {
        // Its text and thinking by the categories of its request's new input
        // (the driver); a request that cost nothing (an error) drove nothing.
        const driver = seg.driver()
        if (anyUsage(m.usage)) seg.fresh = [0, 0, 0]
        for (const c of Array.isArray(m.content) ? m.content : []) {
          if (c?.type === "toolCall") {
            const cat = toolCategory(String(c.name ?? ""), c.arguments)
            tools.set(c.id, cat)
            seg.add(cat, callChars(c), false)
          } else {
            seg.addSplit(chars(String(c?.text ?? c?.thinking ?? "")), driver, false)
          }
        }
        break
      }
      default:
        seg.add(TASK, chars(textOf(m?.content)))
    }
  }
  return seg
}

const TYPES = ["in", "read", "w", "out"] as const
export type Tokens = Record<(typeof TYPES)[number], number>
const zero = (): Tokens => ({ in: 0, read: 0, w: 0, out: 0 })
const tokens = (u: any): Tokens => ({ in: Number(u?.input) || 0, read: Number(u?.cacheRead) || 0,
                                      w: Number(u?.cacheWrite) || 0, out: Number(u?.output) || 0 })

// RT-6.9 step 2: one request's tokens by category. The input by position over
// the known segments (cache read the oldest, then cache write, then fresh
// input; 4 characters a token, scaled down when the segments are longer): the
// front they do not cover is pi's system prompt and tool docs, task work here
// (the server adds the bus's share of it). The output: the tool calls by their
// tool's category, the rest (text, thinking) by the driver. The fourth entry:
// of each input type the tokens the segments did not cover (amendment A6;
// within task work), where the server places the bus's fixed head.
export function requestSplit(usage: any, seg: Segments, message: any): [Tokens, Tokens, Tokens, Tokens] {
  const u = tokens(usage)
  const out: [Tokens, Tokens, Tokens, Tokens] = [zero(), zero(), zero(), zero()]
  const known = seg.total / CHARS_PER_TOKEN
  const total = u.read + u.w + u.in
  const scale = known ? Math.min(1, total / known) : 0
  const base = total - scale * known
  const at = (p: number): Shares => {
    if (p <= base || scale <= 0) return [Math.min(p, base), 0, 0]
    const c = seg.before((p - base) / scale * CHARS_PER_TOKEN)
    return [base + scale * c[TASK] / CHARS_PER_TOKEN, scale * c[OVERHEAD] / CHARS_PER_TOKEN,
            scale * c[COORDINATION] / CHARS_PER_TOKEN]
  }
  let lo: Shares = [0, 0, 0]
  let edge = 0
  for (const k of ["read", "w", "in"] as const) {
    out[3][k] = Math.max(0, Math.min(edge + u[k], base) - Math.min(edge, base))
    edge += u[k]
    const hi = at(edge)
    for (const c of CATS) out[c][k] = Math.max(0, hi[c] - lo[c])
    lo = hi
  }
  const calls: Shares = [0, 0, 0]
  for (const c of Array.isArray(message?.content) ? message.content : []) {
    if (c?.type === "toolCall") calls[toolCategory(String(c.name ?? ""), c.arguments)] += callChars(c) / CHARS_PER_TOKEN
  }
  const sum = calls[0] + calls[1] + calls[2]
  const fit = sum > u.out ? u.out / sum : 1
  const driver = seg.driver()
  for (const c of CATS) out[c].out = calls[c] * fit + (u.out - sum * fit) * driver[c]
  return out
}

const USAGE_MAX = 1e9
// The requests of a run summed; fields() is the `usage` stat: 18 integers,
// o_x + c_x <= a_x for each type x and h_x <= a_x - o_x - c_x for the input
// types, or null without a request.
export class Meter {
  req = 0
  reqOvh = 0
  reqCoord = 0
  all = zero()
  ovh = zero()
  coord = zero()
  front = zero()

  // One assistant message, with the segments of its request's context.
  add(message: any, seg: Segments | null) {
    if (message?.role !== "assistant" || !anyUsage(message.usage)) return
    const s = seg ?? new Segments()
    const split = requestSplit(message.usage, s, message)
    const u = tokens(message.usage)
    this.req += 1
    for (const k of TYPES) {
      this.all[k] += u[k]
      this.ovh[k] += split[OVERHEAD][k]
      this.coord[k] += split[COORDINATION][k]
      this.front[k] += split[3][k]
    }
    // A request whose new input held no task work: coordination when any of
    // it was, else overhead.
    const f = s.fresh
    if (!f[TASK] && f[COORDINATION]) this.reqCoord += 1
    else if (!f[TASK] && f[OVERHEAD]) this.reqOvh += 1
  }

  fields(): Json | null {
    if (!this.req) return null
    const n = (x: number) => Math.min(USAGE_MAX, Math.max(0, Math.round(x)))
    const out: Json = { req: n(this.req), req_ovh: n(this.reqOvh), req_coord: n(this.reqCoord) }
    for (const k of TYPES) {
      const a = n(this.all[k])
      const o = Math.min(n(this.ovh[k]), a)
      const c = Math.min(n(this.coord[k]), a - o)
      Object.assign(out, { [`a_${k}`]: a, [`o_${k}`]: o, [`c_${k}`]: c })
      if (k !== "out") out[`h_${k}`] = Math.min(n(this.front[k]), a - o - c)
    }
    return out
  }
}
// ---- end of the token split ----

// The bus checkout: MM_AGENT_BUS_ROOT, or the checkout this file really lies
// in (pi/; a symlink into ~/.pi/agent/extensions/ resolves).
export function busRoot(env: Env, here: string): string | null {
  for (const dir of [env.MM_AGENT_BUS_ROOT, dirname(dirname(real(here) || here))]) {
    if (dir && existsSync(join(dir, "mcp", "pyproject.toml"))) return dir
  }
  return null
}

const shq = (s: string) => `'${s.replace(/'/g, `'\\''`)}'`

// The registration secret for the server alone: pi runs an env value that
// starts with "!" as a command for that server's environment only, so the
// secret never enters pi's environment or the agent's shell. From
// MM_REG_SECRET_GOPASS (the gopass path, as the corral hooks read it), else
// MM_REG_SECRET_CMD (any command that prints it). Only the command goes into
// the config, never the secret. None under corral (MM_BUS_MCP_ENDPOINT): the
// host hooks registered the bot, the server is a relay (RT-6.18).
export function secretCommand(env: Env): string | null {
  if (env.MM_BUS_MCP_ENDPOINT) return null
  const path = env.MM_REG_SECRET_GOPASS?.trim()
  if (path) return `!gopass show -o ${shq(path)}`
  const cmd = env.MM_REG_SECRET_CMD?.trim()
  return cmd ? `!${cmd}` : null
}

// The MCP server as pi.registerMcpServer takes it (an mcp.json entry). Its
// environment is pi's plus these: a Claude Code session's id and project,
// inherited when pi was started from its shell, would key the server's files
// on that session: blanked, and the workspace named, where the extension
// looks (RT-6.27 step 1, as RT-6.20 step 2). No channel push: Claude Code's.
// Its Python environment in the user's cache (venvDir). Its tools are declared
// to the model ("direct"), not left to codemode.
export function mcpConfig(root: string, workspace: string, env: Env = {}) {
  const secret = secretCommand(env)
  return {
    command: "uv",
    args: ["run", "--locked", "--project", join(root, "mcp"), "mattermost-ai-agent-bus-mcp"],
    env: { CLAUDE_CODE_SESSION_ID: "", CLAUDE_PROJECT_DIR: "", MM_CHANNEL_PUSH: "0", MM_WORKSPACE: workspace,
           UV_PROJECT_ENVIRONMENT: venvDir(env, root), ...(secret ? { MM_REG_SECRET: secret } : {}) },
    cwd: workspace,
    exposure: "direct" as const,
    timeout: MCP_TIMEOUT_SEC,
    description: "AI agent bus: post, reply, wait for events, ask a human (skill ai-agent-bus)",
  }
}

type Turn = {
  running: boolean // a run goes (agent_start until agent_settled)
  working: boolean // the presence the extension set last is "working"
  drains: number
  signoffAsked: boolean
  byBus: boolean
  aborted: boolean // until the human's next prompt: no checks, no wake-up
  listening: boolean // the server's gates held at the last turn end
  ours: number // epoch ms: a message of ours is out, its run not settled (0: none)
  idleAt: number // epoch ms: the last turn end, the long poll's `since`
  human: number // the human's prompts so far: a check they overtook is dropped
  wakeKey: string // the last wake-up's counter (reason:count), "" for new posts
  wakeRepeats: number
  wakeAfter: number // epoch ms: no wake-up for the same counter before
  usageCtx: number // the context % reported last (-1: none)
  usageAt: number
  compacting: number // epoch ms: pi compacts since (0: not)
}

export function create(pi: ExtensionAPI, deps: Deps = {}) {
  const env = deps.env ?? (process.env as Env)
  const root = busRoot(env, deps.here ?? fileURLToPath(import.meta.url))
  const WAIT = waitSec(deps.waitSec)
  const RETRY = retryMs(deps.retrySec)
  const t: Turn = { running: false, working: false, drains: 0, signoffAsked: false, byBus: false, aborted: false,
    listening: false, ours: 0, idleAt: Date.now(), human: 0, wakeKey: "", wakeRepeats: 0, wakeAfter: 0,
    usageCtx: -1, usageAt: 0, compacting: 0 }
  let ctx: ExtensionContext | null = null
  let bus: ReturnType<typeof hookApi> = async () => null
  let label = ""
  let ac: AbortController | null = null
  let closed = true

  const stat = (k: string, fields: Json = {}) => bus("POST", "/v1/stat", { k, ...fields })
  // A text the extension puts into the context, by kind and length only, never the text (RT-6.9 step 1).
  const inject = (name: string, text: string) => stat("inject", { name: injectKind(name), chars: chars(text) })
  // The token split (RT-6.9 step 2): the run's requests, the segments of the
  // request under way, the texts this extension put in (overhead: none of
  // them carries a post's body), the shares before the last compaction.
  const split = { meter: new Meter(), seg: null as Segments | null, ours: new Map<string, number>(),
                  summary: null as Shares | null, last: null as Shares | null }
  const ours = (text: string) => {
    split.ours.delete(text)
    split.ours.set(text, OVERHEAD)
    if (split.ours.size > OURS_KEPT) split.ours.delete(split.ours.keys().next().value!)
  }
  const usageStat = async () => {
    const fields = split.meter.fields()
    split.meter = new Meter()
    if (fields) await stat("usage", fields)
  }
  // One call at a time: an abort's `idle` never overtakes the run's `working`.
  const presence: (state: "working" | "idle" | "bus") => Promise<void> =
    presenceQueue((state) => bus("POST", "/v1/presence", { state, label }))
  const notify = (text: string) => {
    try {
      if (text && ctx?.hasUI) ctx.ui.notify(text, "info")
    } catch {
      // no UI: nothing to show
    }
  }

  // A user message of ours: at once when idle, else after the run's tools
  // (followUp), never into a tool call. pi reports a refusal as an extension
  // error, not here: OURS_MS bounds the wait for its run.
  const deliver = (text: string) => {
    t.ours = Date.now()
    ours(text)
    if (!ctx || ctx.isIdle()) pi.sendUserMessage(text)
    else pi.sendUserMessage(text, { deliverAs: "followUp" })
  }
  const compacting = () => t.compacting > 0 && Date.now() - t.compacting < COMPACT_MAX_MS

  // A pause the loop ends early when something changes (nudge).
  let poke: (() => void) | null = null
  const pause = (ms: number) =>
    new Promise<void>((resolve) => {
      const done = () => {
        clearTimeout(timer)
        if (poke === done) poke = null
        resolve()
      }
      const timer = setTimeout(done, ms)
      ;(timer as any).unref?.()
      poke = done
    })
  const nudge = () => poke?.()

  // ADR-008: the context use for the status, when it moved or once a minute.
  const usage = async () => {
    let u: any
    try {
      u = ctx?.getContextUsage()
    } catch {
      return
    }
    const pct = Number(u?.percent)
    if (u?.percent == null || !Number.isFinite(pct)) return
    const ctxPct = Math.min(100, Math.max(0, Math.round(pct)))
    if (Math.abs(ctxPct - t.usageCtx) < 1 && Date.now() - t.usageAt < USAGE_EVERY_MS) return
    t.usageCtx = ctxPct
    t.usageAt = Date.now()
    const size = Number(u.contextWindow)
    await bus("POST", "/v1/usage", { ctx: ctxPct, ...(Number.isInteger(size) && size > 0 ? { ctx_size: size } : {}) })
  }

  // The Stop hook's checks, asked of the server (RT-6.27 step 4; RT-6.2 steps 1–2).
  const turnEnded = async (aborted: boolean) => {
    t.running = false
    t.ours = 0
    if (aborted || t.aborted) {
      // The human stopped the run: no checks, no turn end (RT-6.9), no wake-up
      // until their next prompt.
      t.aborted = true
      if (t.working) {
        t.working = false
        await presence("idle")
      }
      await usage()
      return
    }
    t.idleAt = Date.now()
    const human = t.human
    // unanswered=1: a bus post without a reply in its thread blocks once (RT-6.2 step 2).
    const q = `drains=${t.drains}&signoff_asked=${+t.signoffAsked}&by_bus=${+t.byBus}&push=1&unanswered=1`
    const r = await bus("GET", `/v1/turn-end?${q}`, undefined, SLOW_MS)
    // The human typed meanwhile, or a wake-up went out: that run ends with its own check.
    if (t.human !== human || t.running || t.ours) return
    // The posts an answer names are marked only now that the block goes out, so
    // a dropped check leaves them due; an answer alone that cannot be marked
    // does not block, so a server that cannot mark never blocks in a loop.
    const posts = Array.isArray(r?.posts) ? r.posts : []
    const marked = posts.length > 0 && !!(await bus("POST", "/v1/reminded", { post_ids: posts }))
    // The same again: a prompt that came during the mark takes over.
    if (t.human !== human || t.running || t.ours) return
    if (r && (r.action === "drain" || (r.action === "answer" && marked) || r.action === "signoff")) {
      if (r.action === "drain") t.drains += 1
      else if (r.action === "signoff") t.signoffAsked = true
      await stat("block", { kind: r.action })
      await inject(r.action, String(r.reason))
      notify(String(r.banner || ""))
      deliver(String(r.reason))
      return
    }
    if (r) await stat("turn_end")
    // A window the server opened (RT-6.2 step 3) says its gates hold: reachable
    // between turns, for as long as the session lives (ADR-009). A failed check
    // changes nothing about that.
    if (r) t.listening = (Number(r.listen_until) || 0) * 1000 > Date.now()
    t.working = false
    await (t.listening ? presence("bus") : presence("idle"))
    await usage()
    nudge()
  }

  // The listener (RT-6.27 step 5, ADR-009): one long poll of /v1/wake from
  // session_start to session_shutdown, renewed when it ends. The server
  // decides, by its gates; it only looks.
  const listen = async (signal: AbortSignal) => {
    let replaced = 0
    while (!closed && !signal.aborted) {
      // Aborted by the human, a message of ours whose run has not settled, or
      // a compaction (pi refuses a prompt meanwhile).
      if (t.aborted || compacting() || (t.ours && Date.now() - t.ours < OURS_MS)) {
        await pause(t.aborted ? IDLE_PAUSE_MS : OURS_MS)
        continue
      }
      const asked = Date.now()
      const since = t.idleAt
      // `until` (a day ahead) is for a server before 0.71.8, as the OpenCode plugin sends it.
      const until = Math.floor(asked / 1000) + 86400
      const r = await bus("GET", `/v1/wake?since=${since}&until=${until}&wait=${WAIT}`, undefined,
                          WAIT * 1000 + READ_MARGIN_MS, signal)
      if (closed || signal.aborted) break
      if (!r) {
        await pause(RETRY) // no server, 503 (not registered yet), a failure
        continue
      }
      if (r.reason === "replaced") {
        // Another poller of the session took over: back off, or two replace each other.
        replaced += 1
        await pause(Math.min(RETRY * 2 ** (replaced - 1), REPLACED_MAX_MS))
        continue
      }
      replaced = 0
      if (!r.wake) {
        t.wakeKey = ""
        // An empty answer long before the wait ran out: a server that does not hold it.
        if (Date.now() - asked < (WAIT - 5) * 1000) await pause(RETRY)
        continue
      }
      // A poll that was out when one of these began: its answer is dropped.
      if (t.aborted || t.ours || compacting()) continue
      // New posts are counted since the last turn end; the counters, a stall
      // and reactions are not: the same one again backs off.
      const key = r.reason === "addressed" || r.reason === "digest" ? "" : `${r.reason}:${r.count}`
      const now = Date.now()
      if (key && key === t.wakeKey) {
        if (now < t.wakeAfter) {
          await pause(RETRY)
          continue
        }
        t.wakeRepeats += 1
      } else {
        t.wakeRepeats = 0
      }
      t.wakeKey = key
      t.wakeAfter = now + wakeBackoffMs(t.wakeRepeats)
      const idle = !t.running
      await stat("wake", { reason: String(r.reason || "") })
      await inject(r.reason === "digest" ? "digest" : "other", String(r.text))
      deliver(String(r.text))
      if (idle) {
        // The bus's turn keeps the drain rounds: only the human's next prompt
        // starts them afresh.
        t.byBus = true
        await stat("turn", { by: "bus" })
      }
    }
  }

  pi.on("session_start", async (_event, c) => {
    ctx = c
    const dir = real(c.cwd) || c.cwd
    label = basename(dir).slice(0, 64) // the presence label's limit (RT-6.17)
    bus = hookApi(env, dir)
    if (root) {
      try {
        pi.registerMcpServer(SERVER, mcpConfig(root, dir, env))
      } catch (e) {
        notify(`${SERVER}: MCP server not registered: ${e instanceof Error ? e.message : String(e)}`)
      }
    }
    Object.assign(split, { meter: new Meter(), seg: null, summary: null, last: null })
    ac?.abort()
    ac = new AbortController()
    closed = false
    void listen(ac.signal)
  })

  pi.on("resources_discover", () => (root ? { skillPaths: [join(root, "skills", SKILL)] } : undefined))

  // A human's prompt (RT-6.27 step 3): pi's own input from an extension, ours
  // included, is not one.
  pi.on("input", async (event, c) => {
    ctx = c
    if (event.source === "extension") return
    t.human += 1
    Object.assign(t, { aborted: false, drains: 0, signoffAsked: false, byBus: false, wakeKey: "", wakeRepeats: 0,
                       wakeAfter: 0 })
    await stat("turn", { by: "human" })
    nudge()
  })

  // Not awaited, here and at turn_end: pi waits for its handlers, and a run
  // must not wait for the bus.
  pi.on("agent_start", (_event, c) => {
    ctx = c
    t.running = true
    if (!t.working) {
      t.working = true
      void presence("working").catch(() => {})
    }
  })

  pi.on("turn_end", (_event, c) => {
    ctx = c
    void usage().catch(() => {})
  })

  // The token split: each request's context, then its answer's usage. Reads
  // only: the handler returns nothing, so pi sends the messages unchanged.
  pi.on("context", (event) => {
    try {
      split.seg = contextSegments(event.messages, (text) => split.ours.get(text), split.summary)
      split.last = split.seg.shares()
    } catch {
      split.seg = null // the split is an estimate: without it, the request counts as task work
    }
  })
  pi.on("message_end", (event) => {
    if ((event.message as any)?.role !== "assistant") return
    try {
      split.meter.add(event.message, split.seg)
    } catch {
      // as above
    }
    split.seg = null
  })

  // Not awaited: pi waits for its settle handlers, and the human must not.
  pi.on("agent_settled", (event, c) => {
    ctx = c
    void usageStat().catch(() => {})
    void turnEnded(Boolean(event.aborted)).catch(() => {})
  })

  // A compaction: no wake-up meanwhile (RT-6.27 step 5).
  pi.on("session_before_compact", (_event, c) => {
    ctx = c
    t.compacting = Date.now()
  })
  pi.on("session_compact_failed", (_event, c) => {
    ctx = c
    t.compacting = 0
    nudge()
  })

  // After a compaction: the own latest posts and the bus skill as context that
  // starts no run, not shown: the human needs neither in the terminal (RT-6.7).
  pi.on("session_compact", async (_event, c) => {
    ctx = c
    t.compacting = 0
    split.summary = split.last // the summary stands for the context it replaced
    nudge()
    const r = await bus("GET", "/v1/handoff")
    const text = String(r?.context || "")
    if (!text) return
    await stat("compact", { chars: text.length })
    try {
      pi.sendMessage({ customType: SERVER, content: text, display: false }, { triggerTurn: false })
    } catch {
      // the next turn goes without it
    }
  })

  pi.on("session_shutdown", () => {
    closed = true
    ac?.abort() // ends a long poll in flight
    nudge()
  })

  return { state: t }
}

export default function mattermostAgentBus(pi: ExtensionAPI) {
  create(pi)
}
