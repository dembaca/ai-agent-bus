# The console server's image, built as the login service's (services/auth/
# Dockerfile): two stages, bases pinned by digest, and the run stage without
# a shell, as not root. The browser app is built with its API base set to "/"
# (same-origin: the login's cookie) so the served bundle talks to the server
# beside it, never to the mock. One Python — 3.13 — on both sides: the mcp
# package needs 3.12+ (#96), the run stage is distroless python3-debian13.
# The venv installs non-editable from the lockfile, without the test groups,
# so it copies and the entrypoint starts.
#
# The build context is the repository root (the server's uv project has a
# path dependency on ../..-/mcp):
#   docker build -t ai-agent-bus-console -f console/server/Dockerfile .
# The release version and source commit the console shows (#182) are the
# build arguments VERSION and REVISION; CI passes both (the version from
# .claude-plugin/plugin.json):
#   docker build --build-arg VERSION=<x.y.z> --build-arg REVISION=<commit> \
#     -t ai-agent-bus-console -f console/server/Dockerfile .
# A build without them says "unknown" in the OCI labels and the build_info
# metric, and "n/a" in the footer: one fallback, never a guess.

FROM node:24-slim@sha256:32e57b5784e8875d2cc0112dcaf13f9100f2d0d9446187808738e99aee29a5a2 AS web
WORKDIR /src
COPY console/web/package.json console/web/package-lock.json ./console/web/
RUN npm --prefix console/web ci --ignore-scripts
COPY console/fixtures ./console/fixtures
COPY console/web ./console/web
RUN VITE_CB_CONSOLE_API=/ npm --prefix console/web run build
# declared here, after npm ci and the build, so a new commit reuses their cache
ARG VERSION=unknown
ARG REVISION=unknown
RUN node console/web/scripts/build-info.mjs console/web/dist/build.json

FROM python:3.13-slim@sha256:1b5c0692836eae9b028cc352d7146872f36939aa1fb0de24d3ef6df307b956c9 AS server
WORKDIR /src
RUN pip install --no-cache-dir uv==0.13.9
COPY mcp/pyproject.toml mcp/uv.lock mcp/README.md ./mcp/
COPY mcp/src ./mcp/src
COPY console/server/pyproject.toml console/server/uv.lock  ./console/server/
COPY console/server/src ./console/server/src
# --frozen: the lockfile decides; --no-dev: the tests stay out; --no-editable:
# /src does not travel with the venv (the reviews' image findings);
# UV_PROJECT_ENVIRONMENT puts the project's venv at /venv itself.
# Python 3.13 on both sides: the mcp package needs 3.12+ (#96), and
# distroless python3-debian13 is 3.13 as the build stage is.
ENV UV_PROJECT_ENVIRONMENT=/venv
RUN uv sync --directory console/server --frozen --no-dev --no-editable

# gcr.io/distroless/python3-debian13: no shell, no package manager; its
# python3 is 3.13, as the install stage's — the mcp package needs 3.12+ (#96).
FROM gcr.io/distroless/python3-debian13:nonroot@sha256:e36c26ad81ef8c9da7605d717a22160193ab66cfbc55bd5977f429f01f6ead99
ARG VERSION=unknown
ARG REVISION=unknown
LABEL org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"
COPY --from=server /venv /venv
COPY --from=web /src/console/web/dist /app/web
ENV PYTHONPATH=/venv/lib/python3.13/site-packages \
    CB_CONSOLE_WEB_DIR=/app/web \
    PYTHONUNBUFFERED=1
USER nonroot:nonroot
EXPOSE 8747
ENTRYPOINT ["python3", "-m", "console_server"]
