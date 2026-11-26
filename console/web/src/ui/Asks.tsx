import type { AgentCard, Ask } from "../api/types"
import { askerName } from "./agents"
import { since } from "../clock"
import "./Asks.css"

/** The asks with "for you" (the strongest mark, in every view) and "I may
 * answer" (weaker), asks older than 24 h shown as expired (a hint), those of
 * an agent without fresh presence as "agent gone" (RT-6.43 step 6).
 * Read-only: an ask is answered in its thread in the chat; the console shows
 * it until the agent ends it. The title is plain text, never markup. */
export function Asks({ asks, cards = [], onOpen }: {
  asks: Ask[]
  /** The fleet: an ask shows its agent's name as the list does (#199). */
  cards?: readonly AgentCard[]
  onOpen: (ask: Ask) => void
}) {
  const rows = [...asks].sort((a, b) =>
    Number(b.forMe) - Number(a.forMe) || a.at - b.at)
  // No operator anchor yet (RT-6.43 step 6): "for you" is n/a, never guessed from a name.
  const unmarked = rows.some((a) => a.forMe === null)
  const open = rows.filter((a) => a.end === "open" || a.end === "expired").length
  return (
    <>
      <div className="page-head">
        <div className="page-head__title">
          <h1>Asks</h1>
          <span className="page-head__sub">
            {open} open · read-only: an ask is answered in its thread in the chat;
            the console shows it until the agent ends it (ADR-015 stage 1).
            {unmarked && <> "For you" is n/a until your own credential on the bus gives your anchor.</>}
          </span>
        </div>
      </div>
      <section className="panel asks" aria-label="Asks">
        <table className="asks__table">
          <thead>
            <tr>
              <th scope="col">for you</th>
              <th scope="col">need</th>
              <th scope="col">by</th>
              <th scope="col">asked human</th>
              <th scope="col">kind</th>
              <th scope="col" className="num">age</th>
              <th scope="col">state</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((ask) => (
              <tr key={ask.id} className="asks__row" onClick={() => onOpen(ask)}>
                <td>
                  {ask.forMe === null && <span className="muted" title="no operator anchor until the login's own record has arrived">n/a</span>}
                  {ask.forMe && <span className="chip chip--for-me" title="asked human: you">for you</span>}
                  {!ask.forMe && ask.mayAnswer &&
                    <span className="chip chip--may-answer" title="an agent whose owner or trusts name you">may answer</span>}
                </td>
                <td className="asks__need">
                  <button type="button" className="linkish"
                    onClick={(e) => { e.stopPropagation(); onOpen(ask) }}>{ask.need}</button>
                </td>
                <td className="mono small" title={`${ask.by} · ${ask.home}`}>{askerName(ask, cards).text}</td>
                <td>{ask.human ?? "–"}</td>
                <td>{ask.kind}</td>
                <td className="num muted">{since(ask.at)}</td>
                <td className="asks__state">
                  {ask.end === "expired"
                    ? <span className="chip chip--expired" title="older than 24 h, a yes approves nothing">expired</span>
                    : ask.end === "open" ? <span className="chip chip--open">open</span>
                    : ask.end}
                  {ask.agentGone && <span className="chip chip--expired" title="no fresh presence of the asking agent">agent gone</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {rows.length === 0 && <p className="asks__empty">No ask is open.</p>}
      </section>
    </>
  )
}
