import { useCallback, useEffect, useRef, useState } from "react"
import type { AgentCard, Ask, ConnState, ConsoleApi, FleetEvent, Operator } from "./api/types"
import { AGENT_STATES } from "./api/types"
import { Unavailable } from "./api/http"
import { Sidebar, type View } from "./ui/Sidebar"
import { Kanban } from "./ui/Kanban"
import { Asks } from "./ui/Asks"
import { Detail } from "./ui/Detail"
import { GraphView } from "./ui/GraphView"
import { Reports } from "./ui/Reports"

/** The whole console: one live fleet, the asks, the chosen view, and the
 * detail pane on the right. Read-only (ADR-015 stage 1): nothing here
 * posts, an ask gets no answer button — answers stay in the humans' chat
 * until the owner opens answering.
 *
 * One connection state for the whole app (Fable's point 6): the fleet
 * stream's own state, and every view's failed load, lands in the sidebar.
 *
 * A 503 is no error at first: after a restart the server is not ready until
 * it has read the fleet's replay, and the proxy in front answers 503 for it.
 * The sidebar says "syncing…" and the app tries again every few seconds;
 * only a 503 that lasts longer than SYNC_GIVE_UP_MS is shown as an error. */
const SYNC_RETRY_MS = 3_000
const SYNC_GIVE_UP_MS = 120_000

export function App({ api }: { api: ConsoleApi }) {
  const [cards, setCards] = useState<AgentCard[]>([])
  const [asks, setAsks] = useState<Ask[]>([])
  const [me, setMe] = useState<Operator | null>(null)
  const [view, setView] = useState<View>("kanban")
  const [agent, setAgent] = useState<AgentCard | null>(null)
  const [ask, setAsk] = useState<Ask | null>(null)
  const [conn, setConn] = useState<ConnState>("connecting")
  const [notice, setNotice] = useState<string | null>(null)
  // bumped to reopen the stream and ask for the operator again after a 503
  const [attempt, setAttempt] = useState(0)
  const syncingSince = useRef<number | null>(null)
  const retryTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => () => { if (retryTimer.current) clearTimeout(retryTimer.current) }, [])

  /** True when the error was the server's "not ready yet" (a 503): the
   * sidebar shows "syncing…" and one retry is scheduled — until the 503
   * has lasted SYNC_GIVE_UP_MS, then it is an error like any other. */
  const syncing = useCallback((error: unknown): boolean => {
    if (!(error instanceof Unavailable)) return false
    const now = Date.now()
    syncingSince.current ??= now
    if (now - syncingSince.current > SYNC_GIVE_UP_MS) return false
    setConn("syncing")
    if (!retryTimer.current) {
      retryTimer.current = setTimeout(() => {
        retryTimer.current = null
        setAttempt((n) => n + 1)
      }, SYNC_RETRY_MS)
    }
    return true
  }, [])

  const onEvent = useCallback((event: FleetEvent) => {
    if (event.type === "snapshot") {
      setCards(inColumnOrder(event.cards))
      setAgent((sel) => (sel ? event.cards.find((c) => c.name === sel.name) ?? sel : sel))
    } else if (event.type === "card") {
      setCards((all) => inColumnOrder(all.filter((c) => c.name !== event.card.name).concat(event.card)))
      setAgent((sel) => (sel && sel.name === event.card.name ? event.card : sel))
    } else if (event.type === "drop") {
      setCards((all) => all.filter((c) => c.name !== event.name))
    }
  }, [])
  const onState = useCallback((state: ConnState) => {
    if (state !== "error") {
      setConn(state)
      return
    }
    // An EventSource hides its status code; the probe finds a real 401
    // (http.ts sends the operator to the server's /login for it) or a 503
    // (syncing: the stream is opened again).
    api.me().then(() => { setConn("error") }, (error: unknown) => {
      if (!syncing(error)) setConn("error")
    })
  }, [api, syncing])
  useEffect(() => api.watchFleet(onEvent, onState), [api, onEvent, onState, attempt])

  // The operator (GET /v1/me); the login itself is the server's — a failed
  // identity call is the one error a read-only console cannot wave away.
  useEffect(() => {
    let gone = false
    api.me()
      .then((who) => {
        syncingSince.current = null  // a load that answered: no longer syncing
        if (!gone) setMe(who)
      })
      .catch((error: unknown) => {
        if (!gone && !syncing(error)) {
          setConn("error")
          setNotice(`operator: ${error instanceof Error ? error.message : String(error)}`)
        }
      })
    return () => { gone = true }
  }, [api, syncing, attempt])

  // Asks change at turn ends; asking every 30 s is faster than that.
  const fail = useCallback((what: string, error: unknown) => {
    if (syncing(error)) return
    setNotice(`${what}: ${error instanceof Error ? error.message : String(error)}`)
  }, [syncing])
  const loadAsks = useCallback(async () => {
    try {
      setAsks(await api.asks())
      syncingSince.current = null  // a load that answered: no longer syncing
    } catch (error: unknown) {
      fail("asks", error)
    }
  }, [api, fail])
  useEffect(() => {
    void loadAsks()
    const timer = setInterval(loadAsks, 30_000)
    return () => { clearInterval(timer) }
  }, [loadAsks])
  // An ask opens or closes between those reads: when an agent comes, goes or
  // states a different number of open asks, the asks are read at once.
  const askMark = useRef<string | null>(null)
  useEffect(() => {
    const mark = cards.map((c) => `${c.name}:${c.openAsks ?? "?"}`).sort().join(",")
    if (askMark.current !== null && askMark.current !== mark) void loadAsks()
    askMark.current = mark
  }, [cards, loadAsks])

  const openAgent = useCallback((card: AgentCard) => {
    setAgent(card)
    setAsk(null)
  }, [])
  const openAsk = useCallback((row: Ask) => {
    setAsk(row)
    setAgent(cards.find((c) => c.name === row.by) ?? null)
  }, [cards])
  const closeDetail = useCallback(() => { setAgent(null); setAsk(null) }, [])

  const open = asks.filter((a) => a.end === "open" || a.end === "expired").length
  const forMe = asks.filter((a) => a.forMe && (a.end === "open" || a.end === "expired")).length

  return (
    <div className="app">
      <Sidebar
        view={view} setView={setView}
        openAsks={open} forMe={forMe}
        conn={conn} operator={me?.username ?? "…"}
        notice={notice} clearNotice={() => setNotice(null)}
      />
      <main className="stage">
        {view === "kanban" && <Kanban cards={cards} selected={agent} onSelect={openAgent} />}
        {view === "asks" && <Asks asks={asks} onOpen={openAsk} />}
        {view === "graph" && <GraphView api={api} cards={cards} asks={asks} onFail={fail} />}
        {view === "reports" && <Reports api={api} onFail={fail} />}
      </main>
      {(agent || ask) && (
        <Detail api={api} card={agent} ask={ask} asks={asks} onClose={closeDetail} onFail={fail} />
      )}
    </div>
  )
}

/** Column order = the state words, in the order the docs list them, the
 * presence v1 words among them; unknown states sort after (the catch-all). */
function inColumnOrder(all: AgentCard[]): AgentCard[] {
  const rank = new Map(AGENT_STATES.map((s, i) => [s, i]))
  return [...all].sort((a, b) =>
    (rank.get(a.state) ?? 99) - (rank.get(b.state) ?? 99) || a.name.localeCompare(b.name))
}
