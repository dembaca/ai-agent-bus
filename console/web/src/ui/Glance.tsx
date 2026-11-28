import { ChevronsLeft, ChevronsRight } from "lucide-react"
import type { AgentCard, Ask, GroupsAnswer } from "../api/types"
import { since } from "../clock"
import { askerName, openAsksFirst } from "./agents"
import { AgentLabel, StateBar } from "./AgentList"
import "./Glance.css"

const STATUS_NAMES: ReadonlyArray<[string, string]> = [
  ["working", "Working"], ["listening", "Listening"], ["idle", "Idle"],
  ["offline", "Offline"], ["other", "Other"],
]

/** The panel "At a glance" on the left of the agents list (#283): four tiles
 * with the operator's own number large and the flock's small, "Needs you" and
 * "Flock by status". Every number is the server's (`GET /v1/groups`); the
 * browser counts nothing over the cards. Folded it is a narrow rail with three
 * numbers: the asks for you, the agents reachable and those working. */
export function Glance({ answer, asks, cards, folded, onFold, onOpenAgent, onOpenAsk }: {
  answer: GroupsAnswer | null
  asks: Ask[]
  cards: AgentCard[]
  folded: boolean
  onFold: (folded: boolean) => void
  onOpenAgent: (card: AgentCard) => void
  onOpenAsk: (ask: Ask) => void
}) {
  const mine = answer?.glance.mine
  const flock = answer?.glance.flock
  if (folded) {
    return (
      <aside className="glance glance--rail" aria-label="At a glance, folded">
        <button type="button" className="glance__fold" aria-label="Unfold the panel At a glance"
          title="Unfold the panel" onClick={() => onFold(false)}>
          <ChevronsRight size={15} aria-hidden="true" />
        </button>
        <Rail value={mine?.asksForYou} label="for you" hot />
        <Rail value={mine?.reachable} label="reachable" />
        <Rail value={mine?.working} label="working" />
      </aside>
    )
  }
  const needs = openAsksFirst(asks).filter((a) => a.forMe === true || a.mayAnswer === true)
  return (
    <aside className="glance" aria-label="At a glance">
      <div className="glance__head">
        <h2>At a glance</h2>
        <button type="button" className="glance__fold" aria-label="Fold the panel At a glance"
          title="Fold the panel" onClick={() => onFold(true)}>
          <ChevronsLeft size={15} aria-hidden="true" />
        </button>
      </div>
      <div className="glance__tiles">
        <Tile label="Reachable" mine={mine?.reachable} flock={flock?.reachable} />
        <Tile label="Working now" mine={mine?.working} flock={flock?.working} />
        <Tile label="For you" mine={mine?.asksForYou} flock={flock?.asksForYou} hot />
        <Tile label="You may answer" mine={mine?.asksMayAnswer} flock={flock?.asksMayAnswer} />
      </div>

      <section className="glance__block" aria-label="Needs you">
        <h3>Needs you</h3>
        {needs.length === 0
          ? <p className="muted small">Nothing waits on you.</p>
          : (
            <ul className="glance__needs">
              {needs.map((ask) => {
                const card = cards.find((c) => c.name === ask.by)
                return (
                  <li key={ask.id}>
                    <button type="button" className="glance__need"
                      onClick={() => (card ? onOpenAgent(card) : onOpenAsk(ask))}>
                      <span className="glance__need-text">{ask.need}</span>
                      <span className="glance__need-by muted small">
                        <AgentLabel name={askerName(ask, cards)} /> · {since(ask.at)} ago
                        {" "}{ask.forMe ? <span className="chip chip--for-me">for you</span>
                          : <span className="chip chip--may-answer">may answer</span>}
                      </span>
                    </button>
                  </li>
                )
              })}
            </ul>
          )}
      </section>

      <section className="glance__block" aria-label="Flock by status">
        <h3>Flock by status</h3>
        {flock
          ? (
            <>
              <StateBar states={flock.states} total={flock.total} />
              <ul className="glance__legend small">
                {STATUS_NAMES.filter(([k]) => k !== "other" || (flock.states.other ?? 0) > 0).map(([k, name]) => (
                  <li key={k}><i className={`statebar__seg statebar__seg--${k}`} aria-hidden="true" />
                    {name} <span className="muted">{flock.states[k] ?? 0}</span></li>
                ))}
              </ul>
            </>
          )
          : <p className="muted small">reading the flock…</p>}
      </section>
    </aside>
  )
}

/** The operator's own number large, the flock's small; "For you" is lit only
 * above zero. A number not read yet is a dash, never 0. */
function Tile({ label, mine, flock, hot = false }: {
  label: string
  mine: number | undefined
  flock: number | undefined
  hot?: boolean
}) {
  const lit = hot && (mine ?? 0) > 0
  return (
    <div className={"tile" + (lit ? " tile--hot" : "")}>
      <span className="tile__label">{label}</span>
      <span className="tile__mine" title="yours">{mine ?? "–"}</span>
      <span className="tile__flock muted small" title="the whole flock">flock {flock ?? "–"}</span>
    </div>
  )
}

function Rail({ value, label, hot = false }: { value: number | undefined; label: string; hot?: boolean }) {
  return (
    <div className={"rail" + (hot && (value ?? 0) > 0 ? " rail--hot" : "")}>
      <span className="rail__n">{value ?? "–"}</span>
      <span className="rail__label">{label}</span>
    </div>
  )
}
