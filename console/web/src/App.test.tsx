import { renderToStaticMarkup } from "react-dom/server"
import { describe, expect, it } from "vitest"
import { App } from "./App"
import { mockApi } from "./api/mock"
import { asPlace } from "./nav"
import { busWord, plainText } from "./ui/Detail"
import { TopBar } from "./ui/TopBar"

describe("the shell", () => {
  it("renders the top bar, the agents list as the home and the footer", () => {
    const html = renderToStaticMarkup(<App api={mockApi()} />)
    expect(html).toContain('class="topbar"')
    expect(html).toContain('aria-label="Views"')
    expect(html).toMatch(/aria-current="page"><span class="topbar__label"><span>Agents<\/span>/)
    expect(html).toContain("<h1>Agents</h1>")
    expect(html).toContain('class="footer"')
    expect(html).toContain("version n/a · commit n/a")  // no build before /v1/me answers
    expect(html).not.toContain("side__")  // the sidebar is gone
  })

  it("logs out with a POST form, never a link (RT-6.43 step 9)", () => {
    const html = renderToStaticMarkup(
      <TopBar view="agents" setView={() => {}} openAsks={2} forMe={1} conn="live"
        operator="dembaca" theme="light" onToggleTheme={() => {}} />)
    expect(html).toMatch(/<form method="post" action="\/logout"[^>]*><button type="submit"/)
    expect(html).not.toMatch(/<a [^>]*logout/)
    expect(html).toContain("topbar__badge--for-me")
  })
})

describe("the history's places", () => {
  it("reads only places it wrote", () => {
    expect(asPlace({ view: "graph" })).toEqual({ view: "graph" })
    expect(asPlace({ view: "agents", agent: "agent-a", ask: "p1" }))
      .toEqual({ view: "agents", agent: "agent-a", ask: "p1" })
    expect(asPlace({ view: "kanban" })).toBeNull()
    expect(asPlace(null)).toBeNull()
    expect(asPlace("agents")).toBeNull()
  })
})

describe("a post's bus word", () => {
  it("is one of the bus's words, any other capital word stays text", () => {
    expect(busWord("RESULT: merged")).toEqual({ word: "RESULT", rest: "merged" })
    expect(busWord("HANDOVER to agent-b")).toEqual({ word: "HANDOVER", rest: "to agent-b" })
    expect(busWord("NATS is down")).toEqual({ word: null, rest: "NATS is down" })
    expect(busWord("PR #182 merged")).toEqual({ word: null, rest: "PR #182 merged" })
  })

  it("is read in backticks too, as the agents write it", () => {
    expect(busWord("`HELLO` · agent-b online")).toEqual({ word: "HELLO", rest: "agent-b online" })
    expect(busWord("`RESULT`: done")).toEqual({ word: "RESULT", rest: "done" })
    expect(busWord("`NATS` · down")).toEqual({ word: null, rest: "`NATS` · down" })
    expect(busWord("`HELLO without its closing tick")).toEqual({ word: null, rest: "`HELLO without its closing tick" })
  })
})

describe("a post's text", () => {
  it("drops the markdown and keeps the words", () => {
    expect(plainText("**Fresh session** on `main`, see [PR 185](https://x/185)"))
      .toBe("Fresh session on main, see PR 185")
    expect(plainText("## Plan\n> quoted *note* and ~~old~~ new"))
      .toBe("Plan\nquoted note and old new")
    expect(plainText("agent_one and 2 * 3 stay")).toBe("agent_one and 2 * 3 stay")
  })

  it("stays text: markup in a post is never HTML", () => {
    expect(plainText("<img src=x onerror=alert(1)>")).toBe("<img src=x onerror=alert(1)>")
  })
})
