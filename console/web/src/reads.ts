// What the console remembers per person and ask for the red count (#285): up to
// where the thread was read, as the number of replies by others the operator had
// seen when the thread was last open (the ask's own post, the thread's root, and the
// operator's own posts never count). Kept in this browser's localStorage under the operator's
// name, like prefs.ts: the server has no per-person store and the console has no
// write path to the bus, so the browser is the simplest place that needs neither.
// Storage can be missing or throw (a private window): then every post of a thread
// counts as new and the console still works. A stored value that is no count is
// dropped, never trusted.

import type { Operator, Post } from "./api/types"

export type Reads = Record<string, number>

const MAX_READS = 500
// v2: the stored number counts replies by others, where it once counted every post
const keyOf = (person: string): string => `choirbus.console.reads.v2.${person}`

export function loadReads(person: string | null): Reads {
  if (!person) return {}
  try {
    const raw: unknown = JSON.parse(window.localStorage.getItem(keyOf(person)) ?? "null")
    if (typeof raw !== "object" || raw === null) return {}
    const out: Reads = {}
    for (const [ask, n] of Object.entries(raw).slice(0, MAX_READS)) {
      if (typeof n === "number" && Number.isInteger(n) && n >= 0) out[ask] = n
    }
    return out
  } catch {
    return {}
  }
}

export function saveReads(person: string | null, reads: Reads): void {
  if (!person) return
  const entries = Object.entries(reads)
  const kept = Object.fromEntries(entries.slice(Math.max(0, entries.length - MAX_READS)))
  try {
    window.localStorage.setItem(keyOf(person), JSON.stringify(kept))
  } catch {
    // a storage that refuses is no error: the count starts again after a reload
  }
}

/** The replies in a thread that count for the red number: those of others. Neither
 * the thread's root (the ask's own post) nor a post of the operator counts; with no
 * operator known none is taken for theirs. */
export function repliesByOthers(posts: readonly Post[], rootId: string, operator: Operator | null): number {
  const mine = new Set<string>()
  for (const name of [operator?.username, operator?.displayName]) {
    if (name) mine.add(name.trim().toLowerCase())
  }
  return posts.filter((p) =>
    p.id !== rootId && p.rootId !== null && !mine.has(p.author.trim().toLowerCase())).length
}

/** The replies by others the operator has not seen: all of them when the thread was
 * never opened. */
export function unread(others: number, reads: Reads, ask: string): number {
  return Math.max(0, others - (reads[ask] ?? 0))
}
