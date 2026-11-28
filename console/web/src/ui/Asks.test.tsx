import { describe, expect, it } from "vitest"
import { renderToStaticMarkup } from "react-dom/server"
import { mockApi } from "../api/mock"
import type { Ask } from "../api/types"
import { answeredText, answersOf, Asks, NewBadge, Unfolded } from "./Asks"
import css from "./Asks.css?raw"
import { PostRow } from "./Detail"
import { ANSWER_DISABLED, ThreadDrawer } from "./ThreadDrawer"

const base: Ask = {
  id: "a1", by: "agent-x", displayName: "x", home: "dev", kind: "approval", human: "dembaca",
  need: "Ship it?", at: Date.now() - 60_000, end: "open", forMe: true, mayAnswer: true,
  agentGone: false, threadId: "a1", answers: [], chosen: null,
}

describe("Asks (#285)", () => {
  it("lists a row with chip, need, meta and a thread button, folded", () => {
    const html = renderToStaticMarkup(<Asks api={mockApi()} asks={[base]} onThread={() => {}} />)
    expect(html).toContain("for you")
    expect(html).toContain("Ship it?")
    expect(html).toContain('aria-expanded="false"')
    expect(html).toContain("Thread, n/a posts, 0 new")
    expect(html).not.toContain("Chat with agent")
  })

  it("draws the red count only for new posts", () => {
    expect(renderToStaticMarkup(<NewBadge n={0} />)).toBe("")
    expect(renderToStaticMarkup(<NewBadge n={3} />)).toContain(">3<")
  })

  it("disables the reply field and says why", () => {
    const html = renderToStaticMarkup(
      <ThreadDrawer api={mockApi()} ask={base} onClose={() => {}} onRead={() => {}} onFail={() => {}} />)
    expect(html).toContain("<textarea disabled")
    expect(html).toContain(ANSWER_DISABLED)
    expect(html).toContain("<button type=\"submit\" disabled")
  })

  const unfolded = (ask: Ask) =>
    renderToStaticMarkup(<Unfolded ask={ask} fresh={0} onThread={() => {}} />)
  const three = ["Ship it", "Hold until Monday", "Ask the owner"]

  it("answer 1: shows exactly the ask's own answers in order; an approval with none reads go / no-go", () => {
    expect(answersOf({ ...base, answers: ["Yes, merge", "Not now"] })).toEqual(["Yes, merge", "Not now"])
    expect(answersOf(base)).toEqual(["go", "no-go"])
    expect(answersOf({ ...base, kind: "question" })).toEqual([])  // never invented
    const html = unfolded({ ...base, kind: "question" })
    expect(html).not.toContain("<button type=\"button\" disabled")
    const ordered = unfolded({ ...base, answers: three })
    expect(ordered.indexOf("Ship it")).toBeLessThan(ordered.indexOf("Hold until Monday"))
    expect(ordered.indexOf("Hold until Monday")).toBeLessThan(ordered.indexOf("Ask the owner"))
  })

  it("answer 2: more than two answers get key caps A to D, exactly two stand side by side without letters", () => {
    const stack = unfolded({ ...base, answers: [...three, "Fourth way"] })
    expect(stack).toContain("asks__answers--stack")
    for (const key of ["A", "B", "C", "D"]) expect(stack).toContain(`>${key}</kbd>`)
    expect(stack).toContain("Fourth way")
    const side = unfolded({ ...base, answers: ["Yes", "No"] })
    expect(side).toContain("asks__answers--side")
    expect(side).not.toContain("<kbd")
    expect(unfolded(base)).not.toContain("<kbd")
  })

  const card = {
    context: "Two PRs touch the same file.\nMine is smaller.", recommendation: "Merge mine first.",
    where: "PR 118", decision: "Merge PR 118 into main",
  }

  it("answer 3: the card's Context, Recommendation and Where, each caption above its text; an empty one left out", () => {
    const html = unfolded({ ...base, ...card })
    const at = (s: string) => html.indexOf(s)
    expect(at(">Context<")).toBeLessThan(at("Two PRs touch the same file."))
    expect(at("Two PRs touch the same file.")).toBeLessThan(at(">Recommendation<"))
    expect(at(">Recommendation<")).toBeLessThan(at("Merge mine first."))
    expect(at("Merge mine first.")).toBeLessThan(at(">Where<"))
    expect(at(">Where<")).toBeLessThan(at("PR 118<"))
    expect(html).toContain("Two PRs touch the same file.\nMine is smaller.")  // line breaks kept, as text
    const some = unfolded({ ...base, ...card, recommendation: "", where: "  " })
    expect(some).toContain(">Context<")
    for (const caption of [">Recommendation<", ">Where<"]) expect(some).not.toContain(caption)
    for (const caption of [">Context<", ">Recommendation<", ">Where<"]) expect(unfolded(base)).not.toContain(caption)
  })

  it("answer 3: long text wraps at about 78 characters, never cut, never scrolled inside", () => {
    expect(css).toMatch(/\.asks__unfolded-main \{[^}]*max-width: 78ch;[^}]*overflow-wrap: anywhere/)
    expect(css).toMatch(/\.asks__text \{ white-space: pre-wrap; \}/)
    expect(css).not.toMatch(/text-overflow|line-clamp|overflow(-y)?: (auto|scroll|hidden)/)
    const long = "x ".repeat(600).trim()
    expect(unfolded({ ...base, context: long })).toContain(long)
  })

  it("the answer box counts as approval of the decision's wording, never of the title", () => {
    const html = unfolded({ ...base, ...card })
    expect(html).toMatch(/This counts as your approval of: <strong[^>]*>Merge PR 118 into main<\/strong>/)
    expect(html).not.toMatch(/approval of: <strong[^>]*>Ship it\?/)
    const none = unfolded(base)
    expect(none).not.toContain("This counts as your approval of:")
    expect(none).toContain("states no decision of its own")
  })

  it("the card is plain text, never markup", () => {
    const html = unfolded({ ...base, context: "<img src=x onerror=alert(1)> **bold**" })
    expect(html).not.toContain("<img")
    expect(html).toContain("&lt;img src=x onerror=alert(1)&gt; **bold**")
  })

  it("answer 5: the answered state replaces the state in its column", () => {
    expect(answeredText({ ...base, end: "answered", chosen: "go" })).toBe("answered: go")
    expect(answeredText({ ...base, end: "answered", answers: three, chosen: "Hold until Monday" })).toBe("answered: B")
    expect(answeredText({ ...base, end: "answered", answers: ["Yes", "No"], chosen: "No" })).toBe("answered: No")
    expect(answeredText({ ...base, end: "answered", chosen: null })).toBe("answered")  // a reply: no word invented
    const row = renderToStaticMarkup(
      <Asks api={mockApi()} asks={[{ ...base, end: "answered", chosen: "go" }]} onThread={() => {}} />)
    expect(row).toContain("answered: go")
    expect(row).not.toContain("chip--open")
  })

  it("answer 6: the narrow window keeps the chip, on a line above the need", () => {
    const narrow = css.slice(css.indexOf("@media (max-width: 799px)"))
    expect(narrow).toMatch(/\.asks__chip \{ grid-column: 1 \/ -1; \}/)
    expect(narrow).not.toMatch(/\.asks__chip[^{]*\{ display: none/)
    expect(narrow).not.toMatch(/\.asks__chip, \.asks__age/)
  })

  it("a reader the API gives the title only sees no caption and an approval's go / no-go as plain text", () => {
    const html = unfolded({ ...base, forMe: false, mayAnswer: false })
    expect(html).not.toContain("asks__box")
    for (const caption of [">Context<", ">Recommendation<", ">Where<"]) expect(html).not.toContain(caption)
    expect(html).toContain("asks__plain")
    expect(html).toContain("no-go")
  })

  it("answer 7: not the operator's to answer, no answer box and the answers as plain text", () => {
    const html = unfolded({ ...base, forMe: false, mayAnswer: false, answers: three })
    expect(html).not.toContain("asks__box")
    expect(html).not.toContain("<button type=\"button\" disabled")
    expect(html).toContain("asks__plain")
    expect(html).toContain("Hold until Monday")
    expect(html).toContain("you read it")
    expect(unfolded(base)).toContain("asks__box")
  })

  it("answer 8: the thread button is a labelled inline svg and the number alone", () => {
    const html = renderToStaticMarkup(<Asks api={mockApi()} asks={[base]} onThread={() => {}} />)
    expect(html).toMatch(/<svg[^>]*role="img"[^>]*aria-label="Thread"/)
    expect(html).not.toContain("posts</")
    expect(html).not.toMatch(/[\u{1F300}-\u{1FAFF}]/u)
  })

  it("keeps every write disabled: buttons and reply field", () => {
    const html = unfolded({ ...base, answers: three })
    expect(html.match(/<button type="button" disabled/g)?.length).toBe(3)
  })
})

describe("the mock's threads match its asks (#285, owner review)", () => {
  it("every mock ask's thread id has sample posts, the root first", async () => {
    const api = mockApi()
    for (const ask of await api.asks()) {
      const page = await api.thread(ask.threadId)
      expect(page.posts.length, ask.threadId).toBeGreaterThan(0)
      expect(page.posts[0].id).toBe(ask.threadId)
      expect(page.posts[0].rootId).toBeNull()
    }
  })

  it("the drawer's rows for the first mock ask show its posts", async () => {
    const api = mockApi()
    const [first] = await api.asks()
    const page = await api.thread(first.threadId)
    const html = renderToStaticMarkup(<>{page.posts.map((p) => <PostRow key={p.id} post={p} />)}</>)
    expect(html).toContain("documented")
    expect(html).toContain(page.posts[0].author)
  })
})
