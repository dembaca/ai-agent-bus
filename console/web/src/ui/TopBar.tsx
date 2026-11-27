import { LogOut } from "lucide-react"
import type { ConnState } from "../api/types"
import { toggleLabel, type Theme } from "../theme"
import { Logo, MoonIcon, SunIcon } from "./icons"
import "./TopBar.css"

export type View = "agents" | "asks" | "graph" | "overview" | "reports"

export const VIEW_LABEL: Record<View, string> = {
  agents: "Agents",
  asks: "Asks",
  graph: "Graph",
  overview: "Overview",
  reports: "Reports",
}

// #200: the Overview first, and the console's default view
const NAV: readonly View[] = ["overview", "agents", "asks", "graph", "reports"]

/** The top bar (the agreed design, 2026-11-25; #200): the Choirbus logo and
 * wordmark, the views — the Overview first, the default — with the number of open asks
 * beside Asks (orange when one is the operator's), the fleet stream's state
 * (one state for the whole console), the theme toggle, and the logged-in
 * operator with the logout. The console is read-only. */
export function TopBar({ view, setView, openAsks, forMe, conn, operator, theme, onToggleTheme }: {
  view: View
  setView: (view: View) => void
  openAsks: number
  forMe: number
  conn: ConnState
  operator: string
  theme: Theme
  onToggleTheme: () => void
}) {
  return (
    <header className="topbar">
      <div className="topbar__inner">
        <button type="button" className="topbar__brand" onClick={() => setView("overview")}
          aria-label="Choirbus, the overview">
          <Logo />
          <span>Choirbus</span>
        </button>
        <nav className="topbar__nav" aria-label="Views">
          {NAV.map((id) => (
            <button
              key={id}
              type="button"
              className={"topbar__link" + (view === id ? " topbar__link--active" : "")}
              aria-current={view === id ? "page" : undefined}
              onClick={() => setView(id)}
            >
              <span className="topbar__label">
                <span>{VIEW_LABEL[id]}</span>
                {/* the bold width, reserved: picking a view moves nothing */}
                <span className="topbar__label-bold" aria-hidden="true">{VIEW_LABEL[id]}</span>
              </span>
              {id === "asks" && (
                <span className={"topbar__badge" + (forMe > 0 ? " topbar__badge--for-me" : "")}
                  title={forMe > 0 ? `${openAsks} open, ${forMe} for you` : `${openAsks} open`}>
                  {openAsks}
                </span>
              )}
            </button>
          ))}
        </nav>
        <div className="topbar__end">
          <span className={"topbar__conn topbar__conn--" + conn} title={"fleet stream: " + conn}>
            <i aria-hidden="true" />{conn === "syncing" ? "syncing…" : conn}
          </span>
          <ThemeToggle theme={theme} onToggle={onToggleTheme} />
          <span className="topbar__who" title={`signed in as ${operator}`}>
            <span className="topbar__avatar" aria-hidden="true">{initials(operator)}</span>
            <span className="topbar__name">{operator}</span>
          </span>
          {/* RT-6.43 step 9: a POST (no link or <img> can log anyone out);
              the server ends its session, then Keycloak's */}
          <form method="post" action="/logout" className="topbar__logout">
            <button type="submit" title="log out of the console and of Keycloak">
              <LogOut size={13} aria-hidden="true" /> Log out
            </button>
          </form>
        </div>
      </div>
    </header>
  )
}

/** The sun/moon button: it names the theme it switches to. */
export function ThemeToggle({ theme, onToggle }: { theme: Theme; onToggle: () => void }) {
  const label = toggleLabel(theme)
  return (
    <button type="button" className="topbar__icon" onClick={onToggle}
      aria-label={label} title={label}>
      {theme === "dark" ? <SunIcon /> : <MoonIcon />}
    </button>
  )
}

/** Two letters for the operator's square: the name's first letters. */
export function initials(name: string): string {
  const parts = name.split(/[^A-Za-z0-9]+/).filter(Boolean)
  if (parts.length === 0) return "?"
  const two = parts.length > 1 ? parts[0][0] + parts[1][0] : parts[0].slice(0, 2)
  return two.toUpperCase()
}
