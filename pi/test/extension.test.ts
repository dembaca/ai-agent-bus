// RT-6.27: the pi extension (ADR-009 phase 3) against a fake hook API and a
// fake pi ExtensionAPI. Runs with node (type stripping, Node 22.18+):
//   node --test pi/test/extension.test.ts
import assert from "node:assert/strict"
import { createHash } from "node:crypto"
import { mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, symlinkSync, writeFileSync } from "node:fs"
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http"
import { homedir, tmpdir } from "node:os"
import { dirname, join } from "node:path"
import { after, afterEach, before, beforeEach, mock, test } from "node:test"
import { fileURLToPath } from "node:url"

import ocBus from "../../opencode/plugin/mattermost-agent-bus.ts"
import { busFile as ocBusFile } from "../../opencode/plugin/mattermost-agent-bus.ts"
import mattermostAgentBus, { busFile, busRoot, create, envFirst, EnvConflict, fileFirst, hookApi, keyless, MCP_TIMEOUT_SEC, mcpConfig, secretCommand, SERVER, SKILL,
  chars, timing, venvDir, waitSec, wakeBackoffMs, contextSegments, COORDINATION, Meter, OVERHEAD, requestSplit, Segments,
  TASK, toolCategory, waitResult, busTool, pyJson } from "../mattermost-agent-bus.ts"

const HERE = dirname(fileURLToPath(import.meta.url))
const REPO = dirname(dirname(HERE))
// A new key per test (beforeEach): a late call of an earlier test's instance gets a 401.
let KEY = "test-key"
let keys = 0
const tmp = realpathSync(mkdtempSync(join(tmpdir(), "mm-pi-")))
const ws = join(tmp, "ws")
const sha16 = (s: string) => createHash("sha1").update(s).digest("hex").slice(0, 16)

type Call = { method: string; path: string; body: any }
let calls: Call[] = []
let answers: Record<string, any> = {}
let server: Server
let endpoint = ""
// The fake server holds /v1/wake until the test answers it (or the client leaves).
const HOLD = Symbol("hold")
let held: Array<{ res: ServerResponse; path: string }> = []
let dropped = 0
// An answer of { [LATER]: ms, body } goes out after ms; `done` lists the calls
// in the order their answers went out (a real server applies a state then).
const LATER = Symbol("later")
let done: Call[] = []

const until = async (cond: () => boolean, what: string, ms = 3000) => {
  for (let i = 0; i < ms / 5 && !cond(); i++) await new Promise((r) => setTimeout(r, 5))
  assert.ok(cond(), what)
}
const settle = (ms = 60) => new Promise((r) => setTimeout(r, ms))
const answerWake = (body: any) => {
  const h = held.shift()
  assert.ok(h, "no request is held")
  h!.res.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify(body))
}
const nowSec = () => Math.floor(Date.now() / 1000)
// The calls of a test besides the session's own: the long poll and the instructions a run
// asks for while they are missing (RT-6.27 step 1, #153).
const rest = () => calls.filter((c) => !c.path.startsWith("/v1/wake") && c.path !== "/v1/instructions" && c.path !== "/v1/login")
  .map((c) => c.method + " " + c.path)
const wakes = () => calls.filter((c) => c.path.startsWith("/v1/wake"))
const bodies = (path: string) => calls.filter((c) => c.path === path).map((c) => c.body)

before(async () => {
  mkdirSync(join(ws, ".git"), { recursive: true })
  server = createServer((req: IncomingMessage, res: ServerResponse) => {
    let raw = ""
    req.on("data", (c) => (raw += c))
    req.on("end", () => {
      if (req.headers.authorization !== `Bearer ${KEY}`) {
        res.writeHead(401).end("{}")
        return
      }
      const path = req.url || ""
      const call = { method: req.method || "", path, body: raw ? JSON.parse(raw) : null }
      calls.push(call)
      const route = path.split("?")[0]
      const answer = typeof answers[route] === "function" ? answers[route](path, call.body) : answers[route]
      if (answer?.[LATER]) {
        const { [LATER]: ms, body } = answer
        setTimeout(() => {
          done.push(call)
          res.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify(body))
        }, ms)
        return
      }
      if (answer !== HOLD) done.push(call)
      if (answer === HOLD) {
        const h = { res, path }
        held.push(h)
        res.on("close", () => {
          if (held.includes(h)) {
            held = held.filter((x) => x !== h)
            dropped += 1
          }
        })
        return
      }
      if (answer === undefined) res.writeHead(503).end('{"error":"no bus session"}')
      else res.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify(answer))
    })
  })
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r))
  endpoint = `http://127.0.0.1:${(server.address() as any).port}`
})

after(() => {
  server.close()
  rmSync(tmp, { recursive: true, force: true })
})

beforeEach(() => {
  // A test must not end with a call of its instance in flight; the per-test key keeps a late one out of the next test's log.
  KEY = "test-key-" + ++keys
  calls = []
  done = []
  held = []
  dropped = 0
  answers = { "/v1/presence": { ok: true }, "/v1/stat": { ok: true }, "/v1/usage": { ok: true },
              "/v1/turn-end": () => ({ action: "idle", listen_since: nowSec(), listen_until: nowSec() + 5400 }),
              "/v1/wake": HOLD, "/v1/handoff": { context: "" }, "/v1/instructions": { instructions: "" } }
  timing.minRetrySec = 0.01
})

const live: Array<() => Promise<void>> = []

afterEach(async () => {
  for (const stop of live.splice(0)) await stop().catch(() => {})
  for (const h of held) h.res.destroy()
  held = []
})

// A fake pi: the ExtensionAPI parts the extension uses, and one context for
// the session, as pi hands them to the factory and to every handler.
function fakePi(env: Record<string, string | undefined> = { CB_BUS_ENDPOINT: endpoint, CB_BUS_KEY: KEY },
                here = join(REPO, "pi", "mattermost-agent-bus.ts")) {
  const handlers: Record<string, Array<(e: any, c: any) => any>> = {}
  const registered: Array<{ name: string; config: any }> = []
  const sent: Array<{ text: string; opts: any }> = []
  const messages: Array<{ message: any; opts: any }> = []
  const box = { idle: true, usage: undefined as any, notes: [] as string[] }
  const ctx = { cwd: ws, mode: "tui", hasUI: true, ui: { notify: (m: string) => box.notes.push(m) },
                isIdle: () => box.idle, getContextUsage: () => box.usage }
  const pi: any = {
    on: (ev: string, h: any) => {
      ;(handlers[ev] ??= []).push(h)
      return () => {}
    },
    registerMcpServer: (name: string, config: any) => registered.push({ name, config }),
    sendUserMessage: (text: string, opts?: any) => sent.push({ text, opts }),
    sendMessage: (message: any, opts?: any) => messages.push({ message, opts }),
  }
  const ext = create(pi, { env, here, retrySec: 0.02 })
  const emit = async (ev: string, event: any = {}) => {
    let out
    for (const h of handlers[ev] ?? []) out = await h({ type: ev, ...event }, ctx)
    return out
  }
  live.push(() => emit("session_shutdown", { reason: "quit" }))
  return { pi, ext, emit, registered, sent, messages, box, handlers }
}

const started = async (env?: Record<string, string | undefined>) => {
  const f = fakePi(env)
  await f.emit("session_start", { reason: "startup" })
  await until(() => held.length === 1, "the long poll is open")
  return f
}

test("RT-6.27 step 1: the MCP server and the skill at session_start, nothing when loaded", async () => {
  const f = fakePi()
  assert.deepEqual(f.registered, [])
  assert.deepEqual(Object.keys(f.handlers).sort(), ["agent_settled", "agent_start", "before_agent_start", "context",
    "input", "message_end", "resources_discover", "session_before_compact", "session_compact",
    "session_compact_failed", "session_shutdown", "session_start", "turn_end"])
  await settle(30)
  assert.equal(calls.length, 0, "no request before the session starts")
  await f.emit("session_start", { reason: "startup" })
  assert.deepEqual(f.registered, [{ name: SERVER, config: {
    command: "uv", args: ["run", "--locked", "--project", join(REPO, "mcp"), "mattermost-ai-agent-bus-mcp"],
    env: { CLAUDE_CODE_SESSION_ID: "", CLAUDE_PROJECT_DIR: "",
           CB_CHANNEL_PUSH: "0", MM_CHANNEL_PUSH: "0", CB_WORKSPACE: ws, MM_WORKSPACE: ws,
           UV_PROJECT_ENVIRONMENT: join(homedir(), ".cache", "mm-agent-bus", `venv-${sha16(realpathSync(REPO))}`) },
    cwd: ws, exposure: "direct", timeout: MCP_TIMEOUT_SEC,
    description: f.registered[0].config.description } }])
  assert.deepEqual(await f.emit("resources_discover", { cwd: ws, reason: "startup" }),
                   { skillPaths: [join(REPO, "skills", SKILL)] })
})

test("RT-6.27 step 1: the server's instructions reach the model as a system prompt section (#153)", async () => {
  answers["/v1/instructions"] = { instructions: "AI agent bus 0.101.0, chat: Mattermost. Answer in the thread." }
  const f = await started()
  const options: any = { sections: {} }
  await f.emit("before_agent_start", { messageType: "user", prompt: "hi", systemPromptOptions: options })
  assert.equal(options.sections[SERVER], "AI agent bus 0.101.0, chat: Mattermost. Answer in the thread.")
})

test("RT-6.27 step 1: without the server's instructions the system prompt is left alone (#153)", async () => {
  const f = await started()
  const options: any = { sections: { other: "x" } }
  await f.emit("before_agent_start", { messageType: "user", prompt: "hi", systemPromptOptions: options })
  assert.deepEqual(options.sections, { other: "x" })
})

test("RT-6.27 step 1: the instructions arrive when the server comes up after session_start (#153, #155)", async () => {
  answers["/v1/instructions"] = undefined // 503: no bus session yet, as outside corral before the server is up
  const f = await started()
  const first: any = { sections: {} }
  await f.emit("before_agent_start", { messageType: "user", prompt: "hi", systemPromptOptions: first })
  assert.deepEqual(first.sections, {}, "nothing invented before the server answers")
  answers["/v1/instructions"] = { instructions: "the rules" }
  const second: any = { sections: {} }
  await f.emit("before_agent_start", { messageType: "user", prompt: "again", systemPromptOptions: second })
  assert.equal(second.sections[SERVER], "the rules", "asked again at the next run")
})

test("RT-6.27 step 1: an empty text drops a section left from before (#155)", async () => {
  answers["/v1/instructions"] = { instructions: "" }
  const f = await started()
  const options: any = { sections: { [SERVER]: "stale", other: "x" } }
  await f.emit("before_agent_start", { messageType: "user", prompt: "hi", systemPromptOptions: options })
  assert.deepEqual(options.sections, { other: "x" })
})

test("RT-6.27 step 1: without a checkout no server and no skill; the rest goes on", async () => {
  const f = fakePi({ CB_BUS_ENDPOINT: endpoint, CB_BUS_KEY: KEY }, join(tmp, "elsewhere", "x.ts"))
  await f.emit("session_start", { reason: "startup" })
  assert.deepEqual(f.registered, [])
  assert.equal(await f.emit("resources_discover", { cwd: ws, reason: "startup" }), undefined)
  await until(() => held.length === 1, "the long poll is open all the same")
})

test("RT-6.27 step 1: a registration pi refuses is shown, not thrown", async () => {
  const f = fakePi()
  f.pi.registerMcpServer = () => {
    throw new Error("registered by another extension")
  }
  await f.emit("session_start", { reason: "startup" })
  assert.match(f.box.notes[0], /not registered: registered by another extension/)
})

test("RT-6.27 step 5: one long poll from session_start to session_shutdown", async () => {
  const f = await started()
  const first = wakes()[0].path
  assert.match(first, /^\/v1\/wake\?since=\d+&until=\d+&wait=50$/)
  answerWake({ wake: false })
  await until(() => held.length === 1 && wakes().length === 2, "renewed when it ends")
  await f.emit("session_shutdown", { reason: "quit" })
  await until(() => dropped === 1, "the request in flight is aborted")
  await settle()
  assert.equal(wakes().length, 2, "no poll after the shutdown")
  await f.emit("session_shutdown", { reason: "quit" }) // idempotent
})

test("RT-6.27 step 5: a wake starts a run when idle, follows up while a human's run goes", async () => {
  const f = await started()
  answerWake({ wake: true, reason: "mentions", count: 1, text: "collect with wait_for_events" })
  await until(() => f.sent.length === 1, "a wake-up is sent")
  assert.deepEqual(f.sent[0], { text: "collect with wait_for_events", opts: undefined })
  await until(() => rest().filter((p) => p === "POST /v1/stat").length === 3, "counted")
  // RT-6.9 step 1: the wake-up's text by kind and length only, never the text.
  assert.deepEqual(bodies("/v1/stat"), [{ k: "wake", reason: "mentions" },
    { k: "inject", name: "other", chars: "collect with wait_for_events".length }, { k: "turn", by: "bus" }])
  await settle()
  assert.equal(held.length, 0, "no poll while the run it started goes")
  await f.emit("agent_start")
  await f.emit("agent_settled", { aborted: false })
  await until(() => held.length === 1, "polled again once that run settled")
  assert.match(calls.find((c) => c.path.startsWith("/v1/turn-end"))!.path, /by_bus=1&push=1&unanswered=1$/)
  // A human's run: the wake goes after its tools.
  await f.emit("input", { text: "go on", source: "interactive" })
  await f.emit("agent_start")
  f.box.idle = false
  answerWake({ wake: true, reason: "addressed", count: 1, text: "a reply in your thread" })
  await until(() => f.sent.length === 2, "a follow-up is sent")
  assert.deepEqual(f.sent[1], { text: "a reply in your thread", opts: { deliverAs: "followUp" } })
  await settle()
  assert.equal(bodies("/v1/stat").filter((b) => b.by === "bus").length, 1, "no turn of the bus inside the human's")
})

test("RT-6.27 step 5: the same counter again backs off", async () => {
  const f = await started()
  const wake = { wake: true, reason: "mentions", count: 2, text: "collect" }
  answerWake(wake)
  await until(() => f.sent.length === 1, "first wake-up")
  await f.emit("agent_settled", { aborted: false })
  await until(() => held.length === 1, "polled again")
  answerWake(wake)
  await until(() => held.length === 1 && wakes().length === 3, "asked again after the pause")
  assert.equal(f.sent.length, 1, "not woken again for the same counter within a minute")
  assert.equal(wakeBackoffMs(0), 60_000)
  assert.equal(wakeBackoffMs(9), 30 * 60_000)
})

test("RT-6.27 step 5: replaced backs off, a failed poll is asked again", async () => {
  const f = await started()
  answerWake({ wake: false, reason: "replaced" })
  await until(() => held.length === 1 && wakes().length === 2, "asked again after a pause")
  answers["/v1/wake"] = undefined // 503
  answerWake({ wake: false })
  await until(() => wakes().length >= 4, "a 503 is asked again")
  assert.equal(f.sent.length, 0)
})

test("RT-6.27 step 4: the checks run at agent_settled, not at agent_end", async () => {
  const f = await started()
  await f.emit("input", { text: "build it", source: "interactive" })
  await f.emit("agent_start")
  await f.emit("agent_end", { messages: [] })
  await settle()
  assert.deepEqual(rest(), ["POST /v1/stat", "POST /v1/presence"], "no check at agent_end")
  assert.deepEqual(bodies("/v1/presence"), [{ state: "working", label: "ws" }])
  answers["/v1/turn-end"] = { action: "drain", reason: "3 messages wait: collect them", banner: "bus: drain" }
  f.box.idle = true
  await f.emit("agent_settled", { aborted: false })
  await until(() => f.sent.length === 1, "the drain round")
  assert.match(calls.find((c) => c.path.startsWith("/v1/turn-end"))!.path,
               /^\/v1\/turn-end\?drains=0&signoff_asked=0&by_bus=0&push=1&unanswered=1$/)
  assert.deepEqual(f.sent[0], { text: "3 messages wait: collect them", opts: undefined })
  assert.deepEqual(f.box.notes, ["bus: drain"])
  assert.deepEqual(bodies("/v1/stat").slice(-2), [{ k: "block", kind: "drain" },
    { k: "inject", name: "drain", chars: "3 messages wait: collect them".length }])  // RT-6.9 step 1
  // The drain's run settles: the next check counts the round; idle with the gates → on the bus.
  answers["/v1/turn-end"] = () => ({ action: "idle", listen_since: nowSec(), listen_until: nowSec() + 5400 })
  await f.emit("agent_start")
  await f.emit("agent_settled", { aborted: false })
  await until(() => bodies("/v1/presence").length === 2, "presence after the turn")
  assert.match(calls.filter((c) => c.path.startsWith("/v1/turn-end"))[1].path, /drains=1&/)
  assert.deepEqual(bodies("/v1/presence"), [{ state: "working", label: "ws" }, { state: "bus", label: "ws" }])
  assert.deepEqual(bodies("/v1/stat").at(-1), { k: "turn_end" })
  // No gates (a BYE, no project channel): idle.
  answers["/v1/turn-end"] = { action: "idle" }
  await f.emit("agent_start")
  await f.emit("agent_settled", { aborted: false })
  await until(() => bodies("/v1/presence").length === 4, "idle")
  assert.deepEqual(bodies("/v1/presence").at(-1), { state: "idle", label: "ws" })
})

test("RT-6.27 step 4, RT-6.2 step 2: a bus post without an answer in its thread: one run, no sign-off counted", async () => {
  const f = await started()
  await f.emit("input", { text: "build it", source: "interactive" })
  await f.emit("agent_start")
  answers["/v1/turn-end"] = { action: "answer", reason: "A post that reached you …", banner: "bus: answer", posts: ["p1"] }
  answers["/v1/reminded"] = { ok: true, marked: 1 }
  f.box.idle = true
  await f.emit("agent_settled", { aborted: false })
  await until(() => f.sent.length === 1, "the answer round")
  assert.deepEqual(f.sent[0], { text: "A post that reached you …", opts: undefined })
  // Marked as the block goes out, not by the read.
  assert.deepEqual(bodies("/v1/reminded"), [{ post_ids: ["p1"] }])
  assert.deepEqual(f.box.notes, ["bus: answer"])
  // RT-6.9 step 1: the answer demand is no kind of its own among the texts: "other".
  assert.deepEqual(bodies("/v1/stat").slice(-2), [{ k: "block", kind: "answer" },
    { k: "inject", name: "other", chars: "A post that reached you …".length }])
  answers["/v1/turn-end"] = { action: "idle" }
  await f.emit("agent_start")
  await f.emit("agent_settled", { aborted: false })
  await until(() => calls.filter((c) => c.path.startsWith("/v1/turn-end")).length === 2, "the next check")
  // The sign-off was not asked yet: the server may still ask for it.
  assert.match(calls.filter((c) => c.path.startsWith("/v1/turn-end"))[1].path, /signoff_asked=0&/)
  // Let the turn end settle (turn_end stat, then idle): a call still on its way
  // would land in the next test's log, which counts presence calls exactly.
  await until(() => bodies("/v1/presence").length === 2, "idle after the check")
  assert.deepEqual(bodies("/v1/presence").at(-1), { state: "idle", label: "ws" })
})

test("RT-6.27 step 3: the human's interrupt: idle, no checks, no wake-up until the next prompt", async () => {
  const f = await started()
  await f.emit("input", { text: "do it", source: "interactive" })
  await f.emit("agent_start")
  await f.emit("agent_settled", { aborted: true })
  await until(() => bodies("/v1/presence").length === 2, "idle")
  assert.deepEqual(bodies("/v1/presence").at(-1), { state: "idle", label: "ws" })
  assert.equal(calls.filter((c) => c.path.startsWith("/v1/turn-end")).length, 0, "no checks (RT-6.9)")
  answerWake({ wake: true, reason: "mentions", count: 1, text: "collect" })
  await settle(100)
  assert.equal(f.sent.length, 0, "no wake-up")
  assert.equal(held.length, 0, "no poll while aborted")
  await f.emit("input", { text: "again", source: "interactive" })
  await until(() => held.length === 1, "the human's prompt opens it again")
  answerWake({ wake: true, reason: "mentions", count: 1, text: "collect" })
  await until(() => f.sent.length === 1, "woken again")
})

test("RT-6.27 step 3: an abort's idle never overtakes the run's working (0.90.1)", async () => {
  const f = await started()
  // The server is slow to take `working`: an `idle` sent beside it would be applied first.
  answers["/v1/presence"] = (_path: string, body: any) => (body?.state === "working" ? { [LATER]: 150, body: { ok: true } }
                                                                                    : { ok: true })
  await f.emit("input", { text: "do it", source: "interactive" })
  await f.emit("agent_start")
  await f.emit("agent_settled", { aborted: true })
  const applied = () => done.filter((c) => c.path === "/v1/presence").map((c) => c.body.state)
  await until(() => applied().length === 2, "both states applied")
  assert.deepEqual(applied(), ["working", "idle"])
  // A state superseded while it waits is not sent: while a slow idle is out,
  // the next run's working and its abort's idle queue up; only the idle goes.
  answers["/v1/presence"] = (_path: string, body: any) => (body?.state === "idle" ? { [LATER]: 100, body: { ok: true } }
                                                                                 : { ok: true })
  done = []
  calls = []
  await f.emit("input", { text: "again", source: "interactive" })
  await f.emit("agent_start")
  await f.emit("agent_settled", { aborted: true })
  await f.emit("input", { text: "and again", source: "interactive" })
  await f.emit("agent_start")
  await f.emit("agent_settled", { aborted: true })
  await until(() => applied().length === 3, "the states applied")
  await settle(150)
  assert.deepEqual(applied(), ["working", "idle", "idle"])
  assert.deepEqual(bodies("/v1/presence").map((b) => b.state), ["working", "idle", "idle"], "the superseded working is not sent")
})

test("RT-6.27 step 3: input from an extension (ours included) is no human's prompt", async () => {
  const f = await started()
  await f.emit("input", { text: "collect", source: "extension" })
  await settle()
  assert.deepEqual(bodies("/v1/stat"), [])
  await f.emit("input", { text: "hi", source: "rpc" })
  await until(() => bodies("/v1/stat").length === 1, "counted")
  assert.deepEqual(bodies("/v1/stat"), [{ k: "turn", by: "human" }])
})

test("RT-6.27 step 4: a check the human overtook is dropped", async () => {
  const f = await started()
  answers["/v1/turn-end"] = HOLD
  await f.emit("agent_start")
  await f.emit("agent_settled", { aborted: false })
  await until(() => held.some((h) => h.path.startsWith("/v1/turn-end")), "the check is out")
  await f.emit("input", { text: "next", source: "interactive" })
  const h = held.find((x) => x.path.startsWith("/v1/turn-end"))!
  held = held.filter((x) => x !== h)
  h.res.writeHead(200, { "Content-Type": "application/json" })
    .end(JSON.stringify({ action: "drain", reason: "collect", banner: "" }))
  await settle(100)
  assert.equal(f.sent.length, 0, "no drain into the human's new turn")
  // RT-6.2 step 2: an answer dropped alike marks nothing, its posts stay due.
  answers["/v1/turn-end"] = HOLD
  await f.emit("agent_start")
  await f.emit("agent_settled", { aborted: false })
  await until(() => held.some((x) => x.path.startsWith("/v1/turn-end")), "the second check is out")
  await f.emit("input", { text: "again", source: "interactive" })
  const a = held.find((x) => x.path.startsWith("/v1/turn-end"))!
  held = held.filter((x) => x !== a)
  a.res.writeHead(200, { "Content-Type": "application/json" })
    .end(JSON.stringify({ action: "answer", reason: "answer it", banner: "", posts: ["p1"] }))
  await settle(100)
  assert.equal(f.sent.length, 0)
  assert.deepEqual(bodies("/v1/reminded"), [], "a dropped answer marks nothing")
  // A prompt that comes while the mark is out takes over: the reason is not delivered.
  answers["/v1/turn-end"] = { action: "answer", reason: "answer it", banner: "", posts: ["p1"] }
  answers["/v1/reminded"] = HOLD
  await f.emit("agent_start")
  await f.emit("agent_settled", { aborted: false })
  await until(() => held.some((x) => x.path === "/v1/reminded"), "the mark is out")
  await f.emit("input", { text: "once more", source: "interactive" })
  const m = held.find((x) => x.path === "/v1/reminded")!
  held = held.filter((x) => x !== m)
  m.res.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify({ ok: true, marked: 1 }))
  await settle(100)
  assert.equal(f.sent.length, 0, "no answer round into the human's new turn")
})

test("RT-6.27 step 6: after a compaction the own posts and the skill as context that starts no run, not shown", async () => {
  const f = await started()
  answers["/v1/handoff"] = { context: "Your conversation was just compacted." }
  await f.emit("session_compact", { reason: "threshold", willRetry: false, fromExtension: false })
  assert.deepEqual(f.messages, [{ message: { customType: SERVER, content: "Your conversation was just compacted.",
                                             display: false }, opts: { triggerTurn: false } }])
  assert.deepEqual(bodies("/v1/stat"), [{ k: "compact", chars: 37 }])
  answers["/v1/handoff"] = { context: "" }
  await f.emit("session_compact", { reason: "manual", willRetry: false, fromExtension: false })
  assert.equal(f.messages.length, 1, "nothing to hand back")
})

test("RT-6.27 step 4 (ADR-008): the context use for the status, when it moved or once a minute", async () => {
  mock.timers.enable({ apis: ["Date"], now: Date.now() })
  try {
    const f = await started()
    f.box.usage = { tokens: 84_800, contextWindow: 200_000, percent: 42.4 }
    assert.equal(f.handlers.turn_end[0]({ type: "turn_end" }, { ...f.box, getContextUsage: () => f.box.usage }),
                 undefined, "not awaited: the run does not wait for the bus")
    await until(() => bodies("/v1/usage").length === 1, "reported")
    await f.emit("turn_end")
    await settle(30)
    assert.deepEqual(bodies("/v1/usage"), [{ ctx: 42, ctx_size: 200_000 }])
    f.box.usage = { tokens: 87_200, contextWindow: 200_000, percent: 43.6 }
    await f.emit("turn_end")
    await until(() => bodies("/v1/usage").length === 2, "moved a point")
    f.box.usage = { tokens: null, contextWindow: 200_000, percent: null } // right after a compaction
    await f.emit("turn_end")
    f.box.usage = { tokens: 87_400, contextWindow: 200_000, percent: 43.7 }
    await f.emit("turn_end")
    await settle(30)
    assert.equal(bodies("/v1/usage").length, 2, "unknown, or under a point within the minute")
    mock.timers.tick(61_000)
    await f.emit("turn_end")
    await until(() => bodies("/v1/usage").length === 3, "once a minute")
    assert.deepEqual(bodies("/v1/usage"), [{ ctx: 42, ctx_size: 200_000 }, { ctx: 44, ctx_size: 200_000 },
                                           { ctx: 44, ctx_size: 200_000 }])
  } finally {
    mock.timers.reset()
  }
})

test("RT-6.27 step 2: the server is found like the OpenCode plugin finds it", async () => {
  const env = { XDG_STATE_HOME: join(tmp, "state") }
  const dir = join(tmp, "state", "mm-agent-bus")
  mkdirSync(dir, { recursive: true })
  assert.equal(busFile(env, ws), null)
  assert.equal(await hookApi(env, ws)("GET", "/v1/handoff"), null, "no file: no call")
  writeFileSync(join(dir, `endpoint-${sha16(ws)}.env`), `CB_BUS_ENDPOINT=${endpoint}\nCB_BUS_KEY=${KEY}\n`)
  assert.equal(busFile(env, ws), join(dir, `endpoint-${sha16(ws)}.env`))
  assert.equal(busFile(env, ws), ocBusFile(env, ws), "the same file as the OpenCode plugin's")
  assert.deepEqual(await hookApi(env, join(ws, "."))("GET", "/v1/handoff"), { context: "" })
  assert.equal(busRoot({}, join(REPO, "pi", "mattermost-agent-bus.ts")), REPO)
  assert.equal(busRoot({ CB_AGENT_BUS_ROOT: REPO }, "/nowhere/x.ts"), REPO)
  assert.equal(waitSec(), 50)
  assert.equal(waitSec(500), 55)
  assert.equal(typeof ocBus.setup, "function")
})

test("RT-6.27: the default export is pi's factory and starts nothing", async () => {
  const handlers: string[] = []
  mattermostAgentBus({ on: (ev: string) => handlers.push(ev) } as any)
  await settle(30)
  assert.ok(handlers.includes("session_start") && handlers.includes("agent_settled"))
  assert.equal(calls.length, 0)
})

test("RT-6.27 step 1: the server's environment in the user's cache, one per checkout, never in it", () => {
  const venv = (env: Record<string, string>, root = REPO) => mcpConfig(root, ws, env).env.UV_PROJECT_ENVIRONMENT
  const id = sha16(realpathSync(REPO))
  assert.equal(venv({ XDG_CACHE_HOME: "/c", HOME: "/h" }), `/c/mm-agent-bus/venv-${id}`)
  assert.equal(venv({ XDG_CACHE_HOME: "", HOME: "/h" }), `/h/.cache/mm-agent-bus/venv-${id}`)
  // Keyed by the real path: a symlink to the checkout shares its environment, another checkout does not.
  const link = join(tmp, "bus-link")
  symlinkSync(REPO, link)
  assert.equal(venv({ HOME: "/h" }, link), venv({ HOME: "/h" }))
  assert.notEqual(venvDir({ HOME: "/h" }, tmp), venvDir({ HOME: "/h" }, REPO))
  // Outside corral alike: one rule, one more environment per user.
  assert.equal(venv({ HOME: "/h", CB_BUS_MCP_ENDPOINT: "tcp://127.0.0.1:1" }), venv({ HOME: "/h" }))
})

test("RT-6.27 step 1: the secret by its command for the server alone, never its value", () => {
  const secret = "s3cr3t-enrollment-token"
  const gopass = mcpConfig(REPO, ws, { MM_REG_SECRET_GOPASS: "bus/it's registrar", MM_REG_SECRET: secret })
  assert.equal(gopass.env.MM_REG_SECRET, "!gopass show -o 'bus/it'\\''s registrar'")
  assert.ok(!JSON.stringify(gopass).includes(secret), "the value never goes into the config")
  assert.equal(mcpConfig(REPO, ws, { MM_REG_SECRET_CMD: " pass show bus/registrar " }).env.MM_REG_SECRET,
               "!pass show bus/registrar")
  assert.equal(secretCommand({ MM_REG_SECRET_GOPASS: "a", MM_REG_SECRET_CMD: "b" }), "!gopass show -o 'a'")
  const none = mcpConfig(REPO, ws, { MM_REG_SECRET: secret })
  assert.ok(!("MM_REG_SECRET" in none.env) && !JSON.stringify(none).includes(secret), "none: pi's environment")
  // Under corral the host hooks registered the bot: the server is a relay.
  assert.equal(secretCommand({ CB_BUS_MCP_ENDPOINT: "tcp://127.0.0.1:1", MM_REG_SECRET_GOPASS: "a" }), null)
})

test("RT-6.27 step 5: no wake-up while pi compacts; a poll that was out is dropped", async () => {
  const f = await started()
  await f.emit("session_before_compact", { reason: "threshold" })
  answerWake({ wake: true, reason: "mentions", count: 1, text: "collect" })
  await settle(100)
  assert.equal(f.sent.length, 0, "pi refuses a prompt while it compacts")
  assert.equal(held.length, 0, "no poll meanwhile")
  await f.emit("session_compact", { reason: "threshold", willRetry: false, fromExtension: false })
  await until(() => held.length === 1, "polled again after it")
  answerWake({ wake: true, reason: "mentions", count: 1, text: "collect" })
  await until(() => f.sent.length === 1, "woken")
  await f.emit("agent_settled", { aborted: false })
  await f.emit("session_before_compact", { reason: "manual" })
  await f.emit("session_compact_failed", { reason: "manual", aborted: true })
  await until(() => held.length === 1, "a failed compaction ends the pause too")
})

test("RT-6.27 step 2: the keep-in-step helpers are the OpenCode plugin's, to the letter", () => {
  const block = (path: string) => {
    const text = readFileSync(path, "utf8")
    const m = text.match(/\/\/ keep-in-step: from here[\s\S]*?\/\/ keep-in-step: end\n/)
    assert.ok(m, `no keep-in-step block in ${path}`)
    return m![0]
  }
  assert.equal(block(join(REPO, "pi", "mattermost-agent-bus.ts")),
               block(join(REPO, "opencode", "plugin", "mattermost-agent-bus.ts")))
})

test("RT-6.9 step 1: an inject's length counts Unicode code points, as the server and the hooks do", () => {
  assert.equal(chars("a\u{1F600}\u2014"), 3)  // a, a grinning face, an em dash: 4 UTF-16 units, 3 code points
})

// RT-6.9 step 2 (ADR-017, method 1): the token split in process, from pi's
// `context` and `message_end` events, one `usage` stat per settled run.
// The name pi gives a bus tool: mcp__<server>__<tool>, "-" turned into "_" (pi-mcp createMcpToolName).
const BUS = "mcp__ai_agent_bus__"
const tok = (u: Record<string, number>) => ({ input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 }, ...u })
const asst = (content: any[], u: Record<string, number>) =>
  ({ role: "assistant", content, usage: tok(u), stopReason: "stop", timestamp: 0 })
const user = (text: string) => ({ role: "user", content: [{ type: "text", text }], timestamp: 0 })
const call = (id: string, name: string, args: any) => ({ type: "toolCall", id, name, arguments: args })
const result = (id: string, name: string, text: string) =>
  ({ role: "toolResult", toolCallId: id, toolName: name, content: [{ type: "text", text }], isError: false, timestamp: 0 })
const usageStats = () => bodies("/v1/stat").filter((b) => b.k === "usage")
const request = async (f: any, messages: any[], reply: any) => {
  await f.emit("context", { messages })
  await f.emit("message_end", { message: reply })
}
const settled = async (f: any) => {
  await f.emit("agent_start")
  await f.emit("agent_settled", { aborted: false })
  await until(() => bodies("/v1/presence").length >= 2, "the run settled")
  await settle()
}
// h: the uncovered front per input type (in, read, w; amendment A6).
const expect = (req: number[], a: number[], o: number[], c: number[], h = [0, 0, 0]) => {
  const out: Record<string, any> = { k: "usage", req: req[0], req_ovh: req[1], req_coord: req[2] }
  ;["in", "read", "w", "out"].forEach((x, i) => Object.assign(out, { [`a_${x}`]: a[i], [`o_${x}`]: o[i], [`c_${x}`]: c[i] }))
  ;["in", "read", "w"].forEach((x, i) => (out[`h_${x}`] = h[i]))
  return out
}

test("RT-6.9 step 2: a bus tool by its word, kind or first word; other bus tools; not the bus's", () => {
  const cases: Array<[string, any, number]> = [
    [BUS + "post_message", { kind: "idle", message: "x" }, OVERHEAD],
    [BUS + "reply_in_thread", { kind: "DONE", message: "IDLE x" }, COORDINATION],
    [BUS + "post_message", { message: "HELLO there" }, OVERHEAD],
    [BUS + "post_message", { message: "**BYE** all" }, OVERHEAD],
    [BUS + "post_message", { kind: "bogus", message: "IDLE x" }, OVERHEAD],
    [BUS + "post_message", { message: "PR #12 merged" }, COORDINATION],
    [BUS + "post_message", { message: "no word" }, COORDINATION],
    ["mcp__mattermost-agent-bus__ask_human", {}, COORDINATION],
    [BUS + "coordinator", {}, COORDINATION],
    [BUS + "wait_for_events", {}, OVERHEAD],
    [BUS + "stats_report", {}, OVERHEAD],
    ["mcp__other__post_message", { kind: "DONE" }, TASK],
    ["mcp__ai-agent-bus__post_message", { kind: "DONE" }, COORDINATION],
    ["mcp__mattermost_agent_bus__wait_for_events", {}, OVERHEAD],
    ["mcp__my_ai_agent_bus_fork__post_message", { kind: "DONE" }, TASK],
    ["mcp__x__ai_agent_bus__post_message", { kind: "DONE" }, TASK],
    ["ai_agent_bus__post_message", { kind: "DONE" }, TASK],
    ["read", { path: "x" }, TASK],
    // Reading the bus skill (amendment A3), absolute or relative; not another skill.
    ["read", { path: "/home/a/ai-agent-bus/skills/ai-agent-bus/SKILL.md" }, OVERHEAD],
    ["read", { path: "skills/ai-agent-bus/coordinator.md" }, OVERHEAD],
    ["read", { path: "/home/a/skills/ai-agent-bus-extra/SKILL.md" }, TASK],
    ["read", { path: "/home/a/skills/other/SKILL.md" }, TASK],
  ]
  for (const [name, args, cat] of cases) assert.equal(toolCategory(name, args), cat, `${name} ${JSON.stringify(args)}`)
})

for (const [word, msg, cat] of [["IDLE", "IDLE wait", OVERHEAD], ["DONE", "DONE ship", COORDINATION]] as const) {
  test(`RT-6.9 step 2: a bus tool round (${word}): call, result and the request it drives`, async () => {
    const f = await started()
    const args = { kind: word, message: msg }
    // Sized as Python's json.dumps writes it, with ", " and ": " (overhead.py).
    assert.equal(pyJson(args), `{"kind": "${word}", "message": "${msg}"}`)
    assert.equal(pyJson(args).length, 40)
    const t = user("x".repeat(400))
    const a1 = asst([call("c1", BUS + "post_message", args)], { input: 100, output: 30 })
    const r1 = result("c1", BUS + "post_message", "r".repeat(160))
    // pi's system prompt and tool docs: the 20 tokens of input in front of the 600 known characters.
    const a2 = asst([{ type: "text", text: "y".repeat(48) }], { cacheRead: 110, cacheWrite: 20, input: 40, output: 12 })
    await request(f, [t], a1)
    await request(f, [t, a1, r1], a2)
    await settled(f)
    // Request 1: all input task work; the call's 40 characters (10 tokens) the bus's, the rest by the prompt.
    // Request 2 by position: read 110 = head 20 + 360 task characters; write 20 = 40 task + 40 of the
    // call; input 40 = the result's 160. Its 12 output tokens follow the result (the driver).
    const bus = [40, 0, 10, 22]
    assert.deepEqual(usageStats(), [expect(cat === OVERHEAD ? [2, 1, 0] : [2, 0, 1], [140, 110, 20, 42],
      cat === OVERHEAD ? bus : [0, 0, 0, 0], cat === OVERHEAD ? [0, 0, 0, 0] : bus, [0, 20, 0])])
    // The next run starts from zero.
    await settled(f)
    assert.equal(usageStats().length, 1)
  })
}

test("RT-6.9 step 2: the extension's own texts are overhead (a wake carries no post's body in pi)", async () => {
  const f = await started()
  const text = "w".repeat(80)
  answerWake({ wake: true, reason: "addressed", count: 1, text })
  await until(() => f.sent.length === 1, "a wake-up is sent")
  // 50 tokens of input: 30 of pi's own front, 20 of the wake text.
  const a1 = asst([{ type: "text", text: "okay" }], { input: 50, output: 8 })
  await request(f, [user(text)], a1)
  // The same length from the human: task work. Cache read 50: pi's 29 tokens in front (41 known
  // of 70), then the wake text and the answer it drove (21).
  await request(f, [user(text), a1, user("h".repeat(80))], asst([], { cacheRead: 50, input: 20, output: 4 }))
  await settled(f)
  assert.deepEqual(usageStats(), [expect([2, 1, 0], [70, 50, 0, 12], [20, 21, 0, 8], [0, 0, 0, 0], [30, 29, 0])])
})

test("RT-6.9 step 2: a compaction starts the segments afresh; its summary has the shares before it", async () => {
  const f = await started()
  // Before: half task, half the extension's handoff (overhead).
  const handoff = { role: "custom", customType: SERVER, content: "h".repeat(200), display: false, timestamp: 0 }
  await request(f, [user("t".repeat(200)), handoff], asst([], { input: 100, output: 0 }))
  await f.emit("session_compact", { reason: "threshold", willRetry: false, fromExtension: false })
  // After: pi's summary (400 characters: 200 of them overhead) and a kept message (400, task).
  const summary = { role: "compactionSummary", summary: "s".repeat(400), tokensBefore: 1000, timestamp: 0 }
  await request(f, [summary, user("u".repeat(400))], asst([], { input: 200, output: 0 }))
  await settled(f)
  assert.deepEqual(usageStats(), [expect([2, 0, 0], [300, 0, 0, 0], [100, 0, 0, 0], [0, 0, 0, 0])])
  // Without a share before it (a resumed session): the summary is task work.
  const seg = contextSegments([summary], () => undefined)
  assert.deepEqual(seg.shares(), [1, 0, 0])
})

test("RT-6.9 step 2: the input by position, the segments scaled down when longer than the input", () => {
  const seg = new Segments()
  seg.add(OVERHEAD, 400)
  seg.add(TASK, 400)
  // 800 characters (200 tokens) for 100 tokens: cache read covers the older half, the overhead.
  const split = requestSplit(tok({ cacheRead: 50, input: 50 }), seg, asst([], {}))
  assert.deepEqual(split.map((x) => [x.read, x.in]), [[0, 50], [50, 0], [0, 0], [0, 0]])
})

test("RT-6.9 step 2: the output: tool calls by their tool, the rest by the new input (the driver)", () => {
  const a0 = asst([call("c0", BUS + "get_thread", {})], { input: 10, output: 1 })
  const seg = contextSegments([user("a".repeat(40)), a0, result("c0", BUS + "get_thread", "g".repeat(100)),
                               user("t".repeat(300))], () => undefined)
  assert.deepEqual(seg.driver(), [0.75, 0, 0.25])
  // 20 output tokens: the ask's call {"question": ""} 16 characters (4 tokens), the 16 others 3:1 task:coordination.
  const a1 = asst([{ type: "thinking", thinking: "hm" }, call("c1", BUS + "ask_human", { question: "" })], { output: 20 })
  const split = requestSplit(a1.usage, seg, a1)
  assert.deepEqual(split.slice(0, 3).map((x) => x.out), [12, 0, 8])
  // A request whose new input was coordination only, one of overhead only.
  const m = new Meter()
  m.add(a1, contextSegments([user("a"), a0, result("c0", BUS + "get_thread", "g")], () => undefined))
  m.add(a1, contextSegments([user("a"), a0, result("c0", BUS + "wait_for_events", "g")].map((x: any) =>
    x.role === "assistant" ? asst([call("c0", BUS + "wait_for_events", {})], { input: 1 }) : x), () => undefined))
  m.add(a1, contextSegments([user("a")], () => undefined))
  assert.deepEqual([m.req, m.reqCoord, m.reqOvh], [3, 1, 1])
})

test("RT-6.9 step 2: o + c <= a for every type, integers in range, whatever the input", () => {
  let seed = 7
  const rnd = (n: number) => ((seed = (seed * 1103515245 + 12345) % 2 ** 31) % n)
  const names = [BUS + "post_message", BUS + "ask_human", BUS + "wait_for_events", "bash"]
  for (let run = 0; run < 200; run++) {
    const m = new Meter()
    const msgs: any[] = []
    for (let i = 0; i < 1 + rnd(6); i++) {
      msgs.push(user("u".repeat(rnd(500))))
      const id = `c${i}`
      const reply = asst([{ type: "text", text: "t".repeat(rnd(300)) },
                          call(id, names[rnd(4)], { message: ["IDLE x", "DONE y", "z"][rnd(3)] + "q".repeat(rnd(200)) })],
                         { input: rnd(300), cacheRead: rnd(400), cacheWrite: rnd(100), output: rnd(60) })
      m.add(reply, contextSegments(msgs, () => undefined, [0.2, 0.5, 0.3]))
      msgs.push(reply, result(id, "x", "r".repeat(rnd(400))))
    }
    const f = m.fields()!
    assert.equal(Object.keys(f).length, 18)
    for (const v of Object.values(f)) assert.ok(Number.isInteger(v) && v >= 0 && v <= 1e9)
    for (const x of ["in", "read", "w", "out"]) {
      assert.ok(f[`o_${x}`] + f[`c_${x}`] + (f[`h_${x}`] ?? 0) <= f[`a_${x}`], `${x} ${JSON.stringify(f)}`)
    }
    assert.ok(f.req_ovh + f.req_coord <= f.req)
  }
})

test("RT-6.9 step 2: no usage stat without a request or without usage; the model's context unchanged", async () => {
  const f = await started()
  await settled(f)
  const msgs = [user("hi"), result("c", BUS + "post_message", "r")]
  const before = structuredClone(msgs)
  assert.equal(await f.emit("context", { messages: msgs }), undefined, "the handler returns nothing")
  assert.deepEqual(msgs, before, "and changes nothing")
  // An error answer: no tokens, no request.
  await f.emit("message_end", { message: { ...asst([], {}), stopReason: "error" } })
  await f.emit("message_end", { message: user("not an answer") })
  await settled(f)
  assert.deepEqual(usageStats(), [])
  assert.equal(new Meter().fields(), null)
})

test("RT-6.9 step 2: a wait_for_events result by its items (amendment A1)", () => {
  const agent = { kind: "message", post: { id: "p1", message: "DONE shipped \u{1F600}" }, from_agent: true }
  const human = { kind: "message", post: { id: "p2", message: "please go on" }, from_trusted_human: true }
  const reaction = { kind: "reaction", post: { message: "the ask" } }
  const own = { kind: "stall", post: { message: "not a post" } }
  const size = (x: any) => chars(JSON.stringify(x))
  // Empty: overhead whole.
  assert.deepEqual(waitResult("[]"), [2, 0, 0])
  // An agent's post: its body coordination (code points), the JSON around it overhead.
  assert.deepEqual(waitResult(JSON.stringify([agent])), [size([agent]) - 14, 0, 14])
  // A trusted human's post: task work.
  assert.deepEqual(waitResult(JSON.stringify({ result: [human] })), [size({ result: [human] }) - 12, 12, 0])
  // A reaction (no post id) carries its post's text; the server's own items are overhead.
  assert.deepEqual(waitResult(JSON.stringify([reaction, own])), [size([reaction, own]) - 7, 0, 7])
  // A result wrapped as a string, and one item per content block.
  assert.deepEqual(waitResult(JSON.stringify({ result: JSON.stringify([agent]) }))[2], 14)
  assert.deepEqual(waitResult(JSON.stringify(agent))[2], 14)
  // What does not parse is overhead, never task work.
  assert.deepEqual(waitResult("timeout: no events"), [18, 0, 0])
  assert.deepEqual(waitResult('"text"'), [6, 0, 0])
  // In the context: a result in two blocks, each on its own.
  const name = BUS + "wait_for_events"
  const seg = contextSegments([asst([call("w", name, { timeout_sec: 1 })], { input: 1 }),
    { ...result("w", name, ""), content: [{ type: "text", text: JSON.stringify(agent) },
                                            { type: "text", text: JSON.stringify(human) }] }], () => undefined)
  assert.deepEqual(seg.fresh, [12, size(agent) - 14 + size(human) - 12, 14])  // task, overhead, coordination
})

test("RT-6.9 step 2: exact bus tool names, Python's JSON sizes, code points without an array", () => {
  assert.equal(busTool("mcp__ai_agent_bus__reply_in_thread"), "reply_in_thread")
  assert.equal(busTool("mcp__mattermost-agent-bus__get_me"), "get_me")
  assert.equal(busTool("mcp__ai_agent_bus_x__get_me"), null)
  assert.equal(busTool("post_message"), null)
  assert.equal(pyJson({ a: [1, true, null], b: { c: "\u00e9\n" } }), '{"a": [1, true, null], "b": {"c": "\u00e9\\n"}}')
  assert.equal(chars("\u{1F600}\u{1F600}x"), 3)
  assert.equal(chars("\ud800x\udc00"), 3, "a lone surrogate is one code point, as in Python")
  assert.equal(chars(""), 0)
})

test("RT-6.9 step 2: the uncovered front per input type, oldest first (amendment A6)", () => {
  const seg = new Segments()
  seg.add(OVERHEAD, 100) // 25 tokens known
  const front = (u: Record<string, number>) => requestSplit(tok(u), seg, asst([], {}))[3]
  // 45 tokens of input: 20 in front of the known 25, all in the cache read.
  assert.deepEqual(front({ cacheRead: 30, cacheWrite: 10, input: 5 }), { in: 0, read: 20, w: 0, out: 0 })
  // A front longer than the cache read runs on into the cache write.
  assert.deepEqual(front({ cacheRead: 10, cacheWrite: 15, input: 20 }), { in: 0, read: 10, w: 10, out: 0 })
  // Segments longer than the input: no front.
  assert.deepEqual(front({ input: 20 }), { in: 0, read: 0, w: 0, out: 0 })
  // No segments known: all input is front.
  assert.deepEqual(requestSplit(tok({ cacheRead: 7, input: 3 }), new Segments(), asst([], {}))[3],
                   { in: 3, read: 7, w: 0, out: 0 })
  // Summed per run; h within what is not the bus's.
  const m = new Meter()
  m.add(asst([], { cacheRead: 30, cacheWrite: 10, input: 5 }), seg)
  m.add(asst([], { cacheRead: 10, cacheWrite: 15, input: 20 }), seg)
  const f = m.fields()!
  assert.deepEqual([f.h_in, f.h_read, f.h_w], [0, 30, 10])
  assert.equal(f.h_out, undefined)
  for (const x of ["in", "read", "w"]) assert.ok(f[`h_${x}`] + f[`o_${x}`] + f[`c_${x}`] <= f[`a_${x}`])
})



test("RT-6.20: a keyless sandbox silences every old name, not only the relay's", () => {
  const box: string[] = []
  const orig = console.warn
  console.warn = (...a: unknown[]) => { box.push(a.map(String).join(" ")) }
  try {
    // the hook wrote MM_BUS_MCP_ENDPOINT; a non-relay old name is the hook's too
    assert.equal(envFirst({ CB_BUS_MCP_ENDPOINT: "tcp://h", MM_AGENT_BUS_ROOT: "/o" },
                          ["CB_AGENT_BUS_ROOT", "MM_AGENT_BUS_ROOT"]), "/o")
    // empty counts as set, ${NAME} as unset: neither hides the warning
    assert.equal(keyless({ CB_BUS_MCP_ENDPOINT: "" }), true)
    assert.equal(keyless({ CB_BUS_MCP_ENDPOINT: "${CB_BUS_MCP_ENDPOINT}" }), false)
  } finally { console.warn = orig }
  assert.equal(box.length, 0)
})
// --- the CB_ names (#169) --------------------------------------------------

test("RT-6.27: a setting reads CB_ first, then MM_ with one warning", () => {
  assert.equal(envFirst({ CB_WORKSPACE: "/n" }, ["CB_WORKSPACE", "MM_WORKSPACE"]), "/n")
  const got: string[] = []
  const orig = console.warn
  console.warn = (...a: unknown[]) => { got.push(a.map(String).join(" ")) }
  try {
    assert.equal(envFirst({ MM_AGENT_BUS_ROOT: "/o" }, ["CB_AGENT_BUS_ROOT", "MM_AGENT_BUS_ROOT"]), "/o")
    assert.equal(envFirst({ MM_AGENT_BUS_ROOT: "/o2" }, ["CB_AGENT_BUS_ROOT", "MM_AGENT_BUS_ROOT"]), "/o2")
  } finally { console.warn = orig }
  assert.equal(got.filter((m) => m.includes("MM_AGENT_BUS_ROOT is deprecated")).length, 1)
})

test("RT-6.27: empty counts as set, a ${NAME} placeholder as unset, a conflict names both", () => {
  assert.equal(envFirst({ CB_WORKSPACE: "" }, ["CB_WORKSPACE", "MM_WORKSPACE"]), "")
  assert.throws(() => envFirst({ CB_WORKSPACE: "", MM_WORKSPACE: "/o" },
                               ["CB_WORKSPACE", "MM_WORKSPACE"]), EnvConflict)
  assert.equal(envFirst({ CB_WORKSPACE: "${CB_WORKSPACE}", MM_WORKSPACE: "/o" },
                        ["CB_WORKSPACE", "MM_WORKSPACE"]), "/o")
  assert.throws(() => envFirst({ CB_BUS_KEY: "new-secret", MM_BUS_KEY: "old-secret" },
                               ["CB_BUS_KEY", "MM_BUS_KEY"]),
                (e: Error) => e instanceof EnvConflict && !e.message.includes("secret"))
})

test("RT-6.27: a file the bus wrote keeps the last line of either spelling", () => {
  assert.equal(fileFirst("MM_BUS_KEY=a\nCB_BUS_KEY=b\n", ["CB_BUS_KEY", "MM_BUS_KEY"]), "b")
  assert.equal(fileFirst("CB_BUS_KEY=new\nMM_BUS_KEY=old\n", ["CB_BUS_KEY", "MM_BUS_KEY"]), "old")
})


test("RT-6.27: a conflict on the checkout or the relay is reported once, and the fallback holds", () => {
  const warned: string[] = []
  const orig = console.warn
  console.warn = (...a: unknown[]) => { warned.push(a.map(String).join(" ")) }
  try {
    // a conflicting checkout: its own checkout wins, the message once
    const env = { CB_AGENT_BUS_ROOT: "/nowhere", MM_AGENT_BUS_ROOT: REPO }
    assert.equal(busRoot(env, join(REPO, "pi", "x.ts")), REPO)
    assert.equal(busRoot(env, join(REPO, "pi", "x.ts")), REPO)
    // a conflicting relay: no secret goes into the config, once
    const relay = { CB_BUS_MCP_ENDPOINT: "tcp://a", MM_BUS_MCP_ENDPOINT: "tcp://b",
                    MM_REG_SECRET_GOPASS: "a" }
    assert.equal(secretCommand(relay), null)
    assert.equal(secretCommand(relay), null)
  } finally { console.warn = orig }
  assert.equal(warned.filter((m) => m.includes("CB_AGENT_BUS_ROOT, MM_AGENT_BUS_ROOT")).length, 1)
  assert.equal(warned.filter((m) => m.includes("CB_BUS_MCP_ENDPOINT, MM_BUS_MCP_ENDPOINT")).length, 1)
})

test("RT-6.27: the old relay names work end to end, and a keyless sandbox stays quiet", async () => {
  answers["/v1/handoff"] = { context: "c" }
  const box: string[] = []
  const orig = console.warn
  console.warn = (...a: unknown[]) => { box.push(a.map(String).join(" ")) }
  try {
    assert.deepEqual(await hookApi({ MM_BUS_ENDPOINT: endpoint, MM_BUS_KEY: KEY }, ws)("GET", "/v1/handoff"),
                     { context: "c" })
  } finally { console.warn = orig }
  assert.equal(box.length, 0)
  const sandbox = { CB_BUS_MCP_ENDPOINT: "tcp://127.0.0.1:9", MM_BUS_ENDPOINT: endpoint, MM_BUS_KEY: KEY }
  assert.equal(keyless(sandbox), true)
  const quiet: string[] = []
  console.warn = (...a: unknown[]) => { quiet.push(a.map(String).join(" ")) }
  try {
    assert.deepEqual(await hookApi(sandbox, ws)("GET", "/v1/handoff"), { context: "c" })
  } finally { console.warn = orig }
  assert.equal(quiet.length, 0)
})

test("RT-6.34: the waiting login approval reaches the TUI once, without a token (#198)", async () => {
  answers["/v1/login"] = { link: "https://kc.example/device?user_code=ABCD-EFGH", user_code: "ABCD-EFGH",
                           expires_in: 600 }
  const f = await started()
  await until(() => f.box.notes.length >= 1, "the login line")
  const line = f.box.notes[0]
  assert.match(line, /kc\.example\/device\?user_code=ABCD-EFGH/)
  assert.match(line, /ABCD-EFGH/)
  assert.doesNotMatch(line, /token|seed/i)
  await until(() => calls.some((c) => c.method === "POST" && c.path === "/v1/login/shown"), "the shown mark")
  answers["/v1/login"] = undefined
})

test("RT-6.34: a grant that starts after the session start reaches the TUI at the turn end (#198)", async () => {
  const f = await started()
  await f.emit("input", { text: "go on", source: "interactive" })
  await f.emit("agent_start")
  answers["/v1/turn-end"] = { action: "idle" }
  answers["/v1/login"] = { link: "https://kc.example/device?user_code=ABCD-EFGH", user_code: "ABCD-EFGH",
                           expires_in: 600 }
  f.box.idle = true
  await f.emit("agent_settled", { aborted: false })
  await until(() => f.box.notes.some((n) => n.includes("ABCD-EFGH")), "the turn end's login line")
  await until(() => calls.some((c) => c.method === "POST" && c.path === "/v1/login/shown"), "the shown mark")
  answers["/v1/login"] = undefined
})
