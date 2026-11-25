import type { Ask } from "../api/types"
import { since } from "../clock"
import "./Asks.css"

/** The asks with "for me" (the strongest mark, in every view) and "I may
 * answer" (weaker), asks older than 24 h shown as expired (a hint), those of
 * an agent without fresh presence as "agent gone" (RT-6.43 step 6).
 * Read-only: an ask is answered in its thread in the chat; the console shows
 * it until the agent ends it. The title is plain text, never markup. */
export function Asks({ asks, onOpen }: {
  asks: Ask[]
  onOpen: (ask: Ask) => void
}) {
  const rows = [...asks].sort((a, b) =>
    Number(b.forMe) - Number(a.forMe) || a.at - b.at)
  // No operator anchor yet (RT-6.43 step 6): "for me" is n/a, never guessed from a name.
  const unmarked = rows.some((a) => a.forMe === null)
  return (
    <div className="asks">
      <div className="asks__note">
        Read-only: an ask is answered in its thread in the chat; the console
        shows it until the agent ends it (ADR-015 stage 1).
        {unmarked && <> "For me" is n/a until your own credential on the bus gives your anchor.</>}
      </div>
      <table className="asks__table">
        <thead>
          <tr>
            <th>for me</th>
            <th>need</th>
            <th>by</th>
            <th>asked human</th>
            <th>kind</th>
            <th>age</th>
            <th>state</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((ask) => (
            <tr key={ask.id} className="asks__row" onClick={() => onOpen(ask)}>
              <td>
                {ask.forMe === null && <span title="no operator anchor until the login's own record has arrived">n/a</span>}
                {ask.forMe && <span className="chip chip--for-me" title="asked human: you">for me</span>}
                {!ask.forMe && ask.mayAnswer &&
                  <span className="chip chip--may-answer" title="an agent whose owner or trusts name you">may answer</span>}
              </td>
              <td className="asks__need">{ask.need}</td>
              <td title={`${ask.by} · ~${ask.home}`}>{ask.displayName}</td>
              <td>{ask.human ?? "–"}</td>
              <td>{ask.kind}</td>
              <td>{since(ask.at)}</td>
              <td>
                {ask.end === "expired"
                  ? <span className="chip chip--expired" title="older than 24 h, a yes approves nothing">expired</span>
                  : ask.end === "open" ? <span className="chip chip--open">open</span>
                  : ask.end }
                {ask.agentGone && <span className="chip chip--expired" title="no fresh presence of the asking agent">agent gone</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
