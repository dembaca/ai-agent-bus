# The console's server (ADR-015 pilot)

The server of the operator console, stage 1, **reading only**: it reads
the NATS homes' presence, crown claims and memberships through the same
JetStream consumers an agent reads through, serves the API of
[ADR-015 point 10](../../docs/arc42/09-architecture-decisions.md) —
`GET /v1/fleet` as server-sent events, `/v1/me`, `/v1/asks`, `/v1/graph`,
`/v1/stats`, `/v1/threads/<id>` — and with it the built browser app
([`console/web`](../web)). The browser logs in over Keycloak
(authorization code with PKCE, a client of its own, members of an
operators group); the access token is **verified against the realm's
JWKS** before anything is served (RISK-12), and nothing is stored on
disk.

Pilot deviation, decided by the owner (2026-11-24): **one read-only NATS
account for the whole console** — lab, one tenant. ADR-015 way A (a
credential per person, made by the login service) follows after the
pilot.

## What it reads

With `CONSOLE_NATS_URL` set, the server connects with its static account
and makes its own push consumer on the PRESENCE stream — the same shape
the login service makes for an agent: no acks, the last message per
subject replayed (so a reconnect finds the fleet as it stands), a
one-hour idle threshold. From the deliveries (their JetStream metadata is
the server's clock — never the host's, as `natsbus.py` does it) it keeps
the presence, membership and crown-claim state, and the HELLO card's
harness, model, task, branch and open asks the presence states (RT-6.37
step 6, self-stated: shown as text, never trusted); everything the bus
does not carry today, and those fields from an older MCP server, arrive as
unknown (`null`/empty), never guessed as zero — the app shows `n/a`.

Presence today carries `online`/`away` (format v1) — the Kanban's two
extra columns. Asks and the STATS lines do not lie on NATS yet:
`/v1/asks` and `/v1/stats` answer empty until they do (stage 1's cut).

## NATS account for the lab (the pilot's deviation)

A user of the tenant (`BUS`) account, **publishing nothing but its two
consumer calls**, reading only its own deliver subject and inbox — the
same rights its live test grants (`tests/test_nats_live.py`):

```
{ nkey: "<CONSOLE PUBLIC NKEY>", permissions: {
    publish: { allow: ["$JS.API.CONSUMER.CREATE.PRESENCE.console",
                       "$JS.API.CONSUMER.DELETE.PRESENCE.console"] },
    subscribe: { allow: ["deliver.console.presence", "_INBOX_console.>"] } } }
```

The consumer is **deleted and made anew at every connect** (as the login
service remakes an agent's at every credential, Go
`consumers/manager.go`): a consumer that still stands under its name
replays nothing on reattach, and the replay is the fleet's state after a
restart. The seed goes to `CONSOLE_NATS_SEED_FILE`, readable by its
owner alone — the server refuses wider. The PRESENCE stream
(`services/auth`: `consumers/streams.go`) must exist; the login service's
`AUTH_JS_SEED_FILE` user or the lab checklist owner makes it.

**Lab risks, named for the pilot** (dembaca's go, 2026-11-24; both close
with the console's own NATS account before the stage after the pilot,
decided the same day, ADR-015):

1. **The reply subject of a consumer request can overwrite the PRESENCE
   state of agents.** Someone holding the console's NATS credential can
   publish through it; nothing can be forged (the sender stays pinned in
   the subject, RISK-09 holds), but state can be erased. It heals by
   republishing: an overwritten presence within 30 s (`HEARTBEAT_SEC`),
   attestation and membership with the agent's next credential renewal —
   at the latest after about 14 minutes (`RENEW_SEC`; attestation is
   written by the login service at every credential, membership returns
   on the renewal's reconnect), and **the crown claim at the agent's
   next crown tick** (the tick claims anew when this agent sees no claim
   of its own or one older than 23 h, `lease.py`; a reconnect alone does
   not re-claim it). A host already running keeps its copy in memory; a
   host started in between sees no attestation until then and judges the
   sender as foreign — fail closed.
2. **The consumer's deliver subject is freely chosen** — NATS
   permissions cannot close what a consumer-create names, so a
   taken-over console account could aim its consumer at `bus.evil.x`.
   Agents drop foreign deliveries (each delivers only to
   `deliver.<name>.…`), so this widens reading into the streams, not
   forgery.

The seed belongs to the lab's console service account: whoever runs the
lab checklist creates it and mounts it as a read-only secret file, owned
by uid 65532 (the distroless nonroot the image runs as) and readable by
that user only; the console owns nothing else. After the pilot an admin (or the login
service, way A) makes the console's consumer and the account keeps only
the read.

## Settings (all `CONSOLE_*`, checked on start; fail closed)

| Variable | What |
|---|---|
| `CONSOLE_PUBLIC_URL` | where the browser reaches the console (redirect URI); https off loopback |
| `CONSOLE_ISSUER`, `CONSOLE_CLIENT_ID` | Keycloak: the issuer and the console's own client |
| `CONSOLE_OPERATOR_GROUP` | the realm group whose members may log in (required: without it the console refuses to start) |
| `CONSOLE_ADDR` | listen, default `127.0.0.1:8747`; in a container set `0.0.0.0:8747` (a kubelet cannot probe a loopback listener) — outside one it stays loopback |
| `CONSOLE_TOKEN_ALGS` | the access token's allowed algs, default `RS256` (asymmetric only, never `HS*`/`none`) |
| `CONSOLE_CONSUMER_NAME` | the consumer's name on the PRESENCE stream, default `console`; the account's publish rights name it too |
| `CONSOLE_HEALTH_SYNC_BOUND` | `/healthz` goes 503 once the reader has seen no replay for this long, default 180 s |
| `CONSOLE_NATS_URL` | `tls://` anywhere, `nats://` loopback only (as `natsbus`) |
| `CONSOLE_NATS_SEED_FILE` | the read-only account's nkey seed |
| `CONSOLE_NATS_CA_FILE` | the CA for `tls://` |
| `CONSOLE_DELIVER_SUBJECT` | default `deliver.console.presence` |
| `CONSOLE_PRESENCE_SUBJECT`, `CONSOLE_PRESENCE_STREAM` | defaults `presence`, `PRESENCE` |
| `CONSOLE_WEB_DIR` | the built browser app (`console/web/dist`) |
| `CONSOLE_SESSION_SEC` | session lifetime, default 8 h |
| `CONSOLE_LOGIN_OFF` | **tests and the mock only** (loopback): no login, the operator is a stand-in |

## The Keycloak client of the console's own (the lab's part)

- a **public** client with the authorization-code flow and PKCE enabled
  (the console is its own client, not the bus's device-flow client,
  ADR-014 point 6);
- one valid redirect URI: `<CONSOLE_PUBLIC_URL>/callback`, exact;
- an **audience mapper** for the client: the verifier reads the client id
  in `aud` — Keycloak sets it only with one;
- a **group mapper** onto the `groups` claim: the match is exact — name
  the group `operators` and set the mapper's full path off, or name it
  `/operators` and set the full path on.

## Deployment notes

- **One replica.** The sessions live in memory (RISK-12: a restart logs
  everyone out, which is the state the pilot wants); a second replica
  would fight over login states and streams. Scale out comes with the
  per-person credential of ADR-015 way A after the pilot.
- The Keycloak client needs **PKCE (S256)** and, in the **access token**:
  an **audience mapper** for the client id (`aud`, `azp`) and a **group
  mapper** onto `groups` (full path on, so the claim carries
  `/operators`; `CONSOLE_OPERATOR_GROUP` names what the check matches
  exactly). **`preferred_username` is required** — it is the operator's
  name on every view — and `name` (the display name) is welcome.
- A `tls://` NATS URL needs the lab server's `handshake_first: true`
  (the console connects TLS first, as every host does).
- **`/healthz` is the readiness probe**; **`/livez` the liveness probe**.
  `/healthz` answers 503 until the reader has synced — a finished replay,
  an empty one included (a consumer created with nothing left to deliver
  stamps it, so a fleet with zero agents is healthy) — and again once no
  replay arrived within `CONSOLE_HEALTH_SYNC_BOUND` (30 s to 24 h,
  default 180 s; the reader reconnects for a replay between 5/8 and 7/8
  of the bound — three quarters taken back half a watch tick, checked
  every quarter of it).
  **A liveness probe on `/healthz` would restart the pod while NATS is
  merely slow** — use `/livez`: it answers 503 only when the reader
  ended without a stop; everything else retries on its own.

## Run

```bash
uv sync                       # installs the mcp package by path, editable
uv run python -m console_server

# the tests (the NATS ones run against a real nats-server, as mcp's:
# CI downloads it; locally point MM_NATS_SERVER at one)
MM_NATS_SERVER=./nats-server MM_TEST_NATS=1 uv run pytest -q
```

The image (`Dockerfile`, built from the repository root) carries the
built browser app and the server, no shell, not root, like the login
service's image. The release pins its bases by digest. `checks.yml`'s
`publish-console` job publishes it, and only after the console's two jobs
have passed: each push to `main` that changes `console/`, `mcp/` (the server's
path dependency) or that workflow builds it from the repository root and pushes `ghcr.io/dembaca/ai-agent-bus-console:<full commit
SHA>` and `:main`, with a build provenance attestation; the digest stands in
the job's summary, and a deployment names the image by its digest, never by a
tag — as `services/auth/README.md` puts it for the login service.
