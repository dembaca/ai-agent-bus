import { afterEach, describe, expect, it, vi } from "vitest"
import type { Operator, Post } from "./api/types"
import { loadReads, repliesByOthers, saveReads, unread } from "./reads"

function fakeStorage(init: Record<string, string> = {}) {
  const data = { ...init }
  vi.stubGlobal("window", { localStorage: {
    getItem: (k: string) => data[k] ?? null,
    setItem: (k: string, v: string) => { data[k] = v },
  } })
  return data
}

afterEach(() => { vi.unstubAllGlobals() })

describe("reads (#285)", () => {
  it("counts replies by others: not the root, not the operator's own", () => {
    const me: Operator = { username: "dembaca", displayName: "Andreas D", groups: [], build: { version: null, revision: null } }
    const post = (id: string, author: string, rootId: string | null): Post =>
      ({ id, rootId, author, at: 1, message: id })
    const thread = [post("root", "agent-x", null), post("r1", "agent-x", "root"), post("r2", "DemBaca", "root"),
      post("r3", "Andreas D", "root"), post("r4", "carol", "root"), post("r5", "agent-y", "root")]
    expect(repliesByOthers(thread, "root", me)).toBe(3)  // r1, r4, r5
    expect(repliesByOthers(thread, "root", null)).toBe(5)  // nobody known: only the root is left out
    expect(repliesByOthers([post("root", "x", null)], "root", me)).toBe(0)
    // never opened: all of them; seen up to 2: the rest; fewer than seen: none
    expect(unread(3, {}, "root")).toBe(3)
    expect(unread(3, { root: 2 }, "root")).toBe(1)
    expect(unread(3, { root: 5 }, "root")).toBe(0)
  })

  it("counts the posts after the read position, all when never opened", () => {
    expect(unread(5, {}, "a")).toBe(5)
    expect(unread(5, { a: 3 }, "a")).toBe(2)
    expect(unread(2, { a: 3 }, "a")).toBe(0)
  })

  it("remembers per person and drops what is no count", () => {
    const data = fakeStorage({ "choirbus.console.reads.v2.bob": JSON.stringify({ a: 2, b: "x", c: -1, d: 1.5 }) })
    expect(loadReads("bob")).toEqual({ a: 2 })
    saveReads("ann", { z: 4 })
    expect(loadReads("ann")).toEqual({ z: 4 })
    expect(Object.keys(data).sort()).toEqual(["choirbus.console.reads.v2.ann", "choirbus.console.reads.v2.bob"])
    expect(loadReads(null)).toEqual({})
  })

  it("works without storage", () => {
    vi.stubGlobal("window", { get localStorage(): never { throw new Error("blocked") } })
    expect(loadReads("ann")).toEqual({})
    expect(() => { saveReads("ann", { a: 1 }) }).not.toThrow()
  })
})
