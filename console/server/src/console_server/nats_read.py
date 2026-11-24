"""The console's read-only NATS side: one static account (the pilot's
deviation from ADR-015 way A), its own ephemeral push consumer on the
PRESENCE stream — the same shape the login service makes for agents —
and nothing published, ever.

Connection rules as the mcp package's transport (``natsbus``): ``tls://``
anywhere, ``nats://`` only to a loopback host, TLS first on the real
connection (a seed in transit mints the account; there is no token
here, but the rule is the tenant's).
"""
from __future__ import annotations

import asyncio
import json
import logging
import ssl
from pathlib import Path

import nats
from nats.aio.client import Client as NatsClient

from . import config
from .fleet import Fleet

log = logging.getLogger("console.nats")

CONSUMER_API_TIMEOUT = 5.0


class NatsError(Exception):
    pass


def tls_context(settings: config.Settings) -> ssl.SSLContext | None:
    """The verifying context for a tls:// URL; None for loopback nats://."""
    if settings.nats_url.startswith("nats://"):
        return None
    return ssl.create_default_context(cafile=settings.nats_ca_file or None)


async def ensure_consumer(nc: NatsClient, settings: config.Settings) -> None:
    """The console's presence consumer, named and made like the login
    service makes an agent's (Go ``consumers/manager.go``, named
    ``<agent>_presence``): a push consumer on the PRESENCE stream, no acks,
    the last message per subject replayed at its making (so a connect
    finds the fleet as it stands), pushed to the console's deliver subject,
    an hour idle threshold. Made at every connect; a still-standing one is
    reused (the queue bridges the gap until it goes idle)."""
    spec = {
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
    # named through the path, as the login service names its consumers: the
    # fleet checks the delivery's metadata against this name
    reply = await nc.request(
        f"$JS.API.CONSUMER.CREATE.{settings.presence_stream}.{settings.consumer_name}",
        json.dumps(spec).encode(),
        timeout=CONSUMER_API_TIMEOUT,
    )
    body = json.loads(reply.data.decode())
    error = body.get("error") if isinstance(body, dict) else {}
    if error is None:
        return  # made
    text = str(error.get("description", "")) if isinstance(error, dict) else ""
    if "already exist" in text:
        return  # one with this name still stands: its queue bridges the gap
    raise NatsError(f"consumer create refused: {body.get('error')!r}")


async def read_forever(settings: config.Settings, fleet: Fleet,
                       on_change: asyncio.Event) -> None:
    """Connect, subscribe the console's deliver subject, make the consumer
    (it replays the fleet to the subscription), and feed every delivery to
    the reader. One failed delivery never ends the loop; the loop never
    publishes anything.
    """
    seed = Path(settings.nats_seed_file).read_text(encoding="utf-8").strip()
    backoff = 1.0
    while True:
        try:
            closed = asyncio.get_running_loop().create_future()

            async def disconnected(closed: "asyncio.Future[None]" = closed) -> None:
                log.warning("console: the NATS connection ended; reconnecting")
                if not closed.done():
                    closed.set_result(None)

            async def err_cb(exc: BaseException) -> None:
                log.warning("console: NATS error %s", exc)

            nc = await nats.connect(
                servers=[settings.nats_url],
                nkeys_seed_str=seed,
                tls=tls_context(settings),
                tls_handshake_first=settings.nats_url.startswith("tls://"),
                allow_reconnect=False,
                connect_timeout=5.0,
                disconnected_cb=disconnected,
                error_cb=err_cb,
                closed_cb=disconnected,
            )
            try:
                fleet.synced = False

                async def handle(msg: object) -> None:
                    if fleet.on_delivery(msg.subject, msg.data, msg.reply,  # type: ignore[attr-defined]
                                         settings.presence_stream,
                                         settings.consumer_name):
                        on_change.set()

                # Subscribe first: the consumer's replay of the fleet flows
                # to exactly this subscription.
                await nc.subscribe(settings.deliver_subject, cb=handle)
                await ensure_consumer(nc, settings)
                backoff = 1.0
                await closed  # disconnected_cb/closed_cb wake this
                await nc.close()
            finally:
                if not nc.is_closed:
                    await nc.close()
        except Exception as exc:  # noqa: BLE001 - the loop outlives every error
            log.warning("console: NATS %s; retrying in %.0f s", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2.0, 60.0)
