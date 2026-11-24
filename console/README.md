# Operator console, server and browser app

The operator console ([ADR-015](../../docs/arc42/09-architecture-decisions.md)),
stage 1, **reading only**:

- [`web/`](web/) — the browser app ("Choirbus"), a React + TypeScript + Vite
  pilot built against a mock of the API.
- [`server/`](server/) — the console server: it reads the NATS homes' presence,
  crown claims and memberships and serves the API of ADR-015 point 10
  (`/v1/fleet` streamed over SSE, `/v1/me`, `/v1/asks`, `/v1/graph`,
  `/v1/stats`, `/v1/threads/<id>`), plus the built browser app.
