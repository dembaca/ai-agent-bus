# The operator console's browser app, "Choirbus" (ADR-015 pilot)

The browser app of the operator console ([ADR-015](../../docs/arc42/09-architecture-decisions.md)),
built as a **read-only pilot**: React with TypeScript on Vite, as
[`site/`](../../site) already is. It runs against a mock of the console API in
development and against the console server ([`../server`](../server)) in its
image.

- **The mock** (`src/api/mock.ts`) serves the API of ADR-015 point 10 behind the
  same `ConsoleApi` interface the real server implements:
  `GET /v1/fleet` as a stream (snapshot, then changes), `/v1/me`, `/v1/asks`,
  `/v1/graph`, `/v1/stats`, `/v1/threads/<id>`. Its data is shaped like
  today's bus and it changes on its own, so the agents list visibly lives
  without a backend. It is development only: the production bundle carries
  neither the mock nor its sample data.
- **Read-only** (ADR-015 stage 1): nothing posts, an ask gets no answer button.
  An ask is answered where it is answered today, in the humans' chat; the
  console shows it open until the agent ends it. There is deliberately no
  `POST /v1/asks/<id>/answer` in this app yet.
- **The look**: the design the owner agreed on 2026-11-25, in the website's
  Choirbus brand — tokens in `src/app.css` after `site/src/page.css`, the
  logo's navy as the accent, the site's word colors (orange asks, blue tasks,
  green done), Inter only (self-hosted, `src/fonts/`, hashed into `/assets/`
  by the build), compact rows, soft shadows, 2 px corners. Light by default;
  dark follows `prefers-color-scheme` until the top bar's sun/moon toggle
  picks one, kept in this browser's `localStorage` (`src/theme.ts`; a
  storage that refuses is no error). `prefers-reduced-motion` turns
  transitions off.
- **Layout and views** (ADR-015 point 3): a top bar with the logo and
  wordmark, the views, the fleet stream's state, the theme toggle and the
  operator with the logout; a footer with the console's release version and
  short commit from `/v1/me`'s `build` (#182; n/a in a development run).
  - **Overview**, the first tab and the default view (#200): agents
    reachable and by status, the open asks, those for you, the way to the
    graph; tiles of a row share their height, columns follow the view's
    width.
  - **Agents** (point 6): the open asks on top, then one compact
    row per agent — state, crown, harness and model, task, branch or PR,
    home and guest channels, context, open asks, last post — with a search
    and a status filter (`src/ui/agents.ts`). It replaced the Kanban. An
    agent shows the name its presence states (#199), a name another card
    states too with a "shared name" mark; the bus id is in the title.
  - **Asks** with *for you* / *may answer*, **Graph** (React Flow: trusts and
    coordinates, cut at depth two with a mark) and **Reports** — counts and
    seconds only, a missing field shows as `n/a`, never as 0.
  - **The detail** of an agent or an ask opens beside the view, half and
    half (#200), the view still in sight; below 1000 px it is the whole
    page. It shows the full card, the task rows the agent holds, the
    session's age, the time since its last post, its HELLO thread and the
    threads of its open asks; a breadcrumb and the browser's Back lead back,
    the list's search and filter stay.

The app talks to the console server same-origin, as its login needs the
cookie: `VITE_CB_CONSOLE_API` is the API base path (`""` to serve the API beside
the built app, or a path like `/api` in front of it) — never a cross-origin URL:
the session cookie stays same-origin and a 401's answer is the server's own
`/login` route (see `src/api/http.ts`; without a base, the mock runs). For
development, Vite's dev server proxies the API to a console server on
`127.0.0.1` with a `server.proxy` entry in `vite.config.ts`.
The operator of the mock is a stand-in; the real visibility rule lives in the
login service, not in this interface.

## Run

```bash
npm install
npm run dev        # the mock bus at http://localhost:5173
npm run typecheck  # tsc --noEmit
npm test           # vitest: the graph model, the mock, the theme, the list's filter, the footer
npm run build      # vite build → dist/
```

CI builds it like `site/`: `npm ci && npm run typecheck && npm test && npm run build`
(the `console` job in the workflow). The image build writes `dist/build.json`
(`scripts/build-info.mjs`: the build arguments `VERSION` and `REVISION`)
for the server to serve; see
[`../server/README.md`](../server/README.md).
