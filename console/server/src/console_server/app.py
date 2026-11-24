"""The console service: everything wired — settings, the NATS reader, the
fleet view, the Keycloak login, the HTTP side and the built browser app.

Read-only (ADR-015 stage 1): the only endpoints that answer with state
are reads; ``/v1/asks`` and ``/v1/stats`` answer empty until asks and
the STATS lines lie on NATS (the pilot shows presence and crown, live).
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from . import auth, config, httpd
from .fleet import Fleet
from .nats_read import read_forever

log = logging.getLogger("console")

COOKIE = "choirbus_session"
STATIC_TYPES = {".html": "text/html; charset=utf-8",
                ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".svg": "image/svg+xml",
                ".png": "image/png", ".ico": "image/x-icon",
                ".woff2": "font/woff2", ".json": "application/json"}


class Console:
    def __init__(self, settings: config.Settings):
        self.settings = settings
        self.fleet = Fleet(presence_subject=settings.presence_subject)
        self.change = asyncio.Event()
        self.sessions = auth.Sessions()
        self.keyring = auth.Keyring()
        self.endpoints: auth.Endpoints | None = None
        self._pending: dict[str, str] = {}  # login state -> code verifier
        self._http: httpd.Httpd | None = None
        self._nats_task: asyncio.Task | None = None  # type: ignore[type-arg]

    # --- life --------------------------------------------------------------------------

    async def start(self, discover: object | None = None) -> None:
        """``discover`` is a seam for tests; without it the issuer's own
        .well-known document names the endpoints."""
        if not self.settings.login_off:
            finder = discover or auth.discover
            self.endpoints = await finder(self.settings.issuer)  # type: ignore[misc,operator]
        if self.settings.nats_url:
            self._nats_task = asyncio.create_task(
                read_forever(self.settings, self.fleet, self.change),
                name="console-nats")
        self._http = httpd.Httpd(handler=self.handle,
                                 host=self.settings.listen_host,
                                 port=self.settings.listen_port)
        await self._http.start()

    async def stop(self) -> None:
        if self._http is not None:
            await self._http.stop()
        if self._nats_task is not None:
            self._nats_task.cancel()
            try:
                await self._nats_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    # --- the web side -------------------------------------------------------------------

    def base(self) -> str:
        return self.settings.public_url.rstrip("/")

    def _cookie(self, token: str) -> str:
        bits = [f"{COOKIE}={token}", "Path=/", "HttpOnly", "SameSite=Lax"]
        if config.secure_cookie(self.settings):
            bits.append("Secure")
        bits.append(f"Max-Age={int(self.settings.session_sec)}")
        return "; ".join(bits)

    def _operator(self, headers: dict[str, str]) -> auth.Operator | None:
        if self.settings.login_off:
            # tests and the mock's seam only, listen on loopback: the
            # operator is a stand-in, never a logged-in person
            return auth.Operator(username="nobody", display="nobody (login off)",
                                 groups=())
        raw = headers.get("cookie", "")
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE:
                operator = self.sessions.get(value)
                if operator is not None:
                    return operator
        return None

    def _data_of(self, path: str) -> Path | None:
        """A file of the built app inside web_dir, or None. No traversal."""
        if not self.settings.web_dir:
            return None
        root = Path(self.settings.web_dir).resolve()
        rel = path.lstrip("/") or "index.html"
        if rel in ("favicon.svg",) or rel.startswith("assets/"):
            want = (root / rel).resolve()
            if want.is_file() and root in want.parents:
                return want
        return None

    def _static(self, path: str) -> httpd.Response:
        if not self.settings.web_dir:
            return httpd.Response.text("Choirbus console: no built web app set"
                                       " (CONSOLE_WEB_DIR)", 404)
        root = Path(self.settings.web_dir).resolve()
        if path == "/" or path == "":
            file = root / "index.html"
        else:
            file = self._data_of(path) or (root / "index.html")
        if not file.is_file():
            return httpd.Response.text("no built web app", 404)
        body = file.read_bytes()
        kind = STATIC_TYPES.get(file.suffix, "application/octet-stream")
        return httpd.Response(status=200, body=body, content_type=kind)

    # --- the fleet stream ----------------------------------------------------------------

    def _stream(self) -> httpd.SSE:
        """One fleet stream: the first event is a snapshot; every change
        after it is one card event per changed or new agent and a drop
        per gone one — each stream diffs against what it sent itself."""

        def next_event(until_synced: dict | None) -> dict:
            cards = {card["name"]: card for card in self.fleet.cards()}
            if not self.fleet.synced or until_synced is None:
                until_synced.clear()
                until_synced.update(cards)
                return {"type": "snapshot", "cards": list(cards.values())}
            fresh = [cards[name] for name in sorted(cards)
                     if cards[name] != until_synced.get(name)]
            drops = [name for name in sorted(until_synced) if name not in cards]
            until_synced.clear()
            until_synced.update(cards)
            if not fresh and not drops:
                return {"type": "cards", "cards": [], "drops": []}
            return {"type": "cards", "cards": fresh, "drops": drops}

        sent: dict = {"sent": {}}  # closed over per stream

        def first() -> dict:
            return next_event(sent["sent"])

        def later() -> dict:
            return next_event(sent["sent"])

        if not self.settings.nats_url:
            return httpd.SSE(first={"type": "snapshot", "cards": []},
                             changes=asyncio.Event(),
                             snapshot=lambda: {"type": "cards", "cards": [], "drops": []})
        return httpd.SSE(first=first(), changes=self.change, snapshot=later)

    # --- routing ---------------------------------------------------------------------------

    async def handle(self, path: str, query: dict[str, str],
                     headers: dict[str, str]) -> httpd.Response | httpd.SSE:
        s = self.settings
        if path == s.health_path:
            return httpd.Response.text("ok")
        if path in ("/login", "/callback"):
            if s.login_off:
                return httpd.Response.text("login off", 400)
            return await self._login(path, query)
        if path == "/v1/me":
            operator = self._operator(headers)
            if operator is None:
                return _unauthorized()
            return httpd.Response.json(
                {"username": operator.username, "display_name": operator.display,
                 "groups": list(operator.groups)})
        if path == "/v1/fleet":
            if self._operator(headers) is None:
                return _unauthorized()
            return self._stream()
        for route in ("/v1/asks", "/v1/stats"):
            if path == route:
                if self._operator(headers) is None:
                    return _unauthorized()
                # empty until asks and STATS lie on NATS (the stage 1 cut)
                return httpd.Response.json([])
        if path.startswith("/v1/threads/"):
            if self._operator(headers) is None:
                return _unauthorized()
            return httpd.Response.json({"posts": [], "next": None})
        if path == "/v1/graph":
            if self._operator(headers) is None:
                return _unauthorized()
            return httpd.Response.json(self.fleet.graph())
        if path == "/logout":
            response = httpd.Response.see("/")
            response.headers = response.headers + (("Set-Cookie",
                                                    f"{COOKIE}=; Path=/; Max-Age=0"),)
            return response
        # the browser app
        operator = self._operator(headers)
        if path == "/" and operator is None and not s.login_off:
            return httpd.Response.see("/login")
        data = self._data_of(path)
        if data is not None or path in ("/", "") or path.startswith("/assets/"):
            return self._static(path)
        return httpd.Response.text("not found", 404)

    # --- the login flow -------------------------------------------------------------------

    async def _login(self, path: str, query: dict[str, str]) -> httpd.Response:
        assert self.endpoints is not None
        s = self.settings
        redirect = f"{self.base()}/callback"
        if path == "/login":
            url, state, verifier = auth.login_url(self.endpoints, s.client_id, redirect)
            self._pending[state] = verifier
            if len(self._pending) > 64:  # an ornament of abandoned logins
                self._pending.clear()
            return httpd.Response.see(url)
        # /callback
        state, code = query.get("state", ""), query.get("code", "")
        verifier = self._pending.pop(state, None)
        if not code or not verifier:
            return httpd.Response.text("login: no code for this state", 400)
        try:
            raw = await auth.exchange(self.endpoints, s.client_id, redirect, code, verifier)
            operator = await auth.verify(s, self.endpoints, self.keyring, raw)
        except auth.AuthError as exc:
            log.warning("console login refused: %s", exc)
            return httpd.Response.text(f"login refused: {exc}", 403)
        token = self.sessions.issue(operator, s.session_sec)
        response = httpd.Response.see("/")
        response.headers = response.headers + (("Set-Cookie", self._cookie(token)),)
        log.info("console login: %s", operator.username)
        return response


def _unauthorized() -> httpd.Response:
    return httpd.Response.json({"error": "operator login required"}, 401)


async def serve(settings: config.Settings) -> None:
    console = Console(settings)
    logging.basicConfig(level=logging.INFO)
    await console.start()
    log.info("console: http://%s:%d %s", settings.listen_host, console._http.bound_port
             if console._http else settings.listen_port,
             f"(web: {settings.web_dir or 'off'})")
    try:
        await asyncio.Event().wait()
    finally:
        await console.stop()
