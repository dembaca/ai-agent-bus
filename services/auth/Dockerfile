# The login service, built as a static binary and shipped on a distroless image.
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
# source-only change keeps the module cache layer. The module has no
# dependencies yet; `go mod download` is a no-op until it does.
COPY go.mod ./
RUN go mod download

COPY . .

# A static binary: no libc, so the runtime stage needs nothing but the file.
ARG VERSION=dev
RUN CGO_ENABLED=0 GOOS=linux go build \
        -trimpath \
        -ldflags "-s -w -X github.com/dembaca/ai-agent-bus/services/auth/internal/httpserver.Version=${VERSION}" \
        -o /out/authd ./cmd/authd

# --- run ---------------------------------------------------------------------
# distroless/static-debian12 is a Debian root filesystem with the CA
# certificates and /etc/passwd and nothing else: no shell, no package manager,
# no libc. :nonroot runs as uid 65532.
FROM gcr.io/distroless/static-debian12:nonroot@sha256:afa5c872c891853ca7fcf1f12c3edb23f7eeef36189728842dd51042ff57f7ab

COPY --from=build /out/authd /usr/local/bin/authd

USER nonroot:nonroot
EXPOSE 8080

# No HEALTHCHECK: probes belong to the platform that runs the container (the
# lab's Kubernetes reads /healthz as its liveness probe). A Docker HEALTHCHECK
# would need a shell or a second mode in the binary, and the image has neither.
ENTRYPOINT ["/usr/local/bin/authd"]
