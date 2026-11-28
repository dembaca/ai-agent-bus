# The console's server (ADR-015 pilot)

The server of the operator console, stage 1, **reading only**: it reads
the NATS homes' presence, crown claims and memberships through the
JetStream consumers the login service makes for each logged-in operator, serves the API of
[ADR-015 point 10](../../docs/arc42/09-architecture-decisions.md) —
`GET /v1/fleet` as server-sent events, `/v1/me`, `/v1/asks`, `/v1/groups`,
`/v1/stats`, `/v1/agent-stats` (own or trusted agents only), `/v1/threads/<id>`, and `GET /metrics` in the Prometheus text
format (#158, RT-6.43 step 10: behind the operator's login like the rest,
the console's own observations and that session's agent numbers, labels
bounded — no agent, owner or home) — and with it the built browser app.
**The unattended variant**: set both `CB_CONSOLE_METRICS_NKEY_SEED_FILE` (the seed of the console's subscribe-only nkey, its two consumers the login service makes) and `CB_CONSOLE_METRICS_TOKEN_FILE` (the Bearer scrape token) — both or neither, the start refuses half a pair — and Prometheus scrapes **a listener of the unattended page's own** (`CB_CONSOLE_METRICS_LISTEN_ADDR`, host:port, default 127.0.0.1:8748, loopback as the owner's ask said): the token's page there serves the service's own reading, aggregates only, every other path 404; the operator's page stays on the main listener, behind the login. An unattended scrape comes from the approved follow-up (a
loopback listener with a scrape token of its own, plus a read-only service
account on the STATS stream — the owner's go, #158), not from a copied
cookie.
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
and connects to `CB_CONSOLE_NATS_URL` with the operator's Keycloak access
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
open asks the presence states, with the time of the agent's last post in
its home (`lastPostAt`; dropped when more than 5 minutes after the
presence that states it, else at most that presence's time; on the cards
of own or trusted agents only, `Fleet.own_or_trusted` with the operator's
anchor as for `/v1/agent-stats`, null on every other card and before the
login's own record arrived) (RT-6.37 step 6, self-stated: shown as text,
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
the session (`CB_CONSOLE_SESSION_SEC`) close the connection and drop key and
tokens the same way. A login from a browser that holds a session ends
that session first, and one person keeps at most four sessions (by
`sub`): a fifth closes the oldest, below the login service's own cap.

The logout (the top bar's button, `POST /logout`; a GET answers 405)
ends Keycloak's session too (RT-6.43 step 9): after dropping its own
session the server redirects to the realm's `end_session_endpoint` with
`client_id`, `post_logout_redirect_uri=<CB_CONSOLE_PUBLIC_URL>/` and the
login's stored ID token as `id_token_hint`, expired or not (kept in
memory with the session; Keycloak checks the hint's signature, not its
expiry). Without one (after a restart) Keycloak asks the person to
confirm the logout. The hint travels in the redirect URL (the browser's
history, Keycloak's and its proxy's access logs); the redirect is
`no-store` and `no-referrer`; the pages themselves send
`Referrer-Policy: same-origin`, so the browser names the console's origin
on the logout POST (under `no-referrer` it sends `Origin: null`, which the
logout refuses). Without an end-session endpoint, or before
the issuer's endpoints are discovered, the logout ends the console's
session and answers a page that says the Keycloak session is still open.


Presence today carries `online`/`away` (format v1) — the agents list counts
them as listening and idle.

A card's `displayName` is the bot's username the agent's presence states,
confirmed only while no other card states it too and it is no other card's
bus name (full or short), else the short bus name.
Beside it, `statedName` carries the stated username even when it is shared,
and `nameShared` says so (#199): the web shows the name with a "shared
name" mark instead of the bare bus id (a 25-character hash), never as a
confirmed name. `statedName` is `""` when the presence states none.

Each card names its `home` and, beside it, its `guests`: the channels its
memberships name that are not its home (every granted channel gets one,
RT-6.37). The home is what the bus itself states — the channel of the
agent's ask record's subject first (RT-6.43 step 5), else the channel it
claims the crown of (only a home may be claimed, RT-6.38), else the first
channel its memberships name; a statement that does not sit beside a
membership is no home; nothing is guessed from an agent's name (#147).
`/v1/groups?by=home|repo|owner|status|none&scope=mine|trusting|everyone`
(#283, RT-6.43 step 11) counts what the agents list and its panel "At a
glance" show: per group the agents' names, the state counts, the reachable and
working agents, the open asks, those for the operator and those it may answer,
and the sum of today's tokens, and the panel's numbers for the operator's own
agents and for the whole flock. "Mine" is the agents whose fresh attestation
names the operator's anchor as owner, "trusting" those that name it as owner or
trusted (so each scope widens the one before), "everyone" all. The panel's ask
tiles are `glance.asks`: `forYou`, `mayAnswer` and `open`, counted over all open
asks in view whoever owns the asking agent. The token sum covers only the
agents the operator may see per-agent numbers of (own or trusted) that state a
count and says how many (`tokensAgents` of `total`); null where none states a count; without the anchor nothing is mine and
no ask is for you. An unknown `by` or `scope` is a 400.

The asks and the reports (the console on the bus as its operator,
RT-6.43, steps 6 to 8): every MCP server with a NATS link publishes one
record of its open asks on `<presence>.ask.<home>.<name>`, inside the
`PRESENCE` stream, and each part of its STATS line on `<stats>.<name>`.
Both reach a session through the login's own consumers, never one of the
console's: the presence consumer the login service makes delivers the ask
records of every channel the person may see with the presence lines, on
`deliver.person.<login>.presence`; the stats consumer delivers the
`STATS` stream from 365 days back (the whole history, for the Stats
page's time range up to 12 months) on `deliver.person.<login>.stats`. Each
session keeps its own fleet and its own reports.

The fleet decodes an ask record strictly (`natsbus.decode_ask_record`: a
record that breaks a rule is dropped whole with a warning that carries
none of it) and `/v1/asks` lists the asks of every record whose `exp` is
ahead of the floored clock, `expired` after 24 h (a hint), `agent_gone`
when its presence is not fresh. "For me" and "may answer" compare the
operator's anchor from the login's own record with the asked human's
anchor, never a login name (point 4 of the console decision's addition of
2026-11-25); until that record came they are `null` (`n/a`) and mark
nothing. Read-only: no answer, no thread text. `/v1/stats?range=24h|7d|30d|90d|12m` (7 days
without it, another value a 400, #284) answers the reports, each over the
range (the activity card, `activity`: the agents' own posts by bus word in
four groups, one bar per day, week or month, none for 24 h, which says why;
the bus overhead share of all tokens per version, estimated; the asks over
the range and the range before it; the bus traffic by category, exact counts
of bus overhead and coordination; the tokens by category, an estimate marked
so, as stacked bars in `stacks`
for the fleet, per version, per role and per harness, in input-token
equivalents and raw tokens), computed with `stats.py` from lines
decoded by `stats.decode_line`, each line once by its stream sequence;
every value without lines is `null` (`n/a`).

## NATS for the console

Nothing of the console's own. The login service needs its console path
on (`CB_AUTH_CONSOLE_CLIENT_ID`, formerly `AUTH_CONSOLE_CLIENT_ID`, naming
the console's Keycloak client, `CB_AUTH_OPERATOR_GROUP`, its JetStream user's consumer rights on `STATS`;
`services/auth/README.md`). The pilot's console user in the NATS server's
configuration, its seed secret and `CB_CONSOLE_NATS_SEED_FILE` are removed:
the server refuses to start while `CB_CONSOLE_NATS_SEED_FILE` is set, and
never reads the file.

## Settings (all `CB_CONSOLE_*`, checked on start; fail closed)

| Variable | What |
|---|---|
| `CB_CONSOLE_PUBLIC_URL` | where the browser reaches the console (redirect URI); https off loopback |
| `CB_CONSOLE_ISSUER`, `CB_CONSOLE_CLIENT_ID` | Keycloak: the issuer and the console's own client |
| `CB_CONSOLE_OPERATOR_GROUP` | the realm group whose members may log in (required: without it the console refuses to start) |
| `CB_CONSOLE_ADDR` | listen, default `127.0.0.1:8747`; in a container set `0.0.0.0:8747` (a kubelet cannot probe a loopback listener) — outside one it stays loopback |
| `CB_CONSOLE_TOKEN_ALGS` | the access token's allowed algs, default `RS256` (asymmetric only, never `HS*`/`none`) |
| `CB_CONSOLE_NATS_URL` | `tls://` anywhere, `nats://` loopback only (as `natsbus`) |
| `CB_CONSOLE_NATS_CA_FILE` | the CA for `tls://` |
| `CB_CONSOLE_PRESENCE_SUBJECT`, `CB_CONSOLE_PRESENCE_STREAM` | defaults `presence`, `PRESENCE` |
| `CB_CONSOLE_STATS_SUBJECT`, `CB_CONSOLE_STATS_STREAM` | the STATS lines' prefix (the login service's `CB_AUTH_STATS_PREFIX`) and stream, defaults `stats`, `STATS` |
| `CB_CONSOLE_NATS_SEED_FILE` | **gone**: set, it refuses the start (the static account's secret goes with it) |
| `CB_CONSOLE_WEB_DIR` | the built browser app (`console/web/dist`) |
| `CB_CONSOLE_SESSION_SEC` | session lifetime, default 8 h |
| `CB_CONSOLE_LOGIN_OFF` | **tests and the mock only** (loopback): no login, the operator is a stand-in |

## The Keycloak client of the console's own (the lab's part)

- a **public** client with the authorization-code flow and PKCE enabled
  (the console is its own client, not the bus's device-flow client,
  ADR-014 point 6);
- one valid redirect URI: `<CB_CONSOLE_PUBLIC_URL>/callback`, exact;
- one valid post logout redirect URI: `<CB_CONSOLE_PUBLIC_URL>/`, exact
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

## The build version

The image build writes `build.json` (`{"version", "revision"}`) beside
`index.html`, with `console/web/scripts/build-info.mjs` (#182): the version
is the build argument `VERSION` (CI takes it from `.claude-plugin/plugin.json`),
the revision the build argument `REVISION`, the source commit. A build
without them says `unknown` in the labels and the metric and n/a in the
footer. The file is never served as a static file, not even by a path that
climbs out of `/assets/`. The server shows it as the `build` field of
`/v1/me`, as the series `choirbus_console_build_info{version,revision}` (1;
`unknown` where a value is missing; no other label) and the image carries it
as the OCI labels `org.opencontainers.image.version` and `.revision`. A run
from source has no `build.json`: both values are null.

Cache headers: files under `/assets/` (Vite's hashed names) are
`public, max-age=31536000, immutable`; `index.html`, the fallback to it for
any other path and `favicon.svg` are `no-cache` (revalidated on each load).

## Deployment notes

- **One replica.** The sessions live in memory (RISK-12: a restart logs
  everyone out, which is the state the pilot wants); a second replica
  would fight over login states and streams, and each login's NATS
  connection lives in the replica that made it.
- The Keycloak client needs **PKCE (S256)** and, in the **access token**:
  an **audience mapper** for the client id (`aud`, `azp`) and a **group
  mapper** onto `groups` (full path on, so the claim carries
  `/operators`; `CB_CONSOLE_OPERATOR_GROUP` names what the check matches
  exactly). **`preferred_username` is required** — it is the operator's
  name on every view — and `name` (the display name) is welcome.
- A `tls://` NATS URL needs the lab server's `handshake_first: true`
  (the console connects TLS first, as every host does).
- **`/healthz` is the readiness probe**; **`/livez` the liveness probe**.
  `/healthz` answers 200 when the settings held at the start and the NATS
  server answers on `CB_CONSOLE_NATS_URL` (a verified TLS handshake for
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
