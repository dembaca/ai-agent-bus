# The console's server (ADR-015 pilot)

The server of the operator console, stage 1, **reading only**: it reads
the NATS homes' presence, crown claims and memberships through the
JetStream consumers the login service makes for each logged-in operator, serves the API of
[ADR-015 point 10](../../docs/arc42/09-architecture-decisions.md) —
`GET /v1/fleet` as server-sent events, `/v1/me`, `/v1/asks`, `/v1/graph`,
`/v1/stats`, `/v1/threads/<id>` — and with it the built browser app
([`console/web`](../web)). The browser logs in over Keycloak
(authorization code with PKCE, a client of its own, members of an
operators group); the access token is **verified against the realm's
JWKS** before anything is served (RISK-12), and nothing is stored on
disk.

Each logged-in operator reads the bus **as themselves**, with a NATS
credential the login service issues for that person (the console on the
bus as its operator,
[RT-6.43](../../docs/arc42/06-runtime-view.md#643-the-console-on-the-bus-as-its-operator),
steps 1 to 4). The pilot's static NATS account is gone: the server holds
no NATS secret of its own, and before a login it reads nothing.

## What it reads

At each login the server makes a key pair for that login (in memory only)
and connects to `CONSOLE_NATS_URL` with the operator's Keycloak access
token, bare, as the connect token; the key signs the server's nonce
([protocol.md, an operator's connection](../../docs/protocol.md#over-nats-an-operators-connection)).
The login service's auth callout checks the token (the console's client,
the operators' group) and issues the person's credential: 15 minutes,
publish nothing, subscribe only to the login's own deliver subjects, and
it makes the login's consumers. The server never derives the person's
name: it subscribes to `deliver.person.<login>.presence` and `.stats` by
their literal names (`<login>`: the login's whole public key, lower case)
and learns the name and anchor from the login's own record, which the
service writes at each issue on `presence.person.<login>.v1`
(`{"v":1,"name","anchor","iat","exp"}`, the anchor in its subject form)
and which arrives with the presence lines; its `exp` is that credential's
end, and the renewal comes five minutes before it at the latest. Until
the record arrives the reading says `waiting: …`, and after 30 s without
it says so — no
inbox, no JetStream call, nothing published. Each session has its own
fleet: what that person's consumers deliver (their presence consumer
delivers the last message per subject, then new ones). From the
deliveries (their JetStream metadata is the server's clock — never the
host's, as `natsbus.py` does it) it keeps the presence, membership and
crown-claim state, and the HELLO card's harness, model, task, branch and
open asks the presence states (RT-6.37 step 6, self-stated: shown as text,
never trusted); everything the bus does not carry today, and those fields
from an older MCP server, arrive as unknown (`null`/empty), never guessed as zero — the app shows `n/a`.

**Renewal.** Every 10 minutes the server refreshes the operator's access
token with the refresh token of the ordinary Keycloak session (the
standard flow's; no `offline_access`), checks it as at the login (the
signature, the console's client, the operators' group, the same person)
and connects again with the same key: the consumers keep their position.
The new connection subscribes while the old one still stands, then the
old one closes; a delivery seen on both is dropped the second time. A
lost connection is taken again the same way. When Keycloak refuses the
refresh (400 or 401; 408, 429 and 5xx are passing) or the refreshed
token lacks the operators' group or names another `sub`, the console
session ends: the key and the tokens are dropped and the browser is sent
to log in. A passing failure (Keycloak or NATS unreachable) is retried
while the old connection runs, up to the credential's 15 minutes. NATS answers
a refused connect (no operator, no anchor, too many logins of the person)
and a login service that is down or slow alike, so the server retries
with fresh tokens until the current credential's `exp`; only then does
the session's reading end, key and tokens wiped, while the session stays
and `/v1/me` says why in `bus.state` (`reading`, `waiting: …`, `refused: …`, `ended: …`, with
`bus.name` and `bus.anchor` once the record came). Logout and the end of
the session (`CONSOLE_SESSION_SEC`) close the connection and drop key and
tokens the same way. A login from a browser that holds a session ends
that session first, and one person keeps at most four sessions (by
`sub`): a fifth closes the oldest, below the login service's own cap.

The logout (the sidebar's button, `POST /logout`; a GET answers 405)
ends Keycloak's session too (RT-6.43 step 9): after dropping its own
session the server redirects to the realm's `end_session_endpoint` with
`client_id`, `post_logout_redirect_uri=<CONSOLE_PUBLIC_URL>/` and the
login's stored ID token as `id_token_hint`, expired or not (kept in
memory with the session; Keycloak checks the hint's signature, not its
expiry). Without one (after a restart) Keycloak asks the person to
confirm the logout.


Presence today carries `online`/`away` (format v1) — the Kanban's two
extra columns.

The asks and the reports (the console on the bus as its operator,
RT-6.43, steps 6 to 8): every MCP server with a NATS link publishes one
record of its open asks on `<presence>.ask.<home>.<name>`, inside the
`PRESENCE` stream, and each part of its STATS line on `<stats>.<name>`.
Both reach a session through the login's own consumers, never one of the
console's: the presence consumer the login service makes delivers the ask
records of every channel the person may see with the presence lines, on
`deliver.person.<login>.presence`; the stats consumer delivers the
`STATS` stream from 7 days back on `deliver.person.<login>.stats`. Each
session keeps its own fleet and its own reports.

The fleet decodes an ask record strictly (`natsbus.decode_ask_record`: a
record that breaks a rule is dropped whole with a warning that carries
none of it) and `/v1/asks` lists the asks of every record whose `exp` is
ahead of the floored clock, `expired` after 24 h (a hint), `agent_gone`
when its presence is not fresh. "For me" and "may answer" compare the
operator's anchor from the login's own record with the asked human's
anchor, never a login name (point 4 of the console decision's addition of
2026-11-25); until that record came they are `null` (`n/a`) and mark
nothing. Read-only: no answer, no thread text. `/v1/stats` answers the
two stage-1 reports (the bus's share of all tokens per version over 7
days; the asks over 24 h and 7 days), computed with `stats.py` from lines
decoded by `stats.decode_line`, each line once by its stream sequence;
every value without lines is `null` (`n/a`).

## NATS for the console

Nothing of the console's own. The login service needs its console path
on (`AUTH_CONSOLE_CLIENT_ID` naming the console's Keycloak client,
`AUTH_OPERATOR_GROUP`, its JetStream user's consumer rights on `STATS`;
`services/auth/README.md`). The pilot's console user in the NATS server's
configuration, its seed secret and `CONSOLE_NATS_SEED_FILE` are removed:
the server refuses to start while `CONSOLE_NATS_SEED_FILE` is set, and
never reads the file.

## Settings (all `CONSOLE_*`, checked on start; fail closed)

| Variable | What |
|---|---|
| `CONSOLE_PUBLIC_URL` | where the browser reaches the console (redirect URI); https off loopback |
| `CONSOLE_ISSUER`, `CONSOLE_CLIENT_ID` | Keycloak: the issuer and the console's own client |
| `CONSOLE_OPERATOR_GROUP` | the realm group whose members may log in (required: without it the console refuses to start) |
| `CONSOLE_ADDR` | listen, default `127.0.0.1:8747`; in a container set `0.0.0.0:8747` (a kubelet cannot probe a loopback listener) — outside one it stays loopback |
| `CONSOLE_TOKEN_ALGS` | the access token's allowed algs, default `RS256` (asymmetric only, never `HS*`/`none`) |
| `CONSOLE_NATS_URL` | `tls://` anywhere, `nats://` loopback only (as `natsbus`) |
| `CONSOLE_NATS_CA_FILE` | the CA for `tls://` |
| `CONSOLE_PRESENCE_SUBJECT`, `CONSOLE_PRESENCE_STREAM` | defaults `presence`, `PRESENCE` |
| `CONSOLE_STATS_SUBJECT`, `CONSOLE_STATS_STREAM` | the STATS lines' prefix (the login service's `AUTH_STATS_PREFIX`) and stream, defaults `stats`, `STATS` |
| `CONSOLE_NATS_SEED_FILE` | **gone**: set, it refuses the start (the static account's secret goes with it) |
| `CONSOLE_WEB_DIR` | the built browser app (`console/web/dist`) |
| `CONSOLE_SESSION_SEC` | session lifetime, default 8 h |
| `CONSOLE_LOGIN_OFF` | **tests and the mock only** (loopback): no login, the operator is a stand-in |

## The Keycloak client of the console's own (the lab's part)

- a **public** client with the authorization-code flow and PKCE enabled
  (the console is its own client, not the bus's device-flow client,
  ADR-014 point 6);
- one valid redirect URI: `<CONSOLE_PUBLIC_URL>/callback`, exact;
- one valid post logout redirect URI: `<CONSOLE_PUBLIC_URL>/`, exact
  (the logout's way back, RT-6.43 step 9; without it Keycloak refuses
  the logout's redirect);
- the **standard flow alone** (device authorization grant, direct access
  grants, implicit flow and service accounts off), refresh tokens on as
  Keycloak's default, and **no `offline_access`**: the server renews
  with the ordinary session's refresh token (RT-6.43 step 4);
- an **audience mapper** for the client: the verifier reads the client id
  in `aud` — Keycloak sets it only with one;
- a **group mapper** onto the `groups` claim: the match is exact — name
  the group `operators` and set the mapper's full path off, or name it
  `/operators` and set the full path on.

## Deployment notes

- **One replica.** The sessions live in memory (RISK-12: a restart logs
  everyone out, which is the state the pilot wants); a second replica
  would fight over login states and streams, and each login's NATS
  connection lives in the replica that made it.
- The Keycloak client needs **PKCE (S256)** and, in the **access token**:
  an **audience mapper** for the client id (`aud`, `azp`) and a **group
  mapper** onto `groups` (full path on, so the claim carries
  `/operators`; `CONSOLE_OPERATOR_GROUP` names what the check matches
  exactly). **`preferred_username` is required** — it is the operator's
  name on every view — and `name` (the display name) is welcome.
- A `tls://` NATS URL needs the lab server's `handshake_first: true`
  (the console connects TLS first, as every host does).
- **`/healthz` is the readiness probe**; **`/livez` the liveness probe**.
  `/healthz` answers 200 when the settings held at the start and the NATS
  server answers on `CONSOLE_NATS_URL` (a verified TLS handshake for
  `tls://`, a TCP connect for loopback `nats://`; asked at most every
  5 s), else 503 "the NATS server does not answer". It waits for no
  fleet: there is no reader before a login, and each login's connection
  renews and retries on its own. `/livez` answers 200 while the server
  serves — **a liveness probe on `/healthz` would restart the pod while
  NATS is merely down**.

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
