import type { Ask } from "../api/types"
import { since } from "../clock"
import "./Asks.css"

/** The asks with "for me" (the strongest mark, in every view) and "I may
 * answer" (weaker), asks older than 24 h shown as expired (ADR-015 point 4).
 * Read-only: an ask is answered where it is answered today, in the humans'
 * chat; the console shows it open until the agent ends it. */
export function Asks({ asks, onOpen }: {
  asks: Ask[]
  onOpen: (ask: Ask) => void
}) {
  const rows = [...asks].sort((a, b) =>
    Number(b.forMe) - Number(a.forMe) || a.at - b.at)
  return (
    <div className="asks">
      <div className="asks__note">
        Read-only pilot: an ask is answered in the humans' chat; the console
        shows it open until the agent ends it (ADR-015 stage 1).
      </div>
      <table className="asks__table">
        <thead>
          <tr>
            <th>for me</th>
            <th>need</th>
            <th>by</th>
            <th>asked human</th>
            <th>age</th>
            <th>answers offered</th>
            <th>state</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((ask) => (
            <tr key={ask.id} className="asks__row" onClick={() => onOpen(ask)}>
              <td>
                {ask.forMe && <span className="chip chip--for-me" title="asked human: you">for me</span>}
                {!ask.forMe && ask.mayAnswer &&
                  <span className="chip chip--may-answer" title="an agent whose owner or trusts name you">may answer</span>}
              </td>
              <td className="asks__need">{ask.need}</td>
              <td title={ask.by}>{ask.displayName}</td>
              <td>{ask.human ?? "–"}</td>
              <td>{since(ask.at)}</td>
              <td className="asks__answers">
                {ask.answers.map((a) => <span key={a} className="chip">{a}</span>)}
              </td>
              <td>
                {ask.end === "expired"
                  ? <span className="chip chip--expired" title="older than 24 h, approves nothing">expired</span>
                  : ask.end === "open" ? <span className="chip chip--open">open</span>
                  : ask.end }
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
