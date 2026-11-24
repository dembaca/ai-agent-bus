import { useCallback, useEffect, useState } from "react"
import type { AgentCard, Ask, ConsoleApi, FleetEvent } from "./api/types"
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
 * until the owner opens answering. */
export function App({ api }: { api: ConsoleApi }) {
  const [cards, setCards] = useState<AgentCard[]>([])
  const [asks, setAsks] = useState<Ask[]>([])
  const [view, setView] = useState<View>("kanban")
  const [agent, setAgent] = useState<AgentCard | null>(null)
  const [ask, setAsk] = useState<Ask | null>(null)

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
  useEffect(() => api.watchFleet(onEvent), [api, onEvent])

  // Asks change at turn ends; asking every 30 s is faster than that.
  useEffect(() => {
    let gone = false
    const load = async () => {
      const loaded = await api.asks()
      if (!gone) setAsks(loaded)
    }
    void load()
    const timer = setInterval(load, 30_000)
    return () => { gone = true; clearInterval(timer) }
  }, [api])

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
      <Sidebar view={view} setView={setView} openAsks={open} forMe={forMe} />
      <main className="stage">
        {view === "kanban" && <Kanban cards={cards} selected={agent} onSelect={openAgent} />}
        {view === "asks" && <Asks asks={asks} onOpen={openAsk} />}
        {view === "graph" && <GraphView api={api} />}
        {view === "reports" && <Reports api={api} />}
      </main>
      {(agent || ask) && (
        <Detail api={api} card={agent} ask={ask} asks={asks} onClose={closeDetail} />
      )}
    </div>
  )
}

/** Column order = the state words, in the order the docs list them. */
function inColumnOrder(all: AgentCard[]): AgentCard[] {
  const rank = new Map(AGENT_STATES.map((s, i) => [s, i]))
  return [...all].sort((a, b) =>
    (rank.get(a.state) ?? 99) - (rank.get(b.state) ?? 99) || a.name.localeCompare(b.name))
}
