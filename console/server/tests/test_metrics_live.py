"""The unattended metrics reading against a real nats-server (#168 round 2,
RT-6.43 step 10), in the real shape: a JetStream stream and push consumers
delivering onto the two deliver subjects, so the **server** publishes the
`$JS.ACK` reply (a client cannot — NATS reserves `$JS` subjects; that was the
round's grammar finding). What it proves, on the wire, with no fakes: the
dial in nats-py's own shape (`nkeys_seed_str`, coroutine callbacks), both
consumer names in the ack metadata, and the two subjects feeding the service
view's books. Needs ``MM_TEST_NATS=1`` and a binary in ``MM_NATS_SERVER``
(the same gate as test_nats_live.py)."""
from __future__ import annotations

import asyncio
import os
import secrets
import select
import subprocess
import time

import nkeys
import pytest

from console_server import config
from console_server.metrics_service import MetricsService
from console_server.nats_read import View

from test_metrics_service import _settings_unit  # the unit settings
from test_asks_stats import LINE           # the house STATS line


pytestmark = pytest.mark.skipif(
    os.environ.get("MM_TEST_NATS") != "1" or not (
        os.path.exists(os.environ.get("MM_NATS_SERVER", ""))),
    reason="needs MM_TEST_NATS=1 and a nats-server binary")


def user_seed() -> tuple[bytes, str]:
    seed32 = secrets.token_bytes(32)
    seed = nkeys.encode_seed(seed32, nkeys.PREFIX_BYTE_USER)
    return seed, nkeys.from_seed(seed).public_key.decode()


@pytest.fixture
def live_nats(tmp_path):
    """One nats-server with JetStream, two nkey users: the console's
    (subscribe-only on the two deliver subjects, publish denied outright —
    the example conf's pinned shape, untouched) and the login service's
    stand-in (the stream and consumer rights, the subjects to publish)."""
    seed, pub = user_seed()
    seed_file = tmp_path / "seed"
    seed_file.write_bytes(seed + b"\n")
    os.chmod(seed_file, 0o600)
    pseed, ppub = user_seed()
    pseed_file = tmp_path / "publisher.seed"
    pseed_file.write_bytes(pseed + b"\n")
    conf = tmp_path / "nats.conf"
    conf.write_text(f"""
listen: 127.0.0.1:42235
jetstream {{ store_dir: "{tmp_path}/js" }}
authorization {{
  users: [
    {{ nkey: "{pub}"
       permissions: {{ publish: {{ deny: ">" }}
                      subscribe: [ "deliver.metrics.presence",
                                   "deliver.metrics.stats" ] }} }},
    {{ nkey: "{ppub}"
       permissions: {{ publish: [ "presence.>", "stats.>", "$JS.API.>" ],
                      subscribe: [ "_INBOX.>" ] }} }}
  ]
}}
""")
    proc = subprocess.Popen(
        [os.environ["MM_NATS_SERVER"], "-c", str(conf)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(tmp_path))
    port = None
    t0 = time.monotonic()
    while time.monotonic() - t0 < 10:
        ready, _, _ = select.select([proc.stderr], [], [], 0.2)
        for stream in ready:
            for line in stream.read1(65536).splitlines():
                if b"istening for client connections" in line:
                    port = int(line.decode().rpartition("127.0.0.1:")[2].split()[0])
                    break
        if port is not None:
            break
        if proc.poll() is not None:
            pytest.fail("nats-server died at start")
    if port is None:
        proc.kill()
        pytest.fail("no port from the nats-server")
    yield port, proc, pseed_file
    proc.terminate()
    proc.wait(timeout=5)


async def test_live_deliveries_feed_both_books(live_nats, tmp_path) -> None:
    """A presence claim and a STATS line published into the streams arrive
    through the push consumers onto the wire — the server's own `$JS.ACK`
    replies, the two consumer names — and land in the service view's fleet
    and stats book."""
    from mattermost_ai_agent_bus import stats as mcp_stats
    from mattermost_ai_agent_bus.natsbus import encode_presence, agent_name
    from nats.js.api import ConsumerConfig

    port, proc, publisher_seed = live_nats
    s = config.Settings(**{**_settings_unit(tmp_path).__dict__,
                           "nats_url": f"nats://127.0.0.1:{port}",
                           "metrics_nkey_file": str(tmp_path / "seed")})
    view = View("presence", "stats")
    svc = MetricsService(s, view)
    task = asyncio.create_task(svc._run())

    import nats.aio.client as nac
    pnc = nac.Client()
    await pnc.connect([f"nats://127.0.0.1:{port}"],
                      nkeys_seed_str=publisher_seed.read_text().strip(),
                      connect_timeout=5)
    js = pnc.jetstream()
    await js.add_stream(name="PRESENCE", subjects=["presence.*.*", "presence.ask.*.*"])
    await js.add_stream(name="STATS", subjects=["stats.*"])
    await js.add_consumer("PRESENCE", config=ConsumerConfig(
        durable_name="console-metrics_presence",
        deliver_subject="deliver.metrics.presence",
        filter_subjects=["presence.*.*", "presence.ask.*.*"],
        ack_policy="none", deliver_policy="last_per_subject"))
    await js.add_consumer("STATS", config=ConsumerConfig(
        durable_name="console-metrics_stats",
        deliver_subject="deliver.metrics.stats",
        filter_subjects=["stats.*"],
        ack_policy="none", deliver_policy="all"))
    try:
        await asyncio.wait_for(svc.subscribed.wait(), 10)

        NKEY = "UDXU4RCSJNZOIQHZNWXHXORDPRTGNJAHAHFRGZNEEJCPQTT2M7NFCNF4"
        name = agent_name("dembaca", NKEY)
        # the second claim after the subscription (a last-per-subject
        # consumer delivers each subject's newest on subscribe)
        await js.publish(f"presence.dembaca.{name}", encode_presence("online", "", ""))
        await js.publish(f"stats.{name}", mcp_stats.nats_body(LINE))
        for _ in range(100):
            if view.fleet.cards() and view.stats_lines >= 1:
                break
            await asyncio.sleep(0.05)

        assert svc.last_delivery > 0.0, "no delivery reached the reading"
        assert view.fleet.cards(), "the fleet received nothing"
        assert view.stats_lines >= 1, "the stats book received nothing"
        assert view.fleet.cards()[0]["state"] == "online"
    finally:
        svc.stop()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, TimeoutError):
            pass
        await pnc.drain()
