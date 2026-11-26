import { renderToStaticMarkup } from "react-dom/server"
import { describe, expect, it } from "vitest"
import {
  THEME_KEY, otherTheme, resolveTheme, storeTheme, storedTheme, toggleLabel,
} from "./theme"
import { ThemeToggle } from "./ui/TopBar"

/** A Storage stand-in: a map, or one that throws like a blocked one. */
function memory(seed: Record<string, string> = {}) {
  const data = new Map(Object.entries(seed))
  return {
    data,
    getItem: (k: string) => data.get(k) ?? null,
    setItem: (k: string, v: string) => { data.set(k, v) },
  }
}
const blocked = {
  getItem: (): string | null => { throw new Error("SecurityError") },
  setItem: (): void => { throw new Error("QuotaExceededError") },
}

describe("the theme", () => {
  it("follows the system until the operator picks one", () => {
    expect(resolveTheme(null, false)).toBe("light")
    expect(resolveTheme(null, true)).toBe("dark")
    expect(resolveTheme("light", true)).toBe("light")
    expect(resolveTheme("dark", false)).toBe("dark")
  })

  it("keeps the pick in storage and reads it back", () => {
    const store = memory()
    expect(storedTheme(store)).toBeNull()
    storeTheme(store, "dark")
    expect(store.data.get(THEME_KEY)).toBe("dark")
    expect(storedTheme(store)).toBe("dark")
  })

  it("ignores a stored value it does not know", () => {
    expect(storedTheme(memory({ [THEME_KEY]: "sepia" }))).toBeNull()
  })

  it("survives a storage that throws or is missing", () => {
    expect(storedTheme(blocked)).toBeNull()
    expect(() => storeTheme(blocked, "dark")).not.toThrow()
    expect(storedTheme(null)).toBeNull()
    expect(() => storeTheme(null, "light")).not.toThrow()
  })

  it("toggles between the two, from the theme shown", () => {
    const shown = resolveTheme(null, true)  // the system is dark, no pick yet
    expect(otherTheme(shown)).toBe("light")
    expect(otherTheme("light")).toBe("dark")
  })
})

describe("the theme toggle", () => {
  it("names the theme it switches to, with a moon in light and a sun in dark", () => {
    const light = renderToStaticMarkup(<ThemeToggle theme="light" onToggle={() => {}} />)
    expect(light).toContain('aria-label="Switch to dark mode"')
    expect(light).toContain("<path d=\"M20.5 14.5")  // the moon
    const dark = renderToStaticMarkup(<ThemeToggle theme="dark" onToggle={() => {}} />)
    expect(dark).toContain('aria-label="Switch to light mode"')
    expect(dark).toContain("<circle")  // the sun
    expect(toggleLabel("dark")).toBe("Switch to light mode")
  })
})
