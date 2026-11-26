// The mock's live changes (#145): the graph view needs an agent that joins
// and leaves, an ask that opens and closes, and states that change.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { liveAskEnd, mockApi, visitorPresent } from "./mock"
import type { FleetEvent } from "./types"

describe("the mock's schedule", () => {
  it("lets the visitor join and later leave", () => {
    const ticks = Array.from({ length: 32 }, (_, i) => visitorPresent(i))
    expect(ticks).toContain(true)
    expect(ticks).toContain(false)
    expect(ticks.lastIndexOf(true)).toBeGreaterThan(ticks.indexOf(true))
  })

  it("opens the live ask and later closes it", () => {
    const ends = Array.from({ length: 40 }, (_, i) => liveAskEnd(i))
    expect(ends[0]).toBeNull()
    expect(ends).toContain("open")
    expect(ends.lastIndexOf("answered")).toBeGreaterThan(ends.indexOf("open"))
  })
})

describe("mockApi live updates", () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it("streams a snapshot, state changes, a join, a drop and the asker's card", async () => {
    const api = mockApi()
    const events: FleetEvent[] = []
    const stop = api.watchFleet((e) => events.push(e))
    expect(events[0]?.type).toBe("snapshot")

    let sawOpen = false
    let sawClosed = false
    for (let t = 0; t < 40; t++) {
      vi.advanceTimersByTime(4000)
      const live = (await api.asks()).find((a) => a.id === "k5w8r2n7c4x1q9m3t6v0bzjdya")
      if (live?.end === "open") sawOpen = true
      if (sawOpen && live?.end === "answered") sawClosed = true
    }
    stop()

    const joins = events.filter((e) => e.type === "card" && e.card.name === "agent-mac-ai-agent-e41a07")
    const drops = events.filter((e) => e.type === "drop" && e.name === "agent-mac-ai-agent-e41a07")
    expect(joins.length).toBeGreaterThan(0)
    expect(drops.length).toBeGreaterThan(0)
    expect(events.some((e) => e.type === "card" && e.card.name === "agent-mac-ai-agent-b7ccf7")).toBe(true)
    expect(sawOpen).toBe(true)
    expect(sawClosed).toBe(true)

    // Nothing after stop.
    const n = events.length
    vi.advanceTimersByTime(20_000)
    expect(events.length).toBe(n)
  })

  it("keeps the fixed asks the Asks view shows", async () => {
    const ids = (await mockApi().asks()).map((a) => a.id)
    expect(ids).toEqual(expect.arrayContaining([
      "8xqk3b1zwtrd5mfy6nqpe4hc1o", "3hq9t1c6pzbuxk8e7wj2r5dn4a",
      "p4n8c2vxk7mq1zr6d3wy5ehb9t", "z7k2m9q4w1x8c5v3b6n0rjtyhd",
    ]))
  })
})
