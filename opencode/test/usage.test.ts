// RT-6.9 step 2: the token split of an OpenCode session (plugin/usage.ts), from
// OpenCode's events to the numbers of the `usage` stat. Imported by
// plugin.test.ts, so CI's `node --test opencode/test/plugin.test.ts` runs it too.
import assert from "node:assert/strict"
import { test } from "node:test"

import { busTool, busWord, cp, eventsSplit, inputSplit, skillRead, OVERHEAD, pyJson, TASK, COORDINATION, toolCategory, Tracker, usageOf,
  type UsageStat } from "../plugin/usage.ts"

const tok = (input: number, output = 0, read = 0, write = 0, reasoning = 0) =>
  ({ input, output, reasoning, cache: { read, write } })
// One model request: started, the tool calls it wrote, ended with its usage.
function request(tr: Tracker, tokens: any, calls: Array<{ id: string; name: string; input: any }> = [], text = "") {
  tr.event("session.step.started", { sessionID: "s1" })
  if (text) tr.event("session.text.ended", { sessionID: "s1", text })
  for (const c of calls) {
    tr.event("session.tool.input.started", { sessionID: "s1", id: c.id, name: c.name })
    tr.event("session.tool.called", { sessionID: "s1", id: c.id, input: c.input, executed: true })
  }
  tr.event("session.step.ended", { sessionID: "s1", finish: calls.length ? "tool-calls" : "stop", tokens })
}
const result = (tr: Tracker, id: string, text: string) =>
  tr.event("session.tool.success", { sessionID: "s1", id, content: [{ type: "text", text }], executed: true })
const stat = (over: Partial<UsageStat>): UsageStat => ({
  req: 0, req_ovh: 0, req_coord: 0, a_in: 0, a_read: 0, a_w: 0, a_out: 0,
  o_in: 0, o_read: 0, o_w: 0, o_out: 0, c_in: 0, c_read: 0, c_w: 0, c_out: 0, h_in: 0, h_read: 0, h_w: 0, ...over })

test("RT-6.9 step 2: OpenCode usage: a bus tool round, IDLE is overhead, DONE coordination", () => {
  const tr = new Tracker()
  // Turn 1: the human's 400 characters (100 tokens), an IDLE post (44 characters of input JSON).
  tr.user("x".repeat(400))
  const idle = { kind: "idle", message: "IDLE · done!!" }
  assert.equal(pyJson(idle), '{"kind": "idle", "message": "IDLE · done!!"}')
  assert.equal(cp(pyJson(idle)), 44)
  request(tr, tok(100, 20), [{ id: "t1", name: "ai-agent-bus_post_message", input: idle }])
  result(tr, "t1", "y".repeat(80))
  // The round after the result: cache read covers the prompt and the call (444 characters),
  // fresh input the result (80); its whole new input is the bus's.
  request(tr, tok(20, 8, 111))
  assert.deepEqual(tr.take(), stat({ req: 2, req_ovh: 1, a_in: 120, a_read: 111, a_out: 28,
    o_in: 20, o_read: 11, o_out: 11 + 8 }))
  // Turn 2: a DONE reply (its word from the message), 28 characters of input, a 40-character result.
  tr.user("z".repeat(100))
  request(tr, tok(25, 10, 131), [{ id: "t2", name: "ai-agent-bus_reply_in_thread", input: { message: "DONE · merged" } }])
  result(tr, "t2", "r".repeat(40))
  request(tr, tok(10, 4, 156, 7))
  assert.deepEqual(tr.take(), stat({ req: 2, req_coord: 1, a_in: 35, a_read: 287, a_w: 7, a_out: 14,
    o_read: 31 + 31, c_in: 10, c_w: 7, c_out: 7 + 4 }))
  // Nothing since: no stat.
  assert.equal(tr.take(), null)
})

test("RT-6.9 step 2: OpenCode usage: a wake text with a woken post's body: body coordination, envelope overhead", () => {
  const tr = new Tracker()
  tr.injected("e".repeat(40) + "b".repeat(60), "b".repeat(60))
  request(tr, tok(25, 5))
  // Output by the driver: 40 % overhead, 60 % coordination.
  assert.deepEqual(tr.take(), stat({ req: 1, req_coord: 1, a_in: 25, a_out: 5, o_in: 10, c_in: 15, o_out: 2, c_out: 3 }))
  // The plugin's texts today carry no body (the wake tells the agent to collect): overhead whole.
  tr.injected("w".repeat(100))
  request(tr, tok(25, 4, 25))
  assert.deepEqual(tr.take(), stat({ req: 1, req_ovh: 1, a_in: 25, a_read: 25, a_out: 4,
    o_read: 10, c_read: 15, o_in: 25, o_out: 4 }))
})

test("RT-6.9 step 2: OpenCode usage: a compaction restarts the segments, its own requests are not counted", () => {
  const tr = new Tracker()
  tr.user("u".repeat(400))
  tr.injected("i".repeat(400))
  request(tr, tok(200, 0))
  assert.equal(tr.take()?.o_in, 100)
  tr.event("session.compaction.started", { sessionID: "s1", reason: "auto" })
  request(tr, tok(5000, 300)) // the summary's request, if OpenCode steps it: not the agent's
  tr.event("session.compaction.ended", { sessionID: "s1", reason: "auto", text: "s".repeat(200) })
  // The context is the summary alone, at the shares of what it sums up (half overhead).
  assert.equal(tr.ctx.end, 200)
  request(tr, tok(50, 0))
  assert.deepEqual(tr.take(), stat({ req: 1, a_in: 50, o_in: 25 }))
})

test("RT-6.9 step 2: OpenCode usage: the input by position, cache read first; the invisible head is task work", () => {
  const tr = new Tracker()
  tr.user("u".repeat(400))      // 100 tokens of task work
  tr.injected("i".repeat(400))  // then 100 of overhead
  // 300 tokens of input, 200 known: the first 100 are the head (system prompt, tool docs).
  request(tr, tok(100, 0, 150, 50))
  // That uncovered front is told apart per type (A6): here it is the first 100 of the cache read.
  assert.deepEqual(tr.take(), stat({ req: 1, req_ovh: 0, a_in: 100, a_read: 150, a_w: 50, o_in: 100, h_read: 100 }))
  // More known than the input: the segments are scaled down to it (half the input is overhead).
  const t2 = new Tracker()
  t2.user("u".repeat(400))
  t2.injected("i".repeat(400))
  request(t2, tok(60, 0, 40))
  assert.deepEqual(t2.take(), stat({ req: 1, a_in: 60, a_read: 40, o_read: 0, o_in: 50 }))
})

test("RT-6.9 step 2: OpenCode usage: the driver rule: text and reasoning by the new input's categories", () => {
  const tr = new Tracker()
  tr.user("u".repeat(300))
  tr.injected("c".repeat(100), "c".repeat(100))
  tr.event("session.step.started", { sessionID: "s1" })
  tr.event("session.reasoning.ended", { sessionID: "s1", text: "r".repeat(40) })
  tr.event("session.text.ended", { sessionID: "s1", text: "t".repeat(40) })
  // OpenCode's output leaves reasoning out: a_out is both.
  tr.event("session.step.ended", { sessionID: "s1", tokens: tok(100, 6, 0, 0, 2) })
  assert.deepEqual(tr.take(), stat({ req: 1, a_in: 100, a_out: 8, c_in: 25, c_out: 2 }))
  // The written text stays in the context by the same shares, and is no new input.
  assert.deepEqual(tr.ctx.sums.at(-1), [300 + 60, 0, 100 + 20])
  assert.deepEqual(tr.ctx.fresh, [0, 0, 0])
  // Tool calls are sized exactly; more than the output: scaled to it.
  request(tr, tok(0, 2, 120), [{ id: "a", name: "ai-agent-bus_ask_human", input: { need: "n".repeat(100) } }])
  assert.deepEqual(tr.take(), stat({ req: 1, a_read: 120, a_out: 2, c_read: 25 + 5, c_out: 2 }))
})

test("RT-6.9 step 2: OpenCode usage: overhead plus coordination never exceed all, per type", () => {
  // A seeded mix of events; the invariant holds whatever the rounding.
  let seed = 7
  const rnd = (n: number) => ((seed = (seed * 1103515245 + 12345) % 2 ** 31) % n)
  const names = ["ai-agent-bus_post_message", "ai-agent-bus_wait_for_events", "ai-agent-bus_ask_human", "bash", "read"]
  for (let round = 0; round < 50; round++) {
    const tr = new Tracker()
    for (let i = 0; i < 12; i++) {
      const pick = rnd(4)
      if (pick === 0) tr.user("u".repeat(rnd(500)))
      else if (pick === 1) tr.injected("w".repeat(rnd(300) + 1), "b".repeat(rnd(100)))
      else {
        const id = `t${i}`
        request(tr, tok(rnd(50), rnd(5), rnd(200), rnd(30), rnd(3)),
                pick === 2 ? [{ id, name: names[rnd(names.length)], input: { kind: ["IDLE", "ACK", ""][rnd(3)], message: "m".repeat(rnd(60)) } }] : [],
                "t".repeat(rnd(40)))
        if (pick === 2) result(tr, id, "x".repeat(rnd(400)))
      }
    }
    const s = tr.take()
    if (!s) continue
    for (const k of ["in", "read", "w", "out"]) {
      const [a, o, c] = [(s as any)[`a_${k}`], (s as any)[`o_${k}`], (s as any)[`c_${k}`]]
      assert.ok(o >= 0 && c >= 0 && o + c <= a, `round ${round} ${k}: ${o} + ${c} <= ${a}`)
      if (k !== "out") {
        const h = (s as any)[`h_${k}`]
        assert.ok(h >= 0 && h <= a - o - c, `round ${round} h_${k}: ${h} <= ${a} - ${o} - ${c}`)
      }
    }
    assert.ok(s.req_ovh + s.req_coord <= s.req)
  }
})

test("RT-6.9 step 2: OpenCode usage: no stat without a request or without usage", () => {
  const tr = new Tracker()
  tr.user("hello")
  assert.equal(tr.take(), null)
  // A failed request without tokens, and one with zero tokens: no request; its new input drives the next.
  tr.event("session.step.started", { sessionID: "s1" })
  tr.event("session.step.failed", { sessionID: "s1", error: { type: "x", message: "overloaded" } })
  request(tr, tok(0, 0))
  assert.equal(tr.take(), null)
  assert.equal(usageOf(undefined), null)
  assert.equal(usageOf(tok(0, 0)), null)
  assert.deepEqual(usageOf(tok(1, 2, 3, 4, 5)), { in: 1, read: 3, w: 4, out: 7 })
  // A failed request that did use tokens counts.
  tr.event("session.step.failed", { sessionID: "s1", error: { type: "x", message: "cut" }, tokens: tok(10, 1) })
  assert.equal(tr.take()?.req, 1)
})

test("RT-6.9 step 2: OpenCode usage: which tool goes where", () => {
  assert.equal(busTool("ai-agent-bus_post_message"), "post_message")
  assert.equal(busTool("mattermost-agent-bus_get_me"), "get_me")
  assert.equal(busTool("mybus_get_me", ["mybus"]), "get_me") // a server configured by hand (busNames)
  assert.equal(busTool("bash"), null)
  assert.equal(busWord({ kind: "hello" }), "HELLO")
  assert.equal(busWord({ message: "PR #12 is up" }), "RESULT")
  assert.equal(busWord({ message: "**BYE** · done" }), "BYE")
  assert.equal(busWord({ message: "thanks" }), "")
  assert.equal(toolCategory("post_message", { message: "IDLE · x" }), OVERHEAD)
  assert.equal(toolCategory("reply_in_thread", { message: "sure" }), COORDINATION)
  assert.equal(toolCategory("reply_in_thread", { kind: "ACK", message: "HELLO" }), COORDINATION)
  for (const t of ["ask_human", "resolve_ask", "coordinator", "get_file", "attach", "get_thread", "list_recent"])
    assert.equal(toolCategory(t, {}), COORDINATION, t)
  for (const t of ["wait_for_events", "announce", "bus_info", "channel_ok", "get_me", "stats_report", "later_tool"])
    assert.equal(toolCategory(t, {}), OVERHEAD, t)
  // A tool that is not the bus's: call and result are task work.
  const tr = new Tracker()
  request(tr, tok(0, 1), [{ id: "b", name: "bash", input: { command: "ls" } }])
  result(tr, "b", "x".repeat(40))
  assert.deepEqual(tr.ctx.sums.at(-1)?.[TASK], cp(pyJson({ command: "ls" })) + 40)
})

test("RT-6.9 step 2: OpenCode usage: a wait_for_events result: bodies coordination, a trusted human's task, the rest overhead", () => {
  const items = [
    { kind: "message", post: { id: "p1", message: "DONE · merged" }, from_agent: true },
    { kind: "message", post: { id: "p2", message: "please also fix x" }, from_trusted_human: true },
    { kind: "event", event: "stall" },
  ]
  const body = JSON.stringify(items)
  assert.deepEqual(eventsSplit(body), [[OVERHEAD, cp(body) - 13 - 17], [TASK, 17], [COORDINATION, 13]])
  assert.deepEqual(eventsSplit("[]"), [[OVERHEAD, 2], [TASK, 0], [COORDINATION, 0]])
  assert.deepEqual(eventsSplit("no json"), [[OVERHEAD, 7]])
  // Wrapped as {"result": "<json>"}, and one item per content block.
  assert.deepEqual(eventsSplit(JSON.stringify({ result: JSON.stringify(items[0]) }))[2], [COORDINATION, 13])
  const tr = new Tracker()
  request(tr, tok(10, 1), [{ id: "w", name: "ai-agent-bus_wait_for_events", input: {} }])
  tr.event("session.tool.success", { sessionID: "s1", id: "w", executed: true,
    content: [{ type: "text", text: JSON.stringify(items[0]) }, { type: "text", text: JSON.stringify(items[2]) }] })
  const s = tr.ctx.sums.at(-1)!
  assert.equal(s[COORDINATION], 13)
  assert.equal(s[OVERHEAD], cp(pyJson({})) + cp(JSON.stringify(items[0])) - 13 + cp(JSON.stringify(items[2])))
  // The next request's new input held coordination and no task work.
  request(tr, tok(100, 1))
  assert.equal(tr.take()?.req_coord, 1)
})

test("RT-6.9 step 2: OpenCode usage: loading the bus skill and reading its side files is overhead", () => {
  assert.equal(skillRead("skill", { name: "ai-agent-bus" }), true)
  assert.equal(skillRead("skill", { name: "other" }), false)
  assert.equal(skillRead("read", { path: "/x/ai-agent-bus/skills/ai-agent-bus/coordinator.md" }), true)
  // Only reads: writing, editing or searching there is task work.
  for (const t of ["write", "edit", "grep", "glob", "bash"])
    assert.equal(skillRead(t, { path: "/x/skills/ai-agent-bus/SKILL.md", filePath: "/x/skills/ai-agent-bus/SKILL.md" }), false, t)
  assert.equal(skillRead("skill", { id: "ai-agent-bus" }), true)
  assert.equal(skillRead("read", { filePath: "/x/src/main.ts" }), false)
  const tr = new Tracker()
  tr.event("session.skill.activated", { sessionID: "s1", id: "ai-agent-bus", name: "ai-agent-bus", text: "s".repeat(80) })
  tr.event("session.skill.activated", { sessionID: "s1", id: "other", name: "other", text: "o".repeat(20) })
  request(tr, tok(0, 0, 0, 0, 0), [{ id: "r", name: "read", input: { path: "/c/skills/ai-agent-bus/tasks.md" } }])
  result(tr, "r", "m".repeat(40))
  const s = tr.ctx.sums.at(-1)!
  assert.equal(s[OVERHEAD], 80 + cp(pyJson({ path: "/c/skills/ai-agent-bus/tasks.md" })) + 40)
  assert.equal(s[TASK], 20)
  // A zero-usage answer is no request and keeps the driver: the next one sees all the new input.
  request(tr, tok(30, 3))
  assert.deepEqual(tr.take(), stat({ req: 1, a_in: 30, a_out: 3,
    o_in: Math.round(30 * (s[OVERHEAD] / (s[OVERHEAD] + 20))), o_out: Math.round(3 * ((80 + 40) / 140)) }))
})

test("RT-6.9 step 2: OpenCode usage: the uncovered front per input type (h_in, h_read, h_w)", () => {
  // 100 known characters (25 tokens) of overhead; 100 tokens of input: the front 75 are uncovered.
  const tr = new Tracker()
  tr.injected("i".repeat(100))
  // read 50 (all uncovered), write 40 (25 uncovered, 15 known), fresh 10 (known).
  request(tr, tok(10, 0, 50, 40))
  request(tr, tok(10, 0, 50, 40)) // the same again: summed over the turn
  assert.deepEqual(tr.take(), stat({ req: 2, req_ovh: 1, a_in: 20, a_read: 100, a_w: 80,
    o_in: 20, o_w: 30, h_read: 100, h_w: 50, h_in: 0 }))
  // All known: nothing uncovered; nothing known: all of it.
  const t2 = new Tracker()
  t2.user("u".repeat(400))
  assert.deepEqual(inputSplit({ in: 20, read: 80, w: 0, out: 0 }, t2.ctx, 400)[3], { in: 0, read: 0, w: 0 })
  assert.deepEqual(inputSplit({ in: 20, read: 80, w: 5, out: 0 }, t2.ctx, 0)[3], { in: 20, read: 80, w: 5 })
})
