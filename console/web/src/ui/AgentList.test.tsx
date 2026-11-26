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
  it("shows an empty value as a dash, not n/a, and the task on one line", async () => {
    const { cards } = await fleet()
    const bare: AgentCard = { ...cards[0], branch: null, ctx: null, openAsks: null, lastPostAt: null,
      task: "a long task that the row cuts to one line" }
    const html = renderToStaticMarkup(
      <AgentList cards={[bare]} asks={[]} filter="all" onFilter={noop} query="" onQuery={noop}
        onOpenAgent={noop} onOpenAsk={noop} onAllAsks={noop} />)
    const row = html.slice(html.indexOf("<tbody>"))
    expect(row).not.toContain("n/a")
    expect(row.split(EMPTY).length - 1).toBe(4)  // branch, context, asks, last post
    expect(row).toContain('title="a long task that the row cuts to one line"')
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
