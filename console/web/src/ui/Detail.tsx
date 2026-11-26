import { useEffect, useState, type ReactNode } from "react"
import type { AgentCard, Ask, ConsoleApi, Post, ThreadPage } from "../api/types"
import { clock, since } from "../clock"
import { harnessLabel, isOpen, stateClass } from "./agents"
import { Context } from "./AgentList"
import { CrownIcon } from "./icons"
import "./Detail.css"

type Fail = (what: string, error: unknown) => void

/** An agent's detail page (the agreed design, 2026-11-25; ADR-015 points 3
 * and 6): the full card, the task rows it holds, the session's age, the time
 * since its last post, its HELLO thread and the threads of its open asks. An
 * ask opened from a list shows on top of its agent's page, or alone when its
 * agent is gone. Read-only: no answer button (stage 1). */
export function Detail({ api, card, ask, asks, from, onBack, onFail }: {
  api: ConsoleApi
  card: AgentCard | null
  ask: Ask | null
  asks: Ask[]
  /** The view the page was opened from: the breadcrumb leads back to it. */
  from: string
  onBack: () => void
  onFail: Fail
}) {
  const title = card?.displayName ?? ask?.displayName ?? ""
  const itsAsks = card ? asks.filter((a) => a.by === card.name && isOpen(a)) : []
  return (
    <div className="detail">
      <nav aria-label="Breadcrumb" className="detail__crumbs">
        <button type="button" className="linkish" onClick={onBack}>{from}</button>
        <span aria-hidden="true">/</span>
        <span className="mono detail__crumb-here">{title}</span>
      </nav>

      <div className="detail__head">
        <div className="detail__title">
          <h1 className="mono">{title}</h1>
          {card && <span className={`pill pill--${stateClass(card.state)}`}>{card.state}</span>}
          {card?.crown && (
            <span className="pill pill--bus pill--plain detail__crown">
              <CrownIcon size={12} /> coordinator of {card.home}
            </span>
          )}
          {!card && ask?.agentGone && <span className="pill pill--offline">agent gone</span>}
        </div>
        {card && (
          <span className="detail__sub">
            {harnessLabel(card.harness)} · {card.model || "model n/a"} · for {card.for}
            {card.autonomous && " (autonomous)"} · {card.version ? `version ${card.version}` : "version n/a"}
            {" "}· on the bus for {since(card.helloAt)}
          </span>
        )}
      </div>

      <div className="detail__grid">
        <div className="detail__main">
          {card && (
            <section className="panel detail__task" aria-label="Current task">
              <div className="detail__task-head">
                <h2>Current task</h2>
                <span className="muted">{card.role}</span>
              </div>
              <div className="detail__task-text">{card.task || "n/a"}</div>
              <div className="detail__task-meta">
                <span>project <span className="ink">{card.project || "n/a"}</span></span>
                <span>branch <span className="mono ink">{card.branch === null ? "n/a" : card.branch || "–"}</span></span>
                {card.pr && <span>PR <span className="chip chip--pr">#{card.pr.number}</span></span>}
                <span className="detail__ctx">context <Context ctx={card.ctx} /></span>
              </div>
            </section>
          )}

          {card && card.tasks.length > 0 && (
            <section className="panel" aria-label="Its task rows">
              <div className="panel__head"><h2>Its task rows</h2></div>
              <table className="detail__rows">
                <thead>
                  <tr><th>title</th><th>state</th><th>status</th><th>by</th><th className="num">at</th></tr>
                </thead>
                <tbody>
                  {card.tasks.map((row) => (
                    <tr key={row.id}>
                      <td>{row.title}{row.pr && <> <span className="chip chip--pr">#{row.pr}</span></>}</td>
                      <td>{row.state}</td>
                      <td>{row.status}</td>
                      <td title={row.by}>{row.displayName ?? row.by}</td>
                      <td className="num">{clock(row.at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          )}

          {!card && ask && (
            <Thread key={ask.threadId} title="Ask thread" api={api} onFail={onFail} threadId={ask.threadId} />
          )}
          {card?.helloPostId && (
            <Thread key={card.helloPostId} title="HELLO thread" api={api}
              onFail={onFail} threadId={card.helloPostId} />
          )}
          {itsAsks.map((a) => (
            <Thread key={a.id} title={`Ask thread · ${a.need}`} api={api}
              onFail={onFail} threadId={a.threadId} />
          ))}
        </div>

        <aside className="detail__side">
          {ask && <AskBlock ask={ask} />}
          {card && itsAsks.filter((a) => a.id !== ask?.id).length > 0 && (
            <section className="panel detail__asks" aria-label="Its open asks">
              <h2 className="section-title">Its open asks</h2>
              <ul>
                {itsAsks.filter((a) => a.id !== ask?.id).map((a) => (
                  <li key={a.id}>
                    <span>{a.need}</span>
                    <span className="muted nowrap">
                      {a.forMe && <span className="chip chip--for-me">for you</span>} {since(a.at)}
                    </span>
                  </li>
                ))}
              </ul>
            </section>
          )}
          {card && <Facts card={card} />}
        </aside>
      </div>
    </div>
  )
}

/** The full card, as the detail alone shows it (ADR-015 point 6). */
function Facts({ card }: { card: AgentCard }) {
  const facts: Array<[string, ReactNode, boolean?]> = [
    ["id on the bus", card.name, true],
    ["home", card.home, true],
    ["guest in", card.guests.join(", ") || "–", true],
    ["role", card.role],
    ["for", `${card.for}${card.autonomous ? " · autonomous" : ""}`],
    ["trusts", card.trusts.join(", ") || "–"],
    ["capabilities", card.capabilities.join(", ") || "–"],
    ["context", card.ctx === null ? "n/a" : `${card.ctx} %`],
    ["limits / cost", card.limits ?? "n/a"],
    ["version", card.version || "n/a"],
    ["HELLO", `${clock(card.helloAt)} · ${since(card.helloAt)} ago`],
    ["last post", card.lastPostAt === null ? "n/a" : `${clock(card.lastPostAt)} · ${since(card.lastPostAt)} ago`],
    ["open asks", card.openAsks === null
      ? "n/a"
      : `${card.openAsks}${(card.asksForOperator ?? 0) > 0 ? ` · ${card.asksForOperator} for you` : ""}`],
  ]
  return (
    <section className="panel detail__facts" aria-label="Session">
      <h2 className="section-title">Session</h2>
      <dl>
        {facts.map(([k, v, mono]) => (
          <div key={k} className="detail__fact">
            <dt>{k}</dt>
            <dd className={mono ? "mono" : undefined}>{v}</dd>
          </div>
        ))}
      </dl>
    </section>
  )
}

/** One ask on the page: its title, kind, who asked whom. Read-only:
 * nothing here answers it — that stays in its thread in the chat. */
function AskBlock({ ask }: { ask: Ask }) {
  return (
    <section className="detail__ask" aria-label="The ask">
      <h2 className="section-title detail__ask-title">
        {ask.forMe ? "Waiting on you" : "Open ask"}
      </h2>
      <strong className="detail__ask-need">{ask.need}</strong>
      <span className="muted">
        {ask.kind} by <span className="mono">{ask.displayName}</span>
        {ask.human && <> to {ask.human}</>} · asked {since(ask.at)} ago
      </span>
      <span className="detail__ask-marks">
        {ask.forMe && <span className="chip chip--for-me">for you</span>}
        {!ask.forMe && ask.mayAnswer && <span className="chip chip--may-answer">may answer</span>}
        {ask.end === "expired" && <span className="chip chip--expired">expired</span>}
        {ask.agentGone && <span className="chip chip--expired">agent gone</span>}
      </span>
      <span className="detail__ask-note">
        {ask.forMe === null
          ? "Read-only; whether it is yours is n/a until your own credential gives your anchor."
          : ask.forMe
          ? "Answer it in its thread in the chat; the console shows it until the agent ends it."
          : "Another human's ask, shown read-only."}
      </span>
    </section>
  )
}

/** The bus's words (protocol.md): only these become a chip. */
export const BUS_WORDS: ReadonlySet<string> = new Set([
  "ACK", "NO", "PROGRESS", "RESULT", "DONE", "WAITING", "IDLE", "BYE", "TASK",
  "ASSIGNMENTS", "HELLO", "BLOCKED", "DELEGATE", "HANDOVER",
])

/** The bus word a post starts with, if it is one of the bus's words; any
 * other capital word stays part of the text. */
export function busWord(message: string): { word: string | null; rest: string } {
  const m = /^([A-Z]+)\b[:\s]*/.exec(message)
  return m && BUS_WORDS.has(m[1])
    ? { word: m[1], rest: message.slice(m[0].length) }
    : { word: null, rest: message }
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
    // Another thread than the one shown resets what was read: opening a
    // second agent must not pile the first's posts under it.
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
    <section className="panel thread" aria-label={title}>
      <div className="panel__head"><h2>{title}</h2><span className="muted">{posts.length} posts</span></div>
      <ol className="thread__posts">
        {posts.map((post) => {
          const { word, rest } = busWord(post.message)
          return (
            <li key={post.id} className="thread__post">
              <span className="thread__time">{clock(post.at)}</span>
              <span className={"thread__word" + (word ? ` thread__word--${wordTone(word)}` : "")}>{word ?? ""}</span>
              <span className="thread__text">
                <span className="thread__author mono">{post.author}</span>
                <span className="thread__message">{rest}</span>
              </span>
            </li>
          )
        })}
      </ol>
    </section>
  )
}

/** The site's word colors: asks orange, tasks blue, done green. */
function wordTone(word: string): string {
  if (["WAITING", "BLOCKED"].includes(word)) return "ask"
  if (["TASK", "PROGRESS", "ACK", "DELEGATE", "HANDOVER", "ASSIGNMENTS"].includes(word)) return "task"
  if (["DONE", "RESULT"].includes(word)) return "done"
  return "plain"
}
