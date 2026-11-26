# Operator console, server and browser app

The operator console ([ADR-015](../../docs/arc42/09-architecture-decisions.md)),
stage 1, **reading only**:

- [`web/`](web/) — the browser app ("Choirbus"), a React + TypeScript + Vite
  pilot built against a mock of the API.
- [`server/`](server/) — the console server: it reads the NATS homes' presence,
  crown claims and memberships, the agents' records of their open asks and
  their STATS lines (RT-6.43, decoded strictly; the asks shown, never
  answered) and serves the API of ADR-015 point 10
  (`/v1/fleet` streamed over SSE, `/v1/me`, `/v1/asks`, `/v1/graph`,
  `/v1/stats`, `/v1/threads/<id>`), plus the built browser app. A card
  names its `home` and its `guests`; the graph draws the home and the
  guest channels apart and marks the operator's own person node `you`
  (#147).
