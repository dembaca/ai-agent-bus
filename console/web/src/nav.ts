import type { ReactNode } from "react"
import { createElement } from "react"
import type { AgentCard, Ask, FleetEvent } from "./api/types"
import type { View } from "./ui/TopBar"

// Where the console stands, as the browser's history keeps it: the view, and
// the agent or ask whose detail page is open (by name and id, never the
// objects: those change with the live fleet). Every move pushes a place, so
// the browser's Back leaves a detail page for the list it came from. The URL
// stays the same: the server serves the app at "/" alone.

export type Place = { view: View; agent?: string; ask?: string }

const VIEWS: readonly string[] = ["overview", "agents", "asks", "graph", "reports"]

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

/** What a place shows with the fleet and asks the console holds now: the
 * view, and the ask and agent of its detail — null for one no longer there
 * (an agent that left: its detail closes, an ask stays with "agent gone"). */
export function resolvePlace(place: Place, cards: readonly AgentCard[], asks: readonly Ask[]):
    { view: View; agent: AgentCard | null; ask: Ask | null } {
  const ask = place.ask ? asks.find((a) => a.id === place.ask) ?? null : null
  const by = place.agent ?? ask?.by
  const agent = by ? cards.find((c) => c.name === by) ?? null : null
  return { view: place.view, agent, ask }
}

/** The agent of an open detail after a fleet event: its newer card, or
 * null once it left the fleet — never a card that is gone. */
export function selectedAfter(event: FleetEvent, sel: AgentCard | null): AgentCard | null {
  if (!sel) return sel
  if (event.type === "snapshot") return event.cards.find((c) => c.name === sel.name) ?? null
  if (event.type === "card") return event.card.name === sel.name ? event.card : sel
  return event.name === sel.name ? null : sel
}

/** The view alone, or the view and a detail beside it (#200): the view stays
 * mounted either way; below 1000 px the CSS hides it beside a detail. */
export function Split({ detail, children }: { detail: ReactNode; children: ReactNode }) {
  return createElement("div", { className: detail ? "split" : "solo" },
    createElement("div", { className: "view" }, children),
    detail ? createElement("div", { className: "split__detail" }, detail) : null)
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
