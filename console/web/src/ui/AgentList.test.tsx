import { renderToStaticMarkup } from "react-dom/server"
import { describe, expect, it } from "vitest"
import type { AgentCard, Ask, GroupBy, GroupsAnswer, Scope } from "../api/types"
import { mockApi, mockGroups } from "../api/mock"
import { DEFAULT_PREFS, type Prefs } from "../prefs"
import { AgentList, EMPTY, foldPatch, matches } from "./AgentList"
import { NO_LAST_POST, NO_STATS_ROW, STATS_ROWS_NOTE, statsByAgent } from "./agents"
import { Detail } from "./Detail"

// The pages render from the API's own shapes: the mock's first snapshot, its
// asks and its groups, nothing else.
async function fleet(): Promise<{ cards: AgentCard[]; asks: Ask[] }> {
  const api = mockApi()
  let cards: AgentCard[] = []
  const stop = api.watchFleet((e) => { if (e.type === "snapshot") cards = e.cards })
  await new Promise((r) => setTimeout(r, 0))
  stop()
  return { cards, asks: await api.asks() }
}

const noop = () => {}

async function list(opts: {
  cards?: AgentCard[]; asks?: Ask[]; by?: GroupBy; scope?: Scope; prefs?: Partial<Prefs>
  stats?: boolean; query?: string; panelFolded?: boolean; selected?: string
  patch?: (answer: GroupsAnswer) => GroupsAnswer
} = {}): Promise<string> {
  const base = await fleet()
  const cards = opts.cards ?? base.cards
  const asks = opts.asks ?? base.asks
  const by = opts.by ?? "home"
  const scope = opts.scope ?? "everyone"
  const rows = await mockApi().agentStats()
  const answer = (opts.patch ?? ((a) => a))(mockGroups(cards, asks, rows, by, scope))
  return renderToStaticMarkup(
    <AgentList answer={answer} cards={cards} asks={asks}
      stats={opts.stats === false ? null : statsByAgent(rows)}
      prefs={{ ...DEFAULT_PREFS, by, scope, ...opts.prefs }} query={opts.query ?? ""}
      onQuery={noop} panelFolded={opts.panelFolded} selected={opts.selected}
      onOpenAgent={noop} />)
}

describe("the agents list (#283)", () => {
  it("shows the stated name, a shared one with its mark, the bus id in the title (#199)", async () => {
    const { cards } = await fleet()
    const shared: AgentCard = { ...cards[0], name: "oudzj4bhpl62iahctur5s5sf4",
      displayName: "oudzj4bhpl62iahctur5s5sf4", statedName: "agent-same", nameShared: true }
    const html = await list({ cards: [shared], asks: [], prefs: { folded: { [`home:${shared.home}`]: false } } })
    expect(html).toContain(">agent-same</button>")
    expect(html).toContain("shared name")
    expect(html).toContain('title="oudzj4bhpl62iahctur5s5sf4 · another agent states this name too: not confirmed"')
  })

  it("searches the home, the agent and the person", async () => {
    const { cards } = await fleet()
    const home = cards.find((c) => c.guests.length > 0)!
    expect(matches(home, home.home.toLowerCase())).toBe(true)
    expect(matches(home, home.displayName)).toBe(true)
    expect(matches(home, home.for.toUpperCase())).toBe(true)   // the person, any case
    expect(matches(home, `${home.for} nothing-like-this`)).toBe(false)  // every word must hit
    const html = await list({ query: "carol" })
    expect(html).toContain("agent-mac-ai-agent-831e38")   // carol's agent stays
    expect(html.match(/class="agents__row/g)?.length).toBe(cards.filter((c) => matches(c, "carol")).length)
  })

  it("draws one row per agent in three columns, a group per home with the server's numbers", async () => {
    const { cards, asks } = await fleet()
    const html = await list({ scope: "everyone", prefs: { folded: Object.fromEntries(
      [...new Set(cards.map((c) => `home:${c.home}`))].map((k) => [k, false])) } })
    expect(html.match(/class="agents__row/g)?.length).toBe(cards.length)
    expect(html.match(/class="group__head"/g)?.length).toBe(new Set(cards.map((c) => c.home)).size)
    const dev = cards.filter((c) => c.home === "~ai-agent-bus-dev")
    expect(html).toContain(`${dev.filter((c) => c.state !== "offline").length} of ${dev.length} reachable`)
    expect(html).toContain("for you")  // the group with the operator's ask
    // the row's three columns
    expect((html.match(/class="agents__who"/g) ?? []).length).toBe(cards.length)
    expect((html.match(/class="agents__status"/g) ?? []).length).toBe(cards.length)
    expect((html.match(/class="agents__work"/g) ?? []).length).toBe(cards.length)
    expect(html).not.toContain("agents__today")  // no fourth column with the panel open
    expect(asks.length).toBeGreaterThan(0)
  })

  it("orders the groups with an ask for the operator first", async () => {
    const { cards, asks } = await fleet()
    const moved = cards.map((c, i) => (i === 0 ? c : { ...c, home: "~aaa-other" }))
    const asked = asks.map((a) => ({ ...a, by: moved[0].name }))
    const html = await list({ cards: moved, asks: asked, scope: "everyone",
      prefs: { folded: { "home:~aaa-other": false, [`home:${moved[0].home}`]: false } } })
    expect(html.indexOf(moved[0].home)).toBeLessThan(html.indexOf("~aaa-other"))
  })

  it("does not repeat what the list is grouped by in the row", async () => {
    const { cards } = await fleet()
    const open = (by: GroupBy) => Object.fromEntries(
      cards.flatMap((c) => [[`${by}:${c.home}`, false], [`${by}:${c.project}`, false],
        [`${by}:${c.for}`, false], [`${by}:working`, false], [`${by}:listening`, false],
        [`${by}:idle`, false], [`${by}:offline`, false]]))
    const byStatus = await list({ by: "status", prefs: { folded: open("status") } })
    expect(byStatus).not.toContain('class="pill pill--working"')
    const byHome = await list({ by: "home", prefs: { folded: open("home") } })
    expect(byHome).toContain('class="pill pill--working"')
    const byRepo = await list({ by: "repo", prefs: { folded: open("repo") } })
    const rowsOfRepo = byRepo.slice(byRepo.indexOf('class="rows"'))
    expect(rowsOfRepo).not.toContain("dembaca/ai-agent-bus</span>")
    expect(byHome).toContain("dembaca/ai-agent-bus</span>")
  })

  it("starts the groups folded in Everyone, except those that need the operator", async () => {
    const html = await list({ scope: "everyone" })
    expect(html).toContain('aria-expanded="true"')            // the operator's ask is in one group
    expect(html).toContain('aria-expanded="false"')           // the others stay folded
    const mine = await list({ scope: "mine" })
    expect(mine).not.toContain('aria-expanded="false"')       // Mine starts open
    // what the operator picked wins over the default
    const picked = await list({ scope: "everyone", prefs: { folded: { "home:~ai-agent-bus-dev": true } } })
    expect(picked).not.toContain('aria-expanded="true"')
  })

  it("folds groups with nobody reachable and no open ask into one row in Everyone", async () => {
    const { cards } = await fleet()
    const gone = cards.map((c, i) => (i < 2 ? c : { ...c, state: "offline" as const, home: `~quiet-${i}` }))
    const html = await list({ cards: gone, asks: [], scope: "everyone" })
    expect(html).toMatch(/\d+ quiet homes/)
    expect(html).not.toContain("~quiet-2")
    expect(html).toContain('class="agents__quiet" aria-expanded="false"')
  })

  it("names the quiet row by the grouping, only in Everyone and only by home, repo or owner", async () => {
    const { cards } = await fleet()
    const gone = cards.map((c, i) => (i < 2 ? c : { ...c, state: "offline" as const,
      home: `~quiet-${i}`, project: `quiet/repo-${i}`, for: `quiet-owner-${i}` }))
    for (const [by, noun] of [["home", "homes"], ["repo", "repos"], ["owner", "owners"]] as const) {
      const html = await list({ cards: gone, asks: [], by, scope: "everyone" })
      expect(html).toMatch(new RegExp(`\\d+ quiet ${noun}`))
    }
    for (const scope of ["mine", "trusting"] as const) {
      expect(await list({ cards: gone, asks: [], by: "home", scope })).not.toContain("quiet homes")
    }
    for (const by of ["status", "none"] as const) {
      expect(await list({ cards: gone, asks: [], by, scope: "everyone" })).not.toMatch(/\d+ quiet /)
    }
    // a group the person unfolded or folded herself is no quiet one
    const picked = await list({ cards: gone, asks: [], by: "home", scope: "everyone",
      prefs: { folded: { "home:~quiet-2": false } } })
    expect(picked).toContain("~quiet-2")
  })

  it("shows the group's tokens, and 'n of m agents' only when the sum leaves agents out", async () => {
    const whole = await list({ scope: "everyone", by: "home", patch: (a) => ({ ...a,
      groups: a.groups.map((g) => ({ ...g, tokens: 92_100, tokensAgents: g.total })) }) })
    expect(whole).toContain("92.1k tokens today")
    expect(whole).not.toContain("group__covers")
    const partial = await list({ scope: "everyone", by: "home", patch: (a) => ({ ...a,
      groups: a.groups.map((g) => ({ ...g, tokens: 92_100, tokensAgents: g.total - 1 })) }) })
    expect(partial).toContain("92.1k tokens today")
    expect(partial).toMatch(/ · \d+ of \d+ agents/)
    expect(partial).toContain("Tokens of the last 24 hours of the agents you may see numbers of")
    const none = await list({ scope: "everyone", by: "home", patch: (a) => ({ ...a,
      groups: a.groups.map((g) => ({ ...g, tokens: null, tokensAgents: 0 })) }) })
    expect(none).not.toContain("tokens today")
  })

  it("shows a search's hits unfolded and writes nothing to the remembered folds", async () => {
    const { cards } = await fleet()
    const target = cards.find((c) => c.for === "carol")!
    // Everyone folds every group without an ask for the operator
    const quietly = await list({ scope: "everyone", prefs: { folded: { [`home:${target.home}`]: true } } })
    expect(quietly).not.toContain(target.name)
    const searched = await list({ scope: "everyone", query: "carol",
      prefs: { folded: { [`home:${target.home}`]: true } } })
    expect(searched).toContain(target.name)
    expect(searched).not.toContain('aria-expanded="false"')
    // a click on a header while searching changes nothing; without a search it folds
    const prefs = { ...DEFAULT_PREFS, scope: "everyone" as const, folded: { "home:~a": true } }
    expect(foldPatch(prefs, "~a", false, true)).toBeNull()
    expect(foldPatch(prefs, "~a", false, false)).toEqual({ folded: { "home:~a": false } })
  })

  it("gives each row a fourth column with the panel folded (posts, tool calls, owner)", async () => {
    const html = await list({ scope: "mine", panelFolded: true })
    expect(html).toContain("agents__row--wide")
    expect(html).toContain("12 posts · 61 tool calls")
    expect(html).toContain('title="the responsible human">dembaca<')
  })

  it("marks the open agent and shows 'waits on you' from the asks", async () => {
    const { cards, asks } = await fleet()
    const asker = asks.find((a) => a.forMe)!.by
    const html = await list({ scope: "everyone", selected: asker })
    expect(html.match(/agents__row--selected/g)?.length).toBe(1)
    expect(html).toContain('aria-current="true"')
    expect(html).toContain(">waits on you</span>")
    expect(cards.some((c) => c.name === asker)).toBe(true)
  })

  it("leaves out what the API does not carry: no context, repo, branch or PR, no invented value", async () => {
    const { cards } = await fleet()
    const bare: AgentCard = { ...cards[0], ctx: null, project: "", branch: null, pr: null,
      lastPostAt: null, task: "a long task that the row cuts to one line" }
    const html = await list({ cards: [bare], asks: [], stats: false, scope: "everyone",
      prefs: { folded: { [`home:${bare.home}`]: false } } })
    expect(html).not.toContain("ctx__track")
    expect(html).not.toContain("agents__where")
    expect(html).not.toContain("chip--pr")
    expect(html).not.toContain("n/a")
    expect(html).toContain('title="a long task that the row cuts to one line"')
    expect(html).toContain(`<span title="${NO_LAST_POST}">last post ${EMPTY}</span>`)
  })

  it("shows the context bar, the tokens of today and the PR when present", async () => {
    const html = await list({ scope: "mine" })
    expect(html).toContain("ctx__track")
    expect(html).toContain("92.1k today")
    expect(html).toContain('<span class="chip chip--pr">#84</span>')
  })

  it("says plainly where the per-agent numbers come from, never as silence (#201)", async () => {
    const html = await list({})
    expect(html).toContain(NO_STATS_ROW)
    expect(html).toContain(STATS_ROWS_NOTE)
    expect(html).toContain(NO_LAST_POST)
  })
})

describe("the detail", () => {
  it("renders an agent's detail with its close button from the same data", async () => {
    const { cards, asks } = await fleet()
    const card = cards[0]
    const detail = renderToStaticMarkup(
      <Detail api={mockApi()} card={card} ask={null} asks={asks} onClose={noop} onFail={noop} />)
    expect(detail).toContain(card.name)
    expect(detail).toContain("Current task")
    expect(detail).toContain('aria-label="Close the detail"')
  })

  it("shows the detail's Today block, or says plainly why there is none (#201)", async () => {
    const { cards, asks } = await fleet()
    const stats = statsByAgent(await mockApi().agentStats())
    const detail = (card: AgentCard, s: typeof stats) => renderToStaticMarkup(
      <Detail api={mockApi()} card={card} ask={null} asks={asks} stats={s} onClose={noop} onFail={noop} />)
    const own = cards.find((c) => c.name === "agent-mac-ai-agent-bb8f09")!
    const today = detail(own, stats)
    for (const fact of ["<dt>tokens</dt><dd>92.1k</dd>", "<dt>cache read</dt><dd>90k</dd>",
      "<dt>cache write 5 m</dt><dd>n/a</dd>", "<dt>posts</dt><dd>12</dd>",
      "<dt>wakes</dt><dd>9</dd>", "<dt>tool calls</dt><dd>61</dd>"]) {
      expect(today).toContain(fact)
    }
    const quiet = detail(cards.find((c) => c.name === "agent-mac-ai-agent-164534")!, stats)
    expect(quiet).toContain("<dt>tokens</dt><dd>n/a</dd>")
    expect(quiet).toContain("<dt>posts</dt><dd>21</dd>")
    const other = cards.find((c) => !stats!.has(c.name))!
    expect(detail(other, stats)).toContain(`${NO_STATS_ROW} ${STATS_ROWS_NOTE}`)
    expect(detail(other, null)).toContain("Not read yet.")
  })

  it("says plainly why a card shows no last post, never as silence (#201)", async () => {
    const { cards, asks } = await fleet()
    const carols = cards.find((c) => c.for === "carol")!  // the mock's agent of another owner
    expect(carols.lastPostAt).toBeNull()
    const detail = renderToStaticMarkup(
      <Detail api={mockApi()} card={carols} ask={null} asks={asks} onClose={noop} onFail={noop} />)
    expect(detail).toContain(`<dt>last post</dt><dd><span class="muted small">${NO_LAST_POST}</span></dd>`)
    expect(detail).not.toContain("silent")
  })
})
