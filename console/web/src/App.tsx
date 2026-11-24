import { useCallback, useEffect, useState } from "react"
import type { AgentCard, Ask, ConnState, ConsoleApi, FleetEvent, Operator } from "./api/types"
import { AGENT_STATES } from "./api/types"
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
 * stream's own state, and every view's failed load, lands in the sidebar. */
export function App({ api }: { api: ConsoleApi }) {
  const [cards, setCards] = useState<AgentCard[]>([])
  const [asks, setAsks] = useState<Ask[]>([])
  const [me, setMe] = useState<Operator | null>(null)
  const [view, setView] = useState<View>("kanban")
  const [agent, setAgent] = useState<AgentCard | null>(null)
  const [ask, setAsk] = useState<Ask | null>(null)
  const [conn, setConn] = useState<ConnState>("connecting")
  const [notice, setNotice] = useState<string | null>(null)

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
  useEffect(() => api.watchFleet(onEvent, setConn), [api, onEvent])

  // The operator (GET /v1/me); the login itself is the server's — a failed
  // identity call is the one error a read-only console cannot wave away.
  useEffect(() => {
    let gone = false
    api.me()
      .then((who) => { if (!gone) setMe(who) })
      .catch((error: unknown) => {
        if (!gone) {
          setConn("error")
          setNotice(`operator: ${error instanceof Error ? error.message : String(error)}`)
        }
      })
    return () => { gone = true }
  }, [api])

  // Asks change at turn ends; asking every 30 s is faster than that.
  const fail = useCallback((what: string, error: unknown) => {
    setNotice(`${what}: ${error instanceof Error ? error.message : String(error)}`)
  }, [])
  const loadAsks = useCallback(async () => {
    try {
      setAsks(await api.asks())
    } catch (error: unknown) {
      fail("asks", error)
    }
  }, [api, fail])
  useEffect(() => {
    void loadAsks()
    const timer = setInterval(loadAsks, 30_000)
    return () => { clearInterval(timer) }
  }, [loadAsks])

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
        {view === "graph" && <GraphView api={api} onFail={fail} />}
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
