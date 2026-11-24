// The real console server behind the same ConsoleApi as the mock. The
// server does the Keycloak authorization-code flow with PKCE; the browser
// holds a same-origin HttpOnly session cookie, which fetch (same-origin)
// and EventSource carry with every call. `VITE_CONSOLE_API` names the
// server; without it the app runs the mock (see main.tsx).
//
// --- The wire contract, in one place -------------------------------------
// The console server (#94) sends the app's own camelCase shapes for the
// fleet (its events and the cards in them), the thread pages (`posts`
// with `rootId`, `at`, …, and `next`, a cursor) and the graph and report
// documents — so those pass through unchanged, and the TypeScript types
// `are` the contract. The ask rows and `/v1/me` use the ADR-015 spellings
// (`for_me`, `may_answer`, `display_name`, `username`), mapped here so no
// cast goes unchecked. A 401 anywhere means the session is gone: the app
// goes to the server's `/login` route (the mock never 401s).
import type {
  AgentCard, Ask, ConsoleApi, ConnState, FleetEvent, GraphData, Post,
  Report, ThreadPage,
} from "./types"

/** The wire contract: snake_case where the ADR spells it that way. Only
 * the fields the app maps itself are declared; everything else passes
 * through with the shape the app types already expect (`asked human` is
 * `human`, the ADR's columns are ADR-015's own words). */
interface WireAsk {
  id: string
  by: string
  display_name: string
  human: string | null
  need: string
  answers: string[]
  at: number
  end: "open" | "answered" | "resolved" | "expired"
  for_me: boolean
  may_answer: boolean
  thread_id: string
}

interface WireThread {
  posts: Post[]
  /** The next page's cursor; null or absent on the last page. */
  next?: string | null
}

const json = async <T>(base: string, path: string): Promise<T> => {
  const res = await fetch(`${base}${path}`, { credentials: "same-origin" })
  if (res.status === 401) {
    // The session is gone: the server owns the login route (it runs the
    // PKCE flow); a same-origin redirect is the honest move.
    if (typeof window !== "undefined") {
      window.location.assign("/login")
    }
    throw new Error("operator login required")
  }
  if (!res.ok) throw new Error(`${path}: ${res.status} ${res.statusText}`)
  return await res.json() as Promise<T>
}

/** Wire → app: the ask rows and pages keep the shapes the server sends;
 * the marked fields get their app names here, so no cast goes unchecked. */
function askOf(row: WireAsk): Ask {
  return {
    id: row.id,
    by: row.by,
    displayName: row.display_name,
    human: row.human,
    need: row.need,
    answers: row.answers,
    at: row.at,
    end: row.end,
    forMe: row.for_me,
    mayAnswer: row.may_answer,
    threadId: row.thread_id,
  }
}

export function httpApi(base: string): ConsoleApi {
  const strip = base.endsWith("/") ? base.slice(0, -1) : base
  return {
    async me() {
      const me = await json<{ username: string; display_name: string; groups: string[] }>(strip, "/v1/me")
      return { username: me.username, displayName: me.display_name, groups: me.groups }
    },
    watchFleet(onEvent: (event: FleetEvent) => void,
               onState?: (state: ConnState) => void): () => void {
      let opened = false
      // Same-origin: EventSource carries the session cookie by default.
      const source = new EventSource(`${strip}/v1/fleet`)
      source.onopen = () => { opened = true; onState?.("live") }
      source.onerror = () => {
        // CLOSED without a session or a server: error; in between, the
        // browser reconnects the stream itself.
        if (!opened || source.readyState === EventSource.CLOSED) {
          onState?.("error")
        } else {
          onState?.("reconnecting")
        }
      }
      source.onmessage = (ev) => {
        try {
          onEvent(JSON.parse(ev.data) as FleetEvent)
        } catch { /* one unreadable line does not end the stream */ }
      }
      return () => source.close()
    },
    async asks(): Promise<Ask[]> {
      const rows = await json<WireAsk[]>(strip, "/v1/asks")
      return rows.map(askOf)
    },
    async graph(): Promise<GraphData> {
      return await json(strip, "/v1/graph")
    },
    async stats(): Promise<Report[]> {
      return await json(strip, "/v1/stats")
    },
    async thread(rootId: string, next?: string | null): Promise<ThreadPage> {
      const query = next ? `?next=${encodeURIComponent(next)}` : ""
      const page = await json<WireThread>(strip, `/v1/threads/${encodeURIComponent(rootId)}${query}`)
      return { rootId, posts: page.posts, next: page.next ?? null }
    },
  }
}

/** Cards arrive as events or as one snapshot payload; HTTP servers may also
 * answer GET /v1/fleet with the plain array once. Exported for the server
 * contract, kept beside the api it belongs to. */
export type FleetSnapshot = AgentCard[]
