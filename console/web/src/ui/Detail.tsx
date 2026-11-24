import { useEffect, useState } from "react"
import { X } from "lucide-react"
import type { AgentCard, Ask, ConsoleApi, Post } from "../api/types"
import { HARNESS_LABEL } from "../api/types"
import { clock, since } from "../clock"
import "./Detail.css"

/** The detail pane (ADR-015 point 3): the full card, the task rows it
 * holds, the session's age, the time since its last post, its HELLO
 * thread and the threads of its open asks. An ask selected in the Asks
 * view shows its thread. Read-only: no answer button (stage 1). */
export function Detail({ api, card, ask, asks, onClose }: {
  api: ConsoleApi
  card: AgentCard | null
  ask: Ask | null
  asks: Ask[]
  onClose: () => void
}) {
  const helloId = card ? `hello-${card.name}` : null
  const [hello, setHello] = useState<Post[]>([])
  useEffect(() => {
    if (!helloId) { setHello([]); return }
    let gone = false
    api.thread(helloId).then((page) => { if (!gone) setHello(page.posts) }).catch(() => {})
    return () => { gone = true }
  }, [api, helloId])

  const openAsks = card
    ? asks.filter((a) => a.by === card.name && (a.end === "open" || a.end === "expired"))
    : []

  return (
    <aside className="detail" aria-label="detail">
      <header className="detail__bar">
        <span className="detail__title">
          {card && (card.crown ? `👑 ${card.name}` : card.name)}
          {!card && ask?.id}
        </span>
        <button type="button" className="detail__close" onClick={onClose} title="close">
          <X size={16} />
        </button>
      </header>

      {ask && <AskDetail ask={ask} api={api} />}

      {card && (
        <div className="detail__body">
          <table className="detail__table">
            <tbody>
              <tr><th>state</th><td>{card.state}</td></tr>
              <tr><th>home</th><td>{card.home}</td></tr>
              <tr><th>harness · model</th>
                <td>{HARNESS_LABEL[card.harness]} · {card.model}</td></tr>
              <tr><th>role</th><td>{card.role}{card.crown && " · 👑"}</td></tr>
              <tr><th>for</th><td>{card.for}{card.autonomous ? " · autonomous" : ""}</td></tr>
              <tr><th>trusts</th><td>{card.trusts.join(", ")}</td></tr>
              <tr><th>project</th><td>{card.project}</td></tr>
              <tr><th>branch / PR</th>
                <td>
                  {card.pr ? `#${card.pr.number} · ${card.branch ?? "–"}` : card.branch ?? "–"}
                </td></tr>
              <tr><th>capabilities</th><td>{card.capabilities.join(", ") || "–"}</td></tr>
              <tr><th>ctx</th><td>{card.ctx === null ? "n/a" : `${card.ctx} %`}</td></tr>
              <tr><th>limits / cost</th><td>{card.limits ?? "n/a"}</td></tr>
              <tr><th>version</th><td>{card.version}</td></tr>
              <tr><th>task</th><td>{card.task}</td></tr>
              <tr><th>HELO since</th><td>{since(card.helloAt)}</td></tr>
              <tr><th>last post</th>
                <td>
                  {card.lastPostAt === null
                    ? "n/a"
                    : `${clock(card.lastPostAt)} · ${since(card.lastPostAt)} ago`}
                </td></tr>
              <tr><th>open asks</th>
                <td>
                  {card.openAsks === 0 ? "0"
                    : `${card.openAsks}${card.asksForOperator > 0 ? ` · ${card.asksForOperator} for you` : ""}`}
                </td></tr>
            </tbody>
          </table>

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
                      <td>{row.by.replace(/^agent-mac-/, "")}</td>
                      <td>{clock(row.at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          )}

          {hello.length > 0 && <Thread title="HELLO thread" posts={hello} api={api} />}
          {openAsks.map((a) => (
            <Thread key={a.id} title={`ask thread · ${a.need}`} posts={null}
              api={api} threadId={a.threadId} />
          ))}
        </div>
      )}
    </aside>
  )
}

/** One ask in the pane: the need, the answers, its thread. The answers are
 * shown, not clicked: they say what a reaction would approve
 * ("counts as your approval") and what a post is (a remark). */
function AskDetail({ ask, api }: { ask: Ask; api: ConsoleApi }) {
  return (
    <div className="detail__ask">
      <div className="detail__ask-need">{ask.need}</div>
      <div className="detail__ask-answers">
        {ask.answers.map((a) => <span key={a} className="chip">{a}</span>)}
      </div>
      <div className="detail__ask-note">
        {ask.forMe
          ? "Your reaction counts as your approval; a post reaches the agent as a remark."
          : "Read-only: another human's ask."}
      </div>
      <Thread title="ask thread" posts={null} api={api} threadId={ask.threadId} />
    </div>
  )
}

/** A thread as the bus has it (GET /v1/threads/<id>), paged once here: the
 * page the server sends, follow-up pages when the server grows them. */
function Thread({ title, posts, api, threadId }: {
  title: string
  posts: Post[] | null
  api: ConsoleApi
  threadId?: string
}) {
  const [shown, setShown] = useState<Post[]>(posts ?? [])
  const id = threadId ?? null
  useEffect(() => {
    if (!id) return
    let gone = false
    api.thread(id).then((page) => { if (!gone) setShown(page.posts) }).catch(() => {})
    return () => { gone = true }
  }, [api, id])
  if (shown.length === 0) return null
  return (
    <section className="thread">
      <h3>{title}</h3>
      {shown.map((post) => (
        <article key={post.id} className="thread__post">
          <header>
            <span>{post.author.replace(/^agent-mac-/, "")}</span>
            <span>{clock(post.at)}</span>
          </header>
          <pre>{post.message}</pre>
        </article>
      ))}
    </section>
  )
}
