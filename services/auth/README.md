# The login service

The service that will replace the registrar's identity work: agents log in with
Keycloak's device flow, and this service issues them short-lived NATS
credentials (ADR-014, `docs/arc42/09-architecture-decisions.md`).

**This is the skeleton only.** It starts, reads its configuration from the
environment, and answers a health probe. It does **not** check tokens, issue
NATS credentials, talk to Keycloak or know about agents yet; those are the next
pull requests, listed in the plan of the third phase.

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
| `AUTH_LISTEN_ADDR` | `:8080` | `host:port` the server listens on; the port must be 1–65535 |
| `AUTH_LOG_LEVEL` | `info` | `debug`, `info`, `warn` or `error` |
| `AUTH_SHUTDOWN_TIMEOUT` | `10s` | how long a stop may take before it is cut off |

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

## Tests

`go test ./...` covers the configuration (defaults, overrides, every rejection)
and the server (health answer, method and path handling, clean shutdown). The
tests use `httptest` and need no network, no container and no Keycloak.
