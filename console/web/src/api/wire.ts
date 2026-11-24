// The wire contract, held from one fixture: `console/fixtures/console-api.json`
// is the API of ADR-015 point 10 as both sides speak it — the app's types are
// checked against it here, and the server holds the same file in its tests
// (`console/server/tests/test_wire_contract.py`, which builds this card from
// live deliveries). Cards, events and thread pages are the app's camelCase
// shapes; ask rows and `/v1/me` use the ADR's spellings (`for_me`, …) and are
// mapped in `http.ts`.
//
// A JSON import widens literal unions (`state`, the event `type`, `end`) to
// plain strings, so the structural assertion is a `satisfies` on the widened
// shape and the literal words are guarded at load. The guards and the fixture
// itself run in development only: the production bundle carries neither.
import { AGENT_STATES, HARNESS_LABEL, type AgentCard, type Post, type ThreadPage } from "./types"
import type { WireAsk } from "./http"
import fixture from "../../../fixtures/console-api.json"
// The fixture's envelope, widened where the JSON import widens it
type FixtureEvShape = {
  snapshot: { type: string, cards: AgentCard[] }
  card: { type: string, card: Omit<AgentCard, "state" | "harness" | "role">
          & { state: string; harness: string; role: string } }
  drop: { type: string, name: string }
}

let shapes: {
  card: Omit<AgentCard, "state" | "harness" | "role"> & { state: string; harness: string; role: string },
  page: ThreadPage, events: FixtureEvShape,
  ask: Omit<WireAsk, "end"> & { end: string }
} | null = null

if (!import.meta.env.PROD) {
  type CardShape = Omit<AgentCard, "state" | "harness" | "role">
    & { state: string; harness: string; role: string }
  const card = fixture.card satisfies CardShape
  const firstPost = fixture.page.posts[0] satisfies Post
  const page = { rootId: fixture.page.rootId, posts: [firstPost], next: fixture.page.next } satisfies ThreadPage
  const events = fixture.events satisfies FixtureEvShape
  const ask = fixture.ask satisfies Omit<WireAsk, "end"> & { end: string }
  if (!(AGENT_STATES as readonly string[]).includes(card.state)) {
    throw new Error(`wire fixture: unknown state ${card.state}`)
  }
  if (!(card.harness in HARNESS_LABEL)) {
    throw new Error(`wire fixture: unknown harness ${card.harness}`)
  }
  if (card.role !== "coordinator" && card.role !== "member") {
    throw new Error(`wire fixture: unknown role ${card.role}`)
  }
  for (const [what, want] of [["snapshot", events.snapshot.type],
                               ["card", events.card.type],
                               ["drop", events.drop.type]] as const) {
    if (want !== what) {
      throw new Error(`wire fixture: the ${what} event says ${want}`)
    }
  }
  if (ask.end !== "open") {
    throw new Error(`wire fixture: the example ask is not open (${ask.end})`)
  }
  shapes = { card, page, events, ask }
}

export const wire_shapes = shapes
