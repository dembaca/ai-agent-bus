import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import { App } from "./App"
import { httpApi } from "./api/http"
import { mockApi } from "./api/mock"
import type { ConsoleApi } from "./api/types"
import "./app.css"

// The console's own server, when it exists: VITE_CONSOLE_API is its base
// URL. Until then the app runs against the mock, so every view is
// buildable and reviewable without a backend.
const base = import.meta.env.VITE_CONSOLE_API as string | undefined
const api: ConsoleApi = base ? httpApi(base) : mockApi()

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App api={api} />
  </StrictMode>,
)
