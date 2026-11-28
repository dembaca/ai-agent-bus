import { describe, expect, it } from "vitest"
import { readdirSync } from "node:fs"
import { sourceText } from "../sourceText"

const strip = (css: string) => css.replace(/\/\*[\s\S]*?\*\//g, "")
const statsCss = strip(sourceText("ui/Stats.css"))

/** Every class a stylesheet's selectors name. */
function classesOf(css: string): Set<string> {
  const out = new Set<string>()
  for (const m of css.matchAll(/\.([a-zA-Z][\w-]*)/g)) out.add(m[1])
  return out
}

/** The selectors of a stylesheet (the text before each `{` that is not an at-rule). */
function selectors(css: string): string[] {
  const out: string[] = []
  for (const m of css.matchAll(/([^{}]+)\{/g)) {
    const head = m[1].trim()
    if (head.startsWith("@")) continue
    for (const s of head.split(",")) out.push(s.trim())
  }
  return out
}

describe("Stats.css stays on the Stats page (#284, owner review)", () => {
  it("scopes every selector under the stats- prefix", () => {
    const sel = selectors(statsCss)
    expect(sel.length).toBeGreaterThan(20)
    const bad = sel.filter((s) => !/^\.stats-[\w-]+/.test(s))
    expect(bad).toEqual([])
    // and no class in a selector lacks the prefix, e.g. `.stats-asks .chip`
    expect([...classesOf(statsCss)].filter((c) => !c.startsWith("stats-"))).toEqual([])
  })

  it("defines no class another stylesheet already defines", () => {
    const others = new Set<string>()
    const files = [...readdirSync(new URL("..", import.meta.url).pathname).filter((f) => f.endsWith(".css")).map((f) => f),
      ...readdirSync(new URL(".", import.meta.url).pathname).filter((f) => f.endsWith(".css") && f !== "Stats.css").map((f) => `ui/${f}`)]
    expect(files.length).toBeGreaterThan(3)
    for (const f of files) for (const c of classesOf(strip(sourceText(f)))) others.add(c)
    expect(others.has("chip")).toBe(true) // the scan really reads the app-wide stylesheet
    const clash = [...classesOf(statsCss)].filter((c) => others.has(c))
    expect(clash).toEqual([])
    expect(classesOf(statsCss).has("chip")).toBe(false)
    expect(classesOf(statsCss).has("asks")).toBe(false)
  })

  it("fits the asks table into 390 px: stacked under 640 px, scrolling inside its container", () => {
    expect(statsCss).toMatch(/\.stats-asks-wrap\s*\{[^}]*overflow-x:\s*auto/)
    const narrow = statsCss.slice(statsCss.indexOf("@media (max-width: 640px)"))
    expect(narrow).toMatch(/\.stats-asks td\s*\{[^}]*display:\s*flex/)
    expect(narrow).toMatch(/\.stats-asks td::before\s*\{[^}]*attr\(data-label\)/)
  })
})
