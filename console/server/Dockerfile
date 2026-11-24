# The console server's image, built as the login service's (services/auth/
# Dockerfile): two stages, bases pinned by digest, and the run stage without
# a shell, as not root. The browser app is built with its API base set to "/"
# (same-origin: the login's cookie) so the served bundle talks to the server
# beside it, never to the mock; the server's packages are installed on the
# run stage's Python (3.11) — non-editable, from the lockfile, without the
# test groups — so the venv copies and the entrypoint starts.
#
# The build context is the repository root (the server's uv project has a
# path dependency on ../..-/mcp):
#   docker build -t ai-agent-bus-console -f console/server/Dockerfile .

FROM node:24-slim@sha256:32e57b5784e8875d2cc0112dcaf13f9100f2d0d9446187808738e99aee29a5a2 AS web
WORKDIR /src
COPY console/web/package.json console/web/package-lock.json ./console/web/
RUN npm --prefix console/web ci --ignore-scripts
COPY console/web ./console/web
RUN VITE_CONSOLE_API=/ npm --prefix console/web run build

FROM python:3.11-slim@sha256:788c5fb8a33aa5416623c540a8b4fa5c6f1b5cd805726d47a86cb24d1524e569 AS server
WORKDIR /src
RUN pip install --no-cache-dir uv==0.13.9
COPY mcp/pyproject.toml mcp/uv.lock mcp/README.md ./mcp/
COPY mcp/src ./mcp/src
COPY console/server/pyproject.toml console/server/uv.lock  ./console/server/
COPY console/server/src ./console/server/src
# --frozen: the lockfile decides; --no-dev: the tests stay out; --no-editable:
# /src does not travel with the venv (the reviews' image findings)
RUN VIRTUAL_ENV=/venv uv sync --directory console/server --frozen --no-dev \
        --no-editable --python 3.11

# gcr.io/distroless/python3-debian12: no shell, no package manager; python3
# is the run stage's interpreter, 3.11 as the install stage's.
FROM gcr.io/distroless/python3-debian12:nonroot@sha256:7d1042ce588ab97019fe95c24ffca7bc5a82ccdac572511d5e09bda4435c89c5
COPY --from=server /venv /venv
COPY --from=web /src/console/web/dist /app/web
ENV PYTHONPATH=/venv/lib/python3.11/site-packages \
    CONSOLE_WEB_DIR=/app/web \
    PYTHONUNBUFFERED=1
USER nonroot:nonroot
EXPOSE 8747
ENTRYPOINT ["python3", "-m", "console_server"]
