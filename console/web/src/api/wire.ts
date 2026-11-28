// The wire contract, held from one fixture: `console/fixtures/console-api.json`
// is the API of ADR-015 point 10 as both sides speak it — the app's types are
// checked against it here, and the server holds the same file in its tests
// (`console/server/tests/test_wire_contract.py`, which builds this card from
// live deliveries). Cards, events and thread pages are the app's camelCase
// shapes; ask rows and `/v1/me` use the ADR's spellings (`for_me`, …) and are
// mapped in `http.ts`; a report is `/v1/stats`' shape as the app takes it.
//
// A JSON import widens literal unions (`state`, the event `type`, `end`) to
// plain strings, so the structural assertion is a `satisfies` on the widened
// shape and the literal words are guarded at load. The guards and the fixture
// itself run in development only: the production bundle carries neither.
import {
  AGENT_STATES, HARNESS_LABEL, type AgentCard, type AgentStatsRow, type Post, type Report,
  type GroupsAnswer, type ReportSegment, type ReportStack, type ThreadPage,
} from "./types"
import type { WireAsk, WireMe } from "./http"
import fixture from "../../../fixtures/console-api.json"
// The fixture's envelope, widened where the JSON import widens it
type FixtureEvShape = {
  snapshot: { type: string, cards: AgentCard[] }
  card: { type: string, card: Omit<AgentCard, "state" | "harness" | "role">
          & { state: string; harness: string | null; role: string } }
  drop: { type: string, name: string }
}

let shapes: {
  card: Omit<AgentCard, "state" | "harness" | "role"> & { state: string; harness: string | null; role: string },
  page: ThreadPage, events: FixtureEvShape,
  ask: Omit<WireAsk, "end" | "kind"> & { end: string; kind: string }
  report: ReportShape
  activity: ReportShape
  activity24h: ReportShape
  activityNoHours: ReportShape
  asksBefore: ReportShape
  agentStats: AgentStatsRow[]
  groups: GroupsAnswer
} | null = null

type ReportFieldShape = Omit<Report["fields"][number], "unit"> & { unit: string }
type ReportSegmentShape = Omit<ReportSegment, "category"> & { category: string }
type ReportStackShape = Omit<ReportStack, "segments" | "raw">
  & { segments: ReportSegmentShape[]; raw: ReportSegmentShape[] }
type ReportShape = Omit<Report, "fields" | "stacks" | "activity">
  & { fields: ReportFieldShape[]; stacks?: ReportStackShape[]
      activity?: Omit<NonNullable<Report["activity"]>, "bucket"> & { bucket: string } }

if (!import.meta.env.PROD) {
  type CardShape = Omit<AgentCard, "state" | "harness" | "role">
    & { state: string; harness: string | null; role: string }
  const card = fixture.card satisfies CardShape
  const firstPost = fixture.page.posts[0] satisfies Post
  const page = { rootId: fixture.page.rootId, posts: [firstPost], next: fixture.page.next } satisfies ThreadPage
  const events = fixture.events satisfies FixtureEvShape
  const ask = fixture.ask satisfies Omit<WireAsk, "end" | "kind"> & { end: string; kind: string }
  const me = fixture.me satisfies WireMe
  const report: ReportShape = fixture.report satisfies ReportShape
  const agentStats: AgentStatsRow[] = fixture.agentStats satisfies AgentStatsRow[]
  const groups = { ...fixture.groups,
    by: fixture.groups.by as GroupsAnswer["by"], scope: fixture.groups.scope as GroupsAnswer["scope"],
  } satisfies GroupsAnswer
  if (ask.kind !== "approval" && ask.kind !== "question") {
    throw new Error(`wire fixture: unknown ask kind ${ask.kind}`)
  }
  // its bars hold different words, which the JSON's inferred union cannot tell: a cast, the checks follow
  const activity = fixture.activity as unknown as ReportShape
  if (!report.window || activity.id !== "activity" || !activity.activity
      || !["hour", "day", "week", "month"].includes(activity.activity.bucket)) {
    throw new Error("wire fixture: the report window or the activity card is missing")
  }
  // 24 h (#284, #290): hourly bars and the note's numbers, or n/a with its reason when no line carries hours
  const activity24h = fixture.activity24h as unknown as ReportShape
  const activityNoHours = fixture.activityNoHours as unknown as ReportShape
  const hours = activity24h.activity?.hours
  if (activity24h.activity?.bucket !== "hour" || activity24h.activity.bars.length !== 24
      || !hours || hours.lines > hours.of || activity24h.activity.unavailable) {
    throw new Error("wire fixture: the 24 h activity card lacks its hourly bars or its hours note")
  }
  if (!activityNoHours.activity?.unavailable || !activityNoHours.activity.reason
      || activityNoHours.activity.bars.length > 0 || activityNoHours.activity.hours) {
    throw new Error("wire fixture: the 24 h card without hours is not n/a with its reason")
  }
  const asksBefore: ReportShape = fixture.asksBefore satisfies ReportShape
  if (asksBefore.id !== "asks-before" || !asksBefore.unavailable
      || asksBefore.fields.some((f) => f.value !== null)) {
    throw new Error("wire fixture: the card of a range not held is not all n/a with its reason")
  }
  for (const field of report.fields) {
    if (!["count", "s", "%"].includes(field.unit)) {
      throw new Error(`wire fixture: unknown report unit ${field.unit}`)
    }
  }
  for (const stack of report.stacks ?? []) {
    for (const seg of [...stack.segments, ...stack.raw]) {
      if (!["task", "overhead", "coordination"].includes(seg.category)) {
        throw new Error(`wire fixture: unknown token category ${seg.category}`)
      }
    }
  }
  if (!(AGENT_STATES as readonly string[]).includes(card.state)) {
    throw new Error(`wire fixture: unknown state ${card.state}`)
  }
  if (card.harness !== null && !(card.harness in HARNESS_LABEL)) {
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
  if (me.build === undefined) {
    throw new Error("wire fixture: /v1/me states no build (#182)")
  }
  if (ask.end !== "open") {
    throw new Error(`wire fixture: the example ask is not open (${ask.end})`)
  }
  if (agentStats.length === 0) {
    throw new Error("wire fixture: no agent-stats row")
  }
  for (const row of agentStats) {
    for (const sums of [row.tok24h, row.tok7d]) {
      if (sums !== null && sums.all === null) {
        throw new Error("wire fixture: a token sum without its total")
      }
    }
  }
  shapes = { card, page, events, ask, report, activity, activity24h, activityNoHours, asksBefore, agentStats, groups }
}

export const wire_shapes = shapes
