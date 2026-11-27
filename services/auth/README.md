# The login service

The service that will replace the registrar's identity work: agents log in with
Keycloak's device flow, and this service issues them short-lived NATS
credentials (ADR-014, `docs/arc42/09-architecture-decisions.md`).

It reads its configuration from the environment, answers a health probe and,
with `CB_AUTH_NATS_URL` set, answers the NATS server's auth callout: it checks
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

**Names (0.103.0).** Every name carries the `CB_` prefix
([docs/env-names.md](../../docs/env-names.md)). The old names without it
(`AUTH_…`, `AUTHD_…`, and `AUTH_MATTERMOST_URL`, `AUTH_MATTERMOST_TEAM`,
`AUTH_MATTERMOST_TOKEN_FILE` for the three `CB_AUTH_MM_…` names) are
deprecated: until the removal release (0.105.0) the service
reads an old name when its new name is unset and logs one warning per process.
Both names set to different values refuse the start and name both; equal values
pass, with a warning that the old name can go. Empty counts as set; a value
that is exactly an unexpanded `${NAME}` placeholder refuses the start, naming
the variable (no manifest fills it in here). From the
removal release on, an old name set without its new name refuses the start.

| Variable | Default | Meaning |
| --- | --- | --- |
| `CB_AUTH_LISTEN_ADDR` | `:8080` | `host:port` the server listens on; the port must be 1–65535 |
| `CB_AUTH_LOG_LEVEL` | `info` | `debug`, `info`, `warn` or `error` |
| `CB_AUTH_SHUTDOWN_TIMEOUT` | `10s` | how long a stop may take before it is cut off |

With `CB_AUTH_NATS_URL` set the service answers the auth callout, and these
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
plain. `CB_AUTH_NATS_URL` and `CB_AUTH_JS_URL` are `tls://` throughout or not at
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
| `CB_AUTH_NATS_URL` | — | the NATS server, `tls://` (plain `nats://` only on a loopback host; websockets are refused, see above); unset: no callout, health only |
| `CB_AUTH_NATS_SEED_FILE` | — | file with the nkey seed of the service's own NATS user, one of the server's `auth_users` |
| `CB_AUTH_ISSUER_SEED_FILE` | — | file with the account key seed that signs the responses and user JWTs; its public key is the server's `auth_callout.issuer` |
| `CB_AUTH_XKEY_SEED_FILE` | — | file with the curve key seed that decrypts requests and encrypts answers; its public key is the server's `auth_callout.xkey`. Plain requests are refused |
| `CB_AUTH_PLAIN_REQUESTS` | unset | `allow` instead of an xkey: the server sends requests unencrypted. An explicit opt-out, for a test server only |
| `CB_AUTH_NATS_CA_FILE`, `CB_AUTH_NATS_CERT_FILE`, `CB_AUTH_NATS_KEY_FILE` | unset | the CA for the server's TLS certificate (unset: the system's), and a client certificate with its key if the server asks for one |
| `CB_AUTH_ACCOUNT` | — | the tenant's account the agents land in |
| `CB_AUTH_LOBBY` | unset | the lobby's channel name |
| `CB_AUTH_SUBJECT_PREFIX`, `CB_AUTH_PRESENCE_PREFIX`, `CB_AUTH_STATS_PREFIX`, `CB_AUTH_NAME_PREFIX` | `bus`, `presence`, `stats`, `agent-` | subject prefix of channels and direct messages, of presence and crown claims, of the agents' `STATS` lines (stream `STATS`, RT-6.43; all three must differ, and none may be `deliver` or `_INBOX`), the start of agent names |
| `CB_AUTH_JS_SEED_FILE`, `CB_AUTH_JS_URL` | —, `CB_AUTH_NATS_URL` | file with the user nkey seed of the service's own user in the tenant account, the only one with JetStream API rights, and its server; required with `CB_AUTH_NATS_URL`: the service makes the streams and every agent's consumers (RT-6.35) and does not start without them. That user needs only (`TestTheDocumentedMinimalRightsSuffice`): publish `$JS.API.STREAM.CREATE.<S>`, `$JS.API.STREAM.INFO.<S>`, `$JS.API.CONSUMER.*.<S>` and `$JS.API.CONSUMER.*.<S>.>` for `<S>` = `BUS`, `PRESENCE`; `$JS.API.STREAM.CREATE.STATS` and `$JS.API.STREAM.INFO.STATS` for the `STATS` lines (RT-6.43), and `$JS.API.CONSUMER.*.STATS` and `$JS.API.CONSUMER.*.STATS.>` for the operators' consumers on them, and `<presence>.person.*.v1` for each console login's own record (RT-6.43); for the channel directory (RT-6.39), which also holds the trust index an operator's consumers are filtered by (RT-6.43), `$JS.API.INFO`, `$JS.API.STREAM.CREATE.KV_BUS_DIRECTORY`, `$JS.API.STREAM.INFO.KV_BUS_DIRECTORY`, `$JS.API.DIRECT.GET.KV_BUS_DIRECTORY.>`, `$KV.BUS_DIRECTORY.>`, and for its key listings `$JS.API.CONSUMER.CREATE.KV_BUS_DIRECTORY.>` and `$JS.API.CONSUMER.DELETE.KV_BUS_DIRECTORY.*` (with another `CB_AUTH_DIRECTORY_BUCKET`, its name in place of `BUS_DIRECTORY`); for the attestations (RT-6.41) `$JS.API.STREAM.MSG.GET.PRESENCE` and `<presence>.attest.*.v1`; for the account records of people (RT-6.42) `<presence>.account.mattermost.*.v1`; for plan B's switches per channel (RT-6.39 step 5) `<presence>.planb.*.v1`, without which every piece stays off on the hosts (the login is not refused); for the STATS history (RT-6.45) `$JS.API.STREAM.UPDATE.STATS` (the in-place update of an existing stream's limits) and `<stats>.import.*` (the import's lines on `<stats>.import.<bot>`), granted before an upgrade starts the service, or the start stops with an error that names the update right; subscribe `_INBOX.>` |
| `CB_MM_STATS_CHANNEL` (also `MM_STATS_CHANNEL`) | `agents-stats` | the stats channel the STATS import reads (`authd dir stats-import`, RT-6.45), the sessions' own setting read the same way: `off`, `none`, `0`, `false` or empty turn the import off |
| `CB_AUTH_REQUEST_TIMEOUT` | `1.5s` | bound of one request (token check, chat lookups, answer), at most 10 s; keep it below the server's callout timeout |
| `CB_AUTH_BRAKE_FILE` | unset | the emergency brake (below) |
| `CB_AUTH_KEYCLOAK_ISSUER`, `CB_AUTH_KEYCLOAK_JWKS_URL`, `CB_AUTH_KEYCLOAK_CLIENT_ID` | — | the realm and the bus's client |
| `CB_AUTH_KEYCLOAK_AUDIENCE`, `CB_AUTH_APPROVER_GROUP`, `CB_AUTH_APPROVER_CLAIM`, `CB_AUTH_TOKEN_LEEWAY` | client id, unset, `groups`, `30s` | see the token check |
| `CB_AUTH_CONSOLE_CLIENT_ID`, `CB_AUTH_OPERATOR_GROUP`, `CB_AUTH_PERSON_PREFIX`, `CB_AUTH_PERSON_MAX_LOGINS` | unset, —, `person-`, `8` | the operator console's path (RT-6.43): the console's own Keycloak client (unset: no person's credential is issued; it must differ from the bus's client and audience), the group a token must list at every issue (required with the client, a full path such as `/ops`: the group mapper's full path on), and the start of a person's name (letters, digits, `-` and `_`, ending in `-`; it and `CB_AUTH_NAME_PREFIX` may not start each other), and how many logins one person holds consumers for at once (1 to 64) |
| `CB_AUTH_NAME_BINDING` | `open` | `open` or `openid-only` (see the name binding) |
| `CB_AUTH_LDAP_ID_CLAIM` | `ldap_id` | the token claim with the LDAP entry's id (a protocol mapper of the user attribute `LDAP_ID`): a Mattermost account that logs in over LDAP with that `auth_data` is proven to be the identity's (see the token check). `LDAP_ID` must be written by the LDAP federation alone: unmanaged attributes off or admin-only, no self-registration, no local accounts that could carry it, else a user could bind another person's Mattermost account to their own anchor |
| `CB_AUTH_MM_URL`, `CB_AUTH_MM_TEAM` | — | the chat (https; http only on a loopback host) and the team of the bus's channels |
| `CB_AUTH_DIRECTORY_BUCKET` | `BUS_DIRECTORY` | the channel directory's key-value bucket in the tenant account (ADR-016, RT-6.39): letters, digits, `-` and `_`. Until the one-time import from Mattermost has written `meta.imported`, membership still comes from Mattermost and every identity's anchor is its name of today, so agents keep their names |
| `CB_AUTH_ADMIN_LISTEN_ADDR` | — | where the directory's admin API listens (RT-6.40), `host:port`; empty: no admin API. Requires `CB_AUTH_DIRECTORY_ADMIN_GROUP`; without a certificate a loopback IP literal only (`127.0.0.1`, `::1`; `localhost` is not taken) |
| `CB_AUTH_ADMIN_TLS_CERT_FILE`, `CB_AUTH_ADMIN_TLS_KEY_FILE` | — | the admin API's certificate and key, together |
| `CB_AUTH_DIRECTORY_ADMIN_GROUP` | — | the Keycloak group (as the group mapper writes it, with or without the leading `/`) whose members administer the directory; nothing in the directory makes anyone an admin |
| `CB_AUTH_DIRECTORY_IMPORTED` | `false` | set `true` once the one-time import has run: from then on a missing bucket is never made anew and a bucket without the import's mark refuses every login, instead of falling back to Mattermost (RT-6.39) |
| `CB_AUTH_MM_TOKEN_FILE` | — | file with a Mattermost access token that may see how accounts log in (a system admin's); without that right every login is refused. The right is asked again every minute and after every 401 or 403 |
An invalid value stops the program at start with a message naming the variable.
A service that starts with half its configuration is worse than one that refuses.
A taken port is a start failure too, but exit code 1 rather than the
configuration's 2: the configuration was fine.

## Keys: making the static NATS keys

The server's static users and the account key are made once, by hand, before
the first start. Everything else — a person's and an agent's nkey — is made by
the login service at runtime; nobody handles those.

**Tool.** `nsc` from the `nats-io/nats-tools` Homebrew tap
(`brew install nats-io/nats-tools/nsc`), or `nk` from `go install
github.com/nats-io/nkeys/nk@latest`.

**Which keys are static:**

- the login service's four: its callout user (`CB_AUTH_NATS_SEED_FILE`), the
  account key that signs the user JWTs and the answers (`CB_AUTH_ISSUER_SEED_FILE`),
  the curve key of the handshake (`CB_AUTH_XKEY_SEED_FILE`) and its JetStream
  user (`CB_AUTH_JS_SEED_FILE`);
- the central bridge's user (`CB_BRIDGE_NATS_SEED_FILE`, `<BRIDGE_USER_PUBLIC_NKEY>`
  in the example server configuration) and the console's `console-metrics` user
  (`CB_CONSOLE_METRICS_NKEY_SEED_FILE`, `<CONSOLE_METRICS_PUBLIC_NKEY>`);
- an operator's user, if the lab gives NATS itself one for administration — it
  is the lab's, not this repository's.

**The rule: the seed goes straight into the secret store and is never printed.**
`nsc generate nkey` prints the seed first and the public key second; write only
the first line to the file and copy only the second into the server
configuration and the other users' records. A seed that reached a terminal, a
chat line, a log or a commit is burned: generate a new one. The service reads
the seed file at start, never a variable that holds it.

```bash
# macOS: any directory only you can read, e.g. ~/.config/choirbus/secrets.
SEED_DIR=/run/secrets
umask 077    # the files are 0600 from the moment they exist

nsc generate nkey --user    | awk -v f="$SEED_DIR/callout-user.seed" 'NR==1{print > f} NR==2'
nsc generate nkey --account | awk -v f="$SEED_DIR/issuer.seed"       'NR==1{print > f} NR==2'
nsc generate nkey --curve   | awk -v f="$SEED_DIR/xkey.seed"         'NR==1{print > f} NR==2'
```

With `nk` instead, the seed is all it prints, and `-pubout` derives the public
key from the file without printing the seed:

```bash
umask 077
nk -gen user > "$SEED_DIR/callout-user.seed" && nk -inkey "$SEED_DIR/callout-user.seed" -pubout
```

Into a secret store, without the seed in argv (where `ps` would show it), and
the plain file gone afterwards:

```bash
vault kv put secret/bus/callout-user seed=@"$SEED_DIR/callout-user.seed"
vault kv put secret/bus/issuer       seed=@"$SEED_DIR/issuer.seed"
vault kv put secret/bus/xkey         seed=@"$SEED_DIR/xkey.seed"
rm "$SEED_DIR"/{callout-user,issuer,xkey}.seed
```

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
  `auth_data` must be the token's `sub`; when it logs in over LDAP (`ldap`)
  and the token carries the LDAP id claim (`CB_AUTH_LDAP_ID_CLAIM`, default
  `ldap_id`, a single string), its `auth_data` must be that id. Either match
  proves the binding (`Identity.ChatProven`, RT-6.42 step 3): only then is
  the account's record written, with its proof and the binding the service
  keeps in its directory and checks against Mattermost every hour. A name
  alone (a password account, LDAP without the claim) lets the login in and
  proves nothing. The lookup needs a token that
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

Exactly one instance runs: the account record's lock per `sub` and the consumers' lock per agent live in the process, and the callout's queue group (`authd`) would let a second instance start silently and share the logins; a second instance would need a compare-and-set spanning instances (on the guard's key-value revision or JetStream's expected last sequence), which is not built.

The NATS server sends every client that is not one of its `auth_users` to
`$SYS.REQ.USER.AUTH`; the service answers with a user JWT or a refusal.

**The service's own NATS user** (`CB_AUTH_NATS_SEED_FILE`, in the callout's
account `AUTH`) has the callout's rights and nothing more
([nats-server.example.conf](nats-server.example.conf), pinned by
`example_conf_test.go`): subscribe `$SYS.REQ.USER.AUTH` (queue group `authd`),
publish nothing (`deny: ">"`) but one answer to each request it got, on the
reply subject the server set (`allow_responses: true`; the server's own inbox,
`$SYS._INBOX.<server>.<random>`). A NATS user without a `publish` or
`subscribe` rule may do that on any subject of its account, so every user of
the example has both, and the test fails for one that lacks either.

**What the host sends.** `auth_token` in its CONNECT is JSON:
`{"token": "<Keycloak access token>", "home": "<channel>", "guests": ["<channel>"], "lobby": true}`.
It connects with its own user nkey (nkey authentication: it signs the
server's nonce). It needs no inbox: agents have no `_INBOX` right.

**What the service checks,** in this order, within `CB_AUTH_REQUEST_TIMEOUT` and
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
  owner and host nkey; and, when the hello carries a `session` part, over owner,
  key and that part). The host never chooses it; it stays the same for every
  reconnect of one host process, and finding another owner's agent's name
  would take 2^128 tries.
  **The hello's `session`** (optional, #163): 26 lower-case base32 characters
  (`^[a-z2-7]{26}$`), the session's own part. With one shared host key
  (`CB_LOGIN_NKEY=host`) the host sends it so two sessions of one owner are two
  names; without it the name is the owner and the key alone. A value that does
  not fit refuses the login; it is self-stated and proves nothing.
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
  owner named `crown` or `member` still gives a three-token presence); for
  the operator console (RT-6.43) its one ask record, only in its home
  (`presence.ask.<home>.<agent>`: one subject per agent, never one per ask,
  so it cannot fill the presence stream), and its `STATS` line
  (`stats.<agent>`).
  Subscribe only to its own deliver subjects (`deliver.<agent>.>`), where the
  push consumers the login service makes deliver; the agent subscribes to each
  by its literal name. **No JetStream API, no key-value subject, no `$SYS`,
  no `_INBOX`:** NATS checks a publish's subject, never its reply subject, so
  any service an agent may call can be made to answer onto another agent's
  subject, under that agent's name, into a channel it has no right to, and
  into the stream. The streams must be created with `no_ack`, every `allow_*`
  switch off, `deny_delete`, `deny_purge` and a size limit (`max_bytes`); presence and crowns are a stream
  of their own, one message per subject, not a key-value bucket; the `STATS`
  lines too (stream `STATS` on `stats.>`: 365 days, 64 MiB, 8 KiB per message,
  at most 500 per subject, the oldest discarded; a stream that keeps more per
  subject or longer, takes other than 8 KiB per message or is not on file is
  refused at the start). The STATS history (RT-6.45): an existing `STATS`
  made with other limits gets the age, the cap per subject and a smaller
  size changed in place at the start, up or down (a larger size is kept;
  a stream with an age up to 365 days and a cap up to 500 passes the check,
  so a shorter one can be brought down), and the service warns
  `stats stream over 80 % full` once per crossing (and
  `consumers: stream limits lowered, older lines dropped` as a warning when
  an update lowers the age or the cap), checked every hour. The
  import of the stats channel publishes on `stats.import.<bot>`, three
  tokens: an agent's right is its own two-token `stats.<name>`.
  `CB_AUTH_SUBJECT_PREFIX`, `CB_AUTH_PRESENCE_PREFIX` and `CB_AUTH_STATS_PREFIX` set the three prefixes.
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

**Emergency brake.** `CB_AUTH_BRAKE_FILE` names a file with one owner per line,
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
  with a nkey of their own and gets an agent for up to 15 minutes. Before that
  model the token never left the host process (ADR-014, point 9), so stealing it meant
  breaking into that process. That changed with the one operating model
  for the host login (the owner's decision 10, #154, the NATS decision's
  amendment of 2026-11-26), built (unreleased): under corral the
  MCP server keeps the refresh token and the nkey seed in the sandbox's
  home, where an agent can read them. The refresh token is the owner's
  offline token, so with a nkey of the thief's own it yields new agents of
  that owner until it is revoked or idles out (RISK-26 and RISK-27 in
  docs/arc42 Section 11). Binding the token to the nkey (a claim the host
  adds at the device approval, or DPoP) would close it; that needs Keycloak's
  side and is not built.
- **Tenants:** one service instance issues into one account (`CB_AUTH_ACCOUNT`).
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
| the admin API's base URL (https; http only on a loopback IP) | `--api` | `CB_AUTHD_DIR_API` |
| the Keycloak issuer | `--issuer` | `CB_AUTH_KEYCLOAK_ISSUER` |
| the bus's public client | `--client-id` | `CB_AUTH_KEYCLOAK_CLIENT_ID` |
| an extra CA for the API's certificate | `--ca-file` | `CB_AUTHD_DIR_CA_FILE` |

| Command | What it does |
|---|---|
| `ls`, `show <channel>` | the channels one may see, one channel with its revision |
| `create <channel> [--owner <sub>] [--public]` | a new channel (admins) |
| `owner <channel> <sub>` or `-`, `public <channel> on` or `off` | owner and visibility (admins), over the revision just read |
| `planb <channel> <n,…>` or `-` | the plan B pieces switched on in the channel, 1 to 13, the others off, `-` all off (admins, RT-6.39 step 5), over the revision just read; `show` names them |
| `members add` or `rm <channel> <sub>…` | members (the channel's owner), over the revision just read |
| `import` | the one import from Mattermost (admins), with its report |
| `stats-import [--all]` | the stats channel's `STATS` lines into `STATS` (admins, RT-6.45), on `stats.import.<bot>`, as often as wanted: a line already there is skipped (by `sid` and `part`, or by its post's id), lines from before 0.91.0 without `sid` are published too, and only the newest 500 per bot are kept, so a second run publishes nothing; one run holds at most 100,000 lines and no more bytes than `STATS` has room for, the newest first, and says truncated (by lines or by room) when it stopped there; `STATS` acknowledges nothing, so the report says published, and how many of them a read after the run found (read once more after half a second when fewer); its report counts the posts read, the lines published and the skipped ones by reason, a line already in `STATS` from an agent apart from one already imported, and lines dropped because `STATS` had less room right before publishing (`skipped_room`); a rerun of the same service stops at posts strictly older than the newest post its last complete run went through (`known_before`, Mattermost's time), unless run with `--all`, which reads the whole channel again (for when `STATS` lost imported lines); old posts edited later are not read again. Before the first real import, check on one live session over NATS that the `sid` and `part` of its line on `STATS` (`nats stream get STATS --last-for 'stats.<name>'`) match those of its post in the stats channel |
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
