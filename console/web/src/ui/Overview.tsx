import type { AgentCard, Ask } from "../api/types"
import { since } from "../clock"
import { askerName, countByGroup, openAsksFirst } from "./agents"
import "./Overview.css"

const GROUPS = [
  { id: "working", label: "working", tone: "task" },
  { id: "listening", label: "listening", tone: "done" },
  { id: "idle", label: "idle", tone: "idle" },
  { id: "offline", label: "offline", tone: "gone" },
  { id: "other", label: "other states", tone: "other" },
] as const

/** The Overview (the agreed design, 2026-11-25), from what the console
 * already holds: the live fleet and the open asks. Key figures, what needs
 * the operator, the agents by status, and the bus map's entry (the Graph).
 * Counts only, nothing made up: what the API does not carry (posts per
 * hour, tokens per agent) is not on this page; the reports have the rest. */
export function Overview({ cards, asks, onOpenAsk, onView }: {
  cards: AgentCard[]
  asks: Ask[]
  onOpenAsk: (ask: Ask) => void
  onView: (view: "agents" | "asks" | "graph" | "reports") => void
}) {
  const counts = countByGroup(cards)
  const open = openAsksFirst(asks)
  const oldest = open.reduce<number | null>((min, a) => (min === null || a.at < min ? a.at : min), null)
  const mine = open.filter((a) => a.forMe === true || a.mayAnswer === true)
  const forMe = open.filter((a) => a.forMe === true).length
  const homes = new Set(cards.map((c) => c.home))
  const guests = new Set(cards.flatMap((c) => c.guests).filter((g) => !homes.has(g)))
  const crowns = cards.filter((c) => c.crown).length
  const total = cards.length

  return (
    <>
      <div className="page-head">
        <div className="page-head__title">
          <h1>Overview</h1>
          <span className="page-head__sub">the fleet and its open asks, live</span>
        </div>
      </div>

      <section className="figures" aria-label="Key figures">
        <button type="button" className="panel figure" onClick={() => onView("agents")}>
          <span className="figure__label">Reachable agents</span>
          <span className="figure__value">{total - counts.offline} <small>of {total}</small></span>
          <span className="figure__note">{counts.offline} offline</span>
        </button>
        <button type="button" className="panel figure figure--ask" onClick={() => onView("asks")}>
          <span className="figure__label">Open asks</span>
          <span className="figure__value figure__value--ask">{open.length}</span>
          <span className="figure__note">{oldest === null ? "none waits" : `the oldest waits ${since(oldest)}`}</span>
        </button>
        <div className="panel figure">
          <span className="figure__label">For you</span>
          <span className={"figure__value" + (forMe > 0 ? " figure__value--ask" : "")}>{forMe}</span>
          <span className="figure__note">asks whose asked human is you</span>
        </div>
        <div className="panel figure">
          <span className="figure__label">Working now</span>
          <span className="figure__value figure__value--task">{counts.working}</span>
          <span className="figure__note">{crowns} {crowns === 1 ? "coordinator" : "coordinators"}</span>
        </div>
      </section>

      <div className="overview__row">
        <section className="panel overview__status" aria-label="Agents by status">
          <h2 className="section-title">Agents by status</h2>
          {total === 0
            ? <p className="hint">No agent on the bus yet.</p>
            : (
              <>
                <div className="status-bar" role="img"
                  aria-label={GROUPS.filter((g) => counts[g.id] > 0).map((g) => `${counts[g.id]} ${g.label}`).join(", ")}>
                  {GROUPS.filter((g) => counts[g.id] > 0).map((g) => (
                    <span key={g.id} className={`status-bar__seg tone--${g.tone}`} style={{ flex: counts[g.id] }} />
                  ))}
                </div>
                <dl className="status-legend">
                  {GROUPS.filter((g) => g.id !== "other" || counts.other > 0).map((g) => (
                    <div key={g.id}>
                      <dt><span className={`status-legend__key tone--${g.tone}`} aria-hidden="true" />{g.label}</dt>
                      <dd>{counts[g.id]}</dd>
                    </div>
                  ))}
                </dl>
              </>
            )}
        </section>

        <section className="panel overview__needs" aria-label="Needs you">
          <h2 className="section-title overview__needs-title">Needs you</h2>
          {mine.length === 0 && <p className="hint overview__needs-empty">Nothing waits on you.</p>}
          {mine.slice(0, 6).map((ask) => (
            <button key={ask.id} type="button" className="needs" onClick={() => onOpenAsk(ask)}>
              <span className="needs__top">
                <strong>{ask.need}</strong>
                <span className="needs__age">{since(ask.at)}</span>
              </span>
              <span className="needs__by">
                <span className="mono" title={ask.by}>{askerName(ask, cards).text}</span>
                {ask.forMe ? " · for you" : " · you may answer"}
              </span>
            </button>
          ))}
          <button type="button" className="overview__more" onClick={() => onView("asks")}>All asks</button>
        </section>

        <section className="panel overview__map" aria-label="Bus map">
          <div className="overview__map-head">
            <h2 className="section-title">Bus map</h2>
            <button type="button" className="overview__more" onClick={() => onView("graph")}>Open the graph</button>
          </div>
          <div className="overview__map-body">
            <span><strong>{homes.size}</strong> {homes.size === 1 ? "home" : "homes"}</span>
            <span><strong>{guests.size}</strong> guest {guests.size === 1 ? "channel" : "channels"}</span>
            <span><strong>{total}</strong> {total === 1 ? "agent" : "agents"}</span>
            <span className="hint">You, the open asks, the agents and their channels, as a live graph.</span>
          </div>
        </section>
      </div>

      <p className="hint">
        Asks to humans over the last day and week, the bus overhead and the
        tokens by category are in the <button type="button" className="linkish overview__link"
          onClick={() => onView("reports")}>reports</button>.
      </p>
    </>
  )
}
