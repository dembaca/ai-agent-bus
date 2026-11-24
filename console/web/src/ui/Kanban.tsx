import type { View } from "./Sidebar"
import { AGENT_STATES, type AgentCard } from "../api/types"
import { HARNESS_LABEL } from "../api/types"
import { since } from "../clock"
import "./Kanban.css"

/** The Kanban: one column per state word, the crown on the card, not the
 * column (ADR-015 point 6). */
export function Kanban({ cards, selected, onSelect }: {
  cards: AgentCard[]
  selected: AgentCard | null
  onSelect: (card: AgentCard) => void
}) {
  return (
    <div className="kanban">
      {AGENT_STATES.map((state) => (
        <section key={state} className="kanban__column" data-state={state}>
          <header className="kanban__head">
            <span className="kanban__state">{state}</span>
            <span className="kanban__count">
              {cards.filter((c) => c.state === state).length}
            </span>
          </header>
          <div className="kanban__cards">
            {cards.filter((c) => c.state === state).map((card) => (
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
  const mark = (text: string, extra?: string) => (
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
        {card.crown && <span className="card__crown" title="the crown of its channel">👑</span>}
        <span className={"logo logo--" + card.harness} title={HARNESS_LABEL[card.harness]}
          aria-hidden="true">{logoGlyph(card.harness)}</span>
        <span className="card__name" title={card.name}>{card.name}</span>
        <span className="card__since" title="since the last post">
          {card.lastPostAt ? since(card.lastPostAt) : "n/a"}
        </span>
      </div>
      <div className="card__task">{card.task}</div>
      <div className="card__meta">
        <span className="card__model">{HARNESS_LABEL[card.harness]} · {card.model}</span>
        {card.role === "coordinator" && mark("coordinator")}
        <span className="card__for">
          for {card.for}{card.autonomous && mark("autonomous")}
        </span>
      </div>
      <div className="card__meta">
        <span className="card__project">{card.project}</span>
        {card.pr && mark(`#${card.pr.number}`, "pr")}
        {!card.pr && card.branch && mark(card.branch)}
      </div>
      {card.capabilities.length > 0 && (
        <div className="card__meta card__caps">
          {card.capabilities.slice(0, 3).map((c) => mark(c))}
        </div>
      )}
      <div className="card__bottom">
        <span className="card__asks"
          title={card.asksForOperator > 0 ? "open asks, for you marked" : "open asks"}>
          {card.openAsks > 0 ? `asks ${card.openAsks}` : ""}
          {card.asksForOperator > 0 && <b className="card__asks-for"> · for you {card.asksForOperator}</b>}
        </span>
        <span className="card__ctx" title="context">
          <span className="card__ctx-bar" style={{ width: card.ctx === null ? 0 : `${Math.min(100, card.ctx)}%` }} />
          <em>{card.ctx === null ? "n/a" : `${card.ctx} %`}</em>
        </span>
        <span className="card__version">v{card.version}</span>
      </div>
    </button>
  )
}

/** One glyph per harness, "other" as the fallback (ADR-015 point 6). The
 * real logo comes with the server; the pilot marks the harness by letter. */
function logoGlyph(harness: AgentCard["harness"]): string {
  return HARNESS_LABEL[harness].slice(0, 1)
}
