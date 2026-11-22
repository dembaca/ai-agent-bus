# The login service, built as a static binary and shipped on a minimal image.
#
# The build context is services/auth/, so the module's own go.mod is the only
# one used:
#
#   docker build -f services/auth/Dockerfile -t ai-agent-bus-auth services/auth
#
# Both stages pin their base image by digest: a tag can be moved, a digest
# cannot, so the image changes only when this file changes.

# --- build -------------------------------------------------------------------
FROM golang:1.27-bookworm@sha256:4fa7c968ba93f1635670522dbb01a2c78150443e1385746924566079f36570ea AS build

WORKDIR /src

# Copy the module first and download its dependencies before the sources, so a
# source-only change keeps the module cache layer.
COPY go.mod go.sum ./
RUN go mod download

COPY . .

# A static binary: no libc, so the runtime stage needs nothing but the file.
ARG VERSION=dev
RUN CGO_ENABLED=0 GOOS=linux go build \
        -trimpath \
        -ldflags "-s -w -X github.com/dembaca/ai-agent-bus/services/auth/internal/httpserver.Version=${VERSION}" \
        -o /out/authd ./cmd/authd

# --- run ---------------------------------------------------------------------
FROM alpine:3.22@sha256:49340b346798875b5592e5d420241f2f9cfba5f47e62ebb1446a1b34734e898f AS run

# CA certificates for the Keycloak calls that arrive with the next pull
# request, and nothing else: no shell tooling, no package manager at runtime.
RUN apk add --no-cache ca-certificates \
    && addgroup -g 10001 -S authd \
    && adduser -u 10001 -S -G authd -H -s /sbin/nologin authd

COPY --from=build /out/authd /usr/local/bin/authd

USER 10001:10001
EXPOSE 8080

# No HEALTHCHECK: probes belong to the platform that runs the container (the
# lab's Kubernetes reads /healthz as its liveness probe). A Docker HEALTHCHECK
# would need a shell or a second mode in the binary, and the image has neither.
ENTRYPOINT ["/usr/local/bin/authd"]
