import { describe, expect, it } from "vitest"
import { renderToStaticMarkup } from "react-dom/server"
import { mockApi } from "../api/mock"
import type { Ask } from "../api/types"
import { Asks, NewBadge } from "./Asks"
import { ANSWER_DISABLED, ThreadDrawer } from "./ThreadDrawer"

const base: Ask = {
  id: "a1", by: "agent-x", displayName: "x", home: "dev", kind: "approval", human: "dembaca",
  need: "Ship it?", at: Date.now() - 60_000, end: "open", forMe: true, mayAnswer: true,
  agentGone: false, threadId: "a1",
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
})
