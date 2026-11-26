import { useCallback, useEffect, useRef, useState } from "react"
import type { AgentCard, Ask, ConnState, ConsoleApi, FleetEvent, Operator } from "./api/types"
import { AGENT_STATES } from "./api/types"
import { Unavailable } from "./api/http"
import { TopBar, VIEW_LABEL, type View } from "./ui/TopBar"
import { Footer } from "./ui/Footer"
import { AgentList } from "./ui/AgentList"
import { Asks } from "./ui/Asks"
import { Detail } from "./ui/Detail"
import { GraphView } from "./ui/GraphView"
import { Overview } from "./ui/Overview"
import { Reports } from "./ui/Reports"
import { isOpen, type StatusFilter } from "./ui/agents"
import { Split, asPlace, pushPlace, replacePlace, resolvePlace, selectedAfter, type Place } from "./nav"
import { useTheme } from "./theme"

/** The whole console (the agreed design, 2026-11-25): a top bar with the
 * views — the agents list is the home — one live fleet, the asks, the chosen
 * view, an agent's or an ask's detail as a page of its own, and the footer
 * with the console's build (#182). Read-only (ADR-015 stage 1): nothing here
 * posts, an ask gets no answer button — answers stay in the humans' chat
 * until the owner opens answering.
 *
 * One connection state for the whole app (Fable's point 6): the fleet
 * stream's own state shows in the top bar, every view's failed load in the
 * notice under it.
 *
 * A 503 is no error at first: after a restart the server is not ready until
 * it has read the fleet's replay, and the proxy in front answers 503 for it.
 * The top bar says "syncing…" and the app tries again every few seconds;
 * only a 503 that lasts longer than SYNC_GIVE_UP_MS is shown as an error. */
const SYNC_RETRY_MS = 3_000
const SYNC_GIVE_UP_MS = 120_000

export function App({ api }: { api: ConsoleApi }) {
  const [cards, setCards] = useState<AgentCard[]>([])
  const [asks, setAsks] = useState<Ask[]>([])
  /** The fleet stream's first snapshot came: the cards are the whole fleet. */
  const [fleetSeen, setFleetSeen] = useState(false)
  const [me, setMe] = useState<Operator | null>(null)
  const [view, setViewState] = useState<View>("overview")
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
   * top bar shows "syncing…" and one retry is scheduled — until the 503
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
      setFleetSeen(true)
    } else if (event.type === "card") {
      setCards((all) => inColumnOrder(all.filter((c) => c.name !== event.card.name).concat(event.card)))
    } else if (event.type === "drop") {
      setCards((all) => all.filter((c) => c.name !== event.name))
    }
    // an open detail follows its agent; one that left closes (nav.ts)
    setAgent((sel) => selectedAfter(event, sel))
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
  // Each read is numbered: an older answer that comes late never overwrites
  // a newer one.
  const asksRead = useRef(0)
  const loadAsks = useCallback(async () => {
    const mine = ++asksRead.current
    try {
      const rows = await api.asks()
      if (mine !== asksRead.current) return
      setAsks(rows)
      syncingSince.current = null  // a load that answered: no longer syncing
    } catch (error: unknown) {
      if (mine === asksRead.current) fail("asks", error)
    }
  }, [api, fail])
  useEffect(() => {
    void loadAsks()
    const timer = setInterval(loadAsks, 30_000)
    return () => { clearInterval(timer) }
  }, [loadAsks])
  // An ask opens or closes between those reads: when an agent comes, goes or
  // states a different number of open asks, the asks are read again, 250 ms
  // later, so a burst of card changes costs one read.
  // The timer lives in a ref: a card event that changes nothing must not
  // cancel a read already scheduled.
  const askMark = useRef<string | null>(null)
  const askTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => {
    const mark = cards.map((c) => `${c.name}:${c.openAsks ?? "?"}`).sort().join(",")
    const changed = askMark.current !== null && askMark.current !== mark
    askMark.current = mark
    if (!changed) return
    if (askTimer.current !== null) clearTimeout(askTimer.current)
    askTimer.current = setTimeout(() => {
      askTimer.current = null
      void loadAsks()
    }, 250)
  }, [cards, loadAsks])
  useEffect(() => () => {
    if (askTimer.current !== null) clearTimeout(askTimer.current)
  }, [])

  // The places in the browser's history (nav.ts): every move pushes one, the
  // browser's Back and Forward come back through popstate. The live fleet and
  // asks are read through refs, so a place finds the current card or ask.
  const cardsNow = useRef(cards)
  cardsNow.current = cards
  const asksNow = useRef(asks)
  asksNow.current = asks
  const viewNow = useRef(view)
  viewNow.current = view
  const go = useCallback((place: Place, push: boolean) => {
    const shown = resolvePlace(place, cardsNow.current, asksNow.current)
    setViewState(shown.view)
    setAsk(shown.ask)
    setAgent(shown.agent)
    if (push) pushPlace(place)
  }, [])
  useEffect(() => {
    replacePlace({ view: "overview" })
    const back = (event: PopStateEvent) => { go(asPlace(event.state) ?? { view: "overview" }, false) }
    window.addEventListener("popstate", back)
    return () => { window.removeEventListener("popstate", back) }
  }, [go])

  const openAgent = useCallback((card: AgentCard) => {
    go({ view: viewNow.current, agent: card.name }, true)
  }, [go])
  const openAsk = useCallback((row: Ask) => {
    go({ view: viewNow.current, ask: row.id, agent: row.by }, true)
  }, [go])
  // The breadcrumb: back to the view the detail was opened from.
  const closeDetail = useCallback(() => { go({ view: viewNow.current }, true) }, [go])
  // A view picked in the top bar closes a detail page that is open.
  const setView = useCallback((next: View) => { go({ view: next }, true) }, [go])
  // The list's search and status filter live here, so a detail page and the
  // way back keep them.
  const [listFilter, setListFilter] = useState<StatusFilter>("all")
  const [listQuery, setListQuery] = useState("")
  const { theme, toggle } = useTheme()

  const open = asks.filter(isOpen).length
  const forMe = asks.filter((a) => a.forMe && isOpen(a)).length
  const detail = agent !== null || ask !== null

  return (
    <div className="app">
      <TopBar
        view={view} setView={setView}
        openAsks={open} forMe={forMe}
        conn={conn} operator={me?.username ?? "…"}
        theme={theme} onToggleTheme={toggle}
      />
      <main className={"stage" + (detail ? " stage--split" : "")}>
        {notice && (
          <div className="notice" role="alert">
            <span>{notice}</span>
            <button type="button" aria-label="dismiss" onClick={() => setNotice(null)}>×</button>
          </div>
        )}
        {/* #200: a detail opens beside the view, which stays mounted and
            visible (half and half); below 1000 px the view hides and the
            detail is the whole page (app.css) */}
        <Split detail={detail && (
          <Detail api={api} card={agent} ask={ask} asks={asks}
            from={VIEW_LABEL[view]} onBack={closeDetail} onFail={fail} />
        )}>
            {view === "agents" && <AgentList cards={cards} asks={asks} compact={detail}
              selected={agent?.name ?? null}
              filter={listFilter} onFilter={setListFilter} query={listQuery} onQuery={setListQuery}
              onOpenAgent={openAgent} onOpenAsk={openAsk} onAllAsks={() => setView("asks")} />}
            {view === "asks" && <Asks asks={asks} cards={cards} onOpen={openAsk} />}
            {view === "graph" && <GraphView api={api} cards={cards} asks={asks} fleetSeen={fleetSeen}
              theme={theme} onOpenAgent={openAgent} onOpenAsk={openAsk} onFail={fail} />}
            {view === "overview" && <Overview cards={cards} asks={asks}
              onOpenAsk={openAsk} onView={setView} />}
            {view === "reports" && <Reports api={api} onFail={fail} />}
        </Split>
      </main>
      <Footer build={me?.build ?? null} />
    </div>
  )
}

/** The list's order = the state words, in the order the docs list them, the
 * presence v1 words among them; unknown states sort after. */
function inColumnOrder(all: AgentCard[]): AgentCard[] {
  const rank = new Map(AGENT_STATES.map((s, i) => [s, i]))
  return [...all].sort((a, b) =>
    (rank.get(a.state) ?? 99) - (rank.get(b.state) ?? 99) || a.name.localeCompare(b.name))
}
