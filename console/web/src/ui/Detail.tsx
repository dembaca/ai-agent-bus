import { X } from "lucide-react"
import { useEffect, useMemo, useState, type ReactNode } from "react"
import type { AgentCard, AgentStatsRow, Ask, ConsoleApi, Post, ThreadPage } from "../api/types"
import { clock, since } from "../clock"
import {
  NO_LAST_POST, NO_STATS_ROW, STATS_ROWS_NOTE, agentName, askerName, harnessLabel, isOpen, stateClass, tokenCount,
} from "./agents"
import { AgentLabel, Context, SharedMark, nameTitle } from "./AgentList"
import { CrownIcon } from "./icons"
import "./Detail.css"

type Fail = (what: string, error: unknown) => void

/** An agent's detail, the content of the drawer from the right (#283; ADR-015
 * points 3 and 6): the full card, the task rows it holds, the session's age,
 * the time since its last post, its HELLO thread and the threads of its open
 * asks. An ask opened from a list shows on top of its agent's detail, or alone
 * when its agent is gone. Read-only: no answer button (stage 1). The cross
 * closes it; the drawer around it handles Esc and the arrow keys (App). */
export function Detail({ api, card, ask, asks, stats = null, onClose, onFail }: {
  api: ConsoleApi
  card: AgentCard | null
  ask: Ask | null
  asks: Ask[]
  /** The per-agent rows of `GET /v1/agent-stats` by agent (#201); null
   * while none was read. */
  stats?: ReadonlyMap<string, AgentStatsRow> | null
  onClose: () => void
  onFail: Fail
}) {
  // #199: the agent's name; its id on the bus in the title and the facts
  const named = card ? agentName(card) : ask ? askerName(ask, []) : null
  const title = named?.text ?? ""
  const itsAsks = card ? asks.filter((a) => a.by === card.name && isOpen(a)) : []
  // its other open asks, beside the one opened; tinted when one is yours
  const others = itsAsks.filter((a) => a.id !== ask?.id)
  const othersForYou = others.some((a) => a.forMe === true)
  return (
    <div className="detail">
      <div className="detail__head">
        <div className="detail__title">
          <h1 className="mono" title={card ? nameTitle(card) : named?.id}>{title}</h1>
          {named?.shared && <SharedMark />}
          {card && <span className={`pill pill--${stateClass(card.state)}`}>{card.state}</span>}
          {card?.crown && (
            <span className="pill pill--bus pill--plain detail__crown">
              <CrownIcon size={12} /> coordinator of {card.home}
            </span>
          )}
          {!card && ask?.agentGone && <span className="pill pill--offline">agent gone</span>}
          <button type="button" className="detail__close" aria-label="Close the detail"
            title="Close (Esc)" onClick={onClose}>
            <X size={16} aria-hidden="true" />
          </button>
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
          {ask && <AskBlock ask={ask} by={card && card.name === ask.by ? agentName(card) : askerName(ask, [])} />}
          {card && others.length > 0 && (
            <section className={"panel detail__asks" + (othersForYou ? " detail__asks--for-you" : "")}
              aria-label={othersForYou ? "Waiting on you" : "Its open asks"}>
              <h2 className="section-title">{othersForYou ? "Waiting on you" : "Its open asks"}</h2>
              <ul>
                {others.map((a) => (
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
          {card && <Today row={stats === null ? undefined : stats.get(card.name) ?? null} />}
          {card && <Facts card={card} />}
        </aside>
      </div>
    </div>
  )
}

/** The agent's numbers of the last 24 hours (#201, `GET /v1/agent-stats`):
 * tokens with the split by type, posts, wakes and tool calls. `row`: undefined
 * while none was read, null when the agent has none (another owner's, not
 * running now, or an own one before its first STATS part). A number no part states is n/a, never 0; no share of the
 * overhead is computed here (its sums cover only the parts that state it). */
function Today({ row }: { row: AgentStatsRow | null | undefined }) {
  const tok = row?.tok24h ?? null
  const n = (v: number | null | undefined) => (v === null || v === undefined ? "n/a" : String(v))
  const t = (v: number | null | undefined) => tokenCount(v ?? null)
  const facts: Array<[string, string]> = row ? [
    ["tokens", t(tok?.all)],
    ["input", t(tok?.in)],
    ["cache read", t(tok?.read)],
    ["cache write 5 m", t(tok?.w5m)],
    ["cache write 1 h", t(tok?.w1h)],
    ["output", t(tok?.out)],
    ["posts", n(row.posts24h)],
    ["wakes", n(row.wakes24h)],
    ["tool calls", n(row.tools24h)],
  ] : []
  return (
    <section className="panel detail__facts" aria-label="Today">
      <h2 className="section-title">Today</h2>
      {row === undefined && <p className="muted small">Not read yet.</p>}
      {row === null && <p className="muted small">{NO_STATS_ROW} {STATS_ROWS_NOTE}</p>}
      {row && (
        <>
          <dl>
            {facts.map(([k, v]) => (
              <div key={k} className="detail__fact"><dt>{k}</dt><dd>{v}</dd></div>
            ))}
          </dl>
          <p className="muted small detail__today-note">
            The last 24 hours: the STATS parts received in that time (a long
            session's part may cover earlier time). {STATS_ROWS_NOTE}
          </p>
        </>
      )}
    </section>
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
    ["last post", card.lastPostAt === null
      ? <span className="muted small">{NO_LAST_POST}</span>
      : `${clock(card.lastPostAt)} · ${since(card.lastPostAt)} ago`],
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
function AskBlock({ ask, by }: { ask: Ask; by: { text: string; shared: boolean; id: string } }) {
  return (
    <section className="detail__ask" aria-label="The ask">
      <h2 className="section-title detail__ask-title">
        {ask.forMe ? "Waiting on you" : "Open ask"}
      </h2>
      <strong className="detail__ask-need">{ask.need}</strong>
      <span className="muted">
        {ask.kind} by <AgentLabel name={by} />
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

/** The bus word a post starts with, bare or in backticks as the agents
 * write it (`` `HELLO` · …``), if it is one of the bus's words, and set off
 * by a space, a colon, a middle dot or a dash — never a hyphen: `NO-GO`,
 * `TASK-123` and `RESULT -5` stay text. Any other capital word stays text. */
export function busWord(message: string): { word: string | null; rest: string } {
  const m = /^(`?)([A-Z]+)\1(?=$|[\s:·—–])(?![ \t]*-)[\s:·—–]*/.exec(message)
  return m && BUS_WORDS.has(m[2])
    ? { word: m[2], rest: message.slice(m[0].length) }
    : { word: null, rest: message }
}

// Where emphasis may open and close: after the start, a space or an opening
// bracket or quote; before a space, punctuation or the end. Inside a word
// (`f(*args, **kwargs)`, `*.py`) nothing is emphasis. Underscores never are:
// `__init__.py` and `__main__` are names, far more often than bold.
const OPEN = String.raw`(^|[\s(\[{"'])`
const CLOSE = String.raw`(?=$|[\s.,;:!?)\]}"'])`
const BOLD = new RegExp(String.raw`${OPEN}\*\*(?=[^\s*])((?:(?!\*\*)[^\n])+?)(?<=[^\s*])\*\*${CLOSE}`, "gm")
const ITALIC = new RegExp(String.raw`${OPEN}\*(?=[^\s*])([^*\n]+?)(?<=[^\s*])\*${CLOSE}`, "gm")
const STRIKE = new RegExp(String.raw`${OPEN}~~(?=\S)((?:(?!~~)[^\n])+?)(?<=\S)~~${CLOSE}`, "gm")
// A link's text is bounded, so a run of brackets costs little; one glued to a
// word (`arr[0](x)`) is code, not a link.
const LINK = /(^|[^\w\]!])!?\[([^\]\n]{0,300})\]\(([^()\s]{1,2000})\)/g
// Code: a fence's content and an inline span's stay verbatim.
const CODE = /```[^\n]*\n([\s\S]*?)```|`([^`\n]+)`/g

function plainProse(text: string): string {
  return text
    .replace(LINK, (_all, before: string, label: string, url: string) =>
      `${before}${label ? `${label} (${url})` : url}`)
    .replace(BOLD, "$1$2")
    .replace(ITALIC, "$1$2")
    .replace(STRIKE, "$1$2")
    .replace(/^#{2,6} +/gm, "")   // a heading; a lone "# of agents" stays
    .replace(/^> /gm, "")          // a quote; ">= 3" stays
}

/** A post's markdown as plain text: code spans and fences keep their
 * content verbatim, bold, italics and strikethrough become their text, a
 * link `text (url)`, heading and quote marks go — and every other character
 * stays. The result is rendered as text by React, never as HTML. */
export function plainText(markdown: string): string {
  let out = ""
  let last = 0
  for (const m of markdown.matchAll(CODE)) {
    out += plainProse(markdown.slice(last, m.index)) + (m[1] ?? m[2] ?? "")
    last = (m.index ?? 0) + m[0].length
  }
  return out + plainProse(markdown.slice(last))
}

/** One post: time, its bus word as a chip, author and plain text —
 * worked out once per message. */
function PostRow({ post }: { post: Post }) {
  const { word, text } = useMemo(() => {
    const { word, rest } = busWord(post.message)
    return { word, text: plainText(rest) }
  }, [post.message])
  return (
    <li className="thread__post">
      <span className="thread__time">{clock(post.at)}</span>
      <span className={"thread__word" + (word ? ` thread__word--${wordTone(word)}` : "")}>{word ?? ""}</span>
      <span className="thread__text">
        <span className="thread__author mono">{post.author}</span>
        <span className="thread__message">{text}</span>
      </span>
    </li>
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
        {posts.map((post) => <PostRow key={post.id} post={post} />)}
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
