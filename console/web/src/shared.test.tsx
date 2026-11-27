import { renderToStaticMarkup } from "react-dom/server"
import { describe, expect, it } from "vitest"
import type { AgentCard, Ask, FleetEvent } from "./api/types"
import { mockApi } from "./api/mock"
import { Split, resolvePlace, selectedAfter } from "./nav"
import { AgentList } from "./ui/AgentList"
import { Asks } from "./ui/Asks"
import { Detail } from "./ui/Detail"
import { Overview } from "./ui/Overview"
import { filterCards } from "./ui/agents"
import { toFlow } from "./ui/graph/model"

// #199, Fable's review of #203: wherever an agent is named beside a
// decision, a shared name carries its mark — an impostor's ask never reads
// as another agent's.

const NATS = "oudzj4bhpl62iahctur5s5sf4"
const noop = () => {}

async function fleet(): Promise<AgentCard[]> {
  let cards: AgentCard[] = []
  const stop = mockApi().watchFleet((e) => { if (e.type === "snapshot") cards = e.cards })
  await new Promise((r) => setTimeout(r, 0))
  stop()
  return cards
}

async function sharedFleet(): Promise<{ card: AgentCard; cards: AgentCard[]; ask: Ask }> {
  const base = (await fleet())[0]
  const card: AgentCard = { ...base, name: NATS, displayName: NATS, statedName: "agent-same",
    nameShared: true, state: "working" }
  const ask: Ask = {
    id: "p-ask", by: NATS, displayName: NATS, home: card.home, kind: "approval", human: "dembaca",
    need: "Merge PR #1?", at: Date.now() - 60_000, end: "open", forMe: true, mayAnswer: false,
    agentGone: false, threadId: "p-ask",
  }
  return { card, cards: [card], ask }
}

const MARK = "shared name"

describe("a shared name keeps its mark", () => {
  it("on the ask tile above the list", async () => {
    const { cards, ask } = await sharedFleet()
    const html = renderToStaticMarkup(<AgentList cards={cards} asks={[ask]} filter="all" onFilter={noop}
      query="" onQuery={noop} onOpenAgent={noop} onOpenAsk={noop} onAllAsks={noop} />)
    const tile = html.slice(html.indexOf('aria-label="Open asks"'), html.indexOf('aria-label="Agents"'))
    expect(tile).toContain(">agent-same</span>")
    expect(tile).toContain(MARK)
  })

  it("in the asks table", async () => {
    const { cards, ask } = await sharedFleet()
    const html = renderToStaticMarkup(<Asks asks={[ask]} cards={cards} onOpen={noop} />)
    expect(html).toContain(">agent-same</span>")
    expect(html).toContain(MARK)
  })

  it("in the Overview's Needs you", async () => {
    const { cards, ask } = await sharedFleet()
    const html = renderToStaticMarkup(<Overview cards={cards} asks={[ask]} onOpenAsk={noop} onView={noop} />)
    const needs = html.slice(html.indexOf('aria-label="Needs you"'))
    expect(needs).toContain(">agent-same</span>")
    expect(needs).toContain(MARK)
  })

  it("in the detail's ask block", async () => {
    const { card, ask } = await sharedFleet()
    const html = renderToStaticMarkup(<Detail api={mockApi()} card={card} ask={ask} asks={[ask]}
      from="Asks" onBack={noop} onFail={noop} />)
    const block = html.slice(html.indexOf('aria-label="The ask"'))
    expect(block).toContain(">agent-same</span>")
    expect(block).toContain(MARK)
  })

  it("on the graph's agent and ask nodes", async () => {
    const { cards, ask } = await sharedFleet()
    const flow = toFlow({ graph: { nodes: [], edges: [], cutDepth: null }, cards, asks: [ask] })
    const agent = flow.nodes.find((n) => n.data.kind === "agent")!
    const asked = flow.nodes.find((n) => n.data.kind === "ask")!
    expect(agent.data).toMatchObject({ label: "agent-same", shared: true })
    expect(asked.data).toMatchObject({ by: "agent-same", byShared: true })
  })

  it("is found by the search", async () => {
    const { cards } = await sharedFleet()
    expect(filterCards(cards, "all", "agent-same").map((c) => c.name)).toEqual([NATS])
  })
})

describe("routing (#200)", () => {
  it("shows a detail beside the list, the list still there", () => {
    const html = renderToStaticMarkup(<Split detail={<p>the detail</p>}><p>the list</p></Split>)
    expect(html).toBe('<div class="split"><div class="view"><p>the list</p></div>'
      + '<div class="split__detail"><p>the detail</p></div></div>')
    expect(renderToStaticMarkup(<Split detail={false}><p>the list</p></Split>))
      .toBe('<div class="solo"><div class="view"><p>the list</p></div></div>')
  })

  it("goes back to the list on popstate: a place without a detail", async () => {
    const cards = await fleet()
    expect(resolvePlace({ view: "agents", agent: cards[0].name }, cards, []))
      .toMatchObject({ view: "agents", agent: { name: cards[0].name }, ask: null })
    expect(resolvePlace({ view: "agents" }, cards, [])).toEqual({ view: "agents", agent: null, ask: null })
  })

  it("closes the detail of an agent that left the fleet", async () => {
    const cards = await fleet()
    const sel = cards[0]
    const drop: FleetEvent = { type: "drop", name: sel.name }
    expect(selectedAfter(drop, sel)).toBeNull()
    expect(selectedAfter({ type: "snapshot", cards: cards.slice(1) }, sel)).toBeNull()
    const newer = { ...sel, task: "a newer task" }
    expect(selectedAfter({ type: "card", card: newer }, sel)).toBe(newer)
    expect(selectedAfter({ type: "drop", name: "someone-else" }, sel)).toBe(sel)
    // a place naming an agent that is gone opens no agent
    expect(resolvePlace({ view: "agents", agent: "agent-gone" }, cards, []).agent).toBeNull()
  })
})
