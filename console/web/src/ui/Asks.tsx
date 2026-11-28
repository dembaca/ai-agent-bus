import { useEffect, useState } from "react"
import type { AgentCard, Ask, ConsoleApi } from "../api/types"
import { askerName } from "./agents"
import { AgentLabel } from "./AgentList"
import { loadThread } from "./threadPosts"
import { ANSWER_DISABLED } from "./ThreadDrawer"
import { since } from "../clock"
import { unread, type Reads } from "../reads"
import "./Asks.css"

const MAX_COUNTED = 50

/** The posts of each ask's thread, read from the API (GET /v1/threads/<id>), again
 * whenever the asks are read again. An ask whose thread cannot be read has no entry:
 * no count, never a guessed 0. */
function useCounts(api: ConsoleApi, asks: Ask[]): Record<string, number> {
  const [counts, setCounts] = useState<Record<string, number>>({})
  useEffect(() => {
    let gone = false
    for (const ask of asks.slice(0, MAX_COUNTED)) {
      loadThread(api, ask.threadId).then((posts) => {
        if (!gone) setCounts((was) => ({ ...was, [ask.id]: posts.length }))
      }).catch(() => { /* no count for this ask until the next read */ })
    }
    return () => { gone = true }
  }, [api, asks])
  return counts
}

/** The red circle with a white number: the posts new since the operator last
 * opened the thread; nothing for none. */
export function NewBadge({ n }: { n: number }) {
  return n > 0 ? <span className="asks__new" aria-label={`${n} new`}>{n}</span> : null
}

/** The answers the approval's buttons would carry: go or no go (RT-6.11 step 3).
 * A question's own answers (their letters A to D) are not on the bus the console
 * reads; with none known, no button is drawn. */
const APPROVAL_ANSWERS = ["go", "no go"]

/** The asks (#285): one row each with "for you" (the strongest mark, in every
 * view) or "may answer" (weaker), the need with agent, home, asked human and kind
 * below it, the age, the state, and the thread button with its post count and the
 * red count of posts new since the operator last opened the thread. A click on the
 * row unfolds it: the need's decision, the answer box and "Chat with agent". Asks
 * older than 24 h are expired; those of an agent without fresh presence "agent
 * gone" (RT-6.43 step 6). Everything that writes is disabled (ADR-015, addition of
 * 2026-11-28): the answer buttons say "answer in the chat for now". The need is
 * plain text, never markup. */
export function Asks({ api, asks, cards = [], reads = {}, onThread }: {
  api: ConsoleApi
  asks: Ask[]
  /** The flock: an ask shows its agent's name as the list does (#199). */
  cards?: readonly AgentCard[]
  /** Per ask, the posts the operator had seen when the thread was last open. */
  reads?: Reads
  onThread: (ask: Ask) => void
}) {
  const rows = [...asks].sort((a, b) =>
    Number(b.forMe) - Number(a.forMe) || a.at - b.at)
  const counts = useCounts(api, asks)
  const [unfolded, setUnfolded] = useState<ReadonlySet<string>>(new Set())
  const toggle = (id: string) => setUnfolded((was) => {
    const next = new Set(was)
    if (!next.delete(id)) next.add(id)
    return next
  })
  // No operator anchor yet (RT-6.43 step 6): "for you" is n/a, never guessed from a name.
  const unmarked = rows.some((a) => a.forMe === null)
  const open = rows.filter((a) => a.end === "open" || a.end === "expired").length
  return (
    <>
      <div className="page-head">
        <div className="page-head__title">
          <h1>Asks</h1>
          <span className="page-head__sub">
            {open} open · answering is disabled for now: answer in the chat; the console
            shows an ask until the agent ends it (ADR-015, until the human security review).
            {unmarked && <> "For you" is n/a until your own credential on the bus gives your anchor.</>}
          </span>
        </div>
      </div>
      <section className="panel asks" aria-label="Asks">
        <ul className="asks__list">
          {rows.map((ask) => {
            const isOpen = unfolded.has(ask.id)
            const total = counts[ask.id]
            const fresh = total === undefined ? 0 : unread(total, reads, ask.id)
            return (
              <li key={ask.id} className="asks__item">
                <div className="asks__row" role="button" tabIndex={0} aria-expanded={isOpen}
                  onClick={() => toggle(ask.id)}
                  onKeyDown={(e) => {
                    if (e.target === e.currentTarget && (e.key === "Enter" || e.key === " ")) {
                      e.preventDefault()
                      toggle(ask.id)
                    }
                  }}>
                  <span className="asks__chip">
                    {ask.forMe === null && <span className="muted" title="no operator anchor until the login's own record has arrived">n/a</span>}
                    {ask.forMe && <span className="chip chip--for-me" title="asked human: you">for you</span>}
                    {!ask.forMe && ask.mayAnswer &&
                      <span className="chip chip--may-answer" title="an agent whose owner or trusts name you">may answer</span>}
                  </span>
                  <span className="asks__main">
                    <span className="asks__need">{ask.need}</span>
                    <span className="asks__meta small muted">
                      <AgentLabel name={askerName(ask, cards)} /> · {ask.home} · asked{" "}
                      {ask.human ?? "no listed human"} · {ask.kind}
                    </span>
                  </span>
                  <span className="asks__age num muted">{since(ask.at)}</span>
                  <span className="asks__state">
                    {ask.end === "expired"
                      ? <span className="chip chip--expired" title="older than 24 h, a yes approves nothing">expired</span>
                      : ask.end === "open" ? <span className="chip chip--open">open</span>
                      : ask.end}
                    {ask.agentGone && <span className="chip chip--expired" title="no fresh presence of the asking agent">agent gone</span>}
                  </span>
                  <button type="button" className="asks__thread" title="Open the thread"
                    aria-label={`Thread, ${total ?? "n/a"} posts, ${fresh} new`}
                    onClick={(e) => { e.stopPropagation(); onThread(ask) }}>
                    {total ?? "–"} posts
                    <NewBadge n={fresh} />
                  </button>
                </div>
                {isOpen && <Unfolded ask={ask} fresh={fresh} onThread={onThread} />}
              </li>
            )
          })}
        </ul>
        {rows.length === 0 && <p className="asks__empty">No ask is open.</p>}
      </section>
    </>
  )
}

/** What the unfolded row shows of an ask. The API sends the need only: the
 * context, the recommendation, where, and the agent's own answers with their
 * letters are not on the bus the console reads, so none is drawn (never invented).
 * The answer box is shown to the asked human and to those the asking agent trusts
 * ("may answer"); everybody else reads. Its buttons are disabled. */
function Unfolded({ ask, fresh, onThread }: {
  ask: Ask
  fresh: number
  onThread: (ask: Ask) => void
}) {
  const mine = ask.forMe === true || ask.mayAnswer === true
  const expired = ask.end === "expired"
  const answered = ask.end === "answered"
  return (
    <div className="asks__unfolded">
      <div className="asks__unfolded-main">
        {mine && !answered && (
          <div className="asks__box" aria-label="Answer">
            {ask.kind === "approval" && (
              <p className="asks__counts">
                This counts as your approval of: <strong>{ask.need}</strong>
              </p>
            )}
            {!ask.forMe && ask.human &&
              <p className="muted small">Meant for {ask.human}; you may answer because the asking agent trusts you.</p>}
            {ask.kind === "approval" && !expired && (
              <div className="asks__answers asks__answers--side">
                {APPROVAL_ANSWERS.map((word) => (
                  <button key={word} type="button" disabled title={ANSWER_DISABLED}>{word}</button>
                ))}
              </div>
            )}
            <p className="muted small asks__why">
              {expired ? "Expired: older than 24 hours, an answer approves nothing."
                : `Disabled: ${ANSWER_DISABLED}.`}
            </p>
          </div>
        )}
        {answered && <p className="muted">Answered; the answer stands in the thread.</p>}
        {!mine && <p className="muted">This ask is not meant for you; you read it.</p>}
      </div>
      <button type="button" className="asks__chat" onClick={() => onThread(ask)}>
        Chat with agent <NewBadge n={fresh} />
      </button>
    </div>
  )
}
