import { Crown } from "lucide-react"
import type { View } from "./Sidebar"
import { AGENT_STATES, type AgentCard, type AgentState } from "../api/types"
import { HARNESS_LABEL } from "../api/types"
import { since } from "../clock"
import "./Kanban.css"

/** The Kanban: one column per state word, the crown on the card, not the
 * column (ADR-015 point 6). Presence's format v1 says `online`/`away`
 * until it carries the state words, and a state the console does not know
 * lands in the last, catch-all column (Fable's point 4). */
export function Kanban({ cards, selected, onSelect }: {
  cards: AgentCard[]
  selected: AgentCard | null
  onSelect: (card: AgentCard) => void
}) {
  const known = AGENT_STATES.filter((state) => cards.some((c) => c.state === state))
  const knownSet = AGENT_STATES as readonly string[]
  const others: AgentCard[] = cards.filter((c) => !knownSet.includes(c.state))
  const columns: Array<AgentState | "other"> = known.length > 0 ? [...known] : [...AGENT_STATES]
  if (others.length > 0) columns.push("other")
  const inColumn = (state: AgentState | "other"): AgentCard[] =>
    state === "other" ? others : cards.filter((c) => c.state === state)
  return (
    <div className="kanban">
      {columns.map((state) => (
        <section key={state} className="kanban__column" data-state={state}>
          <header className="kanban__head">
            <span className="kanban__state">{state === "other" ? "other" : state}</span>
            <span className="kanban__count">{inColumn(state).length}</span>
          </header>
          <div className="kanban__cards">
            {inColumn(state).map((card) => (
              <AgentCardView
                key={card.name}
                card={card}
                active={selected?.name === card.name}
                onSelect={onSelect}
              />
            ))}
          </div>
        </section>
      ))}
    </div>
  )
}

/** What sits on the card itself (ADR-015 point 6): name, task, role, for
 * (with "autonomous"), trusts, project, branch or PR, the first
 * capabilities, harness logo and model; the crown; ctx, the limits or the
 * cost; the version; the number of its open asks, those for the operator
 * marked. */
export function AgentCardView({ card, active, onSelect }: {
  card: AgentCard
  active: boolean
  onSelect: (card: AgentCard) => void
}) {
  const chip = (text: string, extra?: string) => (
    <span className={"chip" + (extra ? ` chip--${extra}` : "")}>{text}</span>
  )
  return (
    <button
      type="button"
      data-state={card.state}
      className={"card" + (active ? " card--active" : "")}
      aria-pressed={active}
      onClick={() => onSelect(card)}
    >
      <div className="card__top">
        {card.crown && <span className="card__crown" title="the crown of its channel">
          <Crown size={12} />
        </span>}
        <span className={"logo logo--" + card.harness} title={HARNESS_LABEL[card.harness]}
          aria-hidden="true">{logoGlyph(card.harness)}</span>
        <span className="card__name" title={card.name}>{card.displayName}</span>
        <span className="card__since" title="since the last post">
          {card.lastPostAt ? since(card.lastPostAt) : "n/a"}
        </span>
      </div>
      <div className="card__task">{card.task}</div>
      <div className="card__meta">
        <span className="card__model">
          {[HARNESS_LABEL[card.harness], card.model].filter(Boolean).join(" · ")}
        </span>
        {card.role === "coordinator" && chip("coordinator")}
        <span className="card__for">
          for {card.for}{card.autonomous && chip("autonomous")}
        </span>
      </div>
      <div className="card__meta">
        <span className="card__project">{card.project}</span>
        {card.pr && chip(`#${card.pr.number}`, "pr")}
        {!card.pr && card.branch && chip(card.branch)}
      </div>
      {card.capabilities.length > 0 && (
        <div className="card__meta card__caps">
          {card.capabilities.slice(0, 3).map((c) => chip(c))}
        </div>
      )}
      <div className="card__bottom">
        <span className="card__asks"
          title={card.asksForOperator ? "open asks, for you marked" : "open asks"}>
          {card.openAsks === null
            ? "asks n/a"
            : `asks ${card.openAsks}`}
          {card.asksForOperator !== null && card.asksForOperator > 0 &&
            <b className="card__asks-for"> · for you {card.asksForOperator}</b>}
        </span>
        <span className="card__ctx" title="context">
          <span className="card__ctx-bar" style={{ width: card.ctx === null ? 0 : `${Math.min(100, card.ctx)}%` }} />
          <em>{card.ctx === null ? "n/a" : `${card.ctx} %`}</em>
        </span>
        <span className="card__version">{card.version ? `v${card.version}` : "n/a"}</span>
      </div>
    </button>
  )
}

/** One glyph per harness, "other" as the fallback (ADR-015 point 6). The
 * real logo comes with the server; the pilot marks the harness by letter. */
function logoGlyph(harness: AgentCard["harness"]): string {
  return HARNESS_LABEL[harness].slice(0, 1)
}
