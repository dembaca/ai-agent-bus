import { useEffect, useState } from "react"
import type { AgentCard, Ask, ConsoleApi, Operator } from "../api/types"
import { askerName } from "./agents"
import { AgentLabel } from "./AgentList"
import { loadThread } from "./threadPosts"
import { ANSWER_DISABLED } from "./ThreadDrawer"
import { since } from "../clock"
import { repliesByOthers, unread, type Reads } from "../reads"
import { SpeechBubbleIcon } from "./icons"
import "./Asks.css"

const MAX_COUNTED = 50

/** What the console counts of an ask's thread: all its posts (the small button's
 * number) and the replies by others (the red number's base). */
interface Counts { total: number; others: number }

/** The posts of each ask's thread, read from the API (GET /v1/threads/<id>), again
 * whenever the asks are read again. An ask whose thread cannot be read has no entry:
 * no count, never a guessed 0. */
function useCounts(api: ConsoleApi, asks: Ask[], operator: Operator | null): Record<string, Counts> {
  const [counts, setCounts] = useState<Record<string, Counts>>({})
  useEffect(() => {
    let gone = false
    for (const ask of asks.slice(0, MAX_COUNTED)) {
      loadThread(api, ask.threadId).then((posts) => {
        if (gone) return
        const row = { total: posts.length, others: repliesByOthers(posts, ask.threadId, operator) }
        setCounts((was) => ({ ...was, [ask.id]: row }))
      }).catch(() => { /* no count for this ask until the next read */ })
    }
    return () => { gone = true }
  }, [api, asks, operator])
  return counts
}

/** The red circle with a white number: the posts new since the operator last
 * opened the thread; nothing for none. */
export function NewBadge({ n }: { n: number }) {
  return n > 0 ? <span className="asks__new" aria-label={`${n} new`}>{n}</span> : null
}

/** An approval that was posted with no answers of its own reads go / no-go
 * (RT-6.11); a question with none has no fixed answer, so none is drawn. The
 * console never makes up answers. */
const APPROVAL_ANSWERS = ["go", "no-go"]
const KEYS = ["A", "B", "C", "D"]

export function answersOf(ask: Ask): string[] {
  if (ask.answers && ask.answers.length > 0) return ask.answers
  return ask.kind === "approval" ? APPROVAL_ANSWERS : []
}

/** The state column after an answer: "answered: go" for two answers (the word),
 * "answered: A" for more (the key cap), "answered" when the answer was a reply or
 * the bus did not say which one. */
export function answeredText(ask: Ask): string {
  const list = answersOf(ask)
  const at = ask.chosen == null ? -1 : list.indexOf(ask.chosen)
  if (at < 0) return "answered"
  return list.length > 2 ? `answered: ${KEYS[at]}` : `answered: ${list[at]}`
}

/** The asks (#285): one row each with "for you" (the strongest mark, in every
 * view) or "may answer" (weaker), the need with agent, home, asked human and kind
 * below it, the age, the state ("answered: go" replaces it once answered), and the
 * thread button with its post count and the red count of replies by others since
 * the operator last opened the thread. A click on the
 * row unfolds it: the need's decision, the answer box and "Chat with agent". Asks
 * older than 24 h are expired; those of an agent without fresh presence "agent
 * gone" (RT-6.43 step 6). Everything that writes is disabled (ADR-015, addition of
 * 2026-11-28): the answer buttons say "answer in the chat for now". The need is
 * plain text, never markup. */
export function Asks({ api, asks, cards = [], reads = {}, operator = null, onThread }: {
  api: ConsoleApi
  asks: Ask[]
  /** The flock: an ask shows its agent's name as the list does (#199). */
  cards?: readonly AgentCard[]
  /** Per ask, the posts the operator had seen when the thread was last open. */
  reads?: Reads
  /** Who is looking: their own posts in a thread never count as new. */
  operator?: Operator | null
  onThread: (ask: Ask) => void
}) {
  const rows = [...asks].sort((a, b) =>
    Number(b.forMe) - Number(a.forMe) || a.at - b.at)
  const counts = useCounts(api, asks, operator)
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
            const count = counts[ask.id]
            const total = count?.total
            const fresh = count === undefined ? 0 : unread(count.others, reads, ask.id)
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
                      : answeredText(ask)}
                    {ask.agentGone && <span className="chip chip--expired" title="no fresh presence of the asking agent">agent gone</span>}
                  </span>
                  <button type="button" className="asks__thread" title="Open the thread"
                    aria-label={`Thread, ${total ?? "n/a"} posts, ${fresh} new`}
                    onClick={(e) => { e.stopPropagation(); onThread(ask) }}>
                    <SpeechBubbleIcon />
                    <span className="num">{total ?? "–"}</span>
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

/** What the unfolded row shows of an ask: the need's decision and the ask's own
 * answers, as the bus carries them. The API sends no context, recommendation or
 * "where" (the ask record holds a title, never the ask's body) and, until a record
 * change is decided separately (#285), none of the ask's own answers either, so
 * none is drawn, and no caption stands over nothing. The answer box is shown to the
 * asked human and to those the asking agent trusts ("may answer"); everybody else
 * reads, and sees the answers as plain text. The box's buttons are disabled. */
export function Unfolded({ ask, fresh, onThread }: {
  ask: Ask
  fresh: number
  onThread: (ask: Ask) => void
}) {
  const mine = ask.forMe === true || ask.mayAnswer === true
  const expired = ask.end === "expired"
  const answered = ask.end === "answered"
  const list = answersOf(ask)
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
            {!expired && list.length > 0 && <AnswerButtons answers={list} />}
            <p className="muted small asks__why">
              {expired ? "Expired: older than 24 hours, an answer approves nothing."
                : `Disabled: ${ANSWER_DISABLED}.`}
            </p>
          </div>
        )}
        {(!mine || answered) && list.length > 0 && <AnswersText ask={ask} answers={list} />}
        {answered && <p className="muted">{answeredText(ask)}; the answer stands in the thread.</p>}
        {!mine && <p className="muted">This ask is not meant for you; you read it.</p>}
      </div>
      <button type="button" className="asks__chat" onClick={() => onThread(ask)}>
        Chat with agent <NewBadge n={fresh} />
      </button>
    </div>
  )
}

/** Exactly two answers stand side by side, without letters; more stand below each
 * other with a key cap A to D and the full text. Always disabled. */
function AnswerButtons({ answers }: { answers: string[] }) {
  if (answers.length === 2) {
    return (
      <div className="asks__answers asks__answers--side">
        {answers.map((a) => <button key={a} type="button" disabled title={ANSWER_DISABLED}>{a}</button>)}
      </div>
    )
  }
  return (
    <div className="asks__answers asks__answers--stack">
      {answers.map((a, i) => (
        <button key={a} type="button" disabled title={ANSWER_DISABLED}>
          <kbd className="asks__key">{KEYS[i]}</kbd><span>{a}</span>
        </button>
      ))}
    </div>
  )
}

/** The answers as plain text, for a reader who may not answer and for an answered
 * ask; the picked one in bold. */
function AnswersText({ ask, answers }: { ask: Ask; answers: string[] }) {
  return (
    <div className="asks__plain">
      <p className="small muted">Answers</p>
      <ul>
        {answers.map((a, i) => (
          <li key={a}>
            {answers.length > 2 && <kbd className="asks__key">{KEYS[i]}</kbd>}
            {a === ask.chosen ? <strong>{a}</strong> : a}
          </li>
        ))}
      </ul>
    </div>
  )
}
