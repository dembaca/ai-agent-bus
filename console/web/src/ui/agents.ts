import { AGENT_STATES, HARNESS_LABEL, type AgentCard, type Ask } from "../api/types"

// The agents list's filter and the status groups the Overview counts by.
// The groups are made of the state words the bus carries (ADR-015 point 6):
// presence's format v1 says online/away until it carries the state words, so
// online counts as listening and away as idle. A state the console does not
// know is never guessed into a group: it shows under "All" and "Other".

export type StatusFilter = "all" | "working" | "listening" | "idle" | "offline" | "other"

export const STATUS_GROUPS: Record<Exclude<StatusFilter, "all" | "other">, readonly string[]> = {
  working: ["working", "bus"],
  listening: ["listening", "online"],
  idle: ["idle", "away", "silent"],
  offline: ["offline"],
}

export const STATUS_FILTERS: readonly { id: StatusFilter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "working", label: "Working" },
  { id: "listening", label: "Listening" },
  { id: "idle", label: "Idle" },
  { id: "offline", label: "Offline" },
  { id: "other", label: "Other" },
]

/** A state no group holds. */
function ungrouped(state: string): boolean {
  return !Object.values(STATUS_GROUPS).some((g) => g.includes(state))
}

/** The text a search looks through: names, task, branch, home, guests,
 * harness and model, the PR number. */
function haystack(card: AgentCard): string {
  return [
    card.name, card.displayName, card.task, card.branch ?? "", card.home,
    ...card.guests, card.model, harnessLabel(card.harness), card.project,
    card.pr ? `#${card.pr.number}` : "",
  ].join(" ").toLowerCase()
}

/** The cards the list shows: in the chosen status group, and holding every
 * word of the search (any order, any case). */
export function filterCards(cards: readonly AgentCard[], filter: StatusFilter,
                            query: string): AgentCard[] {
  const words = query.trim().toLowerCase().split(/\s+/).filter(Boolean)
  const inGroup = (state: string): boolean =>
    filter === "all" ? true : filter === "other" ? ungrouped(state) : STATUS_GROUPS[filter].includes(state)
  return cards.filter((card) =>
    inGroup(card.state) &&
    words.every((w) => haystack(card).includes(w)))
}

/** How many cards each group holds, for the filter's buttons and the
 * Overview; `other` counts the states no group knows. */
export function countByGroup(cards: readonly AgentCard[]): Record<Exclude<StatusFilter, "all">, number> {
  const out = { working: 0, listening: 0, idle: 0, offline: 0, other: 0 }
  for (const card of cards) {
    const hit = (Object.keys(STATUS_GROUPS) as Array<keyof typeof STATUS_GROUPS>)
      .find((g) => STATUS_GROUPS[g].includes(card.state))
    out[hit ?? "other"] += 1
  }
  return out
}

/** The pill's class: the known state words, else "other". */
export function stateClass(state: string): string {
  return (AGENT_STATES as readonly string[]).includes(state) ? state : "other"
}

/** The name an agent shows (#199): the username its presence states —
 * marked `shared` when another card states it too, so it is shown but never
 * as a confirmed name — else the server's name for it (the short bus id).
 * `id` is the technical name on the bus, for a title or secondary text. */
export function agentName(card: Pick<AgentCard, "name" | "displayName" | "statedName" | "nameShared">):
    { text: string; shared: boolean; id: string } {
  const stated = card.statedName ?? ""
  return stated
    ? { text: stated, shared: card.nameShared === true, id: card.name }
    : { text: card.displayName || card.name, shared: false, id: card.name }
}

/** The name an ask's agent shows: its card's when the console holds it,
 * else the ask row's own. */
export function askerName(ask: Pick<Ask, "by" | "displayName">, cards: readonly AgentCard[]):
    { text: string; shared: boolean; id: string } {
  const card = cards.find((c) => c.name === ask.by)
  return card ? agentName(card) : { text: ask.displayName || ask.by, shared: false, id: ask.by }
}

/** The harness's label; "harness n/a" when the wire does not carry it. */
export function harnessLabel(harness: AgentCard["harness"]): string {
  return harness === null ? "harness n/a" : HARNESS_LABEL[harness]
}

/** An ask still waiting: open, or past 24 h (a hint, the asking server decides). */
export function isOpen(ask: Ask): boolean {
  return ask.end === "open" || ask.end === "expired"
}

/** The open asks, the operator's first, then the oldest. */
export function openAsksFirst(asks: readonly Ask[]): Ask[] {
  return asks.filter(isOpen).sort((a, b) =>
    Number(b.forMe === true) - Number(a.forMe === true) ||
    Number(b.mayAnswer === true) - Number(a.mayAnswer === true) ||
    a.at - b.at)
}
