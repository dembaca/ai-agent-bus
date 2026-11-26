import { renderToStaticMarkup } from "react-dom/server"
import { describe, expect, it } from "vitest"
import type { AgentCard, Ask } from "../api/types"
import { mockApi } from "../api/mock"
import { AgentList, EMPTY } from "./AgentList"
import { Detail } from "./Detail"
import { Overview } from "./Overview"

// The pages render from the API's own shapes: the mock's first snapshot
// and its asks, nothing else.
async function fleet(): Promise<{ cards: AgentCard[]; asks: Ask[] }> {
  const api = mockApi()
  let cards: AgentCard[] = []
  const stop = api.watchFleet((e) => { if (e.type === "snapshot") cards = e.cards })
  await new Promise((r) => setTimeout(r, 0))
  stop()
  return { cards, asks: await api.asks() }
}

const noop = () => {}

describe("the pages", () => {
  it("shows the stated name, a shared one with its mark, the bus id in the title (#199)", async () => {
    const { cards } = await fleet()
    const shared: AgentCard = { ...cards[0], name: "agent-oudzj4bhpl62iahctur5s5sf44",
      displayName: "agent-oudzj4bhpl62iahctur5s5sf44", statedName: "agent-same", nameShared: true }
    const html = renderToStaticMarkup(
      <AgentList cards={[shared]} asks={[]} filter="all" onFilter={noop} query="" onQuery={noop}
        onOpenAgent={noop} onOpenAsk={noop} onAllAsks={noop} />)
    expect(html).toContain(">agent-same</button>")
    expect(html).toContain("shared name")
    expect(html).toContain('title="agent-oudzj4bhpl62iahctur5s5sf44 · another agent states this name too: not confirmed"')
  })

  it("keeps four columns beside an open detail and marks the open agent (#200)", async () => {
    const { cards } = await fleet()
    const html = renderToStaticMarkup(
      <AgentList cards={cards} asks={[]} compact selected={cards[1].name} filter="all" onFilter={noop}
        query="" onQuery={noop} onOpenAgent={noop} onOpenAsk={noop} onAllAsks={noop} />)
    expect(html).toMatch(/<colgroup>(<col class="agents__col--[a-z]+"\/>){4}<\/colgroup>/)
    expect(html).not.toContain(">Branch<")
    expect(html.match(/agents__row--selected/g)?.length).toBe(1)
    expect(html).toContain('aria-current="true"')
  })

  it("shows the Other filter with its count only while a state fits no group", async () => {
    const { cards } = await fleet()
    const list = (all: AgentCard[]) => renderToStaticMarkup(
      <AgentList cards={all} asks={[]} filter="all" onFilter={noop} query="" onQuery={noop}
        onOpenAgent={noop} onOpenAsk={noop} onAllAsks={noop} />)
    const known = cards.map((c) => ({ ...c, state: "working" as const }))
    expect(list(known)).not.toContain(">Other<")
    const odd = [...known, { ...known[0], name: "agent-odd", state: "dreaming" as AgentCard["state"] }]
    expect(list(odd)).toMatch(/>Other<span class="seg__count">1<\/span>/)
  })

  it("keeps the guest count outside the home's clip", async () => {
    const { cards } = await fleet()
    const html = renderToStaticMarkup(
      <AgentList cards={[{ ...cards[0], home: "~a-long-home", guests: ["~x", "~y"] }]} asks={[]}
        filter="all" onFilter={noop} query="" onQuery={noop}
        onOpenAgent={noop} onOpenAsk={noop} onAllAsks={noop} />)
    expect(html).toContain('<span class="agents__clip">~a-long-home</span><span class="muted agents__guests">+2</span>')
  })

  it("shows an empty value as a dash, not n/a, and the task on one line", async () => {
    const { cards } = await fleet()
    const bare: AgentCard = { ...cards[0], branch: null, ctx: null, openAsks: null, lastPostAt: null,
      task: "a long task that the row cuts to one line" }
    const html = renderToStaticMarkup(
      <AgentList cards={[bare]} asks={[]} filter="all" onFilter={noop} query="" onQuery={noop}
        onOpenAgent={noop} onOpenAsk={noop} onAllAsks={noop} />)
    const row = html.slice(html.indexOf("<tbody>"))
    expect(row).not.toContain("n/a")
    const shown = row.replace(/ title="[^"]*"/g, "")  // the text, not the titles
    expect(shown.split(EMPTY).length - 1).toBe(4)  // branch, context, asks, last post
    expect(row).toContain('title="a long task that the row cuts to one line"')
  })

  it("fixes the column widths and puts every clipped text in a title", async () => {
    const { cards } = await fleet()
    const card: AgentCard = { ...cards[0], harness: "opencode", model: "glm-5.3-long-model-name",
      branch: "feat/a-very-long-branch-name-that-clips", task: "the task" }
    const html = renderToStaticMarkup(
      <AgentList cards={[card]} asks={[]} filter="all" onFilter={noop} query="" onQuery={noop}
        onOpenAgent={noop} onOpenAsk={noop} onAllAsks={noop} />)
    expect(html).toMatch(/<colgroup>(<col class="agents__col--[a-z]+"\/>){8}<\/colgroup>/)
    expect(html).toContain('title="the task"')
    expect(html).toContain('title="feat/a-very-long-branch-name-that-clips"')
    expect(html).toContain('title="OpenCode · glm-5.3-long-model-name"')
    expect(html).toContain(`title="${card.home}`)
  })

  it("names both pills in the status cell's title, the plain state when none waits", async () => {
    const { cards } = await fleet()
    const base: AgentCard = { ...cards[0], state: "working", asksForOperator: 0 }
    const waitsCard: AgentCard = { ...base, asksForOperator: 1 }
    const waitsHtml = renderToStaticMarkup(
      <AgentList cards={[waitsCard]} asks={[]} filter="all" onFilter={noop} query="" onQuery={noop}
        onOpenAgent={noop} onOpenAsk={noop} onAllAsks={noop} />)
    expect(waitsHtml).toContain('title="working · waits on you"')
    expect(waitsHtml).toContain('>waits on you</span>')
    const plainHtml = renderToStaticMarkup(
      <AgentList cards={[base]} asks={[]} filter="all" onFilter={noop} query="" onQuery={noop}
        onOpenAgent={noop} onOpenAsk={noop} onAllAsks={noop} />)
    expect(plainHtml).toContain('title="working"')
    expect(plainHtml).not.toContain('>waits on you</span>')
  })

  it("lists the open asks on top and one row per agent", async () => {
    const { cards, asks } = await fleet()
    expect(cards.length).toBeGreaterThan(0)
    const html = renderToStaticMarkup(
      <AgentList cards={cards} asks={asks} filter="all" onFilter={noop} query="" onQuery={noop}
        onOpenAgent={noop} onOpenAsk={noop} onAllAsks={noop} />)
    expect(html).toContain('aria-label="Open asks"')
    expect(html.match(/class="agents__row/g)?.length).toBe(cards.length)
    expect(html).toContain('aria-pressed="true"')  // the "All" filter
    for (const card of cards) expect(html).toContain(card.displayName)
  })

  it("renders an agent's detail and the overview from the same data", async () => {
    const { cards, asks } = await fleet()
    const card = cards[0]
    const detail = renderToStaticMarkup(
      <Detail api={mockApi()} card={card} ask={null} asks={asks} from="Agents" onBack={noop} onFail={noop} />)
    expect(detail).toContain(card.name)
    expect(detail).toContain("Current task")
    const overview = renderToStaticMarkup(
      <Overview cards={cards} asks={asks} onOpenAsk={noop} onView={noop} />)
    expect(overview).toContain(`of ${cards.length}`)
  })
})
