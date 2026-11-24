"""The console's read-only NATS side: one static account (the pilot's
deviation from ADR-015 way A), its own push consumer on the PRESENCE
stream — deleted and made anew at every connect, as the login service
remakes an agent's at every credential, so a reconnect (or a restart)
replays the whole fleet — and nothing published, ever.

Connection rules as the mcp package's transport (``natsbus``): ``tls://``
anywhere, ``nats://`` only to a loopback host, TLS first on the real
connection. The account's inbox prefix is the console's own, so its
JetStream replies can be granted without reading any other client's.

A consumer recreated under the same name replays nothing (the reviews'
restart probe), so the console deletes the old one first — the replay is
the state, and no push queue is trusted to bridge a gap.
"""
from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
from pathlib import Path
from typing import Any, Callable

import nats
from nats.aio.client import Client as NatsClient

from . import config
from .fleet import Fleet


log = logging.getLogger("console.nats")

CONSUMER_API_TIMEOUT = 5.0
INBOX_PREFIX = "_INBOX_console"   # the console's own, granted alone
# A silent stream reconnects for a replay; the idle bound couples to the
# probe's bound (a silent fleet's reconnect lands inside the bound, whatever
# it is) with a floor, and the watchdog wakes every quarter of the bound.
IDLE_FLOOR_SEC = 15.0


class NatsError(Exception):
    pass


def tls_context(settings: config.Settings) -> ssl.SSLContext | None:
    """The verifying context for a tls:// URL; None for loopback nats://."""
    if settings.nats_url.startswith("nats://"):
        return None
    return ssl.create_default_context(cafile=settings.nats_ca_file or None)


def consumer_spec(settings: config.Settings) -> dict[str, Any]:
    """The console's presence consumer, named and shaped like the login
    service's (Go ``consumers/manager.go``): a push consumer on the
    PRESENCE stream, no acks, the last message per subject replayed at
    its making, pushed to the console's deliver subject, an hour idle."""
    return {
        "stream_name": settings.presence_stream,
        "config": {
            "ack_policy": "none",
            "ack_wait": 0,          # nats-server needs a value for none
            "deliver_policy": "last_per_subject",
            "deliver_subject": settings.deliver_subject,
            "filter_subject": f"{settings.presence_subject}.>",
            "inactive_threshold": 3600 * 1_000_000_000,
        },
    }


async def _api(nc: NatsClient, subject: str,
                payload: dict[str, Any]) -> dict[str, Any]:
    reply = await nc.request(subject, json.dumps(payload).encode(),
                             timeout=CONSUMER_API_TIMEOUT)
    body = json.loads(reply.data.decode())
    if not isinstance(body, dict):
        raise NatsError("the server's answer is no JetStream object")
    return body


async def ensure_consumer(nc: NatsClient, settings: config.Settings,
                         fleet: Fleet) -> None:
    """Delete then create, as the login service remakes its consumers: a
    consumer that still stands under this name replays nothing on
    reattach, and the replay is the fleet's state after a restart. A
    create that answers at all is a finished replay — an empty fleet or
    one older than the stream's max age delivers nothing, and that is
    still "in sync", not a red probe."""
    await _api(nc, f"$JS.API.CONSUMER.DELETE.{settings.presence_stream}"
                  f".{settings.consumer_name}", {})
    # a DELETE the server cannot answer (none yet) is fine: the CREATE that
    # follows decides; its error is the honest one
    body = await _api(nc, f"$JS.API.CONSUMER.CREATE.{settings.presence_stream}"
                          f".{settings.consumer_name}", consumer_spec(settings))
    error = body.get("error")
    if error is not None:
        raise NatsError(f"consumer create refused: {error!r}")
    # the creation answer says how much there is to deliver; nothing left
    # (an empty stream at creation) is a finished replay. Messages still
    # pending wait for their deliveries — the fleet stamps them when one
    # arrives with pending 0.
    if body.get("num_pending") == 0:
        fleet.synced = True
        fleet.last_sync_at = time.monotonic()



async def read_forever(settings: config.Settings, fleet: Fleet,
                       on_change: Callable[[], None]) -> None:
    """Connect, subscribe the console's deliver subject, remake the
    consumer (its replay flows to exactly this subscription) and feed
    every delivery to the reader. One failed delivery never ends the
    loop; a silent stream reconnects after ``IDLE_RECONNECT_SEC`` — the
    replay then says what still stands. The loop never publishes."""
    backoff = 1.0
    while True:
        try:
            # the seed is read per attempt, inside the try: a seed that
            # becomes unreadable later is an error returned below, not a
            # silent death of the task
            seed = Path(settings.nats_seed_file).read_text(encoding="utf-8").strip()
            closed = asyncio.get_running_loop().create_future()
            last_seen = time.monotonic()

            async def ended(closed: "asyncio.Future[None]" = closed) -> None:
                if not closed.done():
                    closed.set_result(None)

            async def err_cb(exc: BaseException) -> None:
                log.warning("console: NATS error %s", exc)

            nc = await nats.connect(
                servers=[settings.nats_url],
                nkeys_seed_str=seed,
                inbox_prefix=INBOX_PREFIX,
                tls=tls_context(settings),
                tls_handshake_first=settings.nats_url.startswith("tls://"),
                allow_reconnect=False,
                connect_timeout=5.0,
                disconnected_cb=ended,
                closed_cb=ended,
                error_cb=err_cb,
            )
            try:
                fleet.synced = False

                async def handle(msg: object) -> None:
                    nonlocal last_seen
                    last_seen = time.monotonic()
                    if fleet.on_delivery(msg.subject, msg.data, msg.reply,  # type: ignore[attr-defined]
                                         settings.presence_stream,
                                         settings.consumer_name):
                        on_change()

                await nc.subscribe(settings.deliver_subject, cb=handle)
                await ensure_consumer(nc, settings, fleet)
                backoff = 1.0
                # the connection ends by its own word (ended), or by the
                # idle watchdog asking for a replay; it wakes every quarter
                # of the probe's bound (at least 5 s), so a silent fleet's
                # reconnect lands within the bound, not a cycle after it
                wake = max(5.0, settings.health_sync_bound / 4)
                idle = max(IDLE_FLOOR_SEC, settings.health_sync_bound * 3 / 4)
                while not closed.done():
                    try:
                        await asyncio.wait_for(asyncio.shield(closed), wake)
                    except asyncio.TimeoutError:
                        age = time.monotonic() - last_seen
                        if age >= idle:
                            log.info("console: the stream was silent for %.0f s;"
                                     " reconnecting for a replay", age)
                            break
                await nc.close()
                await asyncio.sleep(0.05)  # the DELETE on the next connect
            finally:
                if not nc.is_closed:
                    await nc.close()
        except Exception as exc:  # noqa: BLE001 - the loop outlives every error
            log.warning("console: NATS %s; retrying in %.0f s", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2.0, 60.0)
