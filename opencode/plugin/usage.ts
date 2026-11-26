// The token split of an OpenCode session (RT-6.9 step 2, ADR-017; the
// contract of stage 2b, #139): which share of each model request's tokens is
// task work, bus overhead or multi-agent coordination, per token type. The
// same rules as Claude Code's method 1 (mcp/src/mattermost_ai_agent_bus/overhead.py),
// fed from OpenCode's event stream instead of a transcript, in process: it
// adds no text anywhere the model reads.
//
// - The context since the last compaction is kept as ordered segments
//   (category, characters in Unicode code points). The system prompt and the
//   tool docs are not visible here: they are no segments; the server adds the
//   bus's fixed text per request.
// - A request's input is laid over the segments by position: cache read
//   covers the oldest characters, then cache write, then fresh input, at
//   CHARS_PER_TOKEN characters per token. What the segments do not reach (the
//   invisible head) is task work here.
// - Output: the tool calls the model wrote go to their tool's category, sized
//   by their input's JSON; the rest (text, reasoning) by the categories of the
//   new input since the previous request (the driver rule).
// - A wait_for_events result splits by the posts it delivered (their bodies are
//   coordination); the bus skill's text and its side files are overhead.
// - A request is a step with at least one non-zero usage count.
//
// Pure: no I/O. The plugin feeds events and posts what `take()` returns.

export const CHARS_PER_TOKEN = 4
export const TASK = 0
export const OVERHEAD = 1
export const COORDINATION = 2
type Cat = 0 | 1 | 2
type Triple = [number, number, number]

// The bus's MCP server keys (overhead.BUS); a server added by hand may run under the old one.
export const BUS_SERVERS = ["ai-agent-bus", "mattermost-agent-bus"]
// Own posts by bus word: these are overhead, any other word and a plain reply coordination
// (overhead.OVERHEAD_WORDS); the words are a2a.WORDS.
export const OVERHEAD_WORDS = new Set(["HELLO", "IDLE", "BYE"])
const WORDS = new Set(["TASK", "DELEGATE", "ACK", "PROGRESS", "BLOCKED", "WAITING", "RESULT", "DONE", "NO",
  "CANCEL", "HELLO", "IDLE", "BYE", "HANDOVER", "ASSIGNMENTS"])
const COORDINATING = new Set(["ask_human", "resolve_ask", "coordinator", "get_file", "attach", "get_thread",
  "list_recent"])
const POSTING = new Set(["post_message", "reply_in_thread"])
// a2a.word: the convention word a message starts with ("PR #n" reads as RESULT).
const FIRST = /^[^\p{L}\p{N}_@#]*([A-Z]{2,11})(?![A-Za-z])/u
const PR_FIRST = /^[^\p{L}\p{N}_@#]*PR\s*#\d+/iu

// A text's length in Unicode code points, as Python's len (RT-6.9).
export function cp(text: string): number {
  let n = 0
  for (const _ of text) n++
  return n
}

// JSON as Python's json.dumps(ensure_ascii=False) writes it (", " and ": "), the size
// overhead.py gives a tool call's input.
export function pyJson(v: unknown): string {
  if (v === null || v === undefined) return "null"
  if (Array.isArray(v)) return "[" + v.map(pyJson).join(", ") + "]"
  if (typeof v === "object")
    return "{" + Object.entries(v as Record<string, unknown>).map(([k, x]) => JSON.stringify(k) + ": " + pyJson(x)).join(", ") + "}"
  if (typeof v === "boolean") return v ? "true" : "false"
  return JSON.stringify(v) ?? "null"
}

// The bus tool an OpenCode tool name stands for (`<server>_<tool>`), or null.
export function busTool(name: string, servers: Iterable<string> = BUS_SERVERS): string | null {
  for (const s of new Set([...BUS_SERVERS, ...servers])) {
    if (name.startsWith(s + "_")) return name.slice(s.length + 1).replace(/^_+/, "")
  }
  return null
}

export function busWord(input: Record<string, unknown>): string {
  const kind = String(input.kind ?? "").trim().toUpperCase()
  if (WORDS.has(kind)) return kind
  const message = String(input.message ?? "")
  if (PR_FIRST.test(message)) return "RESULT"
  const m = FIRST.exec(message)
  return m && WORDS.has(m[1]) ? m[1] : ""
}

// A bus tool's call and result: its category (overhead._tool_category).
export function toolCategory(tool: string, input: Record<string, unknown>): Cat {
  if (POSTING.has(tool)) return OVERHEAD_WORDS.has(busWord(input)) ? OVERHEAD : COORDINATION
  return COORDINATING.has(tool) ? COORDINATION : OVERHEAD
}

// The bus skill: loading it (the skill tool) or reading its side files is overhead.
export const SKILL = "ai-agent-bus"
const SKILL_DIR = `/skills/${SKILL}/`
export function skillRead(name: string, input: Record<string, unknown>): boolean {
  const tool = name.toLowerCase()
  if (tool === "skill") return [input.name, input.skill, input.id].some((x) => {
    const s = String(x ?? "")
    return s === SKILL || s.endsWith(`:${SKILL}`)
  })
  return [input.filePath, input.file_path, input.path].some((x) => String(x ?? "").replaceAll("\\", "/").includes(SKILL_DIR))
}

// A wait_for_events result (or one content block of it, an item each), as overhead._events:
// empty is overhead; each delivered post's body is coordination (a trusted human's task
// work), the server's own events and the JSON around the bodies overhead. What does not
// parse is overhead, never task work.
export function eventsSplit(body: string): Array<[Cat, number]> {
  const all = cp(body)
  let items: any
  try {
    items = JSON.parse(body)
    if (items && typeof items === "object" && !Array.isArray(items) && typeof items.result === "string")
      items = JSON.parse(items.result)
  } catch {
    return [[OVERHEAD, all]]
  }
  if (items && typeof items === "object" && !Array.isArray(items) && "result" in items) items = items.result
  if (items && typeof items === "object" && !Array.isArray(items)) items = [items]
  if (!Array.isArray(items)) return [[OVERHEAD, all]]
  const parts = [0, 0, 0]
  for (const item of items) {
    const post = item && typeof item === "object" ? item.post : null
    if (!post || typeof post !== "object" || Array.isArray(post) || (!post.id && item.kind !== "reaction")) continue
    parts[item.from_trusted_human === true ? TASK : COORDINATION] += cp(String(post.message ?? ""))
  }
  const bodies = parts[TASK] + parts[COORDINATION]
  return [[OVERHEAD, Math.max(0, all - bodies)], [TASK, parts[TASK]], [COORDINATION, parts[COORDINATION]]]
}

// The context's segments since the last compaction, as running sums (overhead._Context).
export class Context {
  ends: number[] = [0]
  cats: Cat[] = [TASK]
  sums: Triple[] = [[0, 0, 0]]
  fresh: Triple = [0, 0, 0] // input since the last request, by category

  reset(): void {
    this.ends = [0]
    this.cats = [TASK]
    this.sums = [[0, 0, 0]]
    this.fresh = [0, 0, 0]
  }

  get end(): number {
    return this.ends[this.ends.length - 1]
  }

  add(cat: Cat, n: number, isNew = true): void {
    if (!(n > 0)) return
    const s = [...this.sums[this.sums.length - 1]] as Triple
    s[cat] += n
    this.ends.push(this.end + n)
    this.cats.push(cat)
    this.sums.push(s)
    if (isNew) this.fresh[cat] += n
  }

  addSplit(n: number, shares: Triple, isNew = true): void {
    for (const cat of [TASK, OVERHEAD, COORDINATION] as Cat[]) this.add(cat, n * shares[cat], isNew)
  }

  // The characters of each category in the first q characters.
  before(q: number): Triple {
    if (q <= 0) return [0, 0, 0]
    if (q >= this.end) return [...this.sums[this.sums.length - 1]] as Triple
    let lo = 0
    let hi = this.ends.length - 1
    while (lo < hi) {
      const mid = (lo + hi) >> 1
      if (this.ends[mid] < q) lo = mid + 1
      else hi = mid
    }
    const out = [...this.sums[lo - 1]] as Triple
    out[this.cats[lo]] += q - this.ends[lo - 1]
    return out
  }

  shares(): Triple {
    const t = this.end
    const s = this.sums[this.sums.length - 1]
    return t ? [s[0] / t, s[1] / t, s[2] / t] : [1, 0, 0]
  }

  driver(): Triple {
    const t = this.fresh[0] + this.fresh[1] + this.fresh[2]
    return t ? [this.fresh[0] / t, this.fresh[1] / t, this.fresh[2] / t] : [1, 0, 0]
  }
}

// One request's exact tokens: fresh input, cache read, cache write (any duration: a
// 1 h write folds in; OpenCode does not tell them apart), output with reasoning.
export type Usage = { in: number; read: number; w: number; out: number }
const INPUT_ORDER = ["read", "w", "in"] as const // the input's types from the front
const KEYS = ["in", "read", "w", "out"] as const

// OpenCode's TokenUsage.Info (the parts are disjoint: input is the fresh part, output
// the non-reasoning one), or null when there is none.
export function usageOf(tokens: any): Usage | null {
  if (!tokens || typeof tokens !== "object") return null
  const n = (x: unknown) => (Number.isFinite(Number(x)) && Number(x) > 0 ? Number(x) : 0)
  const u = { in: n(tokens.input), read: n(tokens.cache?.read), w: n(tokens.cache?.write),
              out: n(tokens.output) + n(tokens.reasoning) }
  return u.in + u.read + u.w + u.out > 0 ? u : null
}

// The input by position over the first `known` characters of the context (its state
// when the request went out); the uncovered front part is task work.
export function inputSplit(u: Usage, ctx: Context, known: number): Array<Record<"in" | "read" | "w", number>> {
  const total = u.read + u.w + u.in
  const knownTok = known / CHARS_PER_TOKEN
  const scale = knownTok ? Math.min(1, total / knownTok) : 0
  const base = total - scale * knownTok
  const at = (p: number): Triple => {
    if (p <= base || scale <= 0) return [Math.min(p, base), 0, 0]
    const c = ctx.before(Math.min(known, ((p - base) / scale) * CHARS_PER_TOKEN))
    return [base + (scale * c[0]) / CHARS_PER_TOKEN, (scale * c[1]) / CHARS_PER_TOKEN, (scale * c[2]) / CHARS_PER_TOKEN]
  }
  const out = [{ in: 0, read: 0, w: 0 }, { in: 0, read: 0, w: 0 }, { in: 0, read: 0, w: 0 }]
  let lo: Triple = [0, 0, 0]
  let edge = 0
  for (const k of INPUT_ORDER) {
    edge += u[k]
    const hi = at(edge)
    for (const cat of [TASK, OVERHEAD, COORDINATION]) out[cat][k] = Math.max(0, hi[cat] - lo[cat])
    lo = hi
  }
  return out
}

// The `usage` stat (POST /v1/stat): 15 integers, nothing else.
export type UsageStat = {
  req: number; req_ovh: number; req_coord: number
  a_in: number; a_read: number; a_w: number; a_out: number
  o_in: number; o_read: number; o_w: number; o_out: number
  c_in: number; c_read: number; c_w: number; c_out: number
}
const MAX = 10 ** 9

type Step = { known: number; driver: Triple; fresh: Triple; outCat: Triple }
type Call = { name: string; cat: Cat; wait?: boolean }

// One session's split: fed OpenCode's events (and the plugin's own texts), it sums
// the requests of a turn until take().
export class Tracker {
  ctx = new Context()
  servers: () => Iterable<string>
  private step: Step | null = null
  private calls = new Map<string, Call>()
  private compacting = false
  private sums = { req: 0, ovh: 0, coord: 0, a: zero(), o: zero(), c: zero() }

  constructor(servers: () => Iterable<string> = () => BUS_SERVERS) {
    this.servers = servers
  }

  // A human's prompt: task work.
  user(text: string): void {
    this.ctx.add(TASK, cp(text))
  }

  // A text the plugin put into the context: overhead, except the body of a woken post
  // it carries (coordination; its envelope stays overhead).
  injected(text: string, body = ""): void {
    const all = cp(text)
    const b = Math.min(all, cp(body))
    this.ctx.add(OVERHEAD, all - b)
    this.ctx.add(COORDINATION, b)
  }

  // An OpenCode event of this session (its `type` and `data`).
  event(type: string, d: any): void {
    switch (type) {
      case "session.compaction.started":
        this.compacting = true
        break
      case "session.compaction.failed":
        this.compacting = false
        break
      case "session.compaction.ended": {
        // The segments restart; the summary carries the shares of what it sums up.
        this.compacting = false
        const shares = this.ctx.shares()
        this.ctx.reset()
        this.step = null
        this.calls.clear()
        this.ctx.addSplit(cp(String(d?.text ?? "")), shares)
        break
      }
      case "session.step.started":
        if (this.compacting) break
        this.step = this.start()
        break
      case "session.step.ended":
      case "session.step.failed":
        if (this.compacting) break
        this.request(usageOf(d?.tokens))
        break
      case "session.text.ended":
      case "session.reasoning.ended": {
        if (this.compacting) break
        const shares = this.step?.driver ?? this.ctx.driver()
        this.ctx.addSplit(cp(String(d?.text ?? "")), shares, false)
        break
      }
      case "session.tool.input.started":
        if (d?.id) this.calls.set(String(d.id), { name: String(d.name ?? ""), cat: TASK })
        break
      case "session.tool.called": {
        if (this.compacting) break
        const id = String(d?.id ?? "")
        const call = this.calls.get(id) ?? { name: "", cat: TASK as Cat }
        const input = d?.input && typeof d.input === "object" ? d.input : {}
        const tool = busTool(call.name, this.servers())
        call.cat = tool === null ? (skillRead(call.name, input) ? OVERHEAD : TASK) : toolCategory(tool, input)
        call.wait = tool === "wait_for_events"
        this.calls.set(id, call)
        const size = cp(pyJson(input))
        if (this.step) this.step.outCat[call.cat] += size
        this.ctx.add(call.cat, size, false)
        break
      }
      case "session.tool.success":
      case "session.tool.failed": {
        if (this.compacting) break
        const id = String(d?.id ?? "")
        const call = this.calls.get(id)
        const cat = call?.cat ?? TASK
        this.calls.delete(id)
        const texts: string[] = []
        for (const c of Array.isArray(d?.content) ? d.content : []) if (c?.type === "text") texts.push(String(c.text ?? ""))
        if (call?.wait && type === "session.tool.success") {
          // The posts it delivered: each block (one per item, or one for all) on its own.
          for (const body of texts) for (const [c, n] of eventsSplit(body)) this.ctx.add(c, n)
          break
        }
        let n = 0
        for (const t of texts) n += cp(t)
        if (type === "session.tool.failed") n += cp(String(d?.error?.message ?? ""))
        this.ctx.add(cat, n)
        break
      }
      case "session.skill.activated": {
        // A skill's text put into the context: the bus skill's is overhead.
        if (this.compacting) break
        const bus = [d?.id, d?.name].some((x) => String(x ?? "") === SKILL || String(x ?? "").endsWith(`:${SKILL}`))
        this.ctx.add(bus ? OVERHEAD : TASK, cp(String(d?.text ?? "")))
        break
      }
    }
  }

  private start(): Step {
    const s: Step = { known: this.ctx.end, driver: this.ctx.driver(), fresh: [...this.ctx.fresh] as Triple, outCat: [0, 0, 0] }
    this.ctx.fresh = [0, 0, 0]
    return s
  }

  // One request ended with its usage: all of it, and its overhead and coordination.
  private request(u: Usage | null): void {
    const s = this.step
    this.step = null
    if (!u) {
      // No usage (an error answer): no request; its new input drives the next one.
      if (s) for (const c of [TASK, OVERHEAD, COORDINATION]) this.ctx.fresh[c] += s.fresh[c]
      return
    }
    const step = s ?? this.start()
    const sums = this.sums
    sums.req += 1
    for (const k of KEYS) sums.a[k] += u[k]
    const split = inputSplit(u, this.ctx, step.known)
    const tools = step.outCat.map((c) => c / CHARS_PER_TOKEN) as Triple
    const t = tools[0] + tools[1] + tools[2]
    if (t > u.out) for (const i of [0, 1, 2]) tools[i] = (tools[i] * u.out) / t
    const rest = u.out - Math.min(t, u.out)
    for (const [cat, into] of [[OVERHEAD, sums.o], [COORDINATION, sums.c]] as const) {
      for (const k of INPUT_ORDER) into[k] += split[cat][k]
      into.out += tools[cat] + rest * step.driver[cat]
    }
    // A request whose new input held no task work: coordination when any of it was,
    // else overhead (an empty poll's result).
    const f = step.fresh
    if (!f[TASK] && f[COORDINATION]) sums.coord += 1
    else if (!f[TASK] && f[OVERHEAD]) sums.ovh += 1
  }

  // The turn's sums as the `usage` stat, and a fresh start; null without a request.
  take(): UsageStat | null {
    const s = this.sums
    this.sums = { req: 0, ovh: 0, coord: 0, a: zero(), o: zero(), c: zero() }
    if (!s.req) return null
    const lim = (x: number) => Math.max(0, Math.min(MAX, Math.round(x)))
    const out: Record<string, number> = { req: lim(s.req), req_ovh: lim(s.ovh), req_coord: lim(s.coord) }
    for (const k of KEYS) {
      // The invariant: overhead + coordination <= all; task work is the rest.
      const a = lim(s.a[k])
      const o = Math.min(a, lim(s.o[k]))
      out[`a_${k}`] = a
      out[`o_${k}`] = o
      out[`c_${k}`] = Math.min(a - o, lim(s.c[k]))
    }
    return out as UsageStat
  }
}

function zero(): Usage {
  return { in: 0, read: 0, w: 0, out: 0 }
}
