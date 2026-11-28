import { renderToStaticMarkup } from "react-dom/server"
import { describe, expect, it } from "vitest"
import type { AgentCard, Ask, GroupsAnswer } from "../api/types"
import { mockApi, mockGroups } from "../api/mock"
import { Glance } from "./Glance"

const noop = () => {}

async function fleet(): Promise<{ cards: AgentCard[]; asks: Ask[] }> {
  const api = mockApi()
  let cards: AgentCard[] = []
  const stop = api.watchFleet((e) => { if (e.type === "snapshot") cards = e.cards })
  await new Promise((r) => setTimeout(r, 0))
  stop()
  return { cards, asks: await api.asks() }
}

async function panel(folded: boolean, answerOf = true): Promise<string> {
  const { cards, asks } = await fleet()
  const answer = answerOf ? mockGroups(cards, asks, await mockApi().agentStats(), "home", "mine") : null
  return renderToStaticMarkup(<Glance answer={answer} asks={asks} cards={cards} folded={folded}
    onFold={noop} onOpenAgent={noop} onOpenAsk={noop} />)
}

describe("the panel At a glance (#283)", () => {
  it("shows four tiles with the operator's number large and the flock's small", async () => {
    const html = await panel(false)
    for (const label of ["Reachable", "Working now", "For you", "You may answer"]) {
      expect(html).toContain(`>${label}<`)
    }
    expect((html.match(/class="tile__mine"/g) ?? []).length).toBe(4)
    expect((html.match(/>flock \d+</g) ?? []).length).toBe(4)
  })

  it("counts the asks over the whole view and splits only Reachable and Working now", async () => {
    const { cards, asks } = await fleet()
    const real = mockGroups(cards, asks, [], "home", "mine")
    const answer: GroupsAnswer = { ...real, glance: { ...real.glance,
      asks: { forYou: 3, mayAnswer: 2, open: 9 } } }
    const html = renderToStaticMarkup(<Glance answer={answer} asks={asks} cards={cards} folded={false}
      onFold={noop} onOpenAgent={noop} onOpenAsk={noop} />)
    const tile = (label: string) => {
      const at = html.indexOf(`>${label}<`)
      return html.slice(at, html.indexOf("</div>", at))
    }
    expect(tile("For you")).toContain('title="yours">3<')
    expect(tile("For you")).toContain(">flock 9<")
    expect(tile("You may answer")).toContain('title="yours">2<')
    expect(tile("You may answer")).toContain(">flock 9<")
    expect(tile("Reachable")).toContain(`>flock ${real.glance.flock.reachable}<`)
    expect(tile("Working now")).toContain(`>flock ${real.glance.flock.working}<`)
    // the folded rail's "for you" is the same number
    const rail = renderToStaticMarkup(<Glance answer={answer} asks={asks} cards={cards} folded
      onFold={noop} onOpenAgent={noop} onOpenAsk={noop} />)
    expect(rail).toContain('class="rail__n">3<')
  })

  it("lights only 'For you', and only above zero", async () => {
    expect(await panel(false)).toMatch(/tile tile--hot"[^>]*><span class="tile__label">For you/)
    const { cards, asks } = await fleet()
    const none = mockGroups(cards, asks.map((a) => ({ ...a, forMe: false })), [], "home", "mine")
    const html = renderToStaticMarkup(<Glance answer={none} asks={[]} cards={cards} folded={false}
      onFold={noop} onOpenAgent={noop} onOpenAsk={noop} />)
    expect(html).not.toContain("tile--hot")
    expect(html).toContain("Nothing waits on you.")
  })

  it("lists the asks for the operator and those it may answer, and the flock by status", async () => {
    const html = await panel(false)
    const needs = html.slice(html.indexOf('aria-label="Needs you"'), html.indexOf('aria-label="Flock by status"'))
    expect(needs).toContain("for you")
    expect(html).toContain('aria-label="Flock by status"')
    expect(html).toContain('class="statebar"')
    expect(html).toContain("Listening")
  })

  it("folds to a rail with three numbers: asks for you, reachable, working", async () => {
    const html = await panel(true)
    expect(html).toContain("glance--rail")
    expect(html).not.toContain("tile__mine")
    for (const label of ["for you", "reachable", "working"]) expect(html).toContain(`>${label}<`)
    expect((html.match(/class="rail__n"/g) ?? []).length).toBe(3)
  })

  it("shows a dash, never 0, before the first answer", async () => {
    const html = await panel(false, false)
    expect(html).toContain(">–<")
    expect(html).toContain("reading the flock…")
  })
})
