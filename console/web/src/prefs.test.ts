import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { DEFAULT_PREFS, isFolded, loadPrefs, savePrefs } from "./prefs"
import { agentOf, asPlace, neighbour, placeOfSearch, searchOf } from "./nav"

function fakeStorage(): Storage {
  const data = new Map<string, string>()
  return {
    get length() { return data.size },
    clear: () => data.clear(),
    getItem: (k) => data.get(k) ?? null,
    key: (i) => [...data.keys()][i] ?? null,
    removeItem: (k) => { data.delete(k) },
    setItem: (k, v) => { data.set(k, v) },
  }
}

describe("what the console remembers per person (#283)", () => {
  beforeEach(() => { vi.stubGlobal("window", { localStorage: fakeStorage() }) })
  afterEach(() => { vi.unstubAllGlobals() })

  it("keeps the panel, the grouping, the folded groups and the scope per person", () => {
    savePrefs("dembaca", { panel: "folded", by: "owner", scope: "everyone", folded: { "owner:carol": false }, range: "7d" })
    expect(loadPrefs("dembaca")).toEqual({ panel: "folded", by: "owner", scope: "everyone",
      folded: { "owner:carol": false }, range: "7d" })
    expect(loadPrefs("carol")).toEqual(DEFAULT_PREFS)  // another person on the same browser
    expect(loadPrefs(null)).toEqual(DEFAULT_PREFS)     // not known yet
  })

  it("remembers the Stats range per person, 7 d by default, and drops an unknown one (#284)", () => {
    expect(DEFAULT_PREFS.range).toBe("7d")
    savePrefs("dembaca", { ...DEFAULT_PREFS, range: "90d" })
    expect(loadPrefs("dembaca").range).toBe("90d")
    expect(loadPrefs("carol").range).toBe("7d")
    window.localStorage.setItem("choirbus.console.x", JSON.stringify({ range: "3y" }))
    expect(loadPrefs("x").range).toBe("7d")
    window.localStorage.setItem("choirbus.console.x", JSON.stringify({ range: 7 }))
    expect(loadPrefs("x").range).toBe("7d")
  })

  it("drops what is no longer a known word, never trusts it", () => {
    window.localStorage.setItem("choirbus.console.x",
      JSON.stringify({ panel: "wide", by: "colour", scope: "all", folded: { a: "yes", b: true } }))
    expect(loadPrefs("x")).toEqual({ ...DEFAULT_PREFS, folded: { b: true } })
    window.localStorage.setItem("choirbus.console.x", "{not json")
    expect(loadPrefs("x")).toEqual(DEFAULT_PREFS)
  })

  it("is no error when the storage refuses", () => {
    vi.stubGlobal("window", { get localStorage(): Storage { throw new Error("blocked") } })
    expect(loadPrefs("dembaca")).toEqual(DEFAULT_PREFS)
    expect(() => savePrefs("dembaca", DEFAULT_PREFS)).not.toThrow()
  })

  it("folds by the scope's default until the person picks", () => {
    const everyone = { ...DEFAULT_PREFS, scope: "everyone" as const }
    expect(isFolded(everyone, "~dev", false)).toBe(true)
    expect(isFolded(everyone, "~dev", true)).toBe(false)           // needs the operator
    expect(isFolded({ ...everyone, folded: { "home:~dev": false } }, "~dev", false)).toBe(false)
    expect(isFolded(DEFAULT_PREFS, "~dev", false)).toBe(false)      // Mine starts open
    expect(isFolded({ ...DEFAULT_PREFS, folded: { "home:~dev": true } }, "~dev", false)).toBe(true)
  })
})

describe("the address and the places (#283)", () => {
  it("puts the open agent in the address and reads it back", () => {
    expect(searchOf({ view: "agents" })).toBe("")
    expect(searchOf({ view: "agents", agent: "a b" })).toBe("?agent=a+b")
    expect(searchOf({ view: "asks", ask: "p1", agent: "x" })).toBe("?view=asks&agent=x&ask=p1")
    expect(placeOfSearch("?agent=a+b")).toEqual({ view: "agents", agent: "a b" })
    expect(placeOfSearch("?view=stats")).toEqual({ view: "stats" })
    expect(placeOfSearch("?view=graph")).toEqual({ view: "agents" })   // a view that is gone
    expect(placeOfSearch("")).toEqual({ view: "agents" })
  })

  it("reads only places it wrote", () => {
    expect(asPlace({ view: "agents", agent: "agent-a", ask: "p1" }))
      .toEqual({ view: "agents", agent: "agent-a", ask: "p1" })
    expect(asPlace({ view: "overview" })).toBeNull()
    expect(asPlace({ view: "graph" })).toBeNull()
    expect(asPlace(null)).toBeNull()
  })

  it("finds the agent of a drawer by name, or by the ask's asker; none for one that left", () => {
    const cards = [{ name: "a" }, { name: "b" }]
    expect(agentOf({ view: "agents", agent: "a" }, cards, undefined)).toBe(cards[0])
    expect(agentOf({ view: "asks", ask: "p" }, cards, "b")).toBe(cards[1])
    expect(agentOf({ view: "agents", agent: "gone" }, cards, undefined)).toBeNull()
    expect(agentOf({ view: "agents" }, cards, undefined)).toBeNull()
  })

  it("moves to the next agent with the arrow keys and stops at the ends", () => {
    const order = ["a", "b", "c"]
    expect(neighbour(order, "a", 1)).toBe("b")
    expect(neighbour(order, "b", -1)).toBe("a")
    expect(neighbour(order, "c", 1)).toBe("c")
    expect(neighbour(order, "a", -1)).toBe("a")
    expect(neighbour(order, "folded-away", 1)).toBe("a")   // not in the list: the first
    expect(neighbour(order, null, -1)).toBe("c")
    expect(neighbour([], "a", 1)).toBeNull()
  })
})
