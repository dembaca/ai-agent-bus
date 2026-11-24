// The real console server behind the same ConsoleApi as the mock: fetch for
// the request endpoints, EventSource for the one stream of changes
// (ADR-015 point 10: HTTPS, server-sent events; one direction of push is
// enough). Unused until the console server exists — VITE_CONSOLE_API names
// its base URL.
import type {
  AgentCard, Ask, ConsoleApi, FleetEvent, GraphData, Post, Report,
  ThreadPage,
} from "./types"

const json = async <T>(base: string, path: string): Promise<T> => {
  const res = await fetch(`${base}${path}`)
  if (!res.ok) throw new Error(`${path}: ${res.status} ${res.statusText}`)
  return await res.json() as Promise<T>
}

export function httpApi(base: string): ConsoleApi {
  const strip = base.endsWith("/") ? base.slice(0, -1) : base
  return {
    watchFleet(onEvent: (event: FleetEvent) => void): () => void {
      const source = new EventSource(`${strip}/v1/fleet`)
      source.onmessage = (ev) => {
        try {
          onEvent(JSON.parse(ev.data) as FleetEvent)
        } catch { /* one unreadable line does not end the stream */ }
      }
      return () => source.close()
    },
    async asks(): Promise<Ask[]> {
      return await json(strip, "/v1/asks")
    },
    async graph(): Promise<GraphData> {
      return await json(strip, "/v1/graph")
    },
    async stats(): Promise<Report[]> {
      return await json(strip, "/v1/stats")
    },
    async thread(rootId: string): Promise<ThreadPage> {
      const page = await json<{ posts: Post[]; next: boolean }>(strip, `/v1/threads/${encodeURIComponent(rootId)}`)
      return { rootId, posts: page.posts, next: page.next }
    },
  }
}

/** Cards arrive as events or as one snapshot payload; HTTP servers may also
 * answer GET /v1/fleet with the plain array once. Exported for the server
 * contract, kept beside the api it belongs to. */
export type FleetSnapshot = AgentCard[]
