# The operator console's browser app, "Choirbus" (ADR-015 pilot)

The browser app of the operator console ([ADR-015](../../docs/arc42/09-architecture-decisions.md)),
built as a **read-only pilot**: React with TypeScript on Vite, as
[`site/`](../../site) already is. It runs against a mock of the console API in
development and against the console server ([`../server`](../server)) in its
image.

- **The mock** (`src/api/mock.ts`) serves the API of ADR-015 point 10 behind the
  same `ConsoleApi` interface the real server implements:
  `GET /v1/fleet` as a stream (snapshot, then changes), `/v1/me`, `/v1/asks`,
  `/v1/groups`, `/v1/stats`, `/v1/threads/<id>`. Its data is shaped like
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
- **Layout and views** (ADR-015 point 3; #283): a top bar with the logo and
  wordmark (which leads to the agents), the three views Agents (the start
  page), Asks and Stats, the flock stream's state, the theme toggle and the
  operator with the logout; a footer with the console's release version and
  short commit from `/v1/me`'s `build` (#182; n/a in a development run). The
  Overview page, the bus map and the Graph view are gone, and with them the
  React Flow and dagre libraries.
  - **Agents** (point 6): the panel "At a glance" on the left and the list on
    the right. The list has one row per agent in three columns (left: name,
    coordinator mark, "waits on you", harness and model, the context bar and
    the tokens of today; middle: the state and "last post …"; right: repo and
    branch with the PR chip above the task), grouped by Home, Repo, Owner,
    Status or None, with a header per group (a small bar of its states, "n of m
    reachable", "k working", "n for you", the group's tokens of today with the
    number of agents they cover) that folds it; a scope switch Mine (the
    default), Trusting me or Everyone (in Everyone the groups start folded
    except those that need the operator, and groups with nobody reachable and
    no open ask fold into one row, "22 quiet homes"); a search over home, agent
    and person. **The server counts** (`GET /v1/groups`, `src/api/types.ts`
    `GroupsAnswer`): the browser draws the rows from the stream's cards and
    counts nothing over them. The panel has four tiles (Reachable, Working
    now, For you, You may answer: the operator's own number large, the
    flock's small; only "For you" lights, and only above zero), "Needs you"
    (a click opens the asking agent) and "Flock by status"; it folds to a rail
    with three numbers (asks for you, reachable, working), and each row then
    gains a fourth column (posts and tool calls of today, the owner); on a
    narrow window (under 900 px) it sits above the list and starts folded.
    A field the API does not carry yet (the card's context, PR, repo) is left
    out of the row, never invented. The last post, the tokens and the posts and
    tool calls show only for own or trusted agents (`src/ui/agents.ts` holds
    the sentences, quoted by the server's docs): "No per-agent numbers for
    agents of other owners. Rows are of own or trusted agents running now: one
    has no row until its first STATS part arrives, and loses it when it goes
    offline." A group's token sum adds up the agents the operator may see
    numbers of and says how many that are.
  - **Remembered per person** (`src/prefs.ts`, this browser's `localStorage`
    under the operator's name; a storage that refuses is no error): the panel
    open or folded, the grouping, the folded groups and the scope.
  - **Asks** with *for you* / *may answer*, and **Stats** (formerly Reports) —
    counts and seconds only, a missing field shows as `n/a`, never as 0.
  - **The detail** of an agent or an ask is a drawer from the right, about
    half the width (the whole width below 800 px), not modal: the page behind
    stays usable, a click on another agent swaps the content, the cross, Esc or
    a second click on the same row closes it, arrow up and down move to the
    next agent of the list. The open agent is part of the address
    (`?agent=<name>`, an ask `&ask=<id>`, a view other than Agents
    `&view=asks`), so a link or reload opens the same drawer (`src/nav.ts`).
    It shows the full card, the task rows the agent holds, the session's age,
    the time since its last post, a "Today" block, its HELLO thread and the
    threads of its open asks.

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
npm test           # vitest: the groups and panel, the remembered choices and the address, the mock, the theme, the footer
npm run build      # vite build → dist/
```

CI builds it like `site/`: `npm ci && npm run typecheck && npm test && npm run build`
(the `console` job in the workflow). The image build writes `dist/build.json`
(`scripts/build-info.mjs`: the build arguments `VERSION` and `REVISION`)
for the server to serve; see
[`../server/README.md`](../server/README.md).
