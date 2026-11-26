"""The service reading of the unattended metrics page (#158, RT-6.43 step 10).

One reading of the whole console process — a fleet and a stats book, no
person, no anchor, no login: fed by the two push consumers the login service
makes for the console's subscribe-only nkey (`AUTH_CONSOLE_METRICS` there),
delivered on `deliver.metrics.presence` and `deliver.metrics.stats`. The
seed sits in a file only the console reads
(`CONSOLE_METRICS_NKEY_SEED_FILE`); the page it guards sits behind the
scrape token (`CONSOLE_METRICS_TOKEN_FILE`), not behind an operator's
session — an unattended Prometheus may scrape it.

The reading exists only while the console process runs. It reconnects with
backoff (a restart of the login service makes the consumers again); a
refusal — the account is gone, the seed invalid — is terminal and logged
once: the page then carries the console's own observations, its agent
series absent (n/a, never 0), until the operator fixes it. Nothing here
publishes: the account has no publish right at all.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nats.aio.client import Client as NatsClient

from .nats_read import View
from .nats_read import tls_context  # noqa: PLC0415  the same TLS as every reading
from . import config  # noqa: F401  the type of the settings (a seam for tests)

if TYPE_CHECKING:  # the seam the tests replace: one call per subscribe
    Conn = Any

log = logging.getLogger("console")

CONNECT_TIMEOUT_SEC = 5.0
# the consumers' names as the login service makes them (EnsureMetrics there):
# parse_ack compares exactly, so the deliveries carry these two names
METRICS_PRESENCE_CONSUMER = "console-metrics_presence"
METRICS_STATS_CONSUMER = "console-metrics_stats"
BACKOFF_SEC = 2.0
BACKOFF_MAX_SEC = 30.0
SEED_MAX = 4096        # an nkey seed is one line; anything bigger is no seed


class RefusedTerminal(Exception):
    """The service account cannot connect (a revoked or invalid nkey);
    the reading ends: retrying a refusal all day helps nobody."""


class MetricsService:
    """The console's own reading for /metrics (one per process)."""

    def __init__(self, settings: config.Settings, view: View, *,
                 connect: Any = None) -> None:
        self.settings = settings
        self.view = view
        self._connect = connect or self._dial
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()
        self.last_delivery: float = 0.0   # monotonic, for the page's liveness

    def start(self) -> None:
        """The reading runs beside the HTTP side until the console stops."""
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="console-metrics")

    async def stop(self) -> None:
        self._stopped.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), 3.0)
            except (TimeoutError, asyncio.TimeoutError):
                # the reading may sit in a connect timeout: the console's
                # shutdown goes on; the daemon task dies with the process
                pass

    # -- the wiring ------------------------------------------------------------

    async def _dial(self, seed: str, lost: asyncio.Event | None = None) -> Any:
        def on_lost(_nc: Any) -> None:
            # nats-py hands the callback the connection: a NATS restart or
            # a lost one ends _serve (the loop connects again); without
            # this it would wait forever
            lost.set()

        kw: dict[str, Any] = {"nkeys_seed_str": seed, "allow_reconnect": False,
                              "connect_timeout": int(CONNECT_TIMEOUT_SEC)}
        if lost is not None:
            kw["disconnected_cb"] = on_lost
            kw["closed_cb"] = on_lost
        tls = tls_context(self.settings)
        if tls is not None:
            kw = {"tls": tls, "tls_handshake_first": True, **kw}
        nc = NatsClient()
        await nc.connect([self.settings.nats_url], **kw)
        return nc

    async def _run(self) -> None:
        try:
            seed = Path(self.settings.metrics_nkey_file).read_text().strip()
        except OSError as exc:
            log.warning("console: the metrics nkey seed is not readable (%s);"
                        " the agent numbers stay absent until it is",
                        type(exc).__name__)
            return
        try:
            mode = Path(self.settings.metrics_nkey_file).stat().st_mode
        except OSError:
            return   # unreadable: warned above
        if mode & 0o077:
            log.warning("console: the metrics nkey seed file is readable"
                        " by group or world (%o); the agent numbers stay"
                        " absent -- chmod it", mode & 0o777)
            return
        if (not seed or len(seed) > SEED_MAX or seed.split() != [seed]
                or not seed.startswith("SU")):
            log.warning("console: the metrics nkey seed is not one key;"
                        " the agent numbers stay absent")
            return
        backoff = BACKOFF_SEC
        while not self._stopped.is_set():
            try:
                lost = asyncio.Event()
                nc = await self._connect(seed, lost)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a refused, absent or slow NATS
                text = str(exc).lower()
                if "authorization" in text or "authentication" in text:
                    log.warning("console: the metrics service account was"
                                " refused (%s); check the nkey and the server's"
                                " auth_users; the agent numbers stay absent",
                                type(exc).__name__)
                    return
                log.info("console: the metrics NATS is not reachable (%s);"
                         " retrying", type(exc).__name__)
                await self._pause(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX_SEC)
                continue
            backoff = BACKOFF_SEC
            try:
                await self._serve(nc, lost)
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001 - the loop supersedes any waiter
                log.info("console: the metrics reading ended (%s); again",
                         type(exc).__name__)
            finally:
                try:
                    await nc.drain()
                except Exception:  # noqa: BLE001 - closing what closed
                    pass
            await self._pause(backoff)

    async def _pause(self, sec: float) -> None:
        try:
            await asyncio.wait_for(self._stopped.wait(), sec)
        except TimeoutError:
            pass

    async def _serve(self, nc: Any, lost: asyncio.Event) -> None:
        """Subscribe to the two deliver subjects until the connection ends
        (the event: set by the disconnected callback, awaited here)."""
        done = lost

        async def presence(msg: Any) -> None:
            self.last_delivery = time.monotonic()
            if self.view.fleet.on_delivery(msg.subject, msg.data, msg.reply,
                                            self.settings.presence_stream,
                                            METRICS_PRESENCE_CONSUMER):
                self.view.fire()

        async def stats(msg: Any) -> None:
            self.last_delivery = time.monotonic()
            self.view.stats_lines += 1
            self.view.stats.on_delivery(msg.subject, msg.data, msg.reply,
                                        self.settings.stats_stream, METRICS_STATS_CONSUMER)

        await nc.subscribe("deliver.metrics.presence", cb=presence)
        await nc.subscribe("deliver.metrics.stats", cb=stats)
        await nc.flush()
        log.info("console: the metrics reading is on (deliver.metrics.*)")
        stopped = asyncio.create_task(self._stopped.wait())
        done_wait = asyncio.create_task(done.wait())
        await asyncio.wait([stopped, done_wait], return_when=asyncio.FIRST_COMPLETED)
        for task in (stopped, done_wait):
            task.cancel()
