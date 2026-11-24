# The console server's image, built as the login service's (services/auth/
# Dockerfile): two stages, digest-pinned bases where the release pins
# them, and the run stage without a shell, as not root. The browser app is
# built in the first stage and served by the server, so one image holds
# both (ADR-015 point 14: React with TypeScript on Vite, as site/ is).
#
# The build context is the repository root (the server's uv project has a
# path dependency on ../..-/mcp):
#   docker build -t ai-agent-bus-console -f console/server/Dockerfile .

FROM node:24-slim AS web
WORKDIR /src
COPY console/web/package.json console/web/package-lock.json ./console/web/
RUN npm --prefix console/web ci --ignore-scripts
COPY console/web ./console/web
COPY site/package.json site/package-lock.json ./site/
RUN npm --prefix site ci --ignore-scripts
RUN npm --prefix console/web run build

FROM python:3.13-slim AS server
WORKDIR /src
COPY mcp/pyproject.toml mcp/uv.lock mcp/README.md ./mcp/
COPY mcp/src ./mcp/src
COPY console/server/pyproject.toml console/server/uv.lock  ./console/server/
COPY console/server/src ./console/server/src
RUN pip install --no-cache-dir uv && \
    uv venv /venv && \
    VIRTUAL_ENV=/venv uv pip install --no-cache \
        --directory console/server --editable . \
        hatchling anyio pytest pytest-asyncio httpx cryptography nats-py==2.16.0

# gcr.io/distroless/python3-debian12: no shell, no package manager, no libc
# headers surfacing; python3 is the interpreter. The release pins this base
# by digest, as services/auth pins its.
FROM gcr.io/distroless/python3-debian12:nonroot
COPY --from=server /venv /venv
COPY --from=web /src/console/web/dist /app/web
ENV PYTHONPATH=/venv/lib/python3.11/site-packages \
    CONSOLE_WEB_DIR=/app/web
USER nonroot:nonroot
EXPOSE 8747
ENTRYPOINT ["python3", "-m", "console_server"]
