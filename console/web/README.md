# The operator console's browser app (ADR-015)

The browser app of the operator console ([ADR-015](../docs/arc42/09-architecture-decisions.md)),
built as a **read-only pilot** against a mock of the console API: React with
TypeScript on Vite, as [`site/`](../site) already is. The console server does
not exist yet — the app is the stage a scope-shrink keeps either way.

- **The mock** (`src/api/mock.ts`) serves the API of ADR-015 point 10 behind the
  same `ConsoleApi` interface the real server will implement:
  `GET /v1/fleet` as a stream (snapshot, then changes), `/v1/asks`, `/v1/graph`,
  `/v1/stats`, `/v1/threads/<id>`. Its data is shaped like today's bus and it
  changes on its own, so the Kanban visibly lives without a backend.
- **Read-only** (ADR-015 stage 1): nothing posts, an ask gets no answer button.
  An ask is answered where it is answered today, in the humans' chat; the
  console shows it open until the agent ends it. There is deliberately no
  `POST /v1/asks/<id>/answer` in this app yet.
- **Views**: Kanban of agents by state (the crown is a mark on the card, not a
  column), asks with *for me* / *may answer*, the graph (trusts and
  coordinates, cut at depth two with a mark), and reports — counts and seconds
  only, a missing field shows as `n/a`, never as 0.
- **The detail pane**: the full card, the task rows the agent holds, the
  session's age, the time since its last post, its HELLO thread and the threads
  of its open asks.

When the console server exists, point the app at it:
`VITE_CONSOLE_API=https://…` (see `src/main.tsx`; without it, the mock runs).
The operator of the mock is a stand-in; the real visibility rule lives in the
login service, not in this interface.

## Run

```bash
npm install
npm run dev        # the mock bus at http://localhost:5173
npm run typecheck  # tsc --noEmit
npm run build      # vite build → dist/
```

CI builds it like `site/`: `npm ci && npm run typecheck && npm run build`
(the `console` job in the workflow).
