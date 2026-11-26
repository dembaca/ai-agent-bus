import { useCallback, useEffect, useState } from "react"

// Light or dark: the console follows the system (prefers-color-scheme) until
// the operator picks one with the top bar's toggle; the pick is kept in this
// browser's localStorage. Storage can be missing or throw (a private window,
// blocked site data): every read and write is wrapped, and the console then
// simply follows the system again on the next load.

export type Theme = "light" | "dark"

export const THEME_KEY = "choirbus-console-theme"

type Reader = Pick<Storage, "getItem">
type Writer = Pick<Storage, "setItem">

/** The stored pick, or null: none, an unknown value, or no storage. */
export function storedTheme(storage: Reader | null): Theme | null {
  try {
    const value = storage?.getItem(THEME_KEY)
    return value === "light" || value === "dark" ? value : null
  } catch {
    return null
  }
}

/** Keeps the pick; a storage that refuses is no error (the pick holds for
 * this page until it reloads). */
export function storeTheme(storage: Writer | null, theme: Theme): void {
  try {
    storage?.setItem(THEME_KEY, theme)
  } catch {
    // no storage: the pick lasts until the page reloads
  }
}

/** The theme shown: the pick, else the system's. */
export function resolveTheme(stored: Theme | null, prefersDark: boolean): Theme {
  return stored ?? (prefersDark ? "dark" : "light")
}

export function otherTheme(theme: Theme): Theme {
  return theme === "dark" ? "light" : "dark"
}

/** What the toggle says it does: it names the theme it switches to. */
export function toggleLabel(theme: Theme): string {
  return theme === "dark" ? "Switch to light mode" : "Switch to dark mode"
}

function localStore(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage
  } catch {
    return null
  }
}

const DARK_QUERY = "(prefers-color-scheme: dark)"

function systemDark(): boolean {
  try {
    return typeof window !== "undefined" && !!window.matchMedia?.(DARK_QUERY).matches
  } catch {
    return false
  }
}

/** The theme in use and its toggle. Without a pick, <html> carries no
 * data-theme and the CSS follows the system; a pick sets data-theme. */
export function useTheme(): { theme: Theme; toggle: () => void } {
  const [stored, setStored] = useState<Theme | null>(() => storedTheme(localStore()))
  const [prefersDark, setPrefersDark] = useState<boolean>(systemDark)

  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return
    const query = window.matchMedia(DARK_QUERY)
    const changed = (event: MediaQueryListEvent) => { setPrefersDark(event.matches) }
    query.addEventListener?.("change", changed)
    return () => { query.removeEventListener?.("change", changed) }
  }, [])

  useEffect(() => {
    const root = document.documentElement
    if (stored) root.dataset.theme = stored
    else delete root.dataset.theme
  }, [stored])

  const theme = resolveTheme(stored, prefersDark)
  const toggle = useCallback(() => {
    const next = otherTheme(theme)
    storeTheme(localStore(), next)
    setStored(next)
  }, [theme])
  return { theme, toggle }
}
