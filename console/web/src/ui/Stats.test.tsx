import { describe, expect, it } from "vitest"
import { renderToStaticMarkup } from "react-dom/server"
import type { ActivityBar } from "../api/types"
import { mockApi } from "../api/mock"
import { ActivityChart, RANGES, Stats, barLabel } from "./Stats"

const groups = [{ id: "working", label: "working on it" }, { id: "finished", label: "finished something" },
  { id: "asking", label: "asking a human" }, { id: "organising", label: "organising" }]

describe("Stats (#284)", () => {
  it("offers the five time ranges and names the range under the heading", () => {
    expect(RANGES.map((r) => r.label)).toEqual(["24 h", "7 days", "30 days", "90 days", "12 months"])
    const html = renderToStaticMarkup(<Stats api={mockApi()} onFail={() => {}} />)
    expect(html).toContain("<h1>Stats</h1>")
    expect(html).toContain("the last 7 days")
    expect(html).toContain('aria-pressed="true">7 days<')
    for (const r of RANGES) expect(html).toContain(`>${r.label}</button>`)
  })

  it("draws a stacked bar per bucket with the exact words in its title, and explains the groups", () => {
    const bars: ActivityBar[] = [
      { start: 1763942400, groups: { working: 5, finished: 2, asking: 1, organising: 3 },
        words: { ACK: 3, PROGRESS: 2, RESULT: 1, DONE: 1, ask: 1, TASK: 1, plain: 2 } },
      { start: 1764028800, groups: { working: 0, finished: 0, asking: 0, organising: 0 }, words: {} },
    ]
    const html = renderToStaticMarkup(<ActivityChart activity={{ bucket: "day", groups, bars }} />)
    expect(html.match(/activity__col/g)).toHaveLength(2)
    expect(html).toContain("ACK 3, PROGRESS 2, RESULT 1, DONE 1, ask 1, TASK 1, plain 2")
    for (const l of groups) expect(html).toContain(l.label)
    expect(html).toContain("ACK, PROGRESS")
    expect(html).toContain("TASK, HELLO, IDLE, BYE, plain posts")
    expect(html).toContain("Every post of an agent starts with a bus word")
    expect(html).toContain("people&#x27;s posts are not in it")
    expect(html).not.toMatch(/fleet/i)
  })

  it("says why 24 h has no bars, and when nothing was counted", () => {
    const why = "hourly figures need agents that publish hourly"
    const html = renderToStaticMarkup(<ActivityChart activity={{ bucket: "hour", groups, bars: [], unavailable: why }} />)
    expect(html).toContain(why)
    expect(html).not.toContain("activity__col")
    const none = renderToStaticMarkup(<ActivityChart activity={{ bucket: "day", groups, bars: [] }} />)
    expect(none).toContain("no posts counted in this range")
  })

  it("labels a bar by its bucket, in UTC", () => {
    expect(barLabel(Date.UTC(2026, 10, 2) / 1000, "week")).toMatch(/^week of /)
    expect(barLabel(Date.UTC(2026, 10, 2) / 1000, "month")).toMatch(/26/)
  })
})
