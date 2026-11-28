import { renderToStaticMarkup } from "react-dom/server"
import { describe, expect, it } from "vitest"
import type { AgentCard, Ask } from "./api/types"
import { mockApi, mockGroups } from "./api/mock"
import { Asks } from "./ui/Asks"
import { Detail } from "./ui/Detail"
import { Glance } from "./ui/Glance"
import { matches } from "./ui/AgentList"

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
  it("in the panel's Needs you", async () => {
    const { cards, ask } = await sharedFleet()
    const answer = mockGroups(cards, [ask], [], "home", "everyone")
    const html = renderToStaticMarkup(<Glance answer={answer} asks={[ask]} cards={cards}
      folded={false} onFold={noop} onOpenAgent={noop} onOpenAsk={noop} />)
    const needs = html.slice(html.indexOf('aria-label="Needs you"'))
    expect(needs).toContain(">agent-same</span>")
    expect(needs).toContain(MARK)
  })

  it("in the asks list", async () => {
    const { cards, ask } = await sharedFleet()
    const html = renderToStaticMarkup(<Asks api={mockApi()} asks={[ask]} cards={cards} onThread={noop} />)
    expect(html).toContain(">agent-same</span>")
    expect(html).toContain(MARK)
  })

  it("in the detail's ask block", async () => {
    const { card, ask } = await sharedFleet()
    const html = renderToStaticMarkup(<Detail api={mockApi()} card={card} ask={ask} asks={[ask]}
      onClose={noop} onFail={noop} />)
    const block = html.slice(html.indexOf('aria-label="The ask"'))
    expect(block).toContain(">agent-same</span>")
    expect(block).toContain(MARK)
  })

  it("is found by the search", async () => {
    const { card } = await sharedFleet()
    expect(matches(card, "agent-same")).toBe(true)
  })
})
