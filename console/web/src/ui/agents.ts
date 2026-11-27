import { AGENT_STATES, HARNESS_LABEL, type AgentCard, type AgentStatsRow, type Ask } from "../api/types"

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
    card.name, card.displayName, card.statedName ?? "", card.task, card.branch ?? "", card.home,
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

/** Why an agent has no per-agent numbers (#201, decided 2026-11-27): `GET
 * /v1/agent-stats` has rows only for agents whose fresh attestation names the
 * operator as owner or trusted, and whose STATS parts the session holds: an
 * own running agent has none until its first part arrives. Said plainly,
 * never as "n/a" (that is a number no part states). The README, the
 * CHANGELOG and ADR-015 quote both sentences word for word. */
export const NO_STATS_ROW = "No per-agent numbers for agents of other owners."
export const STATS_ROWS_NOTE =
  "Rows are of own or trusted agents running now: one has no row until its first STATS part arrives, and loses it when it goes offline."

/** Why a card shows no last post (#201, decided 2026-11-27): the console puts
 * it only on the cards of own or trusted agents, and only once the agent's
 * presence states one. Said plainly, so that no agent looks silent for it. */
export const NO_LAST_POST =
  "The last post shows only for own or trusted agents, from their first post in their home on."

/** A token count for a dense cell: 950, 92.1k, 1.2M; null is n/a, never 0. */
export function tokenCount(n: number | null): string {
  if (n === null) return "n/a"
  if (n < 1000) return String(n)
  if (n < 1_000_000) return `${trim(n / 1000)}k`
  return `${trim(n / 1_000_000)}M`
}

function trim(x: number): string {
  return (x < 100 ? x.toFixed(1) : x.toFixed(0)).replace(/\.0$/, "")
}

/** The per-agent rows by agent name; null while none was read. */
export function statsByAgent(rows: readonly AgentStatsRow[] | null): ReadonlyMap<string, AgentStatsRow> | null {
  return rows === null ? null : new Map(rows.map((r) => [r.agent, r]))
}

/** The open asks, the operator's first, then the oldest. */
export function openAsksFirst(asks: readonly Ask[]): Ask[] {
  return asks.filter(isOpen).sort((a, b) =>
    Number(b.forMe === true) - Number(a.forMe === true) ||
    Number(b.mayAnswer === true) - Number(a.mayAnswer === true) ||
    a.at - b.at)
}
