import { useCallback, useEffect, useRef, useState } from "react"
import type {
  AgentCard, AgentStatsRow, Ask, ConnState, ConsoleApi, FleetEvent, GroupsAnswer, Operator,
} from "./api/types"
import { AGENT_STATES } from "./api/types"
import { Unavailable } from "./api/http"
import { TopBar, type View } from "./ui/TopBar"
import { Footer } from "./ui/Footer"
import { AgentList } from "./ui/AgentList"
import { Asks } from "./ui/Asks"
import { Detail } from "./ui/Detail"
import { Glance } from "./ui/Glance"
import { Stats } from "./ui/Stats"
import { isOpen, statsByAgent } from "./ui/agents"
import { agentOf, asPlace, loadedPlace, neighbour, pushPlace, replacePlace, type Place } from "./nav"
import { DEFAULT_PREFS, loadPrefs, savePrefs, type Prefs } from "./prefs"
import { useTheme } from "./theme"

/** The whole console (the agreed design, 2026-11-25; #283): a top bar with the
 * three views Agents (the start page), Asks and Stats, one live flock, the asks,
 * the chosen view, an agent's or an ask's detail as a drawer from the right
 * (not modal, the open agent in the address), and the footer
 * with the console's build (#182). Read-only (ADR-015 stage 1): nothing here
 * posts, an ask gets no answer button — answers stay in the humans' chat
 * until the owner opens answering.
 *
 * One connection state for the whole app (Fable's point 6): the flock
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
  // where the console stands: the view and the open drawer's agent or ask,
  // also the address (nav.ts); the cards and asks are looked up live
  const [place, setPlace] = useState<Place>(loadedPlace)
  const view: View = place.view
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
  // The per-agent numbers (#201, `GET /v1/agent-stats`): rows of own or
  // trusted agents only, read every 30 s like the asks; null until the first
  // answer, so a cell says "not read yet" rather than "another owner's".
  const [agentStats, setAgentStats] = useState<ReadonlyMap<string, AgentStatsRow> | null>(null)
  const statsRead = useRef(0)
  const loadAgentStats = useCallback(async () => {
    const mine = ++statsRead.current
    try {
      const rows = await api.agentStats()
      if (mine !== statsRead.current) return
      setAgentStats(statsByAgent(rows))
    } catch (error: unknown) {
      if (mine === statsRead.current) fail("agent stats", error)
    }
  }, [api, fail])
  useEffect(() => {
    void loadAgentStats()
    const timer = setInterval(loadAgentStats, 30_000)
    return () => { clearInterval(timer) }
  }, [loadAgentStats])
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

  // The places in the browser's history and address (nav.ts): every move pushes
  // one, the browser's Back and Forward come back through popstate. The live
  // flock and asks are looked up by name and id, so a drawer follows its agent.
  const placeNow = useRef(place)
  placeNow.current = place
  const go = useCallback((next: Place, push = true) => {
    setPlace(next)
    if (push) pushPlace(next); else replacePlace(next)
  }, [])
  useEffect(() => {
    replacePlace(placeNow.current)
    const back = (event: PopStateEvent) => { setPlace(asPlace(event.state) ?? loadedPlace()) }
    window.addEventListener("popstate", back)
    return () => { window.removeEventListener("popstate", back) }
  }, [])
  const askRow = place.ask ? asks.find((a) => a.id === place.ask) ?? null : null
  const agent = agentOf(place, cards, askRow?.by)
  const [asksSeen, setAsksSeen] = useState(false)
  useEffect(() => { if (asks.length > 0) setAsksSeen(true) }, [asks])
  // a drawer for an agent that is not in the flock (a stale link, an agent
  // that left) closes; an ask whose agent is gone stays, alone
  useEffect(() => {
    if (!fleetSeen || agent !== null) return
    if (place.ask ? askRow === null && asksSeen : place.agent !== undefined) {
      go({ view: place.view }, false)
    }
  }, [fleetSeen, agent, askRow, asksSeen, place, go])

  const openAgent = useCallback((card: AgentCard) => {
    const now = placeNow.current
    // a second click on the same agent closes the drawer
    go(now.agent === card.name && !now.ask ? { view: now.view } : { view: now.view, agent: card.name })
  }, [go])
  const openAsk = useCallback((row: Ask) => {
    const now = placeNow.current
    go(now.ask === row.id ? { view: now.view } : { view: now.view, ask: row.id, agent: row.by })
  }, [go])
  const closeDetail = useCallback(() => { go({ view: placeNow.current.view }) }, [go])
  // a view picked in the top bar closes the drawer
  const setView = useCallback((next: View) => { go({ view: next }) }, [go])
  const [listQuery, setListQuery] = useState("")
  const { theme, toggle } = useTheme()

  // What the console remembers per person: the panel, the grouping, the folded
  // groups and the scope (prefs.ts), read once the operator is known.
  const person = me?.username ?? null
  const personNow = useRef(person)
  personNow.current = person
  const [prefs, setPrefsState] = useState<Prefs>(DEFAULT_PREFS)
  useEffect(() => { setPrefsState(loadPrefs(person)) }, [person])
  const setPrefs = useCallback((patch: Partial<Prefs>) => {
    setPrefsState((was) => {
      const next = { ...was, ...patch }
      savePrefs(personNow.current, next)
      return next
    })
  }, [])
  // a narrow window starts with the panel folded until a pick is remembered
  const [narrow] = useState(() => {
    try { return window.matchMedia("(max-width: 899px)").matches } catch { return false }
  })
  const panelFolded = prefs.panel === null ? narrow : prefs.panel === "folded"

  // The groups and the panel's numbers (`GET /v1/groups`), counted by the
  // server: read again when the grouping or scope changes, when a card's state
  // or home changes (250 ms later, so a burst costs one read), when the asks
  // change, and every 30 s.
  const [answer, setAnswer] = useState<GroupsAnswer | null>(null)
  const groupsRead = useRef(0)
  const loadGroups = useCallback(async () => {
    const mine = ++groupsRead.current
    try {
      const got = await api.groups(prefs.by, prefs.scope)
      if (mine === groupsRead.current) setAnswer(got)
    } catch (error: unknown) {
      if (mine === groupsRead.current) fail("groups", error)
    }
  }, [api, fail, prefs.by, prefs.scope])
  useEffect(() => {
    const timer = setInterval(loadGroups, 30_000)
    return () => { clearInterval(timer) }
  }, [loadGroups])
  const cardsMark = cards.map((c) => `${c.name}:${c.state}:${c.home}`).join(",")
  useEffect(() => {
    const timer = setTimeout(() => { void loadGroups() }, 250)
    return () => { clearTimeout(timer) }
  }, [loadGroups, cardsMark, asks, me])

  // The agents of the list in the order shown (AgentList), for the arrow keys.
  const order = useRef<string[]>([])
  const onVisible = useCallback((names: string[]) => { order.current = names }, [])
  const detail = agent !== null || askRow !== null
  // Esc closes the drawer; arrow up and down move to the next agent of the list
  // — never while the keys belong to a field.
  useEffect(() => {
    if (!detail) return
    const key = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        closeDetail()
      } else if ((event.key === "ArrowDown" || event.key === "ArrowUp")
                 && !event.metaKey && !event.ctrlKey && !event.altKey && view === "agents") {
        const el = event.target instanceof Element ? event.target : null
        if (el?.closest("input, textarea, select, [contenteditable]")) return
        const next = neighbour(order.current, agent?.name ?? null, event.key === "ArrowDown" ? 1 : -1)
        if (next) {
          event.preventDefault()
          go({ view, agent: next })
        }
      }
    }
    window.addEventListener("keydown", key)
    return () => { window.removeEventListener("keydown", key) }
  }, [detail, agent, view, closeDetail, go])

  const open = asks.filter(isOpen).length
  const forMe = asks.filter((a) => a.forMe && isOpen(a)).length

  return (
    <div className="app">
      <TopBar
        view={view} setView={setView}
        openAsks={open} forMe={forMe}
        conn={conn} operator={me?.username ?? "…"}
        theme={theme} onToggleTheme={toggle}
      />
      <main className="stage">
        {notice && (
          <div className="notice" role="alert">
            <span>{notice}</span>
            <button type="button" aria-label="dismiss" onClick={() => setNotice(null)}>×</button>
          </div>
        )}
        <div className="solo">
          <div className="view">
            {view === "agents" && (
              <div className={"agents-page" + (panelFolded ? " agents-page--rail" : "")}>
                <Glance answer={answer} asks={asks} cards={cards} folded={panelFolded}
                  onFold={(folded) => setPrefs({ panel: folded ? "folded" : "open" })}
                  onOpenAgent={openAgent} onOpenAsk={openAsk} />
                <div className="agents-page__list">
                  <AgentList answer={answer} cards={cards} asks={asks} stats={agentStats}
                    prefs={prefs} onPrefs={setPrefs} query={listQuery} onQuery={setListQuery}
                    selected={agent?.name ?? null} panelFolded={panelFolded}
                    onOpenAgent={openAgent} onVisible={onVisible} />
                </div>
              </div>
            )}
            {view === "asks" && <Asks asks={asks} cards={cards} onOpen={openAsk} />}
            {view === "stats" && <Stats api={api} onFail={fail} />}
          </div>
        </div>
      </main>
      {/* #283: the detail is a drawer from the right, not modal: the page
          behind stays usable, a click on another agent swaps its content */}
      {detail && (
        <aside className="drawer" aria-label="Detail">
          <Detail api={api} card={agent} ask={askRow} asks={asks} stats={agentStats}
            onClose={closeDetail} onFail={fail} />
        </aside>
      )}
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
