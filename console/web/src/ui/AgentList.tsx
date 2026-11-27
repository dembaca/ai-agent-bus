import type { AgentCard, AgentStatsRow, Ask } from "../api/types"
import { since } from "../clock"
import {
  NO_STATS_ROW, STATS_ROWS_NOTE, STATUS_FILTERS, agentName, askerName, countByGroup, filterCards, harnessLabel,
  openAsksFirst, stateClass, tokenCount, type StatusFilter,
} from "./agents"
import { CircleHelp } from "lucide-react"
import { CrownIcon, SearchIcon } from "./icons"
import "./AgentList.css"

/** An empty value in the list: a dash, quieter than "n/a" in a dense table. */
export const EMPTY = "–"

/** How many open asks sit above the table; the rest are one click away. */
const ASKS_ON_TOP = 4

/** The home view (the agreed design, 2026-11-25): the open asks on top, then
 * one compact row per agent — the table replaces the Kanban. Search and a
 * status filter narrow the rows; a row opens the agent's detail. Read-only
 * (ADR-015 stage 1): an ask is answered in its thread in the chat. */
export function AgentList({ cards, asks, stats = null, compact = false, selected = null,
                            filter, onFilter: setFilter, query, onQuery: setQuery,
                            onOpenAgent, onOpenAsk, onAllAsks }: {
  cards: AgentCard[]
  asks: Ask[]
  /** The per-agent rows of `GET /v1/agent-stats` by agent (#201); null
   * while none was read. */
  stats?: ReadonlyMap<string, AgentStatsRow> | null
  /** Beside an open detail (#200): the half-width list keeps agent,
   * status, task and last post. */
  compact?: boolean
  /** The agent whose detail is open beside the list. */
  selected?: string | null
  /** The status filter and the search: the app keeps them, so they
   * survive a detail page and the way back. */
  filter: StatusFilter
  onFilter: (filter: StatusFilter) => void
  query: string
  onQuery: (query: string) => void
  onOpenAgent: (card: AgentCard) => void
  onOpenAsk: (ask: Ask) => void
  onAllAsks: () => void
}) {
  const rows = filterCards(cards, filter, query)
  const counts = countByGroup(cards)
  const open = openAsksFirst(asks)
  const reachable = cards.length - counts.offline
  const forMe = open.filter((a) => a.forMe === true).length

  return (
    <>
      <div className="page-head">
        <div className="page-head__title">
          <h1>Agents</h1>
          <span className="page-head__sub">
            {cards.length} on the bus · {reachable} reachable
            {forMe > 0 && <> · <span className="agents__for-me">{forMe} waiting on you</span></>}
          </span>
        </div>
        <div className="agents__controls">
          <label className="agents__search">
            <SearchIcon />
            <span className="sr-only">Search agents</span>
            <input type="search" value={query} placeholder="Search agents, tasks, branches"
              onChange={(e) => setQuery(e.target.value)} />
          </label>
          <div className="seg" role="group" aria-label="Filter by status">
            {/* "Other" only while an agent's state fits no group, or it is picked */}
            {STATUS_FILTERS.filter((f) => f.id !== "other" || counts.other > 0 || filter === "other").map((f) => (
              <button key={f.id} type="button" aria-pressed={filter === f.id}
                className={"seg__btn" + (filter === f.id ? " seg__btn--on" : "")}
                onClick={() => setFilter(f.id)}>
                {f.label}
                {f.id !== "all" && <span className="seg__count">{counts[f.id]}</span>}
              </button>
            ))}
          </div>
        </div>
      </div>

      {open.length > 0 && (
        <section className="asks-top" aria-label="Open asks">
          {open.slice(0, ASKS_ON_TOP).map((ask) => (
            <AskTile key={ask.id} ask={ask} cards={cards} onOpen={onOpenAsk} />
          ))}
          {open.length > ASKS_ON_TOP && (
            <button type="button" className="asks-top__more" onClick={onAllAsks}>
              All {open.length} open asks
            </button>
          )}
        </section>
      )}

      <section className="panel agents" aria-label="Agents">
        <table className={"agents__table" + (compact ? " agents__table--compact" : "")}>
          {/* fixed widths: no column pushes the table past its card; the
              task takes the rest, every long text ends in an ellipsis */}
          <colgroup>
            <col className="agents__col--agent" />
            <col className="agents__col--status" />
            <col className="agents__col--task" />
            {!compact && <>
              <col className="agents__col--branch" />
              <col className="agents__col--home" />
              <col className="agents__col--ctx" />
              <col className="agents__col--asks" />
              <col className="agents__col--tokens" />
            </>}
            <col className="agents__col--last" />
          </colgroup>
          <thead>
            <tr>
              <th scope="col">Agent</th>
              <th scope="col">Status</th>
              <th scope="col">Task</th>
              {!compact && <>
                <th scope="col">Branch</th>
                <th scope="col">Home</th>
                <th scope="col">Context</th>
                <th scope="col" className="num">Asks</th>
                <th scope="col" className="num" title="tokens of the last 24 hours">Tokens today</th>
              </>}
              <th scope="col" className="num">Last post</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((card) => (
              <AgentRow key={card.name} card={card} compact={compact} stats={stats}
                selected={card.name === selected} onOpen={onOpenAgent} />
            ))}
          </tbody>
        </table>
        {rows.length === 0 && (
          <p className="agents__empty">
            {cards.length === 0 ? "No agent on the bus yet." : "No agent matches this filter."}
          </p>
        )}
      </section>
      <p className="hint agents__note">
        Live from the agents' presence on the bus; a row changes in place. Task,
        model and branch are what each agent states about itself.
        {!compact && <> Tokens today come from the agents' STATS lines of the last
          24 hours, for your own agents and those that trust you: a dash there
          means no per-agent numbers for agents of other owners. {STATS_ROWS_NOTE}</>}
      </p>
    </>
  )
}

function AgentRow({ card, compact, stats, selected, onOpen }: {
  card: AgentCard
  compact: boolean
  stats: ReadonlyMap<string, AgentStatsRow> | null
  selected: boolean
  onOpen: (card: AgentCard) => void
}) {
  const state = stateClass(card.state)
  const waits = (card.asksForOperator ?? 0) > 0  // the cell's title and the badge both show it
  const model = `${harnessLabel(card.harness)} · ${card.model || "model n/a"}`
  const branch = card.branch || EMPTY
  return (
    <tr className={"agents__row" + (state === "offline" ? " agents__row--offline" : "")
      + (selected ? " agents__row--selected" : "")}
      onClick={() => onOpen(card)}>
      <td className="agents__who">
        <span className="agents__name">
          <button type="button" className="linkish mono agents__clip" title={nameTitle(card)}
            aria-current={selected ? "true" : undefined}
            onClick={(e) => { e.stopPropagation(); onOpen(card) }}>
            {agentName(card).text}
          </button>
          {agentName(card).shared && <SharedMark />}
          {card.crown && <span className="crown"><CrownIcon label="coordinator" /></span>}
        </span>
        <span className="agents__model agents__clip" title={model}>{model}</span>
      </td>
      <td className="nowrap agents__status" title={`${card.state}${waits ? " · waits on you" : ""}`}>
        <span className={`pill pill--${state}`}>{card.state}</span>
        {waits && (
          <span className="pill pill--ask pill--plain agents__waits">waits on you</span>
        )}
      </td>
      <td className="agents__task">
        {card.task
          ? <span className="agents__task-text" title={card.task}>{card.task}</span>
          : <span className="muted">{EMPTY}</span>}
      </td>
      {!compact && <>
        <td className="mono small muted">
          <span className="agents__clip" title={card.pr ? `#${card.pr.number} · ${branch}` : branch}>
            {card.pr && <span className="chip chip--pr">#{card.pr.number}</span>}{" "}
            {branch}
          </span>
        </td>
        <td className="mono small">
          <span className="agents__home"
            title={card.guests.length ? `${card.home}, guest in ${card.guests.join(", ")}` : card.home}>
            <span className="agents__clip">{card.home}</span>
            {/* the guest count stays outside the clip: the name gives way, not the count */}
            {card.guests.length > 0 && <span className="muted agents__guests">+{card.guests.length}</span>}
          </span>
        </td>
        <td><Context ctx={card.ctx} /></td>
        <td className="num">
          {card.openAsks === null ? <span className="muted">{EMPTY}</span> : card.openAsks}
        </td>
      </>}
      {!compact && <td className="num nowrap"><TokensToday stats={stats} name={card.name} /></td>}
      <td className="num muted nowrap">{card.lastPostAt ? since(card.lastPostAt) : EMPTY}</td>
    </tr>
  )
}

/** The agent's tokens of the last 24 hours (#201): a dash when it has no row
 * (another owner's agent, or one not running now), n/a when its row states no
 * token counts, never 0 for either. */
function TokensToday({ stats, name }: { stats: ReadonlyMap<string, AgentStatsRow> | null; name: string }) {
  if (stats === null) return <span className="muted" title="per-agent numbers not read yet">{EMPTY}</span>
  const row = stats.get(name)
  if (!row) {
    return <span className="muted" title={`${NO_STATS_ROW} ${STATS_ROWS_NOTE}`}>{EMPTY}</span>
  }
  const all = row.tok24h?.all ?? null
  return all === null
    ? <span className="muted" title="no token counts in its STATS parts of the last 24 hours">n/a</span>
    : <span title={`${all} tokens in the last 24 hours`}>{tokenCount(all)}</span>
}

/** The title of an agent's name: the technical id on the bus, and why a
 * shared name is not confirmed. */
export function nameTitle(card: Pick<AgentCard, "name" | "displayName" | "statedName" | "nameShared">): string {
  const n = agentName(card)
  return n.shared
    ? `${n.id} · another agent states this name too: not confirmed`
    : n.id
}

/** An agent's name with its "shared name" mark, the bus id in the title:
 * every place that names an agent beside a decision (asks, the Overview,
 * the detail's ask) uses it, so an impostor's ask never reads as another
 * agent's (#199). */
export function AgentLabel({ name, className = "mono" }: {
  name: { text: string; shared: boolean; id: string }
  className?: string
}) {
  return (
    <>
      <span className={className} title={name.shared
        ? `${name.id} · another agent states this name too: not confirmed`
        : name.id}>{name.text}</span>
      {name.shared && <> <SharedMark /></>}
    </>
  )
}

/** The mark beside a name another agent states too (#199). */
export function SharedMark() {
  return (
    <span className="chip chip--shared" title="another agent states this name too: not confirmed">
      shared name
    </span>
  )
}

/** The context bar: the share used; unknown is n/a, never 0. */
export function Context({ ctx }: { ctx: number | null }) {
  if (ctx === null) return <span className="ctx__text" title="context not stated">{EMPTY}</span>
  const pct = Math.max(0, Math.min(100, ctx))
  return (
    <span className="ctx" title={`context ${ctx} %`}>
      <span className="ctx__track">
        <span className={"ctx__fill" + (pct >= 70 ? " ctx__fill--high" : "")} style={{ width: `${pct}%` }} />
      </span>
      <span className="ctx__text">{ctx} %</span>
    </span>
  )
}

/** One open ask above the table: its title (plain text), who asked, how
 * long ago, its marks. A click opens it; nothing answers it here. */
export function AskTile({ ask, cards, onOpen }: { ask: Ask; cards: readonly AgentCard[]; onOpen: (ask: Ask) => void }) {
  const by = askerName(ask, cards)
  return (
    <button type="button" className={"ask-tile" + (ask.forMe ? " ask-tile--for-me" : "")}
      onClick={() => onOpen(ask)}>
      <span className="ask-tile__icon"><CircleHelp size={15} strokeWidth={2.2} aria-hidden="true" /></span>
      <span className="ask-tile__body">
        <strong className="ask-tile__need">{ask.need}</strong>
        <span className="ask-tile__by">
          asked by <AgentLabel name={by} /> · {since(ask.at)} ago
          {ask.human && <> · to {ask.human}</>}
        </span>
      </span>
      <span className="ask-tile__marks">
        {ask.forMe && <span className="chip chip--for-me">for you</span>}
        {!ask.forMe && ask.mayAnswer && <span className="chip chip--may-answer">may answer</span>}
        {ask.end === "expired" && <span className="chip chip--expired">expired</span>}
        {ask.agentGone && <span className="chip chip--expired">agent gone</span>}
      </span>
    </button>
  )
}
