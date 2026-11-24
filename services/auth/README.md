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

The NATS server's side of it is in
[`nats-server.example.conf`](nats-server.example.conf): the callout's account
and the tenant's, the two users of this service, `auth_callout`, TLS and
JetStream, with placeholders instead of keys. A test starts a server from it.

Over `tls://` the service connects with the TLS handshake first, before the
server's INFO, as the agents' hosts do. The server therefore needs
`handshake_first: true` in its `tls` block; without it neither the service nor
a host gets in. To move a server whose older clients still connect in the
classic order, set a duration instead (`handshake_first: "300ms"`): the server
waits that long for a TLS handshake, then lets a classic client in. Set it
to `true` once every client speaks TLS first. A loopback `nats://` stays
plain. `AUTH_NATS_URL` and `AUTH_JS_URL` are `tls://` throughout or not at
all: a list that mixes `tls://` with another scheme is refused at the start,
since it would reach a `handshake_first` server in the classic order. The
hosts also reach a server over its WebSocket listener (`wss://`), the service
does not yet: before it may, the sender-authenticity suite has to run against
a websocket listener, and the TLS-first rule has no meaning inside the
WebSocket handshake (RT-6.36). Websocket URLs are refused at the start, and the
credentials the service issues allow standard connections only
(`AllowedConnectionTypes = STANDARD`, `service.go`), so a websocket listener
cannot be used end to end until both change.

| Variable | Default | Meaning |
| --- | --- | --- |
| `AUTH_NATS_URL` | — | the NATS server, `tls://` (plain `nats://` only on a loopback host; websockets are refused, see above); unset: no callout, health only |
| `AUTH_NATS_SEED_FILE` | — | file with the nkey seed of the service's own NATS user, one of the server's `auth_users` |
| `AUTH_ISSUER_SEED_FILE` | — | file with the account key seed that signs the responses and user JWTs; its public key is the server's `auth_callout.issuer` |
| `AUTH_XKEY_SEED_FILE` | — | file with the curve key seed that decrypts requests and encrypts answers; its public key is the server's `auth_callout.xkey`. Plain requests are refused |
| `AUTH_PLAIN_REQUESTS` | unset | `allow` instead of an xkey: the server sends requests unencrypted. An explicit opt-out, for a test server only |
| `AUTH_NATS_CA_FILE`, `AUTH_NATS_CERT_FILE`, `AUTH_NATS_KEY_FILE` | unset | the CA for the server's TLS certificate (unset: the system's), and a client certificate with its key if the server asks for one |
| `AUTH_ACCOUNT` | — | the tenant's account the agents land in |
| `AUTH_LOBBY` | unset | the lobby's channel name |
| `AUTH_SUBJECT_PREFIX`, `AUTH_PRESENCE_PREFIX`, `AUTH_NAME_PREFIX` | `bus`, `presence`, `agent-` | subject prefix of channels and direct messages, of presence and crown claims (must differ, and neither may be `deliver`), the start of agent names |
| `AUTH_JS_SEED_FILE`, `AUTH_JS_URL` | —, `AUTH_NATS_URL` | file with the user nkey seed of the service's own user in the tenant account, the only one with JetStream API rights, and its server; required with `AUTH_NATS_URL`: the service makes the streams and every agent's consumers (RT-6.35) and does not start without them. That user needs only (`TestTheDocumentedMinimalRightsSuffice`): publish `$JS.API.STREAM.CREATE.<S>`, `$JS.API.STREAM.INFO.<S>`, `$JS.API.CONSUMER.*.<S>` and `$JS.API.CONSUMER.*.<S>.>` for `<S>` = `BUS`, `PRESENCE`; for the channel directory (RT-6.39) `$JS.API.INFO`, `$JS.API.STREAM.CREATE.KV_BUS_DIRECTORY`, `$JS.API.STREAM.INFO.KV_BUS_DIRECTORY`, `$JS.API.DIRECT.GET.KV_BUS_DIRECTORY.>` and `$KV.BUS_DIRECTORY.>` (with another `AUTH_DIRECTORY_BUCKET`, its name in place of `BUS_DIRECTORY`); for the attestations (RT-6.41) `$JS.API.STREAM.MSG.GET.PRESENCE` and `<presence>.attest.*.v1`; subscribe `_INBOX.>` |
| `AUTH_REQUEST_TIMEOUT` | `1.5s` | bound of one request (token check, chat lookups, answer), at most 10 s; keep it below the server's callout timeout |
| `AUTH_BRAKE_FILE` | unset | the emergency brake (below) |
| `AUTH_KEYCLOAK_ISSUER`, `AUTH_KEYCLOAK_JWKS_URL`, `AUTH_KEYCLOAK_CLIENT_ID` | — | the realm and the bus's client |
| `AUTH_KEYCLOAK_AUDIENCE`, `AUTH_APPROVER_GROUP`, `AUTH_APPROVER_CLAIM`, `AUTH_TOKEN_LEEWAY` | client id, unset, `groups`, `30s` | see the token check |
| `AUTH_NAME_BINDING` | `open` | `open` or `openid-only` (see the name binding) |
| `AUTH_MATTERMOST_URL`, `AUTH_MATTERMOST_TEAM` | — | the chat (https; http only on a loopback host) and the team of the bus's channels |
| `AUTH_DIRECTORY_BUCKET` | `BUS_DIRECTORY` | the channel directory's key-value bucket in the tenant account (ADR-016, RT-6.39): letters, digits, `-` and `_`. Until the one-time import from Mattermost has written `meta.imported`, membership still comes from Mattermost and every identity's anchor is its name of today, so agents keep their names |
| `AUTH_ADMIN_LISTEN_ADDR` | — | where the directory's admin API listens (RT-6.40), `host:port`; empty: no admin API. Requires `AUTH_DIRECTORY_ADMIN_GROUP`; without a certificate a loopback IP literal only (`127.0.0.1`, `::1`; `localhost` is not taken) |
| `AUTH_ADMIN_TLS_CERT_FILE`, `AUTH_ADMIN_TLS_KEY_FILE` | — | the admin API's certificate and key, together |
| `AUTH_DIRECTORY_ADMIN_GROUP` | — | the Keycloak group (as the group mapper writes it, with or without the leading `/`) whose members administer the directory; nothing in the directory makes anyone an admin |
| `AUTH_DIRECTORY_IMPORTED` | `false` | set `true` once the one-time import has run: from then on a missing bucket is never made anew and a bucket without the import's mark refuses every login, instead of falling back to Mattermost (RT-6.39) |
| `AUTH_MATTERMOST_TOKEN_FILE` | — | file with a Mattermost access token that may see how accounts log in (a system admin's); without that right every login is refused. The right is asked again every minute and after every 401 or 403 |

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

Each merge to `main` that changes this directory publishes the image to
`ghcr.io/dembaca/ai-agent-bus-auth`, tagged with the full commit SHA (and a
moving `:main`), once the tests in `.github/workflows/auth.yml` passed. Deploy
it by the digest from that run's job summary, not by a tag; where and how is in
the [deployment view](../../docs/arc42/07-deployment-view.md).

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
server's nonce). It needs no inbox: agents have no `_INBOX` right.

**What the service checks,** in this order, within `AUTH_REQUEST_TIMEOUT` and
never past the server's request's own `exp`: the request decrypts with the
xkey, is signed by a server key that is the `server_id` it names, is meant for
`nats-authorization-request` and has not expired (otherwise it is not
answered); the host's signature over the nonce belongs to the nkey it names;
the emergency brake does not hold everyone; the token check (above),
including the name binding; the brake does not hold this owner. Then it grants
the channels: the home and up to 16 guest channels the human is a member of in
Mattermost (one user lookup per request, channel ids kept for a minute), and
the lobby if asked for by the flag or by its name. The channels in the request
are a wish: an invalid name, a 17th guest channel or one the human is no
member of is logged with its reason, and the agent gets the others. A lookup
that fails or runs out of time refuses the whole login: a partial credential
would look whole. Up to 32 requests are answered at once.

**What it issues.** A user JWT for the connection, in the tenant's account,
signed by the account key, valid 15 minutes from the check:

- **Name:** `agent-` and 26 characters of base32 (128 bits of SHA-256 over
  owner and host nkey). The host never chooses it; it stays the same for every
  reconnect of one host process, and finding another owner's agent's name
  would take 2^128 tries.
  The service keeps no state: name, owner and key follow from the request.
  A store for the name binding on first login will be the first state.
- **Tags:** `owner:<owner>`, `host-nkey:<nkey>`, and `approver` for a member
  of the approvers' group.
- **Permissions,** deny by default, the one model of ADR-014 point 16
  (`internal/callout/perms.go` names every subject in one table, to be aligned
  with the protocol description once it is pinned). Publish only with its own
  name as the last token: in its channels (`bus.ch.<channel>.<agent>`), to
  other agents' direct subjects (`bus.dm.<recipient>.<agent>`), its presence
  (`presence.<owner>.<agent>`, the owner from the token with `.` as `~`, `-` when
  it is no lower-case username: pinned like the name, RT-6.37) and its crown
  claims only in its home, the hello's `home` (`presence.crown.<home>.<agent>`:
  a claim in a guest channel is refused, RT-6.38) and its memberships
  (`presence.member.<channel>.<agent>`, apart from the crown: four tokens, so an
  owner named `crown` or `member` still gives a three-token presence).
  Subscribe only to its own deliver subjects (`deliver.<agent>.>`), where the
  push consumers the login service makes deliver; the agent subscribes to each
  by its literal name. **No JetStream API, no key-value subject, no `$SYS`,
  no `_INBOX`:** NATS checks a publish's subject, never its reply subject, so
  any service an agent may call can be made to answer onto another agent's
  subject, under that agent's name, into a channel it has no right to, and
  into the stream. The streams must be created with `no_ack`, every `allow_*`
  switch off, `deny_delete`, `deny_purge` and a size limit (`max_bytes`); presence and crowns are a stream
  of their own, one message per subject, not a key-value bucket.
  `AUTH_SUBJECT_PREFIX` and `AUTH_PRESENCE_PREFIX` set the two prefixes.
  **The proof** is `internal/callout/native_sender_test.go`: an embedded
  nats-server (the version in `go.mod`, 2.15.3) with this callout, attacked
  on every subject and header, with controls that show the forgery succeeds
  once acknowledgements, schedules or rollups are allowed. An update of the
  NATS server is allowed only after this suite is green on it.
- **Limits:** 64 subscriptions, 1 MiB per message, plain NATS connections only
  (no websocket, leafnode or MQTT).

**Why 15 minutes from the check, not up to the token's `exp`** (decided,
ADR-014 point 7): every issue checks the Keycloak token and the person again,
and nothing issued can be used without the host's nkey seed. A Keycloak
session that ends therefore takes effect within 15 minutes at the latest.

**Emergency brake.** `AUTH_BRAKE_FILE` names a file with one owner per line,
or `*` for everyone; `#` starts a comment; names compare in lower case, and a
line that is no valid username is logged and holds nobody. It is read again
when it changes, without a restart, and takes effect at the next issue, so
within 15 minutes. Replace it atomically (write a new file, rename it over the
old one). A configured brake file that cannot be read stops everyone. Ending a
connection at once needs a disconnect through the server's system account;
that is an open point, not this service's job.

**Audit.** One JSON log line per issue and per refusal (`event` `issued` or
`refused`) with the server id, the client's address, the connection's user
nkey, and once known the host nkey, owner, token `sub` and `jti`, agent name,
granted channels and the reason; one line per refused channel; one line per
request that is not answered. Never a token, a seed or a password.

**The NATS server's side.** Accounts for the callout service (with its nkey
user) and for each tenant; `authorization.auth_callout` with `issuer` (the
account key's public key), `account` (the callout's account), `auth_users`
(the service's nkey; at least one nkey user also makes the server send the
nonce a host signs) and `xkey` (so requests and answers are encrypted). The
bus's streams must be created with `no_ack`: a stream that acknowledges a post
sends its ack to the post's reply subject, which the agent chose, with the
stream's own rights (a test shows it). Only
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
- **Consumers** (built, RT-6.35): agents get no JetStream rights; the login service makes
  and removes their push consumers (at login, on leaving a channel, at sign-off
  and on the emergency brake; history from a sequence the hello names), and at
  every issue of a credential to remove those of channels no longer in it: the
  subscribe right `deliver.<agent>.>` does not depend on the channels. Crown
  claims reach an agent only for its own channels, and the crown goes to the
  earliest claim among agents with a fresh presence. `since` in the hello
  (`{"since": {"<channel>": <sequence>}}` beside `token`, `home`, `guests`,
  the last sequence the agent has seen) asks for history from the next one. The
  design is in ADR-014 point 16; it is built with the NATS adapter.
- **Message ids and a side channel:** a `Nats-Msg-Id` is checked across the
  whole stream within its 100 ms duplicate window, so an agent that guesses
  another's id suppresses that message: honest clients send none. With
  expectation headers an agent learns whether and when a channel it cannot
  read had posts, not what; only a stream per channel closes that.
- **`auth_data` for admins:** whether Mattermost returns `auth_data` to a
  system admin's token at all depends on its privacy settings; the lab checks
  it. Without it an OpenID account's `sub` cannot be compared, and the check
  refuses the login.
- **A smaller Mattermost right than a system admin's** needs a license
  with custom permission schemes. The lab checked it (2026-11-23): its Entry
  license refuses the system role "User Manager" ("Custom Permission Schemes
  not supported by current license"), and a bot without that role gets
  `auth_service` back empty. The service itself also accepts only a token
  with the role `system_admin` (`internal/mattermost/client.go`): any other
  leaves `auth_service` unknown and refuses every login. A smaller role thus
  needs a license with custom permission schemes and a code change; whether
  "User Manager" then sees `auth_service` and `auth_data` is unverified.
- **Who sent a post, as the receiver sees it:** the subject's last token
  names the sender, and only the sender can publish there: agents talk to no
  service, and the streams do not acknowledge (ADR-014 point 16). A receiver
  takes the sender from the subject alone, never from a header or the body.
  How a receiver learns the
  sender's owner (ADR-001's foreign agents) is an open design question for
  the protocol layer.
- **A restart is a new agent:** the name follows from the host's nkey, which
  the host process makes anew at each start, so a restarted session gets a
  new name and, once consumers exist, a new durable consumer. ADR-014 expects
  an agent to keep its inbox across restarts; how (a host key kept on disk,
  or a name the owner's session keeps) is a question with the owner.
- **Memberships are asked all at once,** one lookup per channel in parallel,
  for at most 17 channels (home and 16 guests), within the request's
  deadline.
- **Immediate disconnect:** see the emergency brake.

## The directory from the command line (`authd dir`)

`authd dir <command>` is the command line for the channel directory's admin
API (RT-6.40); the same binary, no service is started. It logs in with
Keycloak's device flow on every run: it prints a link and a code, the person
confirms them in the browser, and the access token lives only in that
process's memory: never on disk, never printed, never in the environment.

| Setting | Flag | Environment |
|---|---|---|
| the admin API's base URL (https; http only on a loopback IP) | `--api` | `AUTHD_DIR_API` |
| the Keycloak issuer | `--issuer` | `AUTH_KEYCLOAK_ISSUER` |
| the bus's public client | `--client-id` | `AUTH_KEYCLOAK_CLIENT_ID` |
| an extra CA for the API's certificate | `--ca-file` | `AUTHD_DIR_CA_FILE` |

| Command | What it does |
|---|---|
| `ls`, `show <channel>` | the channels one may see, one channel with its revision |
| `create <channel> [--owner <sub>] [--public]` | a new channel (admins) |
| `owner <channel> <sub>` or `-`, `public <channel> on` or `off` | owner and visibility (admins), over the revision just read |
| `members add` or `rm <channel> <sub>…` | members (the channel's owner), over the revision just read |
| `import` | the one import from Mattermost (admins), with its report |
| `account show <name>`, `account set <name> <sub> [--rev N]`, `account rm <name> --rev N` | the Mattermost account index (admins): which identity a name resolves to, with `moved_from` when it moved; set creates without `--rev` and replaces over it |
| `pending`, `confirm <name>`, `drop <name>` | the claims of password accounts (with the full channels one waits on), an admin's confirmation over the listed revision, and deleting a claim over the listed revision |

`--json` writes the answer as the
server sent it, for pipes and scripts, not for a terminal (text output is the
cleaned one); an answer that is not JSON is an error and nothing is written. A change made meanwhile by
someone else ends with "changed meanwhile; run again" (exit 1); a usage
error exits 2. Text the server sends is printed without control characters.

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
