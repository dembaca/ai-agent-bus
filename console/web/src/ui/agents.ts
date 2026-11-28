import { AGENT_STATES, HARNESS_LABEL, type AgentCard, type AgentStatsRow, type Ask } from "../api/types"

// The shared words of the agents list and the detail (the groups themselves are
// the server's, #283: `GET /v1/groups`).

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

/** A token count for a dense cell: 950, 92.1k, 1.2M; null is n/a, never 0.
 * The unit is picked after rounding, so 999,999 is 1M, never 1000k. */
export function tokenCount(n: number | null): string {
  if (n === null) return "n/a"
  if (n < 1000) return String(n)
  const k = rounded(n / 1000)
  return k < 1000 ? `${k}k` : `${rounded(n / 1_000_000)}M`
}

/** One decimal below 100, none from 100 on. */
function rounded(x: number): number {
  return x < 100 ? Math.round(x * 10) / 10 : Math.round(x)
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
