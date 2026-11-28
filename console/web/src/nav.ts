import type { View } from "./ui/TopBar"

// Where the console stands, as the browser's history and the address keep it:
// the view, and the agent or ask whose detail drawer is open (by name and id,
// never the objects: those change with the live flock). The open agent is
// part of the address (`?agent=<name>`, an ask `&ask=<id>`, a view other than
// the start page `&view=asks`), so a link or a reload opens the same drawer;
// the path stays "/" because the server serves the app there alone. Every
// move pushes a place, the browser's Back and Forward come back through
// popstate.

export type Place = { view: View; agent?: string; ask?: string }

const VIEWS: readonly string[] = ["agents", "asks", "stats"]

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

/** The place an address names: `?view=`, `?agent=` and `?ask=`; anything
 * else (no query, an unknown view) is the start page. */
export function placeOfSearch(search: string): Place {
  const q = new URLSearchParams(search)
  const view = q.get("view") ?? "agents"
  const place: Place = { view: VIEWS.includes(view) ? view as View : "agents" }
  const agent = q.get("agent")
  const ask = q.get("ask")
  if (agent) place.agent = agent
  if (ask) place.ask = ask
  return place
}

/** The query string of a place: empty for the start page alone. */
export function searchOf(place: Place): string {
  const q = new URLSearchParams()
  if (place.view !== "agents") q.set("view", place.view)
  if (place.agent) q.set("agent", place.agent)
  if (place.ask) q.set("ask", place.ask)
  const text = q.toString()
  return text ? `?${text}` : ""
}

/** The agent of an open drawer: the one the place names, else the asker of
 * the ask it names; null when neither is in the flock now. */
export function agentOf<T extends { name: string }>(place: Place, cards: readonly T[],
                                                    askBy: string | undefined): T | null {
  const name = place.agent ?? askBy
  return name ? cards.find((c) => c.name === name) ?? null : null
}

/** The agent after the open one in the list's order: `step` 1 is down, -1 up;
 * stops at the ends. The first one when none is open. */
export function neighbour(order: readonly string[], open: string | null, step: 1 | -1): string | null {
  if (order.length === 0) return null
  const at = open === null ? -1 : order.indexOf(open)
  if (at < 0) return order[step === 1 ? 0 : order.length - 1]
  return order[Math.max(0, Math.min(order.length - 1, at + step))]
}

function write(how: "pushState" | "replaceState", place: Place): void {
  try {
    window.history[how](place, "", window.location.pathname + searchOf(place))
  } catch {
    // no history (a test, a sandboxed frame): the page still moves
  }
}

export function pushPlace(place: Place): void { write("pushState", place) }
export function replacePlace(place: Place): void { write("replaceState", place) }

/** The place the page was loaded with. */
export function loadedPlace(): Place {
  try {
    return placeOfSearch(window.location.search)
  } catch {
    return { view: "agents" }
  }
}
