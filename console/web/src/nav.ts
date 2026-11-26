import type { View } from "./ui/TopBar"

// Where the console stands, as the browser's history keeps it: the view, and
// the agent or ask whose detail page is open (by name and id, never the
// objects: those change with the live fleet). Every move pushes a place, so
// the browser's Back leaves a detail page for the list it came from. The URL
// stays the same: the server serves the app at "/" alone.

export type Place = { view: View; agent?: string; ask?: string }

const VIEWS: readonly string[] = ["agents", "asks", "graph", "overview", "reports"]

/** A place from history.state, or null for anything else (another app's
 * state, a page loaded fresh). */
export function asPlace(state: unknown): Place | null {
  if (typeof state !== "object" || state === null) return null
  const s = state as Record<string, unknown>
  if (typeof s.view !== "string" || !VIEWS.includes(s.view)) return null
  const place: Place = { view: s.view as View }
  if (typeof s.agent === "string") place.agent = s.agent
  if (typeof s.ask === "string") place.ask = s.ask
  return place
}

export function pushPlace(place: Place): void {
  try {
    window.history.pushState(place, "")
  } catch {
    // no history (a test, a sandboxed frame): the page still moves
  }
}

export function replacePlace(place: Place): void {
  try {
    window.history.replaceState(place, "")
  } catch {
    // as above
  }
}
