import { renderToStaticMarkup } from "react-dom/server"
import { describe, expect, it } from "vitest"
import { App } from "./App"
import { mockApi } from "./api/mock"
import { asPlace } from "./nav"
import { busWord, plainText } from "./ui/Detail"
import { TopBar } from "./ui/TopBar"

describe("the shell", () => {
  it("renders the top bar, the Overview first and as the default, and the footer (#200)", () => {
    const html = renderToStaticMarkup(<App api={mockApi()} />)
    expect(html).toContain('class="topbar"')
    expect(html).toContain('aria-label="Views"')
    expect(html).toMatch(/aria-current="page"><span class="topbar__label"><span>Overview<\/span>/)
    // the Overview is the first tab
    expect(html.indexOf(">Overview<")).toBeLessThan(html.indexOf(">Agents<"))
    expect(html).toContain("<h1>Overview</h1>")
    expect(html).toContain('class="solo"')  // no detail open: no split
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
    expect(asPlace({ view: "overview" })).toEqual({ view: "overview" })
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

  it("never takes a hyphen as the separator", () => {
    for (const text of ["NO-GO for the merge", "TASK-123 fix", "RESULT -5"]) {
      expect(busWord(text)).toEqual({ word: null, rest: text })
    }
    expect(busWord("`NO` · go")).toEqual({ word: "NO", rest: "go" })
    expect(busWord("NO – reason")).toEqual({ word: "NO", rest: "reason" })
  })
})

describe("a post's text", () => {
  it("drops the markdown and keeps the words", () => {
    expect(plainText("**Fresh session** on `main`, see [PR 185](https://x/185)"))
      .toBe("Fresh session on main, see PR 185 (https://x/185)")
    expect(plainText("## Plan\n> quoted *note* and ~~old~~ new"))
      .toBe("Plan\nquoted note and old new")
    expect(plainText("agent_one and 2 * 3 stay")).toBe("agent_one and 2 * 3 stay")
  })

  it("keeps every character that is not real markdown", () => {
    for (const text of [
      "__init__.py and __main__",
      "*.py and *.ts files",
      "arr[0](x)",
      ">= 3 agents",
      "# of agents: 3",
      "a_b_c and snake_case_name",
    ]) expect(plainText(text)).toBe(text)
    expect(plainText("def f(*args, **kwargs) and **more**")).toBe("def f(*args, **kwargs) and more")
  })

  it("keeps a code span's and a fence's content as is", () => {
    expect(plainText("`a ** b ** c`")).toBe("a ** b ** c")
    expect(plainText("run `**x**` then **y**")).toBe("run **x** then y")
    expect(plainText("```py\ndef f(*a, **k): [0](x)\n```")).toBe("def f(*a, **k): [0](x)\n")
  })

  it("is fast on a run of brackets", () => {
    const started = performance.now()
    expect(plainText("[".repeat(16_000))).toBe("[".repeat(16_000))
    expect(plainText("[x](".repeat(4_000))).toBe("[x](".repeat(4_000))
    expect(performance.now() - started).toBeLessThan(500)
  })

  it("stays text: markup in a post is never HTML", () => {
    expect(plainText("<img src=x onerror=alert(1)>")).toBe("<img src=x onerror=alert(1)>")
  })
})
