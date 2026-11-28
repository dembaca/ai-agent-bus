// What the console remembers per person and ask for the red count (#285): up to
// where the thread was read, as the number of posts the operator had seen when the
// thread was last open. Kept in this browser's localStorage under the operator's
// name, like prefs.ts: the server has no per-person store and the console has no
// write path to the bus, so the browser is the simplest place that needs neither.
// Storage can be missing or throw (a private window): then every post of a thread
// counts as new and the console still works. A stored value that is no count is
// dropped, never trusted.

export type Reads = Record<string, number>

const MAX_READS = 500
const keyOf = (person: string): string => `choirbus.console.reads.${person}`

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

/** The posts of a thread the operator has not seen: all, when it was never opened. */
export function unread(total: number, reads: Reads, ask: string): number {
  return Math.max(0, total - (reads[ask] ?? 0))
}
