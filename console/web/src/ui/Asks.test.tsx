import { describe, expect, it } from "vitest"
import { renderToStaticMarkup } from "react-dom/server"
import { mockApi } from "../api/mock"
import type { Ask } from "../api/types"
import { answeredText, answersOf, Asks, NewBadge, Unfolded } from "./Asks"
import css from "./Asks.css?raw"
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

  it("answer 3: no context, recommendation or where caption, as the API carries none; long text wraps by css", () => {
    const html = unfolded(base)
    for (const caption of ["Context", "Recommendation", "Where"]) expect(html).not.toContain(caption)
    expect(css).toMatch(/\.asks__unfolded-main \{[^}]*max-width: 78ch;[^}]*overflow-wrap: anywhere/)
    expect(css).not.toMatch(/text-overflow|line-clamp/)
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
