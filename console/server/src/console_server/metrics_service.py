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
            await asyncio.wait_for(asyncio.shield(self._task), 3.0)

    # -- the wiring ------------------------------------------------------------

    async def _dial(self, seed: str) -> Any:
        kw: dict[str, Any] = {"nkeys": seed, "allow_reconnect": False, "connect_timeout":
                              int(CONNECT_TIMEOUT_SEC)}
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
        if not seed or len(seed) > SEED_MAX or seed.split() != [seed]:
            log.warning("console: the metrics nkey seed is not one key;"
                        " the agent numbers stay absent")
            return
        backoff = BACKOFF_SEC
        while not self._stopped.is_set():
            try:
                nc = await self._connect(seed)
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
                await self._serve(nc)
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

    async def _serve(self, nc: Any) -> None:
        """Subscribe to the two deliver subjects until the connection ends."""
        done = asyncio.Event()

        async def presence(msg: Any) -> None:
            self.last_delivery = time.monotonic()
            if self.view.fleet.on_delivery(msg.subject, msg.data, msg.reply,
                                            self.settings.presence_stream,
                                            "console-metrics"):
                self.view.fire()

        async def stats(msg: Any) -> None:
            self.last_delivery = time.monotonic()
            self.view.stats_lines += 1
            self.view.stats.on_delivery(msg.subject, msg.data, msg.reply,
                                        self.settings.stats_stream, "console-metrics")

        async def lost() -> None:
            done.set()

        await nc.subscribe("deliver.metrics.presence", cb=presence)
        await nc.subscribe("deliver.metrics.stats", cb=stats)
        await nc.flush()
        log.info("console: the metrics reading is on (deliver.metrics.*)")
        try:
            await done.wait()
        finally:
            pass
