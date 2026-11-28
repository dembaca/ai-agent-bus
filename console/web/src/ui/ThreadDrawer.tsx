import { useEffect, useState } from "react"
import type { Ask, ConsoleApi, Operator, Post } from "../api/types"
import { repliesByOthers } from "../reads"
import { PostRow } from "./Detail"
import { loadThread } from "./threadPosts"
import { since } from "../clock"

export const ANSWER_DISABLED = "answer in the chat for now"

/** The thread of an ask as a drawer from the right (#285): its posts, read
 * only. The reply field is disabled and says why: no write path to the bus is
 * built before the human security review (ADR-015, RT-6.43). Opening it marks
 * the posts shown as read (`onRead`). */
export function ThreadDrawer({ api, ask, operator = null, onClose, onRead, onFail }: {
  api: ConsoleApi
  ask: Ask
  /** Who is reading: their own posts never count as new. */
  operator?: Operator | null
  onClose: () => void
  /** The replies by others now in the thread, which the operator has seen. */
  onRead: (ask: string, others: number) => void
  onFail: (what: string, error: unknown) => void
}) {
  const [posts, setPosts] = useState<Post[] | null>(null)
  useEffect(() => {
    setPosts(null)
    let gone = false
    loadThread(api, ask.threadId).then((all) => {
      if (gone) return
      setPosts(all)
      onRead(ask.id, repliesByOthers(all, ask.threadId, operator))
    }).catch((error: unknown) => { if (!gone) onFail("thread", error) })
    return () => { gone = true }
  }, [api, ask.id, ask.threadId, operator, onRead, onFail])
  return (
    <div className="threadd">
      <div className="threadd__head">
        <h1>Chat with agent</h1>
        <button type="button" className="detail__close" aria-label="Close" onClick={onClose}>×</button>
      </div>
      <p className="muted threadd__sub">
        {ask.need} · asked {since(ask.at)} ago
      </p>
      <section className="panel thread" aria-label="Thread">
        <div className="panel__head">
          <h2>Thread</h2>
          <span className="muted">{posts === null ? "loading…" : `${posts.length} posts`}</span>
        </div>
        {posts !== null && posts.length === 0 &&
          <p className="muted threadd__empty">The console has no posts of this thread to show yet.</p>}
        <ol className="thread__posts">
          {(posts ?? []).map((post) => <PostRow key={post.id} post={post} />)}
        </ol>
      </section>
      <form className="threadd__reply" onSubmit={(e) => { e.preventDefault() }}>
        <textarea disabled rows={3} aria-label="Reply" placeholder={ANSWER_DISABLED} />
        <div className="threadd__reply-row">
          <span className="muted">
            Later, what is written here reaches the agent as a remark of yours and approves nothing.
            To restrict an approval, say so here: the agent asks again with the restriction in its words.
            Disabled: {ANSWER_DISABLED}.
          </span>
          <button type="submit" disabled title={ANSWER_DISABLED}>Send</button>
        </div>
      </form>
    </div>
  )
}
