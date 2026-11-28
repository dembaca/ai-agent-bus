import { afterEach, describe, expect, it, vi } from "vitest"
import { loadReads, saveReads, unread } from "./reads"

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
  it("counts the posts after the read position, all when never opened", () => {
    expect(unread(5, {}, "a")).toBe(5)
    expect(unread(5, { a: 3 }, "a")).toBe(2)
    expect(unread(2, { a: 3 }, "a")).toBe(0)
  })

  it("remembers per person and drops what is no count", () => {
    const data = fakeStorage({ "choirbus.console.reads.bob": JSON.stringify({ a: 2, b: "x", c: -1, d: 1.5 }) })
    expect(loadReads("bob")).toEqual({ a: 2 })
    saveReads("ann", { z: 4 })
    expect(loadReads("ann")).toEqual({ z: 4 })
    expect(Object.keys(data).sort()).toEqual(["choirbus.console.reads.ann", "choirbus.console.reads.bob"])
    expect(loadReads(null)).toEqual({})
  })

  it("works without storage", () => {
    vi.stubGlobal("window", { get localStorage(): never { throw new Error("blocked") } })
    expect(loadReads("ann")).toEqual({})
    expect(() => { saveReads("ann", { a: 1 }) }).not.toThrow()
  })
})
