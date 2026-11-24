import type { AgentCard } from "./types"

// The envelope of the three events the fleet stream sends (the app's whole
// alphabet: one snapshot, then one `card` per change and one `drop` per gone
// agent — ADR-015 point 10). Shared by the fixture check and the client.
export type EvShape = {
  snapshot: { type: string, cards: AgentCard[] }
  card: { type: string, card: AgentCard }
  drop: { type: string, name: string }
}
