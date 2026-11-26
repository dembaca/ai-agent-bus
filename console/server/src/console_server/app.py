"""The console service: everything wired — settings, the NATS reader, the
fleet view, the Keycloak login, the HTTP side and the built browser app.

Read-only (ADR-015 stage 1): the only endpoints that answer with state
are reads; ``/v1/asks`` lists the open asks of the agents' ask records
and ``/v1/stats`` the two stage-1 reports from their STATS lines: shown,
never answered.
Each logged-in operator reads the bus with a NATS connection of their own
(RT-6.43): the fleet a session shows is what that person's consumers
deliver, and there is no reader before a login.

The reviews' edges, held here as invariants: the login refuses when the
operators group is unset (fail closed), a stream ends with its session
(logout and expiry), one login state per browser with an expiry, and the
events are the app's own three: ``snapshot``, one ``card`` per change,
one ``drop`` per gone agent.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections import deque

import httpx
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import auth, config, httpd, telemetry
from .metrics_service import MetricsService
from .auth import Endpoints
from .fleet import Fleet
from .nats_read import PersonReader, View, reachable, tls_context

log = logging.getLogger("console")

LOGGED_OUT_LOCALLY = ("Logged out of the console. The Keycloak session is still"
                      " open: close the browser to end it.")
COOKIE = "choirbus_session"        # the session (8 h default)
FLOW_COOKIE = "choirbus_flow"      # binds a login to the browser (10 min)
STATE_TTL = 600.0                  # a login state lives ten minutes
STATE_MAX = 64                     # then the oldest goes, never all
STATE_RATE_MAX = 16                # new states per minute; past that /login
STATE_RATE_SEC = 60.0              # answers 429 without creating one
STREAMS_PER_OPERATOR = 4            # every further stream answers 429
SESSIONS_PER_OPERATOR = 4           # a fifth login closes the oldest (the
                                    # login service allows 8 per person)
REACH_CACHE_SEC = 5.0               # /healthz asks NATS at most this often
STATIC_TYPES = {".html": "text/html; charset=utf-8",
                ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".svg": "image/svg+xml",
                ".png": "image/png", ".ico": "image/x-icon",
                ".woff2": "font/woff2", ".json": "application/json"}

API_HEADERS = (("Cache-Control", "no-store"),
               ("X-Content-Type-Options", "nosniff"),
               ("Referrer-Policy", "no-referrer"),
               ("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'"))
# the pages: same-origin, not no-referrer. Under no-referrer a browser sends
# `Origin: null` with the page's own logout POST, which /logout refuses as
# cross-site (RT-6.43 step 9); same-origin still sends no referrer to any
# other site
STATIC_HEADERS = (("X-Content-Type-Options", "nosniff"),
                  ("Referrer-Policy", "same-origin"),
                  ("Content-Security-Policy",
                   "default-src 'self'; img-src 'self' data:;"
                   " font-src 'self' data:; base-uri 'none';"
                   " frame-ancestors 'none'"))


class Console:
    def __init__(self, settings: config.Settings):
        self.settings = settings
        # the stand-in view: login off (tests and the mock) and sessions made
        # without one; nothing feeds it — every login has a view of its own
        self.telemetry = telemetry.Telemetry()   # the console's /metrics counters
        self.standin = View(settings.presence_subject, settings.stats_subject,
                            count=self.telemetry.note_event)
        # the unattended metrics page (#158): the console's own reading on
        # the service nkey, and the scrape token that gates it; both or
        # neither, the settings check pairs them
        self.service = View(settings.presence_subject, settings.stats_subject,
                             count=self.telemetry.note_event)
        self.metrics = MetricsService(settings, self.service) if settings.metrics_nkey_file else None
        self._metrics_http: httpd.Httpd | None = None
        self.sessions = auth.Sessions()
        self.keyring = auth.Keyring()
        self.endpoints: auth.Endpoints | None = None
        self._pending: dict[str, tuple[str, float, str]] = {}  # state→(verifier, until, flow)
        self._state_rate: deque[float] = deque()  # when states were made
        self._http: httpd.Httpd | None = None
        self._discovery: asyncio.Task | None = None  # type: ignore[type-arg]
        self._streams: dict[str, int] = {}  # session token → open streams
        self._reach: tuple[float, bool] | None = None  # (when asked, answer)

    @property
    def fleet(self) -> Fleet:
        """The stand-in view's fleet (login off; tests)."""
        return self.standin.fleet

    @fleet.setter
    def fleet(self, value: Fleet) -> None:
        self.standin.fleet = value

    # --- the fleet's wake: every stream has its own event -----------------------------

    def fire(self) -> None:
        """The stand-in fleet changed: wake its streams, none may steal it."""
        self.standin.fire()

    def _view(self, token: str | None) -> View:
        view = self.sessions.view(token)
        return view if isinstance(view, View) else self.standin

    # --- foreign dependencies ----------------------------------------------------------

    async def start(self, discover: Callable[[str], Awaitable[Endpoints]] | None = None) -> None:
        """The HTTP side comes up first (health, the web app); the issuer's
        endpoints are then discovered, and kept for — Keycloak down at the
        start, or for a while, leaves /login a 503, never a dead console.
        ``discover`` is a seam for tests."""
        if not self.settings.login_off:
            finder = discover or auth.discover
            self._discovery = asyncio.create_task(self._discover_loop(finder),
                                                 name="console-discovery")
        self._http = httpd.Httpd(handler=self.handle,
                                 host=self.settings.listen_host,
                                 port=self.settings.listen_port)
        await self._http.start()
        if self.metrics is not None:
            # the unattended page's listener of its own: loopback by
            # default, the token page alone on it (#158). The token is read
            # here once, at the start — never lazily on a request path.
            try:
                self._scrape_token_value = Path(
                    self.settings.metrics_token_file).read_text().strip()
            except OSError:
                self._scrape_token_value = ""
                log.warning("console: the metrics scrape token is not"
                            " readable; the token path stays closed")
            host = self.settings.metrics_listen_host
            if host not in ("127.0.0.1", "::1", "localhost") and tls_context(self.settings) is None:
                log.warning("console: the metrics listener is on %s without"
                            " TLS; a non-loopback listener belongs behind"
                            " TLS", host)
            self._metrics_http = httpd.Httpd(handler=self.handle_metrics,
                                             host=host,
                                             port=self.settings.metrics_listen_port)
            await self._metrics_http.start()
            self.metrics.start()

    async def _discover_loop(self, finder: Callable[[str], Awaitable[Endpoints]]) -> None:
        while True:
            try:
                self.endpoints = await finder(self.settings.issuer)
                log.info("console: the issuer's endpoints are in")
                if not self.endpoints.end_session:
                    log.warning("console: the issuer names no end-session endpoint;"
                                " a logout leaves the Keycloak session open"
                                " (RT-6.43 step 9)")
                return
            except (auth.AuthError, httpx.HTTPError, OSError, TimeoutError) as exc:
                log.warning("console: %s; /login answers 503; retrying", exc)
                await asyncio.sleep(5.0)

    @property
    def _scrape_token(self) -> str:
        """#158: the unattended page's scrape token, read once at start; ''
        while it is off, unreadable or empty (nobody scrapes with a secret
        the operator never set)."""
        if not hasattr(self, "_scrape_token_value"):
            # start() reads it; without it the path stays closed: no secret
            # is read on a request path
            self._scrape_token_value = ""
        return self._scrape_token_value

    async def handle_metrics(self, path: str, query: dict[str, str],
                             headers: dict[str, str]) -> httpd.Response:
        """The unattended page's listener (#158): only ``GET /metrics`` with
        the scrape token answers — the service reading, aggregates only.
        Without or with a wrong token: 401, plain, no operator session here;
        any other path: 404. The operator's own page stays on the main
        listener, behind the login."""
        if path != "/metrics" or headers.get(":method", "GET") != "GET":
            self.telemetry.note(telemetry.route_bucket(path), headers.get(":method", "GET"), 404)
            return httpd.Response(status=404, body=b"not found",
                                  content_type="text/plain")
        # the token in bytes (a non-ASCII Authorization cannot raise), the
        # status counted once per answer, at it
        auth = headers.get("authorization", "").encode("utf-8", "replace")
        want = ("Bearer " + self._scrape_token).encode("utf-8", "replace")
        if not secrets.compare_digest(auth, want):
            self.telemetry.note(telemetry.route_bucket(path), "GET", 401)
            return httpd.Response(status=401, body=b"unauthorized",
                                  content_type="text/plain")
        self.telemetry.note(telemetry.route_bucket(path), "GET", 200)
        return httpd.Response(status=200,
                              body=telemetry.render(
                                  self.telemetry, self.sessions.count(),
                                  sum(self._streams.values()),
                                  [], self.service,
                                  live=bool(self.metrics and
                                             self.metrics.last_delivery > 0)).encode(),
                              content_type="text/plain; version=0.0.4; charset=utf-8",
                              headers=API_HEADERS)

    async def stop(self) -> None:
        if self._http is not None:
            await self._http.stop()
        if self.metrics is not None:
            await self.metrics.stop()
        if self._metrics_http is not None:
            await self._metrics_http.stop()
            self._metrics_http = None
        for view in self.sessions.views():
            if isinstance(view, View) and view.reader is not None:
                await view.reader.aclose()  # key and tokens dropped
        for task in (self._discovery,):
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

    # --- the web side -------------------------------------------------------------------

    def base(self) -> str:
        return self.settings.public_url.rstrip("/")

    def _cookie(self, name: str, token: str, max_age: int) -> str:
        bits = [f"{name}={token}", "Path=/", "HttpOnly", "SameSite=Lax"]
        if config.secure_cookie(self.settings):
            bits.append("Secure")
        bits.append(f"Max-Age={max_age}")
        return "; ".join(bits)

    def _cookie_value(self, headers: dict[str, str], name: str) -> str | None:
        for part in headers.get("cookie", "").split(";"):
            key, _, value = part.strip().partition("=")
            if key == name:
                return value
        return None

    def _operator(self, headers: dict[str, str]) -> auth.Operator | None:
        if self.settings.login_off:
            # tests and the mock's seam only, loopback by config: the
            # operator is a stand-in, never a logged-in person
            return auth.Operator(username="nobody", display="nobody (login off)",
                                 groups=())
        return self.sessions.get(self._cookie_value(headers, COOKIE))

    def _session_token(self, headers: dict[str, str]) -> str | None:
        return self._cookie_value(headers, COOKIE)

    def _data_of(self, path: str) -> Path | None:
        """A file of the built app inside web_dir, or None. No traversal."""
        if not self.settings.web_dir:
            return None
        root = Path(self.settings.web_dir).resolve()
        rel = path.lstrip("/").split("?")[0]
        if rel == "favicon.svg" or rel.startswith("assets/"):
            want = (root / rel).resolve()
            if want.is_file() and root in want.parents:
                return want
        return None

    def _static(self, path: str) -> httpd.Response:
        if not self.settings.web_dir:
            return httpd.Response.text("Choirbus console: no built web app set"
                                       " (CONSOLE_WEB_DIR)", 404)
        root = Path(self.settings.web_dir).resolve()
        if path in ("/", ""):
            file = root / "index.html"
        else:
            file = self._data_of(path) or (root / "index.html")
        if not file.is_file():
            return httpd.Response.text("no built web app", 404)
        body = file.read_bytes()
        kind = STATIC_TYPES.get(file.suffix, "application/octet-stream")
        return httpd.Response(status=200, body=body, content_type=kind,
                               headers=STATIC_HEADERS)

    # --- the fleet stream ----------------------------------------------------------------

    def _stream(self, token: str | None) -> httpd.Response | httpd.SSE:
        """One fleet stream over SSE: the first event is a snapshot; every
        change after it is one ``card`` event per changed or new agent and
        one ``drop`` per gone agent — the app's three event shapes, and
        each stream diffs against what it sent itself. The stream ends
        with its session (logout, expiry) and counts against its cap."""
        if self.sessions.get(token) is None and not self.settings.login_off:
            return _unauthorized()
        open_now = self._streams.get(token or "-", 0)
        if token and open_now >= STREAMS_PER_OPERATOR:
            return httpd.Response.json({"error": "too many fleet streams"}, 429,
                                        API_HEADERS)
        view = self._view(token)
        wake = asyncio.Event()
        view.wakes.add(wake)
        if token:
            self._streams[token] = open_now + 1

        def done() -> None:
            view.wakes.discard(wake)
            if token:
                left = self._streams.get(token, 1) - 1
                if left > 0:
                    self._streams[token] = left
                else:
                    self._streams.pop(token, None)

        sent: dict[str, dict[str, Any]] = {}

        def next_events() -> list[dict[str, Any]]:
            cards: dict[str, dict[str, Any]] = {c["name"]: c for c in view.fleet.cards()}
            if not view.fleet.synced or not sent:
                sent.clear()
                sent.update(cards)
                return [{"type": "snapshot", "cards": list(cards.values())}]
            out: list[dict[str, Any]] = [{"type": "card", "card": cards[name]}
                                    for name in sorted(cards)
                                    if cards[name] != sent.get(name)]
            out += [{"type": "drop", "name": name}
                    for name in sorted(sent) if name not in cards]
            sent.clear()
            sent.update(cards)
            return out

        first = next_events()
        return httpd.SSE(first=first[0], changes=wake, snapshot=next_events,
                         alive=(None if not token
                                else lambda: self.sessions.get(token) is not None),
                         teardown=done,
                         beat=next_events)  # the same diff, on the beat

    # --- routing ---------------------------------------------------------------------------

    async def handle(self, path: str, query: dict[str, str],
                     headers: dict[str, str]) -> httpd.Response | httpd.SSE:
        answer = await self._route(path, query, headers)
        # the console's own observation (#158): every answered request, by its
        # bucketed route, its method (the logout posts) and its status; an
        # SSE counts as the 200 its stream answers
        self.telemetry.note(telemetry.route_bucket(path, self.settings.health_path),
                            headers.get(":method", "GET"),
                            getattr(answer, "status", 200))
        return answer

    async def _route(self, path: str, query: dict[str, str],
                     headers: dict[str, str]) -> httpd.Response | httpd.SSE:
        s = self.settings
        if (headers.get(":method", "GET") == "POST") != (path == "/logout"):
            # the logout alone changes state and takes POST only, so no
            # cross-site link or <img> can end a session (RT-6.43 step 9)
            return httpd.Response(status=405, body=b"method not allowed",
                                  content_type="text/plain",
                                  headers=(("Allow", "POST" if path == "/logout"
                                            else "GET"),))
        if path == s.health_path:
            # readiness: the settings held at the start, and the NATS server
            # answers (a TLS handshake for tls://). There is no reader before
            # a login (RT-6.43), so no fleet to wait for: each operator's
            # connection is that session's, renewed and retried on its own
            if self.settings.nats_url and not await self._nats_reachable():
                return httpd.Response.text("the NATS server does not answer", 503)
            return httpd.Response.text("ok")
        if path == "/livez":
            # liveness: the process serves; every reader retries on its own
            return httpd.Response.text("ok")
        if path in ("/login", "/callback"):
            if s.login_off:
                return httpd.Response.text("login off", 400)
            if self.endpoints is None:
                return httpd.Response.text("the issuer's endpoints are not"
                                           " in yet", 503)
            return await self._login(path, query, headers)
        if path == "/v1/me":
            if self._operator(headers) is None:
                return _unauthorized()
            operator = self._operator(headers)
            assert operator is not None
            view = self._view(self._session_token(headers))
            return httpd.Response.json(
                {"username": operator.username, "display_name": operator.display,
                 "groups": list(operator.groups),
                 # the session's own reading (RT-6.43): "reading", "waiting: …",
                 # "refused: …", "ended…" — the reason a fleet stays empty
                 "bus": view.bus()}, headers=API_HEADERS)
        if path == "/v1/fleet":
            return self._stream(self._session_token(headers))
        if path in ("/v1/asks", "/v1/stats"):
            if self._operator(headers) is None:
                return _unauthorized()
            # RT-6.43 steps 6 and 8: read-only, from the agents' own records as this
            # session's own consumers deliver them; "for me" by the operator's anchor
            view = self._view(self._session_token(headers))
            return httpd.Response.json(view.asks() if path == "/v1/asks"
                                       else view.stats.reports(), headers=API_HEADERS)
        if path == "/metrics":
            # #158: who may scrape — the operator that is logged in, the same
            # right /v1/* needs; no anonymous scrape, no token of its own and no
            # new NATS right (the owner's go settled it the service variant).
            # The agent numbers are this session's own reading, the same data
            # /v1/stats shows it (RT-6.43); console metrics are the process's.
            # The unattended variant serves on its own listener only
            # (handle_metrics): a token-gated page has no place on the
            # operators' network, the owner's ask said loopback.
            if self._operator(headers) is None:
                return _unauthorized()
            readers = [v.reader for v in self.sessions.views()
                       if v.reader is not None]
            return httpd.Response(status=200,
                                  body=telemetry.render(
                                      self.telemetry, self.sessions.count(),
                                      sum(self._streams.values()), readers,
                                      self._view(self._session_token(headers))).encode(),
                                  content_type="text/plain; version=0.0.4; charset=utf-8",
                                  headers=API_HEADERS)
        if path.startswith("/v1/threads/"):
            if self._operator(headers) is None:
                return _unauthorized()
            return httpd.Response.json({"posts": [], "next": None},
                                       headers=API_HEADERS)
        if path == "/v1/graph":
            if self._operator(headers) is None:
                return _unauthorized()
            view = self._view(self._session_token(headers))
            # point 7: the operator's own person node is marked from the login's own
            # record (RT-6.43 step 2), never from anything the browser sends
            return httpd.Response.json(view.fleet.graph(view.anchor()), headers=API_HEADERS)
        if path == "/logout":
            own = urlsplit(s.public_url)
            want = f"{own.scheme}://{own.netloc}"
            origin = headers.get("origin", "")
            if origin and origin != want:
                return httpd.Response.text("cross-site logout", 403)
            # a cross-site <img> or navigation sends no Origin; Fetch
            # Metadata names the site anyway (absent: as old as the header)
            fetch_site = headers.get("sec-fetch-site", "")
            if fetch_site and fetch_site not in ("same-origin", "same-site", "none"):
                return httpd.Response.text("cross-site logout", 403)
            token = self._session_token(headers)
            # the server ends it, not only the cookie
            hint = self.sessions.drop(token) if token else ""
            # RT-6.43 step 9: then the realm's own session (RP-initiated
            # logout), so /login does not slip back in through Keycloak's
            # single sign-on; the stored ID token is the hint, expired or not;
            # with no live session (a restart) the realm asks to confirm
            if s.login_off:
                response = httpd.Response.see("/")
            else:
                target = ""
                if self.endpoints is None:
                    log.warning("console logout: the issuer's endpoints are not"
                                " in yet; the Keycloak session stays open")
                else:
                    target = auth.logout_url(self.endpoints, s.client_id,
                                             f"{self.base()}/", hint)
                    if not target:
                        log.warning("console logout: the issuer names no"
                                    " end-session endpoint; the Keycloak"
                                    " session stays open")
                # without the realm's logout, a page that starts no login:
                # /login would come straight back in through the open session
                response = (httpd.Response.see(target) if target
                            else httpd.Response.text(LOGGED_OUT_LOCALLY))
            # the hint travels in the Location URL: kept out of caches and
            # of the next page's Referer (it still reaches the browser's
            # history and the access logs of Keycloak and its proxy)
            response.headers = response.headers + (
                ("Set-Cookie", f"{COOKIE}=; Path=/; Max-Age=0"),
                ("Cache-Control", "no-store"),
                ("Referrer-Policy", "no-referrer"))
            return response
        # the browser app
        operator = self._operator(headers)
        if path == "/" and operator is None:
            return httpd.Response.see("/login")
        data = self._data_of(path)
        if data is not None or path in ("/", "") or path.startswith("/assets/"):
            return self._static(path)
        return httpd.Response.text("not found", 404)

    # --- the login flow -------------------------------------------------------------------

    async def _login(self, path: str, query: dict[str, str],
                     headers: dict[str, str]) -> httpd.Response:
        assert self.endpoints is not None
        s = self.settings
        redirect = f"{self.base()}/callback"
        if path == "/login":
            url, state, verifier = auth.login_url(self.endpoints, s.client_id, redirect)
            flow = _b64u_cookie()
            now = time.time()
            while self._state_rate and now - self._state_rate[0] > STATE_RATE_SEC:
                self._state_rate.popleft()
            if len(self._state_rate) >= STATE_RATE_MAX:
                # a flood gets a 429, never the power to evict a genuine
                # login state by filling the table
                return httpd.Response.text("too many logins; wait a minute", 429)
            self._state_rate.append(now)
            self._pending = {k: row for k, row in self._pending.items()
                             if row[1] > now}
            while len(self._pending) >= STATE_MAX:
                oldest = min(self._pending, key=lambda k: self._pending[k][1])
                del self._pending[oldest]
            self._pending[state] = (verifier, now + STATE_TTL, flow)
            response = httpd.Response.see(url)
            response.headers = response.headers + (
                ("Set-Cookie", self._cookie(FLOW_COOKIE, flow, int(STATE_TTL))),)
            return response
        # /callback
        state, code = query.get("state", ""), query.get("code", "")
        got_flow = self._cookie_value(headers, FLOW_COOKIE)
        row = self._pending.get(state)
        if not code or row is None or row[1] < time.time():
            self._pending.pop(state, None)
            return httpd.Response.text("login: no code for this state", 400)
        verifier, _until, flow = row
        if not got_flow or not secrets.compare_digest(got_flow, flow):
            # a login of ours has its flow cookie; without it this is not ours
            return httpd.Response.text("login: the flow cookie does not match", 400)
        self._pending.pop(state, None)
        try:
            tokens = await auth.exchange(self.endpoints, s.client_id, redirect, code, verifier)
        except auth.Refused as exc:
            # a stale or used code: nothing about the person
            log.warning("console login: the code was refused: %s", exc)
            return httpd.Response.text("login expired, please try again", 400)
        except (auth.AuthError, httpx.HTTPError, OSError, TimeoutError) as exc:
            log.warning("console login failed: %s", exc)
            return httpd.Response.text(f"login failed: {exc}", 502)
        try:
            operator = await auth.verify(s, self.endpoints, self.keyring, tokens.access)
        except auth.Refused as exc:
            log.warning("console login refused: %s", exc)
            return httpd.Response.text("login refused: not a member of the"
                                       " operators group", 403)
        except (auth.AuthError, httpx.HTTPError, OSError, TimeoutError) as exc:
            # discovery, token endpoint, jwks, transport: the server's
            # fault, not the person's
            log.warning("console login failed: %s", exc)
            return httpd.Response.text(f"login failed: {exc}", 502)
        presented = self._session_token(headers)
        if presented:
            # a login from a browser that still holds a session ends that one
            self.sessions.drop(presented)
        token = self._open_session(operator, tokens)
        response = httpd.Response.see("/")
        response.headers = response.headers + (
            ("Set-Cookie", self._cookie(COOKIE, token, int(s.session_sec))),
            ("Set-Cookie", f"{FLOW_COOKIE}=; Path=/; Max-Age=0"),)
        log.info("console login: %s", operator.username)
        return response


    # --- the operator's own connection (RT-6.43) ---------------------------------------

    def _open_session(self, operator: auth.Operator, tokens: auth.Tokens) -> str:
        """A session with a view of its own; with NATS set, a reader that
        connects as this operator with a key pair made for this login."""
        s = self.settings
        view = View(s.presence_subject, s.stats_subject,
                    count=self.telemetry.note_event)
        if s.nats_url:
            sub = operator.sub

            async def renew(refresh: str) -> auth.Tokens:
                return await self._renew(sub, refresh)

            view.reader = PersonReader(s, tokens, renew, view,
                                       until=time.time() + s.session_sec,
                                       on_end=lambda: self.sessions.end(view))
        token = self.sessions.issue(operator, s.session_sec, view,
                                    per_person=SESSIONS_PER_OPERATOR,
                                    id_token=tokens.id_token)
        if view.reader is not None:
            view.reader.start()
        return token

    async def _renew(self, sub: str, refresh: str) -> auth.Tokens:
        """Fresh tokens for a login's renewal, verified as at the login: the
        realm's refusal, a token without the operators' group or of another
        ``sub`` is ``auth.Refused`` and ends the session (RT-6.43 step 4)."""
        if self.endpoints is None:
            raise auth.AuthError("the issuer's endpoints are not in")
        tokens = await auth.refresh(self.endpoints, self.settings.client_id, refresh)
        operator = await auth.verify(self.settings, self.endpoints, self.keyring,
                                     tokens.access)
        if operator.sub != sub:
            raise auth.Refused("the refreshed token names another person")
        return tokens

    async def _nats_reachable(self) -> bool:
        now = time.monotonic()
        if self._reach is None or now - self._reach[0] > REACH_CACHE_SEC:
            self._reach = (now, await reachable(self.settings))
        return self._reach[1]


def _b64u_cookie() -> str:
    import base64
    return base64.urlsafe_b64encode(secrets.token_bytes(24)).rstrip(b"=").decode()


def _unauthorized() -> httpd.Response:
    return httpd.Response.json({"error": "operator login required"}, 401,
                               API_HEADERS)


async def serve(settings: config.Settings) -> None:
    console = Console(settings)
    logging.basicConfig(level=logging.INFO)
    await console.start()
    log.info("console: http://%s:%d %s", settings.listen_host,
             console._http.bound_port if console._http else settings.listen_port,
             f"(web: {settings.web_dir or 'off'})")
    try:
        await asyncio.Event().wait()
    finally:
        await console.stop()
