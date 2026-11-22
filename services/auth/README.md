# The login service

The service that will replace the registrar's identity work: agents log in with
Keycloak's device flow, and this service issues them short-lived NATS
credentials (ADR-014, `docs/arc42/09-architecture-decisions.md`).

It reads its configuration from the environment, answers a health probe and,
with `AUTH_NATS_URL` set, answers the NATS server's auth callout: it checks
the Keycloak token a host presents, issues the agent a user JWT for 15 minutes
and names it. Not in it yet: the path over a pod's service-account token
(TokenReview), and the durable consumers the agents read from.

Go was chosen for the service: the NATS Go client brings auth callout and user
JWTs ready-made (the owner's decision, 2026-11-22).

## Layout

```
services/auth/
  cmd/authd/          the program: configuration, start, wait for a signal, stop cleanly
  internal/config/    configuration from the environment, validated
  internal/httpserver/ the HTTP server and its health endpoint
  internal/token/     the check of the Keycloak access token
  internal/callout/   the auth callout: request, nkey proof, channels, permissions, brake
  internal/mattermost/ the chat lookups: the account behind a name, channel membership
  internal/app/       the callout's settings and wiring
  internal/testrealm/ a Keycloak stand-in for tests (JWKS over httptest, real tokens)
  Dockerfile          static binary on a minimal image, base image pinned by digest
```

The module is separate from the rest of the repository: `go build ./...` here
touches nothing outside `services/auth/`, and the plugin, hooks and MCP server
are Python, TypeScript and shell.

## Configuration

Read from the environment only. Nothing secret belongs in the repository, and
no default carries a credential. Unknown variables are ignored, so one
environment can serve several services.

| Variable | Default | Meaning |
| --- | --- | --- |
| `AUTH_LISTEN_ADDR` | `:8080` | `host:port` the server listens on; the port must be 1–65535 |
| `AUTH_LOG_LEVEL` | `info` | `debug`, `info`, `warn` or `error` |
| `AUTH_SHUTDOWN_TIMEOUT` | `10s` | how long a stop may take before it is cut off |

With `AUTH_NATS_URL` set the service answers the auth callout, and these
apply. Secrets are always files, never values in the environment; example
values are placeholders.

| Variable | Default | Meaning |
| --- | --- | --- |
| `AUTH_NATS_URL` | — | the NATS server; unset: no callout, health only |
| `AUTH_NATS_SEED_FILE` | — | file with the nkey seed of the service's own NATS user, one of the server's `auth_users` |
| `AUTH_ISSUER_SEED_FILE` | — | file with the account key seed that signs the responses and user JWTs; its public key is the server's `auth_callout.issuer` |
| `AUTH_XKEY_SEED_FILE` | unset | file with the curve key seed that decrypts requests; its public key is the server's `auth_callout.xkey`. Unset: plain requests; set: plain ones are refused |
| `AUTH_ACCOUNT` | — | the tenant's account the agents land in |
| `AUTH_LOBBY` | unset | the lobby's channel name |
| `AUTH_SUBJECT_PREFIX`, `AUTH_STREAM`, `AUTH_PRESENCE_BUCKET`, `AUTH_NAME_PREFIX` | `bus`, `BUS`, `presence`, `agent-` | subject prefix, the tenant's stream, the presence bucket, the start of agent names |
| `AUTH_REQUEST_TIMEOUT` | `1.5s` | bound of one request (token check, chat lookups, answer), at most 10 s; keep it below the server's callout timeout |
| `AUTH_BRAKE_FILE` | unset | the emergency brake (below) |
| `AUTH_KEYCLOAK_ISSUER`, `AUTH_KEYCLOAK_JWKS_URL`, `AUTH_KEYCLOAK_CLIENT_ID` | — | the realm and the bus's client |
| `AUTH_KEYCLOAK_AUDIENCE`, `AUTH_APPROVER_GROUP`, `AUTH_APPROVER_CLAIM`, `AUTH_TOKEN_LEEWAY` | client id, unset, `groups`, `30s` | see the token check |
| `AUTH_NAME_BINDING` | `open` | `open` or `openid-only` (see the name binding) |
| `AUTH_MATTERMOST_URL`, `AUTH_MATTERMOST_TEAM` | — | the chat and the team of the bus's channels |
| `AUTH_MATTERMOST_TOKEN_FILE` | — | file with a Mattermost access token that may see how accounts log in (a system admin's); without that right every login is refused |

An invalid value stops the program at start with a message naming the variable.
A service that starts with half its configuration is worse than one that refuses.
A taken port is a start failure too, but exit code 1 rather than the
configuration's 2: the configuration was fine.

## Endpoints

| Path | Answer |
| --- | --- |
| `GET /healthz` | `200` with `{"status":"ok","service":"authd","version":"…"}`, always while the process runs |

`/healthz` is a liveness probe: it says the process is up and serving, not that
a dependency is reachable. When the service learns to reach Keycloak, a separate
readiness probe will say that; mixing the two makes a dependency outage look
like a dead process.

Anything else, and any method other than `GET` and `HEAD`, gets `404` or `405`.

## Running it

```bash
cd services/auth
go vet ./... && go test ./...
go run ./cmd/authd
curl -s localhost:8080/healthz
```

As a container, from the repository root:

```bash
docker build -f services/auth/Dockerfile -t ai-agent-bus-auth services/auth
docker run --rm -p 8080:8080 ai-agent-bus-auth
```

Both base images are pinned by digest. A tag can be moved; a digest cannot, and
the running service should change only when this repository changes. When a
base image needs updating, the digest changes in the same pull request that
says why.

The final stage is `gcr.io/distroless/static-debian12:nonroot`: a Debian root
filesystem with the CA certificates and `/etc/passwd` and nothing else — no
shell, no package manager, no libc. The binary is static, so nothing else is
needed. It runs as `nonroot` (uid 65532).

## Token check (`internal/token`)

`token.Verifier` checks the Keycloak access token an agent presents (ADR-014,
point 6). The auth callout calls it with the request's deadline.

- **Signature and lifetime** against the realm's JWKS (`github.com/golang-jwt/jwt/v5`,
  keys from `github.com/MicahParks/keyfunc/v3`). Only the configured asymmetric
  methods pass (default RS256), also when the JWKS names no `alg`, so
  `alg: none` and an HS256 token keyed with the public key fail. The token must
  name a key id (`kid`) of a signing key: a key with `use: enc` or without
  `use` is never taken. `iss` must be the realm
  exactly; `exp` is required; `exp`, `nbf` and `iat` allow a clock skew of 30 s
  by default, 60 s at most. A token over 16 KiB is refused unparsed.
- **The bus's client:** `aud` (a string or a list) must contain the configured
  audience (default: the client id) and `azp` must be the client; `typ` must be
  `Bearer`, so no ID or refresh token passes.
- **The owner** is `preferred_username`, bound to `sub`; `email` is never read.
  The chat must know an active human account of exactly that name: not a bot,
  not deactivated, not a guest (guests own no agents). When that account logs
  in over OpenID Connect (Mattermost's `auth_service` `openid`), its
  `auth_data` must be the token's `sub`. The lookup needs a token that
  Mattermost shows `auth_service` and `auth_data` to; an answer without them
  is refused, never taken for a password account.
- **Approvers:** membership in the configured group of the configured claim
  (default `groups`) sets `Identity.Approver`. It never refuses a start; the
  session's trusted list selects within it.
- **Keys:** the JWKS is fetched hourly, and when a token names an unknown key,
  at most once a minute. A token with an unknown key in between is refused at
  once when the next fetch is more than 10 s away, and otherwise waits for it;
  the caller bounds `Verify` with a deadline of its own. Each fetch has 10 s, follows no redirect and reads at most 1 MiB. A
  realm that is down at the start is fetched again on the first token. Failed
  fetches go to `Config.KeyFetchError` for the caller to log.
- Errors are one reason each (`ErrAudience`, `ErrExpired`, `ErrGuest`, …),
  without the token, a key id from it or the chat's error text. The package
  does not log.

**Prerequisites in Keycloak**, on the bus's public client: an *audience mapper*
that adds the client to `aud` (Keycloak puts only `account` there by default),
and a *group mapper* that writes the user's groups into the configured claim.

**Open points and known risks**

- **Revocation:** the check holds up to the token's `exp` and learns of no
  revocation before it. The user JWT issued from it lives 15 minutes (below).
- **Name binding for accounts without OpenID:** for every chat account whose
  `auth_service` is not `openid`, the name alone binds the token to it: a
  password, LDAP or SAML, and also Keycloak through the GitLab route that
  Mattermost Team Edition uses. That holds only as far as the realm's names
  can be trusted:
  - no self-registration and no change of usernames in the realm;
  - no identity brokering or federation that takes names from outside, and
    realm admins trusted as far as the chat's owners;
  - the other way round: whoever renames a chat account to the name of a realm
    user without a chat account becomes the owner account for that user's
    agents. Chat usernames should only be changeable by admins.

## Auth callout (`internal/callout`)

The NATS server sends every client that is not one of its `auth_users` to
`$SYS.REQ.USER.AUTH`; the service answers with a user JWT or a refusal.

**What the host sends.** `auth_token` in its CONNECT is JSON:
`{"token": "<Keycloak access token>", "home": "<channel>", "guests": ["<channel>"], "lobby": true}`.
It connects with its own user nkey (nkey authentication: it signs the
server's nonce) and with the inbox prefix `_INBOX.<its public nkey>`.

**What the service checks,** in this order, within `AUTH_REQUEST_TIMEOUT`:
the request is signed by the server (and decrypted with the xkey); the host's
signature over the nonce belongs to the nkey it names; the emergency brake
does not hold everyone; the token check (above), including the name binding;
the brake does not hold this owner. Then it grants the channels: the home and
guest channels the human is a member of in Mattermost, and the lobby if asked.
The channels in the request are a wish; each refused one is logged with its
reason, and the agent gets the others.

**What it issues.** A user JWT for the connection, in the tenant's account,
signed by the account key, valid 15 minutes from the check:

- **Name:** `agent-` and the first 12 hex digits of SHA-256 over owner and host
  nkey. The host never chooses it; it stays the same for every reconnect of
  one host process and is practically never another owner's agent's name.
  The service keeps no state: name, owner and key follow from the request.
  A store for the name binding on first login will be the first state.
- **Tags:** `owner:<owner>`, `host-nkey:<nkey>`, and `approver` for a member
  of the approvers' group.
- **Permissions,** deny by default (`internal/callout/perms.go` names every
  subject in one table, to be aligned with the protocol description once it is
  pinned): publish only under its own name as the last token in its channels
  (`bus.ch.<channel>.<agent>`) and to other agents' direct subjects
  (`bus.dm.<recipient>.<agent>`); subscribe only to its channels and its own
  direct subject; read and acknowledge its own durable consumer, never create
  or change one; write its own presence entry, read everyone's; its own
  inbox.

**Why 15 minutes from the check, not up to the token's `exp`** (decided,
ADR-014 point 7): every issue checks the Keycloak token and the person again,
and nothing issued can be used without the host's nkey seed. A Keycloak
session that ends therefore takes effect within 15 minutes at the latest.

**Emergency brake.** `AUTH_BRAKE_FILE` names a file with one owner per line,
or `*` for everyone; `#` starts a comment. It is read again when it changes,
without a restart, and takes effect at the next issue, so within 15 minutes.
A configured brake file that cannot be read stops everyone. Ending a
connection at once needs a disconnect through the server's system account;
that is an open point, not this service's job.

**Audit.** One JSON log line per issue and per refusal (`event` `issued` or
`refused`) with the host nkey, owner, agent name, granted channels and the
reason; one line per refused channel. Never a token, a seed or a password.

**The NATS server's side.** Accounts for the callout service (with its nkey
user) and for each tenant; `authorization.auth_callout` with `issuer` (the
account key's public key), `account` (the callout's account), `auth_users`
(the service's nkey; at least one nkey user also makes the server send the
nonce a host signs) and `xkey` (so requests and answers are encrypted). Only
the users in `auth_users` bypass the callout. Rotating the issuer key means
changing the server's `issuer` and the service's seed file together; the
server knows one issuer at a time.

**Open points and known risks of the callout**

- **A stolen access token:** the host's nkey proves only that the connecting
  process holds some key, not that it is the host that made the device
  approval. Whoever steals an access token (300 s in the lab) can connect
  with a nkey of their own and gets an agent for up to 15 minutes. The token
  never leaves the host process (ADR-014, point 9), so stealing it means
  breaking into that process. Binding the token to the nkey (a claim the host
  adds at the device approval, or DPoP) would close it; that needs Keycloak's
  side and is not built.
- **Tenants:** one service instance issues into one account (`AUTH_ACCOUNT`).
  Accounts share no subjects; the test checks that another account's post does
  not reach an agent. Rights inside a JetStream stream shared by tenants are
  not built: each tenant has its own account and stream.
- **Durable consumers:** an agent may read only its own consumer; who creates
  it, with a filter that matches the granted channels, is still to be built.
- **Immediate disconnect:** see the emergency brake.

## Tests

`go test ./...` covers the configuration (defaults, overrides, every rejection),
the server (health answer, method and path handling, clean shutdown) and the
token check: real JWTs signed with keys made in the test, a JWKS served by
`httptest`, and every refusal (wrong `aud` or `azp`, expired, a stranger's key,
`alg: none`, HS256, RS384 and PS256 when not configured, a key not for
signing, another issuer, a bot, a guest, a mismatching `sub`, a lookup without
auth data), a key rotation and a realm down at the start, both with a JWKS that
answers in 50 ms, and a fetch that is redirected or too large. The callout's
tests run a real `nats-server` embedded with `auth_callout` and an xkey, and
connect real clients: a granted and a refused channel, publishing under
another name, an expired token, a nonce signed by another nkey, a guest, a
post that reaches another agent, another account that shares nothing, and the
emergency brake. The Mattermost client is tested against its REST routes over
`httptest`. The tests need no network, no container, no Keycloak and no
Mattermost.
