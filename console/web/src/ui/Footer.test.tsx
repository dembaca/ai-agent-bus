import { renderToStaticMarkup } from "react-dom/server"
import { describe, expect, it } from "vitest"
import { Footer, buildLabel, shortRevision } from "./Footer"

describe("the footer's version (#182)", () => {
  it("shows the release version and the short commit the server states", () => {
    const build = { version: "0.103.0", revision: "28d6475a0c3e9b1f2d4a6c8e0b2d4f6a8c0e2b4d" }
    expect(buildLabel(build)).toBe("version 0.103.0 · commit 28d6475")
    const html = renderToStaticMarkup(<Footer build={build} />)
    expect(html).toContain("version 0.103.0 · commit 28d6475")
    // the whole commit on hover
    expect(html).toContain('title="28d6475a0c3e9b1f2d4a6c8e0b2d4f6a8c0e2b4d"')
  })

  it("says n/a where the server has no build, never a number of its own", () => {
    expect(buildLabel({ version: null, revision: null })).toBe("version n/a · commit n/a")
    expect(buildLabel(null)).toBe("version n/a · commit n/a")
    expect(buildLabel({ version: "0.103.0", revision: null })).toBe("version 0.103.0 · commit n/a")
    expect(shortRevision("")).toBeNull()
  })
})
