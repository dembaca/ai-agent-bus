// OpenCode plugin: the AI agent bus in OpenCode sessions (RT-6.20 in
// docs/arc42/06-runtime-view.md; install: adapters/opencode.md). Plugin API V2
// (OpenCode 2.x); OpenCode 1.x (plugin API V1) is not supported.
//
// Thin glue: the session's MCP server decides and words everything; this file
// turns OpenCode's hooks and events into calls to the server's hook API
// (RT-6.17) and the server's answers into session input:
//   mcp.transform      the MCP server (unless configured)
//   skill.transform    the skill
//   hook "prompt"      a human's prompt starts a turn: presence working (RT-6.2, RT-6.3)
//   session.execution.succeeded / .failed
//                      a turn ends: GET /v1/turn-end → drain, answer (a bus
//                      post without a reply in its thread) or sign-off as
//                      synthetic input that starts the next round, else
//                      presence "on the bus" (reachable, no listen window:
//                      the session is reachable as long as it lives, like a
//                      Claude Code channel, RT-6.12; the Stop hook, RT-6.2)
//   session.execution.interrupted
//                      the human stopped the turn: presence idle, no checks (RT-6.9)
//   one long poll      GET /v1/wake?wait=50 (ADR-009), in flight between turns:
//                      the server answers when the bus has something → synthetic
//                      input that tells the agent to collect with
//                      wait_for_events (the listener's rules, RT-6.2 step 4);
//                      no timer, no listen window; the same counter again
//                      after the turn it woke backs off; "replaced" backs off
//   session.compaction.ended
//                      GET /v1/handoff → the own latest posts and the bus
//                      skill as synthetic context that starts no turn
//                      (RT-6.7); a compaction between turns (/compact) runs
//                      as an execution of its own, whose end is no turn end
//   session.step.*, .text/.reasoning.ended, .tool.*
//                      the token split (usage.ts, RT-6.9 step 2): POST /v1/stat
//                      {k: "usage", …} at each turn end, numbers only
// It holds no token and no secret, writes no file, and fails open: without a
// server or a bus session every call comes back empty and nothing happens.
// Only top-level sessions of its own location count: a subagent's session
// (parentID) is left alone. OpenCode 2 runs one plugin instance per location
// in its long-running service and unloads a location after an hour without
// use: the turn state is kept in the plugin's storage, so a reload goes on.
// One bus session per OpenCode process (RT-6.20 step 1, RT-6.18 step 6,
// #119): with a corral session's relay variables the first top-level session
// that prompts carries the bus, in any server, shared or private; every other
// top-level session is told so on its prompt, and its hooks and bus tools do
// nothing. Without them every top-level session of the location has its server.

import type { Plugin } from "@opencode/plugin"
import { createHash } from "node:crypto"
import { existsSync, readFileSync, realpathSync } from "node:fs"
import { homedir } from "node:os"
import { basename, dirname, join } from "node:path"
import { fileURLToPath, pathToFileURL } from "node:url"
import type { Tracker } from "./usage.ts"

export const SERVER = "ai-agent-bus"
export const SKILL = "ai-agent-bus"
// Marks the input this plugin sends, so the prompt hook tells it from the human's.
export const MARK = "ai-agent-bus"
const QUICK_MS = 3000
// /v1/turn-end reads several channels (the Stop hook's 8 s).
const SLOW_MS = 8000
// The long poll of /v1/wake: the server holds it WAIT_SEC (at most 55) and
// answers empty; the read timeout is a margin above (RT-6.17 step 7).
export const WAIT_SEC = 50
const READ_MARGIN_MS = 10_000
// After a failed or empty-handed poll, a wake that backs off, and as the base
// of the pause after "replaced" (doubling, at most REPLACED_MAX_MS).
export const RETRY_SEC = 15
const REPLACED_MAX_MS = 60_000
// A turn running or no session known: asked again when something changes,
// at the latest after this.
const IDLE_PAUSE_MS = 60_000
const WAKE_RETRY_MS = 60_000
// The same unread counter, stall or reaction again after the turn it woke: a
// counter wait_for_events cannot clear. Wait 1, 2, 4 … min, at most 30.
export const WAKE_BACKOFF_MS = 60_000
export const WAKE_BACKOFF_MAX_MS = 30 * 60_000
const RESUBSCRIBE_MS = 1000
const KEEP_MS = 24 * 3600_000 // turn state older than a day is not restored
// How long the first prompt waits for OpenCode to reload its MCP servers (RT-6.20 step 1).
const RELOAD_MS = 10_000
const BOTS_KEY = "ai-agent-bus/bots" // OpenCode session id → its bot, to tell a changed one (RT-6.20 step 3)

type Env = Record<string, string | undefined>
type Json = Record<string, any>

type Kind = "drain" | "answer" | "signoff" | "wake" | "context"

export type Options = {
  // The bus checkout (with mcp/ and skills/); default: where this file really lies.
  root?: string
  // false: add no MCP server (the user configures one).
  mcp?: boolean
  // The MCP server's command instead of `uv run … mattermost-ai-agent-bus-mcp`,
  // e.g. one that fetches MM_REG_SECRET itself (adapters/opencode.md).
  command?: string[]
  environment?: Record<string, string>
  // How long the server holds the long poll of /v1/wake (s, 10 to 55; default 50).
  waitSec?: number
  // The pause after a failed or empty-handed poll, and the base of the one
  // after "replaced" (s, default 15).
  retrySec?: number
}

// keep-in-step: from here to the end marker, the same text in
// opencode/plugin/mattermost-agent-bus.ts and pi/mattermost-agent-bus.ts
// (pi/test/ compares them; RT-6.20 step 2, RT-6.27 step 2).

// The CB_ names (docs/env-names.md, #169): the reading rule of the bus's page.
// The new name first; the old one when only it is set, with one deprecation
// warning per process (none for the relay's internal values, none in a keyless
// sandbox, whose host hook wrote the old names); an unexpanded ${NAME} counts as
// unset; an empty value counts as set, so an empty new name never falls back;
// two names with different values are an EnvConflict naming both, never a value,
// reported once per process (the plugin then behaves as without a bus). A file
// the bus wrote keeps the last line of either spelling, nothing refuses.
const PLACEHOLDER = /^\$\{[A-Za-z_][A-Za-z0-9_]*\}$/
const warned = new Set<string>()
const conflictsWarned = new Set<string>()

export class EnvConflict extends Error {}

export function envFirst(env: Env, names: readonly string[], warn = true): string {
  const found = names
    .map((n) => [n, env[n]] as const)
    .filter(([, v]) => v !== undefined && !PLACEHOLDER.test(v as string))
  if (!found.length) return ""
  if (new Set(found.map(([, v]) => v)).size > 1) {
    throw new EnvConflict(`${found.map(([n]) => n).join(", ")} are set to different values; ` +
                          `set only ${names[0]}`)
  }
  const [from, value] = found[0]
  if (warn && from !== names[0] && !keyless(env) && !warned.has(from)) {
    warned.add(from)
    console.warn(`[${SERVER}] ${from} is deprecated; set ${names[0]} instead ` +
                 `(the old name goes in a later release)`)
  }
  return value as string
}

// The relay's values, which the host hook sets: never a deprecation warning.
export function relayEnv(env: Env, names: readonly string[]): string {
  return envFirst(env, names, false)
}

// A keyless sandbox: the host hook started the session's server and wrote the
// settings here, an older hook under the old names only (#169). As
// config.keyless_sandbox: an empty value counts as set, ${NAME} as unset.
export function keyless(env: Env): boolean {
  return ["CB_BUS_MCP_ENDPOINT", "MM_BUS_MCP_ENDPOINT"].some((n) => {
    const v = env[n]
    return v !== undefined && !PLACEHOLDER.test(v)
  })
}

export function reportConflict(e: EnvConflict): void {
  if (conflictsWarned.has(e.message)) return
  conflictsWarned.add(e.message)
  console.warn(`[${SERVER}] ${e.message}`)
}

// A setting of one of the bus's own files: the last line of either spelling
// wins (bin/mm-agent-lib.sh's mm_kv and config.value read alike).
export function fileFirst(text: string, names: readonly string[]): string {
  let got = ""
  for (const line of text.split("\n")) {
    const i = line.indexOf("=")
    if (i > 0 && names.includes(line.slice(0, i))) got = line.slice(i + 1)
  }
  return got
}
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
// session's directory), whose CB_WORKSPACE names that real directory (RT-6.17
// step 2; the name as mm_bus_file in bin/mm-agent-lib.sh writes it). No session id, no other
// variable, no walk-up: inherited from a Claude Code shell, or a parent
// directory's file, they may name another session's server.
export function busFile(env: Env, directory: string, prefix = "endpoint"): string | null {
  const state = join(env.XDG_STATE_HOME || join(env.HOME || homedir(), ".local", "state"), "mm-agent-bus")
  const dir = real(directory)
  const f = dir && join(state, `${prefix}-${sha16(dir)}.env`)
  return f && existsSync(f) ? f : null
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
    let endpoint = ""
    let key = ""
    try {
      endpoint = relayEnv(env, ["CB_BUS_ENDPOINT", "MM_BUS_ENDPOINT"])
      key = relayEnv(env, ["CB_BUS_KEY", "MM_BUS_KEY"])
    } catch (e) {
      if (!(e instanceof EnvConflict)) throw e
      reportConflict(e)
      return null
    }
    if (!endpoint || !key) {
      const f = busFile(env, directory)
      if (!f) {
        missing()
        return null
      }
      try {
        const text = readFileSync(f, "utf8")
        endpoint = fileFirst(text, ["CB_BUS_ENDPOINT", "MM_BUS_ENDPOINT"])
        key = fileFirst(text, ["CB_BUS_KEY", "MM_BUS_KEY"])
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

// The long poll's wait in seconds: the option, else WAIT_SEC; 10 to 55 (a
// shorter one would defeat the pause for a server that answers at once).
export function waitSec(options: Options): number {
  const sec = Number(options.waitSec ?? WAIT_SEC)
  return Math.min(55, Math.max(10, Number.isFinite(sec) && sec > 0 ? Math.floor(sec) : WAIT_SEC))
}

// The least pause after a failed poll, as the old `pollSec` had: a server that
// is gone must not be asked in a tight loop. `timing` is the tests' to lower.
export const timing = { minRetrySec: 5 }

// The pause after a failed or empty-handed poll in ms: the option, else
// RETRY_SEC, at least `timing.minRetrySec`.
export function retryMs(options: Options): number {
  const sec = Number(options.retrySec ?? RETRY_SEC)
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
export function chars(text: string): number {
  return [...text].length
}

// The token split (usage.ts) beside this file; a copied file finds it in the checkout
// (root). Without it there is no `usage` stat, the rest works.
type Split = typeof import("./usage.ts")
// What setup() loads the split with (a test swaps it).
export const usageLoader = { load: (root: string | null) => loadSplit(root) }
export async function loadSplit(root: string | null): Promise<Split | null> {
  const specs = ["./usage.ts", ...(root ? [pathToFileURL(join(root, "opencode", "plugin", "usage.ts")).href] : [])]
  for (const spec of specs) {
    try {
      return (await import(spec)) as Split
    } catch {
      // not there: the next place
    }
  }
  return null
}

// The bus checkout: the option, CB_AGENT_BUS_ROOT (the old MM_ name still
// read), or the checkout this file
// really lies in (opencode/plugin/) (a symlink into .opencode/plugins/ resolves).
// The checkout from the environment: CB_AGENT_BUS_ROOT first (#169). A conflict
// is reported once and yields none, so the plugin finds its own checkout.
function rootOf(env: Env): string {
  try {
    return envFirst(env, ["CB_AGENT_BUS_ROOT", "MM_AGENT_BUS_ROOT"])
  } catch (e) {
    if (!(e instanceof EnvConflict)) throw e
    reportConflict(e)
    return ""
  }
}

export function busRoot(options: Options, env: Env, here: string): string | null {
  for (const dir of [options.root, rootOf(env), dirname(dirname(dirname(real(here) || here)))]) {
    if (dir && existsSync(join(dir, "mcp", "pyproject.toml"))) return dir
  }
  return null
}

// A corral session's relay variables (RT-6.18): the bus of this process is
// the host's, which serves one agent with one bot.
export function relayVars(env: Env): boolean {
  try {
    return Boolean(relayEnv(env, ["CB_BUS_MCP_ENDPOINT", "MM_BUS_MCP_ENDPOINT"]) ||
                   relayEnv(env, ["CB_BUS_ENDPOINT", "MM_BUS_ENDPOINT"]))
  } catch (e) {
    if (!(e instanceof EnvConflict)) throw e
    reportConflict(e)
    return true  // a relay is there; the call reports the conflict
  }
}

// The session that carries the bus under relay variables (RT-6.20 step 1),
// per process. Kept on globalThis, so every plugin instance of the process,
// and one that OpenCode loads again after an idle unload, sees the same; never
// stored on disk: a new process binds afresh. refused: the host's hook API
// serves another OpenCode session (RT-6.18 step 6): this process has no bus.
export type Bound = { id: string; dir: string; refused?: boolean }
const BOUND = Symbol.for("ai-agent-bus.bound")
export function bindings(): Map<string, Bound> {
  const g = globalThis as any
  return (g[BOUND] ??= new Map<string, Bound>())
}

// What a top-level session that does not carry the bus is told (RT-6.20 step 1).
export const NO_BUS = "No bus in this session: this OpenCode server already carries the bus for another session " +
  "(one agent, one bot). Start `opencode` on its own for this session, in its own corral session (adapters/opencode.md)."
// What the bound session is told when the host serves another OpenCode
// session (RT-6.18 step 6): a restart with a new session, a second process.
export const HOST_REFUSED = "No bus in this session: this corral session's bus already serves another OpenCode " +
  "session (one agent, one bot). Continue that session (`opencode --continue` or `--session <id>`) or start a new " +
  "corral session (adapters/opencode.md)."
// The tool a refused bus tool call is pointed at (RT-6.20 step 1): it fails
// with the reason, so the call never reaches the bus server.
export const REFUSAL_TOOL = "ai_agent_bus_not_in_this_session"

// What the bound session is told when its bot is another than the one it had
// (RT-6.20 step 3): the name changed, nothing else carries over.
export function renamedText(old: string, now: string): string {
  return `Your bus bot changed for this OpenCode session: @${old} → @${now}. Nothing carries over from @${old} ` +
    `(threads, asks, trust): say so in your next HELLO (announce) if others knew you as @${old}.`
}

// A local MCP entry that runs the bus's server, under any name.
const runsBus = (cfg: any): boolean =>
  Array.isArray(cfg?.command) && cfg.command.some((a: unknown) => String(a).includes("mattermost-ai-agent-bus-mcp"))

// The MCP transform of a location whose sessions do not carry the bus under
// relay variables: every bus server goes, the plugin's name and a
// hand-configured one alike, or it would relay with no session or another's
// (RT-6.20 step 1). A host without remove() keeps them (none known: 2.1.2 has
// it). The names removed.
export function dropMcp(editor: any): string[] {
  if (typeof editor?.remove !== "function") return []
  const names = editor.list().filter(([n, cfg]: [string, any]) => n === SERVER || runsBus(cfg)).map(([n]: [string]) => n)
  if (editor.get(SERVER) && !names.includes(SERVER)) names.push(SERVER)
  for (const n of names) editor.remove(n)
  return names
}

// The MCP transform: the server, unless the user has it (by name, or another
// local entry that runs mattermost-ai-agent-bus-mcp). true: the user's server
// has no CB_WORKSPACE, so its endpoint file may lie where the plugin does not look.
// session: the bound OpenCode session's id, as OPENCODE_SESSION_ID, which the
// relay hands the host as an identity value (RT-6.18 step 6, RT-6.20 step 1);
// a server configured by hand gets it too.
// The server entry's two settings go under both names. A user's `environment`
// that names one spelling of a pair is copied to the other, so the server never
// sees a disagreement it would refuse (#169, RT-6.20 step 2). Both set to
// different values stays a disagreement, as it should.
const SERVER_PAIRS: ReadonlyArray<readonly [string, string]> = [
  ["CB_WORKSPACE", "MM_WORKSPACE"],
  ["CB_CHANNEL_PUSH", "MM_CHANNEL_PUSH"],
]

export function bothSpellings(environment: Record<string, string>): Record<string, string> {
  const out = { ...environment }
  for (const [a, b] of SERVER_PAIRS) {
    if (a in environment && !(b in environment)) out[b] = environment[a]
    else if (b in environment && !(a in environment)) out[a] = environment[b]
  }
  return out
}

export function addMcp(editor: any, options: Options, root: string | null, directory: string,
                       env: Env = {}, session = ""): boolean {
  const ownName: string | undefined = editor.get(SERVER) ? SERVER
    : editor.list().find(([, cfg]: [string, any]) => runsBus(cfg))?.[0]
  const own = ownName ? editor.get(ownName) : undefined
  if (own && session && typeof editor.update === "function") {
    editor.update(ownName, (cfg: any) => {
      cfg.environment = { ...(cfg.environment ?? {}), OPENCODE_SESSION_ID: session }
    })
  }
  if (options.mcp !== false && !own) {
    const command =
      options.command ??
      (root ? ["uv", "run", "--locked", "--project", join(root, "mcp"), "mattermost-ai-agent-bus-mcp"] : null)
    // The secret is never written here: the server inherits the OpenCode
    // service's environment, or the user's command fetches it
    // (adapters/opencode.md). A Claude Code session's id and project,
    // inherited when the service was started from its shell, would key the
    // server's files on that session: blanked, and the workspace named, where
    // busFile looks (RT-6.20 step 2). No channel push: that is Claude Code's.
    const dir = real(directory) || directory
    // The default command's Python environment in the user's cache (venvDir):
    // the tree may be read-only, a checkout under a corral read grant or the
    // host copy (RT-6.28), whose own mcp/.venv is the host's.
    const venv = !options.command && root ? { UV_PROJECT_ENVIRONMENT: venvDir(env, root) } : {}
    // The two settings go under both names, so a child of either version reads
    // them (#169); the server reads CB_ first.
    const environment = { CLAUDE_CODE_SESSION_ID: "", CLAUDE_PROJECT_DIR: "",
                          CB_CHANNEL_PUSH: "0", MM_CHANNEL_PUSH: "0",

                          CB_WORKSPACE: dir, MM_WORKSPACE: dir,
                          ...venv, ...bothSpellings(options.environment ?? {}),
                          ...(session ? { OPENCODE_SESSION_ID: session } : {}) }
    // Direct tools, not Code Mode (issue #152): the model sees the bus tools and
    // their docs as written, as in Claude Code and pi, and the token split counts
    // bus calls exactly (#150). It costs about 2,460 tokens of tool docs in every
    // request (≈3,240 with the instructions, mostly cache reads); Code Mode is
    // cheaper on the window but hides the tools behind `execute` — measured in
    // #152. `false` is the server's whole setting, no per-tool one.
    if (command) editor.set(SERVER, { type: "local", command, environment, cwd: dir, codemode: false })
  }
  return Boolean(own && !own.environment?.CB_WORKSPACE && !own.environment?.MM_WORKSPACE)
}

// The skill as OpenCode reads a SKILL.md: name and description from the
// front matter, the rest as content; path next to coordinator.md and
// self-registration.md, which the skill names.
export function skillInfo(root: string | null): Json | null {
  const path = root ? join(root, "skills", SKILL, "SKILL.md") : ""
  let text: string
  try {
    text = readFileSync(path, "utf8")
  } catch {
    return null
  }
  const m = text.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?/)
  const front = m ? m[1].split(/\r?\n/) : []
  const field = (name: string): string => {
    const i = front.findIndex((l) => l.startsWith(`${name}:`))
    if (i < 0) return ""
    const value = front[i].slice(name.length + 1).trim()
    if (!/^[>|][-+]?$/.test(value)) return value.replace(/^["']|["']$/g, "")
    const block: string[] = []
    for (const l of front.slice(i + 1)) {
      if (!/^\s+\S/.test(l)) break
      block.push(l.trim())
    }
    return block.join(" ")
  }
  const description = field("description")
  return { id: SKILL, name: field("name") || SKILL, ...(description ? { description } : {}), path,
           content: m ? text.slice(m[0].length) : text }
}

type Turn = {
  busy: boolean
  idleAt: number
  drains: number
  signoffAsked: boolean
  byBus: boolean
  aborted: boolean // until the human's next prompt: no checks, no wake-up
  inFlight: boolean
  listening: boolean // the server's gates hold (it opened a window): the long poll runs; no end by time
  heldSince: number // epoch ms: the `since` of a wake dropped while a turn ran, kept for the next poll (0: none)
  wakeKey: string // the last wake-up's counter (reason:count), "" for new posts
  wakeRepeats: number
  wakeAfter: number // epoch ms: no wake-up for the same counter before
  sent: string // the text of the input the plugin sent last
  turn: boolean // a turn started by a prompt or the plugin's input runs
  compactOnly: boolean // the running execution only compacts (/compact between turns)
}

// The events the token split reads (usage.ts): a request's usage, the text and tool
// calls it wrote, tool results, skills loaded, compactions; no deltas.
const TRACKED = /^session\.(step\.(started|ended|failed)|(text|reasoning)\.ended|tool\.(input\.started|called|success|failed)|skill\.activated|compaction\.(started|ended|failed))$/
const fresh = (): Turn => ({ busy: false, idleAt: Date.now(), drains: 0, signoffAsked: false, byBus: false, aborted: false,
  inFlight: false, listening: false, heldSince: 0, wakeKey: "", wakeRepeats: 0, wakeAfter: 0, sent: "", turn: false, compactOnly: false })

export async function setup(ctx: Plugin.Context) {
  const options = (ctx.options ?? {}) as Options
  const directory = String(ctx.location.directory)
  const realDir = real(directory) || directory
  const env = process.env as Env
  // One bus session per process under relay variables (RT-6.20 step 1,
  // #119): not the server's command line, which may be spelt in any way,
  // decides, but which session prompted first.
  const relay = relayVars(env)
  const bound = (): Bound | undefined => (relay ? bindings().get("process") : undefined)
  const storeKey = `${SERVER}/${sha16(realDir)}`
  let unkeyed = false // a hand-configured server without CB_WORKSPACE
  let warned = false
  const bus = hookApi(env, directory, () => {
    if (!unkeyed || warned) return
    warned = true
    console.warn(`[${SERVER}] no MCP server found for ${directory}; set CB_WORKSPACE in its environment ` +
                 `(MM_WORKSPACE is still read; adapters/opencode.md)`)
  }, {
    // Every call names the bound session; the host refuses another's (RT-6.18 step 6).
    session: () => bound()?.id || "",
    forbidden: () => void hostRefused(),
  })
  const label = basename(realDir).slice(0, 64) // the presence label's limit (RT-6.17)
  const turns = new Map<string, Turn>() // top-level sessions only
  const tops = new Map<string, boolean>() // session id → a top-level session of this location
  const parents = new Map<string, string>() // session id → its parent's ("" for a top-level one)
  const told = new Set<string>() // top-level sessions told that they carry no bus
  const busNames = new Set<string>([SERVER]) // the MCP servers that run the bus here
  let botChecked = false
  let last = "" // the top-level session that had a prompt or ended a turn last: the one a wake starts
  let wakeFailedAt = 0
  let closed = false
  // The token split per top-level session (RT-6.9 step 2): in memory, a reload starts it afresh.
  let split: Split | null = null
  const trackers = new Map<string, Tracker>()
  // Every call into the split goes through here: a throw there never skips the
  // plugin's own handling (a turn end, a compaction, a send's result).
  const counting = (f: () => void) => {
    try {
      f()
    } catch {
      // the split is an estimate: this event is left out
    }
  }
  const track = (id: string): Tracker | null => {
    if (!split) return null
    let tr = trackers.get(id)
    if (!tr) {
      tr = new split.Tracker(() => busNames)
      trackers.set(id, tr)
    }
    return tr
  }

  // The turn state survives an unload of the location (restart-safe).
  try {
    const saved: any = await ctx.storage.get(storeKey)
    for (const [id, t] of Object.entries<any>(saved?.turns ?? {})) {
      // listenUntil: the state of a plugin before ADR-009 (a window going on counts as listening).
      if (Date.now() - Number(t?.idleAt || 0) < KEEP_MS)
        turns.set(id, { ...fresh(), ...t, busy: false, inFlight: false, turn: false, compactOnly: false,
                        listening: Boolean(t?.listening) || Number(t?.listenUntil || 0) * 1000 > Date.now() })
    }
    if (turns.has(saved?.last)) last = saved.last
  } catch {
    // no storage: start empty
  }
  const save = async () => {
    try {
      const out: Json = {}
      for (const [id, t] of turns) out[id] = { ...t, busy: false, inFlight: false }
      await ctx.storage.set(storeKey, { last, turns: out })
    } catch {
      // best effort
    }
  }

  const turn = (id: string): Turn => {
    let t = turns.get(id)
    if (!t) {
      t = fresh()
      turns.set(id, t)
    }
    return t
  }
  const anyBusy = () => [...turns.values()].some((t) => t.busy || t.inFlight)
  const ours = (loc: any) => !loc?.directory || (real(String(loc.directory)) || String(loc.directory)) === realDir

  const topLevel = async (id: string): Promise<boolean> => {
    if (!id) return false
    if (tops.has(id)) return tops.get(id)!
    try {
      const info: any = await ctx.session.get({ sessionID: id } as any)
      if (!info?.id) return false
      parents.set(id, String(info.parentID || ""))
      const top = !info.parentID && ours(info.location)
      tops.set(id, top)
      return top
    } catch {
      return false
    }
  }

  // The session this instance serves: under relay variables the bound one, if
  // it is of this location and the host serves it; else any top-level one.
  const carries = (id: string): boolean => {
    if (!relay) return Boolean(id)
    const b = bound()
    return Boolean(id) && b?.id === id && b.dir === realDir && !b.refused
  }
  // The first top-level session that prompts binds the bus (RT-6.20 step 1);
  // the MCP server is added only now, with its id: OpenCode reloads its MCP
  // servers, awaited (at most RELOAD_MS) before the prompt's turn runs.
  const bind = async (id: string) => {
    if (bound()) return
    bindings().set("process", { id, dir: realDir })
    const reload = (ctx.mcp as any).reload
    if (typeof reload !== "function") return
    let timer: ReturnType<typeof setTimeout> | undefined
    await Promise.race([Promise.resolve().then(() => reload()).catch(() => {}),
                        new Promise<void>((r) => (timer = setTimeout(r, RELOAD_MS)))])
    clearTimeout(timer)
  }
  // A top-level session that does not carry the bus: told once, visibly, in
  // the session, and in OpenCode's log (RT-6.20 step 1).
  const refuse = async (id: string, text = NO_BUS) => {
    if (told.has(id)) return
    told.add(id)
    console.warn(`[${SERVER}] ${text}`)
    try {
      await ctx.session.synthetic({ sessionID: id, text, description: "AI agent bus: not in this session",
        metadata: { [MARK]: "nobus" }, delivery: "steer", resume: false } as any)
    } catch {
      // the log line stays
    }
  }
  // A session of the bound one's tree (itself or its subagents, by parentID):
  // the bus tools serve it (RT-6.20 step 1).
  const inTree = async (id: string): Promise<boolean> => {
    for (let cur = id, hops = 0; cur && hops < 16; hops++) {
      if (carries(cur)) return true
      if (!parents.has(cur)) {
        try {
          const info: any = await ctx.session.get({ sessionID: cur } as any)
          parents.set(cur, String(info?.parentID || ""))
        } catch {
          return false
        }
      }
      cur = parents.get(cur) || ""
    }
    return false
  }
  // Ours and bound: the hooks act on the bus only for it (RT-6.20 step 1).
  const mine = async (id: string): Promise<boolean> => (await topLevel(id)) && carries(id)
  // The host's hook API refused the bound session (403, RT-6.18 step 6): this
  // process has no bus; the session is told once and the hooks stop.
  const hostRefused = async () => {
    const b = bound()
    if (!b || b.refused) return
    b.refused = true
    if (b.dir === realDir) await refuse(b.id, HOST_REFUSED)
    nudge()
  }

  const stat = (k: string, fields: Json = {}) => bus("POST", "/v1/stat", { k, ...fields })
  // A text the plugin puts into the context, by kind and length only, never the text (RT-6.9 step 1).
  const inject = (name: string, text: string) => stat("inject", { name: injectKind(name), chars: chars(text) })
  // One call at a time: the prompt hook's `working` and an interrupt's `idle`
  // (the event stream) never overtake each other.
  const setPresence = presenceQueue((state) => bus("POST", "/v1/presence", { state, label }))
  const presence = (state: "working" | "idle") => setPresence(state)
  // Reachable between turns, renewed by the server's heartbeat (RT-6.6 step 1).
  const reachable = () => setPresence("bus")

  // Synthetic input: the plugin's, not the human's. It starts a turn (resume),
  // except the context after a compaction, which waits for the next one.
  const send = async (id: string, text: string, kind: Kind, description: string): Promise<boolean> => {
    try {
      const t = turn(id)
      t.sent = text
      if (kind !== "context") t.turn = true
      await ctx.session.synthetic({ sessionID: id, text, description: description || "AI agent bus",
        metadata: { [MARK]: kind }, delivery: kind === "context" ? "steer" : "queue", resume: kind !== "context" } as any)
      counting(() => track(id)?.injected(text)) // the plugin's texts carry no post body: overhead whole
      return true
    } catch {
      return false
    }
  }

  // A human's prompt starts a turn (RT-6.20 step 3).
  const humanTurn = async (id: string, text = "") => {
    if (!(await topLevel(id))) return
    if (!carries(id)) {
      await refuse(id, bound()?.id === id && bound()?.refused ? HOST_REFUSED : NO_BUS)
      return
    }
    counting(() => track(id)?.user(text))
    const t = turn(id)
    Object.assign(t, { busy: true, turn: true, aborted: false, drains: 0, signoffAsked: false, byBus: false,
                       heldSince: 0, wakeKey: "", wakeRepeats: 0, wakeAfter: 0 })
    // `listening` stays as the last turn end left it: this turn's end decides (a failed check keeps it).
    last = id
    // Requested with `busy`, before the stat: an interrupt's idle comes after it (RT-6.20 step 3).
    const working = presence("working")
    await stat("turn", { by: "human" })
    await working
    await save()
    nudge()
    await checkBot(id)
  }

  // The bot of the bound session, once per setup: another than the one it had
  // before (a new registration after a restart) is said in the session, old →
  // new; no trust carries over (RT-6.20 step 3, #119).
  const checkBot = async (id: string) => {
    if (botChecked) return
    const r = await bus("GET", "/v1/me")
    const now = String(r?.username || "")
    if (!now) return
    botChecked = true
    try {
      const bots: Json = ((await ctx.storage.get(BOTS_KEY)) as Json | undefined) ?? {}
      const old = String(bots[id]?.username || "")
      if (old && old !== now) {
        const text = renamedText(old, now)
        await inject("other", text)
        await send(id, text, "context", "AI agent bus: your bot changed")
      }
      bots[id] = { username: now, at: Date.now() }
      for (const [k, v] of Object.entries<any>(bots)) if (Date.now() - Number(v?.at || 0) > 30 * 86400_000) delete bots[k]
      await ctx.storage.set(BOTS_KEY, bots)
    } catch {
      // no storage: nothing to compare
    }
  }

  // The Stop hook's checks, asked of the server (RT-6.20; RT-6.2 steps 1–2).
  const turnEnded = async (id: string) => {
    if (!(await mine(id))) return
    const t = turn(id)
    if (t.inFlight) return
    // The token split of the requests since the last turn end, numbers only (RT-6.9 step 2).
    let usage: Json | null | undefined = null
    counting(() => (usage = trackers.get(id)?.take()))
    if (usage) await stat("usage", usage)
    if (t.compactOnly) {
      // A compaction between turns runs as an execution of its own: no turn
      // ended, the presence and the listen window stay as they are.
      t.compactOnly = false
      t.busy = false
      return
    }
    t.turn = false
    if (t.aborted) return
    t.busy = false
    t.idleAt = Date.now()
    last = id
    t.inFlight = true
    try {
      // unanswered=1: a bus post without a reply in its thread blocks once (RT-6.2 step 2).
      const q = `drains=${t.drains}&signoff_asked=${+t.signoffAsked}&by_bus=${+t.byBus}&push=1&unanswered=1`
      const r = await bus("GET", `/v1/turn-end?${q}`, undefined, SLOW_MS)
      await loginLine(id) // a grant that started after the session start (#198)
      // The posts an answer names are marked only now that the block goes out, so
      // a dropped check leaves them due; an answer alone that cannot be marked
      // does not block, so a server that cannot mark never blocks in a loop.
      // Not after an interrupt, a close or another session's turn: nothing would block.
      const posts = Array.isArray(r?.posts) && !closed && !t.aborted && last === id ? r.posts : []
      const marked = posts.length > 0 && !!(await bus("POST", "/v1/reminded", { post_ids: posts }))
      if (r && (r.action === "drain" || (r.action === "answer" && marked) || r.action === "signoff")) {
        if (r.action === "drain") t.drains += 1
        else if (r.action === "signoff") t.signoffAsked = true
        t.busy = true
        await stat("block", { kind: r.action })
        await inject(r.action, String(r.reason))
        if (await send(id, String(r.reason), r.action, String(r.banner || ""))) return
        t.busy = false
      }
      if (r) await stat("turn_end")
      // A window the server opened (RT-6.2 step 3) says its gates hold: the
      // session is reachable and the long poll runs, with no end by time
      // (ADR-009). None (a BYE, no project channel, the listener off): idle.
      if (!r) {
        // The check failed (timeout, 503): nothing is known, so what held stays:
        // a session that was listening keeps listening, reachable while it lives
        // (the status says so, not "working" from the turn).
        await (t.listening ? reachable() : presence("idle"))
        nudge()
      } else if ((Number(r.listen_until) || 0) * 1000 > Date.now()) {
        t.listening = true
        await reachable()
        nudge()
      } else {
        t.listening = false
        await presence("idle")
      }
    } finally {
      t.inFlight = false
      await save()
    }
  }

  // The human stopped the turn: no checks, no turn end (RT-6.9), no wake-up
  // until their next prompt.
  const interrupted = async (id: string) => {
    if (!(await mine(id))) return
    const t = turn(id)
    t.aborted = true
    t.turn = false
    t.compactOnly = false
    if (t.busy) {
      t.busy = false
      await presence("idle")
    }
    await save()
  }

  // After a compaction: the own latest posts as context (RT-6.7).
  const compacted = async (id: string) => {
    if (!(await mine(id))) return
    const r = await bus("GET", "/v1/handoff")
    const text = String(r?.context || "")
    if (!text) return
    await stat("compact", { chars: text.length })
    await send(id, text, "context", "AI agent bus: your latest posts")
  }

  // A pause the loop ends early when something changes (nudge): a turn ended,
  // a prompt came, the plugin unloads.
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

  const WAIT = waitSec(options)
  const RETRY = retryMs(options)
  const ac = new AbortController()

  // The wake-up: the answer of the long poll, for the session it was asked for.
  // False: nothing was sent (a turn runs, backoff, a failure).
  const woken = async (id: string, t: Turn, r: Json, since: number): Promise<boolean> => {
    // A turn that started meanwhile holds it: asked again once it has ended,
    // with the same `since`: a post that wakes by its time only (addressed,
    // digest) would be older than the new idle time and never wake.
    if (!closed && anyBusy() && !t.aborted && last === id) {
      t.heldSince = Math.min(t.heldSince || since, since)
      return false
    }
    if (closed || t.aborted || last !== id) return false
    // New posts are counted since the session went idle; the counters,
    // a stall and reactions are not: the same one again backs off.
    const key = r.reason === "addressed" || r.reason === "digest" ? "" : `${r.reason}:${r.count}`
    const now = Date.now()
    if (key && key === t.wakeKey) {
      if (now < t.wakeAfter) return false
      t.wakeRepeats += 1
    } else {
      t.wakeRepeats = 0
    }
    t.wakeKey = key
    t.wakeAfter = now + wakeBackoffMs(t.wakeRepeats)
    // The bus's turn keeps the drain rounds: only the human's next prompt
    // starts them afresh.
    t.busy = true
    t.byBus = true
    await stat("wake", { reason: String(r.reason || "") })
    await inject(r.reason === "digest" ? "digest" : "other", String(r.text))
    if (await send(id, String(r.text), "wake", "AI agent bus: wake-up")) {
      // Requested before the stat, and not after an interrupt that came meanwhile
      // (it cleared `busy` and requested idle): working never follows that idle.
      const working = t.busy ? presence("working") : Promise.resolve()
      t.heldSince = 0
      await stat("turn", { by: "bus" })
      await working
      await save()
      return true
    }
    t.busy = false
    wakeFailedAt = Date.now()
    await save()
    return false
  }

  // The listener (RT-6.20 step 5, ADR-009 phase 2): one long poll of
  // /v1/wake in flight for the current session while no turn runs, renewed
  // when it ends, ended by the unload (ac). The server decides; it only looks.
  const listen = async () => {
    let replaced = 0
    while (!closed) {
      // Under relay variables, before a session prompted: the host hears that
      // a plugin waits for one and holds back its no-relay warning (RT-6.18 step 11).
      if (relay && !bound()) void bus("POST", "/v1/waiting", {})
      const id = last
      const t = id ? turns.get(id) : undefined
      // No session to wake, a turn at work (its human is there, and a wake
      // would only be held), no gates, or the last wake just failed.
      if (!id || !t || !carries(id) || !t.listening || t.aborted || anyBusy() || Date.now() - wakeFailedAt < WAKE_RETRY_MS) {
        await pause(id && Date.now() - wakeFailedAt < WAKE_RETRY_MS ? RETRY : IDLE_PAUSE_MS)
        continue
      }
      const asked = Date.now()
      const since = t.heldSince || t.idleAt
      // `until` (a day ahead) is for a server before 0.71.8, which refuses a wake without it
      // (400) and would be asked in vain for ever; later ones hold the request and ignore it.
      const until = Math.floor(asked / 1000) + 86400
      const r = await bus("GET", `/v1/wake?since=${since}&until=${until}&wait=${WAIT}`, undefined, WAIT * 1000 + READ_MARGIN_MS,
                          ac.signal)
      if (closed) break
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
        // Held for the wait it is empty at its end; an empty answer long before
        // is a server that does not hold the request: no tight loop.
        if (Date.now() - asked < (WAIT - 5) * 1000) await pause(RETRY)
        continue
      }
      if (!(await woken(id, t, r, since)) && !anyBusy() && !closed) await pause(RETRY) // backoff: look again in a while
    }
  }

  // The NATS login approval waiting now, as one synthetic line in the TUI (#198):
  // link and code only, never a token; marked shown so a later turn does not repeat it.
  const loginLine = async (id: string) => {
    try {
      if (!(await mine(id))) return
      const r = await bus("GET", "/v1/login")
      const link = typeof r?.link === "string" ? r.link : ""
      if (!link) return
      const code = typeof r?.user_code === "string" ? r.user_code : ""
      const text = `Bus login: open ${link}${code ? ` — the page shows the code ${code}` : ""}. ` +
                   "Approve only if it matches."
      // The plugin's own text, not the human's: no turn, and marked shown only after
      // it really went out (#198).
      if (!(await send(id, text, "context", "AI agent bus: login approval"))) return
      await bus("POST", "/v1/login/shown", {})
    } catch {
      // no server yet: a later turn's end shows the same line
    }
  }

  const onEvent = async (ev: any) => {
    const d = ev?.data ?? {}
    const id = String(d.sessionID || "")
    const type = String(ev?.type || "")
    if (split && TRACKED.test(type) && (await mine(id))) counting(() => track(id)?.event(type, d))
    switch (type) {
      case "session.created":
        if (id) {
          tops.set(id, !d.parentID && ours(d.location))
          parents.set(id, String(d.parentID || ""))
          if (!d.parentID && ours(d.location)) void loginLine(id) // a waiting grant (#198)
        }
        break
      case "session.deleted":
        trackers.delete(id)
        if (turns.delete(id)) {
          if (last === id) last = ""
          await save()
        }
        break
      case "session.execution.started":
        if (turns.has(id) && carries(id)) turn(id).busy = true
        break
      case "session.execution.succeeded":
      case "session.execution.failed":
        await turnEnded(id)
        break
      case "session.execution.interrupted":
        // superseded: a newer execution of the same session takes over.
        if (d.reason !== "superseded") await interrupted(id)
        break
      case "session.compaction.started":
        // Asked for between turns (/compact): its execution ends no turn.
        if (turns.has(id) && carries(id) && !turns.get(id)!.turn) turn(id).compactOnly = true
        break
      case "session.compaction.ended":
        await compacted(id)
        break
    }
  }

  const registrations: Array<{ dispose: () => Promise<void> }> = []
  const keep = async (r: Promise<any>) => {
    try {
      const reg = await r
      if (reg?.dispose) registrations.push(reg)
    } catch {
      // an older or newer host without this hook
    }
  }

  const root = busRoot(options, env, fileURLToPath(import.meta.url))
  split = await usageLoader.load(root)
  await keep(ctx.mcp.transform((editor: any) => {
    for (const [n, cfg] of editor.list()) if (n === SERVER || runsBus(cfg)) busNames.add(n)
    if (!relay) {
      unkeyed = addMcp(editor, options, root, directory, env)
      return
    }
    // Under relay variables only the location of the bound session runs the
    // server, with its id; before a session is bound, or for another
    // location's, none (RT-6.20 step 1, RT-6.18 step 6).
    const b = bound()
    if (b && b.dir === realDir) unkeyed = addMcp(editor, options, root, directory, env, b.id)
    else dropMcp(editor)
  }))
  if (relay) {
    // A second top-level session of the bound one's location shares the
    // location's MCP server, which speaks as the bound session's bot: its bus
    // tool calls are pointed at a tool that fails with the reason (RT-6.20
    // step 1), not refused by a throw alone.
    const tool: any = (ctx as any).tool
    if (typeof tool?.hook !== "function" || typeof tool?.transform !== "function") {
      console.warn(`[${SERVER}] this OpenCode has no tool hook: a second session of this server could call the bus ` +
        `tools of the first (adapters/opencode.md)`)
    } else {
      await keep(tool.transform((editor: any) => {
        if (editor.get?.(REFUSAL_TOOL)) return
        editor.add({ name: REFUSAL_TOOL, description: "Not for use: answers a bus tool call from an OpenCode session that carries no bus.",
          input: { type: "object", properties: {}, additionalProperties: true },
          execute: async () => {
            throw new Error(bound()?.refused ? HOST_REFUSED : NO_BUS)
          } })
      }))
      await keep(tool.hook("execute.before", async (ev: any) => {
        const name = String(ev?.tool || "")
        if (![...busNames].some((n) => name === n || name.startsWith(`${n}_`))) return
        const b = bound()
        if (b && !b.refused && (await inTree(String(ev?.sessionID || "")))) return
        if (!b) return // nothing bound: no bus server runs under relay variables
        ev.tool = REFUSAL_TOOL
        ev.input = {}
      }))
    }
  }
  const skill = skillInfo(root)
  if (skill) {
    await keep(ctx.skill.transform((editor: any) => {
      if (!editor.get(SKILL)) editor.add(skill)
    }))
  }
  await keep(ctx.session.hook("prompt", async (ev: any) => {
    const t = turns.get(ev?.sessionID)
    // The plugin's own input (marked, or the text it just sent) is not the human's.
    if (ev?.metadata?.[MARK] || ev?.prompt?.metadata?.[MARK] || (t?.sent && ev?.prompt?.text === t.sent)) return
    const id = String(ev?.sessionID || "")
    // Under relay variables the first top-level session that prompts binds
    // the bus, before its turn runs (RT-6.20 step 1).
    if (relay && !bound() && (await topLevel(id))) await bind(id)
    void humanTurn(id, String(ev?.prompt?.text ?? ""))
  }))

  void (async () => {
    while (!ac.signal.aborted) {
      try {
        for await (const ev of ctx.event.subscribe({ signal: ac.signal })) {
          if (ac.signal.aborted) break
          await onEvent(ev).catch(() => {})
        }
      } catch {
        // the stream broke: subscribe again
      }
      if (!ac.signal.aborted) await new Promise((r) => setTimeout(r, RESUBSCRIBE_MS))
    }
  })()

  void listen()

  return async () => {
    closed = true
    ac.abort() // ends the event stream and a long poll in flight
    nudge()
    for (const r of registrations) await r.dispose().catch(() => {})
    await save()
  }
}

// Plugin.define({id, setup}) returns its argument: the plain object needs no
// runtime import, so the file runs without `npm install`.
export default { id: SERVER, setup } satisfies Plugin.Plugin
