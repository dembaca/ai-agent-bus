import "./Sidebar.css"

export type View = "kanban" | "asks" | "graph" | "reports"

/** The sidebar with the views: Kanban, Graph and Reports, the number of
 * open asks beside them (ADR-015 point 3), the Asks view beside the stage
 * 1 cut. The pilot is read-only. */
export function Sidebar({ view, setView, openAsks, forMe }: {
  view: View
  setView: (view: View) => void
  openAsks: number
  forMe: number
}) {
  const item = (id: View, label: string, badge?: number, strong?: boolean) => (
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
        <span className="side__logo" aria-hidden="true">🚌</span>
        <span>agent bus<br /><em>operator console</em></span>
      </div>
      {item("kanban", "Kanban")}
      {item("asks", "Asks", openAsks, forMe > 0)}
      {item("graph", "Graph")}
      {item("reports", "Reports")}
      <div className="side__foot">
        read-only pilot<br />
        operator: dembaca
      </div>
    </nav>
  )
}
