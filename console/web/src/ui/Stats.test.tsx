import { describe, expect, it } from "vitest"
import { renderToStaticMarkup } from "react-dom/server"
import type { ActivityBar } from "../api/types"
import { mockApi } from "../api/mock"
import { ActivityChart, AsksCard, Cards, RANGES, ShareCard, Stats, TokensCard, TrafficCard, barLabel } from "./Stats"

const groups = [{ id: "working", label: "working on it" }, { id: "finished", label: "finished something" },
  { id: "asking", label: "asking a human" }, { id: "organising", label: "organising" }]

describe("Stats (#284)", () => {
  it("offers the five time ranges and names the range under the heading", () => {
    expect(RANGES.map((r) => r.label)).toEqual(["24 h", "7 days", "30 days", "90 days", "12 months"])
    const html = renderToStaticMarkup(<Stats api={mockApi()} onFail={() => {}} range="30d" onRange={() => {}} />)
    expect(html).toContain("<h1>Stats</h1>")
    expect(html).toContain("the last 30 days")
    expect(html).toContain('aria-pressed="true">30 days<')
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
    const html = renderToStaticMarkup(
      <ActivityChart activity={{ bucket: "hour", groups, bars: [], unavailable: "no hourly data yet", reason: why }} />)
    expect(html).toContain("no hourly data yet")
    expect(html).toContain(why)
    expect(html).not.toMatch(/>0</)
    expect(html).not.toContain("activity__col")
    const none = renderToStaticMarkup(<ActivityChart activity={{ bucket: "day", groups, bars: [] }} />)
    expect(none).toContain("no posts counted in this range")
  })

  it("builds no old report markup anywhere on the page", async () => {
    const html = renderToStaticMarkup(<Cards reports={await mockApi().stats("7d")} />)
    expect(html).not.toContain("report__")
    expect(html).not.toMatch(/class="report[ "]/)
    expect(html.match(/<section/g)).toHaveLength(5)
  })

  it("share card: a thin bar per version, full width 20 %, n/a for a version without counts", async () => {
    const r = (await mockApi().stats("7d")).find((x) => x.id === "share-per-version")!
    const html = renderToStaticMarkup(<ShareCard report={r} />)
    expect(html).toContain("0.100.0")
    expect(html).toContain("7.4%")
    expect(html).toContain('aria-label="0.100.0: 7.4 percent of all tokens"')
    expect(html).toContain("width:37%")
    expect(html).toContain('aria-label="0.99.0: n/a, no token counts"')
    expect(html).toContain("The bar&#x27;s full width is 20%")
    expect(html).not.toMatch(/>0%</)
  })

  it("asks card: the range and the range before as table rows, headers with scope", async () => {
    const reports = await mockApi().stats("7d")
    const html = renderToStaticMarkup(<AsksCard now={reports.find((x) => x.id === "asks")!}
      before={reports.find((x) => x.id === "asks-before")} />)
    for (const h of ["Window", "Asked", "Answered", "Mean wait", "Longest wait"]) expect(html).toContain(`scope="col">${h}<`)
    expect(html).toContain('scope="row">7 d<')
    expect(html).toContain('scope="row">the 7 d before<')
    expect(html).toContain("stats-asks__row--before")
    expect(html).toContain('<td data-label="Asked">19</td>')
    expect(html).toContain("11 min")
    expect(html).toContain("38 min")
    expect(html).toContain("An ask that expired without an answer counts as asked")
  })

  it("asks card: the range before the stream does not hold is n/a with its reason, never 0", async () => {
    const reports = await mockApi().stats("12m")
    const before = reports.find((r) => r.id === "asks-before")!
    expect(before.window).toBe("the 12 mo before")
    expect(before.fields.every((f) => f.value === null)).toBe(true)
    const html = renderToStaticMarkup(<AsksCard now={reports.find((x) => x.id === "asks")!} before={before} />)
    expect(html).toContain("the 12 mo before: n/a, the stream holds 12 months at most")
    expect(html.match(/<td data-label="[^"]+">n\/a<\/td>/g)).toHaveLength(5)
    expect(html).not.toMatch(/<td[^>]*>0/)
    const held = await mockApi().stats("7d")
    expect(held.find((r) => r.id === "asks-before")!.unavailable).toBeUndefined()
  })

  it("traffic card: bars relative to the largest with thousands separators, n/a where missing", async () => {
    const r = (await mockApi().stats("7d")).find((x) => x.id === "traffic-by-category")!
    const html = renderToStaticMarkup(<TrafficCard report={r} />)
    expect(html).toContain("96,400")
    expect(html).toContain("width:100%")
    expect(html).toContain('aria-label="Items, bus overhead: 212"')
    expect(html).toContain("Counted from the STATS lines every session publishes.")
    const na = { ...r, fields: r.fields.map((f) => ({ ...f, value: null })), unavailable: "no lines in this range" }
    const nahtml = renderToStaticMarkup(<TrafficCard report={na} />)
    expect(nahtml).toContain("n/a: no lines in this range")
    expect(nahtml).toContain('aria-label="Items, bus overhead: n/a"')
    expect(nahtml).not.toMatch(/>0</)
  })

  it("tokens card: estimated chip, unit switch, a stacked bar per row with its numbers in the label", async () => {
    const r = (await mockApi().stats("7d")).find((x) => x.id === "tokens-by-category")!
    const html = renderToStaticMarkup(<TokensCard report={r} />)
    expect(html).toContain("estimated</span>")
    expect(html).toContain('aria-pressed="true">Input-token equivalents<')
    expect(html).toContain('aria-pressed="false">Raw tokens<')
    expect(html).toContain('aria-label="flock: 83 percent task work, 6 percent bus overhead, 11 percent coordination"')
    for (const l of ["task work", "bus overhead", "coordination"]) expect(html).toContain(l)
    expect(html).toContain("A request&#x27;s tokens are split by what its context and output hold")
    const none = renderToStaticMarkup(<TokensCard report={{ ...r, stacks: [], unavailable: "no token split" }} />)
    expect(none).toContain("n/a: no token split")
  })

  it("labels a bar by its bucket, in UTC", () => {
    expect(barLabel(Date.UTC(2026, 10, 2) / 1000, "week")).toMatch(/^week of /)
    expect(barLabel(Date.UTC(2026, 10, 2) / 1000, "month")).toMatch(/26/)
  })
})
