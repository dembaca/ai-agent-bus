import { describe, expect, it } from "vitest"
import { sourceText } from "../sourceText"

const css = sourceText("ui/Detail.css")
const tsx = sourceText("ui/Detail.tsx")

describe("the agent drawer's task table at 390 px (#283)", () => {
  it("scrolls inside its container, not the page", () => {
    expect(css).toMatch(/\.detail__rows-wrap\s*\{[^}]*overflow-x:\s*auto/)
    expect(tsx).toMatch(/<div className="detail__rows-wrap">\s*<table className="detail__rows">/)
  })
})
