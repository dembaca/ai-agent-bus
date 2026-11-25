import { useEffect, useState } from "react"
import { Crown, X } from "lucide-react"
import type { AgentCard, Ask, ConsoleApi, Post, ThreadPage } from "../api/types"
import { HARNESS_LABEL } from "../api/types"
import { clock, since } from "../clock"
import "./Detail.css"

type Fail = (what: string, error: unknown) => void

/** The detail pane (ADR-015 point 3): the full card, the task rows it
 * holds, the session's age, the time since its last post, its HELLO
 * thread and the threads of its open asks. An ask selected in the Asks
 * view shows its thread. Read-only: no answer button (stage 1). */
export function Detail({ api, card, ask, asks, onClose, onFail }: {
  api: ConsoleApi
  card: AgentCard | null
  ask: Ask | null
  asks: Ask[]
  onClose: () => void
  onFail: Fail
}) {
  return (
    <aside className="detail" aria-label="detail">
      <header className="detail__bar">
        <span className="detail__title">
          {card && (<>{card.crown && <Crown size={12} />} {card.displayName}</>)}
          {!card && ask?.displayName}
        </span>
        <button type="button" className="detail__close" onClick={onClose} title="close">
          <X size={16} />
        </button>
      </header>

      {ask && <AskDetail ask={ask} />}

      {card && (
        <div className="detail__body">
          <FullCard card={card} />

          {card.tasks.length > 0 && (
            <section className="detail__tasks">
              <h3>its task rows</h3>
              <table className="detail__task-table">
                <thead>
                  <tr><th>title</th><th>state</th><th>status</th><th>by</th><th>at</th></tr>
                </thead>
                <tbody>
                  {card.tasks.map((row) => (
                    <tr key={row.id}>
                      <td>{row.title}{row.pr && <span className="chip chip--pr">#{row.pr}</span>}</td>
                      <td>{row.state}</td>
                      <td>{row.status}</td>
                      <td title={row.by}>{row.displayName ?? row.by}</td>
                      <td>{clock(row.at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          )}

          {card.helloPostId && (
            <Thread key={card.helloPostId} title="HELLO thread" api={api}
                     onFail={onFail} threadId={card.helloPostId} />
          )}
          {asks.filter((a) => a.by === card.name && (a.end === "open" || a.end === "expired"))
            .map((a) => (
              <Thread key={a.id} title={`ask thread · ${a.need}`} api={api}
                onFail={onFail} threadId={a.threadId} />
            ))}
        </div>
      )}
    </aside>
  )
}

/** The full card, as the detail alone shows it (ADR-015 point 6). */
function FullCard({ card }: { card: AgentCard }) {
  return (
    <table className="detail__table">
      <tbody>
        <tr><th>name</th><td>{card.displayName}</td></tr>
        <tr><th>id on the bus</th><td>{card.name}</td></tr>
        <tr><th>state</th><td>{card.state}</td></tr>
        <tr><th>home</th><td>{card.home}</td></tr>
        <tr><th>harness · model</th>
          <td>{card.harness === null ? "n/a" : HARNESS_LABEL[card.harness]} · {card.model || "n/a"}</td></tr>
        <tr><th>role</th>
          <td className="detail__role">
            {card.role}{card.crown && <span className="detail__crown"><Crown size={11} /></span>}
          </td></tr>
        <tr><th>for</th><td>{card.for}{card.autonomous ? " · autonomous" : ""}</td></tr>
        <tr><th>trusts</th><td>{card.trusts.join(", ")}</td></tr>
        <tr><th>project</th><td>{card.project}</td></tr>
        <tr><th>branch / PR</th>
          <td>
            {card.pr ? `#${card.pr.number} · ` : ""}
            {card.branch === null ? "n/a" : card.branch || "–"}
          </td></tr>
        <tr><th>capabilities</th><td>{card.capabilities.join(", ") || "–"}</td></tr>
        <tr><th>ctx</th><td>{card.ctx === null ? "n/a" : `${card.ctx} %`}</td></tr>
        <tr><th>limits / cost</th><td>{card.limits ?? "n/a"}</td></tr>
        <tr><th>version</th><td>{card.version || "n/a"}</td></tr>
        <tr><th>task</th><td>{card.task || "n/a"}</td></tr>
        <tr><th>HELLO since</th><td>{since(card.helloAt)}</td></tr>
        <tr><th>last post</th>
          <td>
            {card.lastPostAt === null
              ? "n/a"
              : `${clock(card.lastPostAt)} · ${since(card.lastPostAt)} ago`}
          </td></tr>
        <tr><th>open asks</th>
          <td>
            {card.openAsks === null
              ? "n/a"
              : card.openAsks === 0 ? "0"
              : `${card.openAsks}${(card.asksForOperator ?? 0) > 0 ? ` · ${card.asksForOperator} for you` : ""}`}
          </td></tr>
      </tbody>
    </table>
  )
}

/** One ask in the pane: the need and the answers it offers. Read-only:
 * nothing here answers it — that stays in the humans' chat until the
 * owner opens answering (stage 1). */
function AskDetail({ ask }: { ask: Ask }) {
  return (
    <div className="detail__ask">
      <div className="detail__ask-need">{ask.need}</div>
      <div className="detail__ask-answers">
        {ask.answers.map((a) => <span key={a} className="chip">{a}</span>)}
      </div>
      <div className="detail__ask-note">
        {ask.forMe
          ? "One of your open asks; this read-only pilot shows it, nothing else."
          : "Another human's ask, shown read-only."}
      </div>
    </div>
  )
}

/** A thread as the bus has it (GET /v1/threads/<id>): the page the server
 * sends, then any page its cursor names — the whole thread at last. */
function Thread({ title, api, onFail, threadId }: {
  title: string
  api: ConsoleApi
  onFail: Fail
  threadId: string
}) {
  const [pages, setPages] = useState<ThreadPage[]>([])
  useEffect(() => {
    // Another thread than the one shown resets what was read: picking a
    // second card must not pile the first's posts under it.
    setPages([])
    let gone = false
    const load = async (cursor: string | null) => {
      try {
        const page = await api.thread(threadId, cursor)
        if (gone) return
        setPages((all) => [...all, page])
        if (page.next) await load(page.next)
      } catch (error: unknown) {
        if (!gone) onFail("thread", error)
      }
    }
    void load(null)
    return () => { gone = true }
  }, [api, onFail, threadId])
  const posts: Post[] = pages.flatMap((page) => page.posts)
  if (posts.length === 0) return null
  return (
    <section className="thread">
      <h3>{title}</h3>
      {posts.map((post) => (
        <article key={post.id} className="thread__post">
          <header>
            <span>{post.author}</span>
            <span>{clock(post.at)}</span>
          </header>
          <pre>{post.message}</pre>
        </article>
      ))}
    </section>
  )
}
