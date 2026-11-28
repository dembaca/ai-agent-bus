import type { AgentCard, AgentStatsRow, Ask, Group, GroupBy, GroupsAnswer, Scope } from "../api/types"
import { useEffect, useMemo, useState } from "react"
import { ChevronDown, ChevronRight } from "lucide-react"
import { since } from "../clock"
import { DEFAULT_PREFS, isFolded, type Prefs } from "../prefs"
import {
  NO_LAST_POST, NO_STATS_ROW, STATS_ROWS_NOTE, agentName, harnessLabel, stateClass, tokenCount,
} from "./agents"
import { CrownIcon, SearchIcon } from "./icons"
import "./AgentList.css"

/** An empty value in the list: a dash, quieter than "n/a" in a dense table. */
export const EMPTY = "–"

export const BY_LABEL: Record<GroupBy, string> = {
  home: "Home", repo: "Repo", owner: "Owner", status: "Status", none: "None",
}
const BY_NOUN: Partial<Record<GroupBy, string>> = { home: "homes", repo: "repos", owner: "owners" }

export const SCOPE_LABEL: Record<Scope, string> = {
  mine: "Mine", trusting: "Trusting me", everyone: "Everyone",
}

/** The words a search looks through: the agent's names, its home and guest
 * channels, the person it is for, the repo, branch, task, harness, model and PR. */
function haystack(card: AgentCard): string {
  return [
    card.name, card.displayName, card.statedName ?? "", card.home, ...card.guests, card.for,
    card.project, card.branch ?? "", card.task, harnessLabel(card.harness), card.model,
    card.pr ? `#${card.pr.number}` : "",
  ].join(" ").toLowerCase()
}

/** Whether a card holds every word of the search (any order, any case). */
export function matches(card: AgentCard, query: string): boolean {
  const words = query.trim().toLowerCase().split(/\s+/).filter(Boolean)
  const text = haystack(card)
  return words.every((w) => text.includes(w))
}

/** What the list shows of one group: its agents' cards in the group's order
 * (those the search holds), or none when the search holds none of them. */
export interface Shown { group: Group; cards: AgentCard[]; folded: boolean }

/** The start page's list (#283, the owner's design in the thread of
 * 2026-11-27): one row per agent in three columns, the agents grouped as the
 * server grouped them (`GET /v1/groups`: the counts, the reachable agents, the
 * asks for the operator and the token sum come from there, the browser counts
 * nothing over the cards). A group has a header that folds it; in "Everyone"
 * the groups start folded except those that need the operator, and groups with
 * nobody reachable and no open ask fold into one row. A row opens the agent's
 * drawer. Read-only (ADR-015 stage 1). */
export function AgentList({ answer, cards, asks, stats = null, prefs = DEFAULT_PREFS,
                            onPrefs, query, onQuery, selected = null, panelFolded = false,
                            onOpenAgent, onVisible }: {
  /** The groups of `GET /v1/groups`; null until the first answer. */
  answer: GroupsAnswer | null
  cards: AgentCard[]
  asks: Ask[]
  /** The per-agent rows of `GET /v1/agent-stats` (#201) by agent; null
   * while none was read. */
  stats?: ReadonlyMap<string, AgentStatsRow> | null
  prefs?: Prefs
  onPrefs?: (patch: Partial<Prefs>) => void
  query: string
  onQuery: (query: string) => void
  /** The agent whose drawer is open. */
  selected?: string | null
  /** The "At a glance" panel is folded: each row gains a fourth column. */
  panelFolded?: boolean
  onOpenAgent: (card: AgentCard) => void
  /** The names of the rows shown, in order: the arrow keys walk them. */
  onVisible?: (names: string[]) => void
}) {
  const byName = useMemo(() => new Map(cards.map((c) => [c.name, c])), [cards])
  const searching = query.trim() !== ""
  const [quietOpen, setQuietOpen] = useState(false)
  const waiting = useMemo(
    () => new Set(asks.filter((a) => a.forMe === true && a.end !== "answered").map((a) => a.by)), [asks])

  const shown: Shown[] = []
  const quiet: Shown[] = []
  for (const group of answer?.groups ?? []) {
    const members = group.agents.flatMap((n) => byName.get(n) ?? [])
      .filter((c) => matches(c, query))
    if (searching && members.length === 0) continue
    const item: Shown = { group, cards: members,
      // a search shows its hits: no group hides them
      folded: !searching && isFolded(prefs, group.key, group.asksForYou > 0) }
    const asleep = prefs.scope === "everyone" && !searching && group.reachable === 0
      && group.asksOpen === 0 && (prefs.by === "home" || prefs.by === "repo" || prefs.by === "owner")
      && prefs.folded[`${prefs.by}:${group.key}`] === undefined
    ;(asleep ? quiet : shown).push(item)
  }
  const rowsShown = [...shown, ...(quietOpen ? quiet : [])]
  const order = rowsShown.filter((s) => !s.folded).flatMap((s) => s.cards.map((c) => c.name))
  const orderKey = order.join("\n")
  useEffect(() => { onVisible?.(orderKey ? orderKey.split("\n") : []) }, [orderKey, onVisible])

  const fold = (g: Group, folded: boolean) =>
    onPrefs?.({ folded: { ...prefs.folded, [`${prefs.by}:${g.key}`]: folded } })

  return (
    <>
      <div className="page-head">
        <div className="page-head__title">
          <h1>Agents</h1>
          <span className="page-head__sub">
            {answer
              ? <>{answer.glance.flock.total} in the flock · {answer.glance.flock.reachable} reachable</>
              : "reading the flock…"}
          </span>
        </div>
        <div className="agents__controls">
          <label className="agents__search">
            <SearchIcon />
            <span className="sr-only">Search home, agent or person</span>
            <input type="search" value={query} placeholder="Search home, agent, person"
              onChange={(e) => onQuery(e.target.value)} />
          </label>
          <div className="seg" role="group" aria-label="Group by">
            <span className="seg__label">Group by</span>
            {(Object.keys(BY_LABEL) as GroupBy[]).map((id) => (
              <button key={id} type="button" aria-pressed={prefs.by === id}
                className={"seg__btn" + (prefs.by === id ? " seg__btn--on" : "")}
                onClick={() => onPrefs?.({ by: id })}>{BY_LABEL[id]}</button>
            ))}
          </div>
          <div className="seg" role="group" aria-label="Scope">
            {(Object.keys(SCOPE_LABEL) as Scope[]).map((id) => (
              <button key={id} type="button" aria-pressed={prefs.scope === id}
                className={"seg__btn" + (prefs.scope === id ? " seg__btn--on" : "")}
                onClick={() => onPrefs?.({ scope: id })}>{SCOPE_LABEL[id]}</button>
            ))}
          </div>
        </div>
      </div>

      <section className="agents" aria-label="Agents">
        {shown.map((s) => (
          <GroupBlock key={s.group.key} item={s} by={prefs.by} stats={stats} waiting={waiting}
            selected={selected} panelFolded={panelFolded}
            onFold={(f) => fold(s.group, f)} onOpenAgent={onOpenAgent} />
        ))}
        {quiet.length > 0 && (
          <button type="button" className="agents__quiet" aria-expanded={quietOpen}
            onClick={() => setQuietOpen(!quietOpen)}>
            {quietOpen ? <ChevronDown size={14} aria-hidden="true" /> : <ChevronRight size={14} aria-hidden="true" />}
            {quiet.length} quiet {BY_NOUN[prefs.by] ?? "groups"}
            <span className="muted"> · nobody reachable, no open ask</span>
          </button>
        )}
        {quietOpen && quiet.map((s) => (
          <GroupBlock key={s.group.key} item={s} by={prefs.by} stats={stats} waiting={waiting}
            selected={selected} panelFolded={panelFolded}
            onFold={(f) => fold(s.group, f)} onOpenAgent={onOpenAgent} />
        ))}
        {answer !== null && shown.length + quiet.length === 0 && (
          <p className="agents__empty">
            {searching ? "No agent matches this search."
              : prefs.scope === "everyone" ? "No agent on the bus yet."
              : `No agent in "${SCOPE_LABEL[prefs.scope]}": pick Everyone to see the whole flock.`}
          </p>
        )}
      </section>
      <p className="hint agents__note">
        Live from the agents' presence on the bus; a row changes in place. Task,
        model and branch are what each agent states about itself. {NO_LAST_POST}
        {" "}A group's tokens of today add up the agents you may see numbers of;
        the group says how many that are. {NO_STATS_ROW} {STATS_ROWS_NOTE}
      </p>
    </>
  )
}

const STATE_ORDER = ["working", "listening", "idle", "offline", "other"] as const

/** The small stacked bar of a group's states, with its numbers in the title. */
export function StateBar({ states, total }: { states: Record<string, number>; total: number }) {
  if (total === 0) return null
  const title = STATE_ORDER.filter((k) => (states[k] ?? 0) > 0).map((k) => `${states[k]} ${k}`).join(", ")
  return (
    <span className="statebar" role="img" aria-label={title} title={title}>
      {STATE_ORDER.map((k) => (states[k] ?? 0) > 0
        ? <i key={k} className={`statebar__seg statebar__seg--${k}`} style={{ flexGrow: states[k] }} />
        : null)}
    </span>
  )
}

function GroupBlock({ item, by, stats, waiting, selected, panelFolded, onFold, onOpenAgent }: {
  item: Shown
  by: GroupBy
  stats: ReadonlyMap<string, AgentStatsRow> | null
  waiting: ReadonlySet<string>
  selected: string | null
  panelFolded: boolean
  onFold: (folded: boolean) => void
  onOpenAgent: (card: AgentCard) => void
}) {
  const { group: g, cards, folded } = item
  const tokenTitle = `Tokens of the last 24 hours of the agents you may see numbers of (own or trusted): ${g.tokensAgents} of ${g.total} agents`
  return (
    <div className="group">
      {by !== "none" ? (
        <button type="button" className="group__head" aria-expanded={!folded}
          onClick={() => onFold(!folded)}>
          <span className="group__chev" aria-hidden="true">
            {folded ? <ChevronRight size={14} /> : <ChevronDown size={14} />}
          </span>
          <span className="group__name mono" title={g.label}>{g.label}</span>
          <StateBar states={g.states} total={g.total} />
          <span className="group__facts muted">{g.reachable} of {g.total} reachable</span>
          {g.working > 0 && <span className="group__facts muted">{g.working} working</span>}
          {g.asksForYou > 0 && <span className="chip chip--for-me">{g.asksForYou} for you</span>}
          <span className="group__tokens muted" title={tokenTitle}>
            {g.tokens === null ? EMPTY : `${tokenCount(g.tokens)} tokens today`}
            {g.tokens !== null && (
              <span className="group__covers"> · {g.tokensAgents} of {g.total} agents</span>
            )}
          </span>
        </button>
      ) : null}
      {(!folded || by === "none") && (
        <ul className="rows">
          {cards.map((card) => (
            <AgentRow key={card.name} card={card} by={by} stats={stats}
              waits={waiting.has(card.name) || (card.asksForOperator ?? 0) > 0}
              panelFolded={panelFolded} selected={card.name === selected} onOpen={onOpenAgent} />
          ))}
        </ul>
      )}
    </div>
  )
}

/** One agent: three columns of three lines (a fourth with the panel folded).
 * What the list is grouped by is not repeated in the row. A field the API does
 * not carry is left out, never invented. */
function AgentRow({ card, by, stats, waits, panelFolded, selected, onOpen }: {
  card: AgentCard
  by: GroupBy
  stats: ReadonlyMap<string, AgentStatsRow> | null
  waits: boolean
  panelFolded: boolean
  selected: boolean
  onOpen: (card: AgentCard) => void
}) {
  const state = stateClass(card.state)
  const name = agentName(card)
  const model = `${harnessLabel(card.harness)} · ${card.model || "model n/a"}`
  const row = stats?.get(card.name) ?? null
  const today = row?.tok24h?.all ?? null
  const repo = by === "repo" ? "" : card.project
  return (
    <li className={"agents__row" + (state === "offline" ? " agents__row--offline" : "")
        + (selected ? " agents__row--selected" : "") + (panelFolded ? " agents__row--wide" : "")}
      data-agent={card.name} onClick={() => onOpen(card)}>
      <div className="agents__who">
        <span className="agents__name">
          <button type="button" className="linkish mono agents__clip" title={nameTitle(card)}
            aria-current={selected ? "true" : undefined}
            onClick={(e) => { e.stopPropagation(); onOpen(card) }}>
            {name.text}
          </button>
          {name.shared && <SharedMark />}
          {card.crown && <span className="crown"><CrownIcon label="coordinator" /></span>}
          {waits && <span className="chip chip--for-me">waits on you</span>}
        </span>
        <span className="agents__model agents__clip" title={model}>{model}</span>
        {(card.ctx !== null || today !== null) && (
          <span className="agents__ctxline">
            {card.ctx !== null && <Context ctx={card.ctx} />}
            {today !== null && <span className="muted" title={`${today} tokens in the last 24 hours`}>
              {tokenCount(today)} today</span>}
          </span>
        )}
      </div>
      <div className="agents__status" title={`${card.state}${waits ? " · waits on you" : ""}`}>
        {by !== "status" && <span className={`pill pill--${state}`}>{card.state}</span>}
        <span className="muted small">
          {card.lastPostAt ? <>last post {since(card.lastPostAt)}</>
            : <span title={NO_LAST_POST}>last post {EMPTY}</span>}
        </span>
      </div>
      <div className="agents__work">
        {(repo || card.branch || card.pr) && (
          <span className="agents__where mono small muted"
            title={[repo, card.branch, card.pr ? `#${card.pr.number}` : ""].filter(Boolean).join(" · ")}>
            {repo && <span className="agents__clip">{repo}</span>}
            {card.branch && <span className="agents__clip">{card.branch}</span>}
            {card.pr && <span className="chip chip--pr">#{card.pr.number}</span>}
          </span>
        )}
        {card.task
          ? <span className="agents__task-text" title={card.task}>{card.task}</span>
          : <span className="muted">{EMPTY}</span>}
      </div>
      {panelFolded && (
        <div className="agents__today small muted">
          {row && (row.posts24h !== null || row.tools24h !== null) && (
            <span title="posts and tool calls of the last 24 hours">
              {row.posts24h ?? "n/a"} posts · {row.tools24h ?? "n/a"} tool calls</span>
          )}
          {by !== "owner" && card.for && <span title="the responsible human">{card.for}</span>}
        </div>
      )}
    </li>
  )
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
 * every place that names an agent beside a decision (asks, the panel,
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
