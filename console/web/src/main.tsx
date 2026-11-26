// The tokens and shared rules first: every component's stylesheet loads
// after them and wins a tie of specificity.
import "./app.css"
import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import { App } from "./App"
import "./api/wire"  // the fixture's contract guards run at load
import { httpApi } from "./api/http"
import { mockApi } from "./api/mock"
import type { ConsoleApi } from "./api/types"
import { applyStoredTheme } from "./theme"

applyStoredTheme()  // before the first render: no flash of the other theme

// The console's own server: VITE_CB_CONSOLE_API is its base, else the old
// VITE_CONSOLE_API (a build has no process to warn in) — same-origin path,
// "" or "/api…", never a cross-origin URL: the login's cookie is
// same-origin. Behind the built bundle the server itself answers, so the
// mock runs in development only and stays out of it.
const base = (import.meta.env.VITE_CB_CONSOLE_API ??
  import.meta.env.VITE_CONSOLE_API) as string | undefined
const api: ConsoleApi = base || import.meta.env.PROD
  ? httpApi(base ?? "/")
  : mockApi()

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App api={api} />
  </StrictMode>,
)
