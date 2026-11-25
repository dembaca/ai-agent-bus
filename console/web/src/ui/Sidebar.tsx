import { Bus, Crown } from "lucide-react"
import type { ConnState } from "../api/types"
import "./Sidebar.css"

export type View = "kanban" | "asks" | "graph" | "reports"

/** The sidebar with the views: Kanban, Graph and Reports, the number of
 * open asks beside them (ADR-015 point 3), the Asks view beside the stage
 * 1 cut — and, since one state stands for the whole console, the stream's
 * own state and the logged-in operator. The pilot is read-only. */
export function Sidebar({ view, setView, openAsks, forMe, conn, operator,
                          notice, clearNotice }: {
  view: View
  setView: (view: View) => void
  openAsks: number
  forMe: number
  conn: ConnState
  operator: string
  notice: string | null
  clearNotice: () => void
}) {
  const item = (id: View, label: string, badge?: number, strong = false) => (
    <button
      key={id}
      type="button"
      className={"side__item" + (view === id ? " side__item--active" : "")}
      aria-current={view === id ? "page" : undefined}
      onClick={() => setView(id)}
    >
      <span>{label}</span>
      {badge !== undefined && (
        <span className={"side__badge" + (strong ? " side__badge--for-me" : "")}>{badge}</span>
      )}
    </button>
  )
  return (
    <nav className="side" aria-label="views">
      <div className="side__brand">
        <span className="side__logo" aria-hidden="true"><Bus size={20} /></span>
        <span>Choirbus<br /><em>agent bus · operator console</em></span>
      </div>
      {item("kanban", "Kanban")}
      {item("asks", "Asks", openAsks, forMe > 0)}
      {item("graph", "Graph")}
      {item("reports", "Reports")}
      {notice && (
        <div className="side__notice" role="alert">
          <span>{notice}</span>
          <button type="button" className="side__notice-x" onClick={clearNotice}>×</button>
        </div>
      )}
      <div className="side__foot">
        <span className={"side__conn side__conn--" + conn} title={"fleet stream: " + conn}>
          <i /> {conn === "syncing" ? "syncing…" : conn}
        </span>
        <span>operator: {operator}</span>
        <span className="side__crown"><Crown size={11} /> the crown, a mark on the card</span>
        read-only pilot
      </div>
    </nav>
  )
}
