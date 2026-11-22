# The login service

The service that will replace the registrar's identity work: agents log in with
Keycloak's device flow, and this service issues them short-lived NATS
credentials (ADR-014, `docs/arc42/09-architecture-decisions.md`).

**This is the skeleton only.** It starts, reads its configuration from the
environment, and answers a health probe. The token check exists as a package
(below) but is not wired in. It does **not** issue NATS credentials or know
about agents yet; those are the next pull requests, listed in the plan of the
third phase.

Go was chosen for the service: the NATS Go client brings auth callout and user
JWTs ready-made (the owner's decision, 2026-11-22).

## Layout

```
services/auth/
  cmd/authd/          the program: configuration, start, wait for a signal, stop cleanly
  internal/config/    configuration from the environment, validated
  internal/httpserver/ the HTTP server and its health endpoint
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
| `AUTH_LISTEN_ADDR` | `:8080` | `host:port` the server listens on |
| `AUTH_LOG_LEVEL` | `info` | `debug`, `info`, `warn` or `error` |
| `AUTH_SHUTDOWN_TIMEOUT` | `10s` | how long a stop may take before it is cut off |

An invalid value stops the program at start with a message naming the variable.
A service that starts with half its configuration is worse than one that refuses.

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

The container runs as an unprivileged user (uid 10001) on a minimal image, with
no shell and no package manager in the final stage.

## Token check (`internal/token`)

`token.Verifier` checks the Keycloak access token an agent presents (ADR-014,
point 6). It is not wired into `authd` yet; the configuration from the
environment and the chat lookup arrive with that wiring.

- **Signature and lifetime** against the realm's JWKS (`github.com/golang-jwt/jwt/v5`,
  keys from `github.com/MicahParks/keyfunc/v3`). Only asymmetric methods are
  accepted (default RS256), so `alg: none` and an HS256 token keyed with the
  public key fail. `iss` must be the realm; `exp` is required; `exp`, `nbf` and
  `iat` allow a clock skew of 30 s by default, 60 s at most.
- **The bus's client:** `aud` must contain the configured audience (default:
  the client id) and `azp` must be the client; `typ` must be `Bearer`, so no ID
  or refresh token passes.
- **The owner** is `preferred_username`, bound to `sub`; `email` is never read.
  The chat must know an active human account of that name, not a bot. When that
  account logs in over OpenID Connect (Mattermost's `auth_service` `openid`),
  its `auth_data` must be the token's `sub`; otherwise the name is the binding,
  so the service works with any Mattermost.
- **Approvers:** membership in the configured group of the configured claim
  (default `groups`) sets `Identity.Approver`. It never refuses a start; the
  session's trusted list selects within it.
- **Keys:** the JWKS is fetched hourly and when a token names an unknown key,
  at most once a minute; a token with an unknown key in between is refused at
  once.
- Errors wrap one reason each (`ErrAudience`, `ErrExpired`, …) and never carry
  the token. The package does not log.

**Prerequisites in Keycloak**, on the bus's public client: an *audience mapper*
that adds the client to `aud` (Keycloak puts only `account` there by default),
and a *group mapper* that writes the user's groups into the configured claim.

## Tests

`go test ./...` covers the configuration (defaults, overrides, every rejection),
the server (health answer, method and path handling, clean shutdown) and the
token check: real JWTs signed with keys made in the test, a JWKS served by
`httptest`, and every refusal (wrong `aud` or `azp`, expired, a stranger's key,
`alg: none`, HS256, another issuer, a bot, a mismatching `sub`, a key rotation). The
tests use `httptest` and need no network, no container and no Keycloak.
