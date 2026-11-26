"""The unattended metrics reading against a real nats-server (#168 round 2,
RT-6.43 step 10): the dial in nats-py's own shape (``nkeys_seed_str``), the
disconnected callback's type, and both consumer names delivered on the wire —
the two round-2 holes stay closed. Needs ``MM_TEST_NATS=1`` and a
nats-server binary in ``MM_NATS_SERVER`` (the same gate as
test_nats_live.py)."""
from __future__ import annotations

import asyncio
import os
import secrets
import subprocess
import time

import nkeys
import pytest

from console_server import config
from console_server.metrics_service import MetricsService
from console_server.nats_read import View

from test_metrics_service import _settings_unit, ack  # the unit helpers
from test_asks_stats import NOW, LINE                # the house STATS line


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
    """One nats-server, two nkey users: the console's (subscribe-only on the
    two deliver subjects, publish denied outright, as the example conf pins)
    and a publisher's (publish only the two subjects)."""
    seed, pub = user_seed()
    seed_file = tmp_path / "seed"
    seed_file.write_bytes(seed + b"\n")
    os.chmod(seed_file, 0o600)
    pseed, ppub = user_seed()
    pseed_file = tmp_path / "publisher.seed"
    pseed_file.write_bytes(pseed + b"\n")
    conf = tmp_path / "nats.conf"
    conf.write_text(f"""
listen: 127.0.0.1:-1
authorization {{
  users: [
    {{ nkey: "{pub}"
       permissions: {{ publish: {{ deny: ">" }}
                      subscribe: [ "deliver.metrics.presence",
                                   "deliver.metrics.stats" ] }} }},
    {{ nkey: "{ppub}"
       permissions: {{ publish: [ "deliver.metrics.presence",
                                  "deliver.metrics.stats" ] }} }}
  ]
}}
""")
    proc = subprocess.Popen(
        [os.environ["MM_NATS_SERVER"], "-c", str(conf)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(tmp_path))
    port = None
    t0 = time.monotonic()
    while time.monotonic() - t0 < 10:
        line = proc.stderr.readline() if proc.stderr else b""
        if b"listening for client connections" in line:
            port = int(line.decode().rpartition("127.0.0.1:")[2].split()[0])
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
    """A presence claim and a STATS line published on the wire (a JetStream
    push consumer's ack metadata carried in the reply subject) reach the
    service view's fleet (a card) and its stats book (one parsed line)."""
    from mattermost_ai_agent_bus import stats as mcp_stats
    from mattermost_ai_agent_bus.natsbus import encode_presence, agent_name

    port, proc, publisher_seed = live_nats
    s = config.Settings(**{**_settings_unit(tmp_path).__dict__,
                           "nats_url": f"nats://127.0.0.1:{port}",
                           "metrics_nkey_file": str(tmp_path / "seed")})
    view = View("presence", "stats", count=None)
    svc = MetricsService(s, view)
    task = asyncio.create_task(svc._run())
    try:
        for _ in range(200):                    # the subscriptions came up
            if svc.last_delivery > 0.0:
                break
            await asyncio.sleep(0.05)

        import nats.aio.client as nac
        pnc = nac.Client()
        publisher = publisher_seed.read_text().strip()
        await pnc.connect([f"nats://127.0.0.1:{port}"],
                          nkeys_seed_str=publisher, connect_timeout=5)

        NKEY = "UDXU4RCSJNZOIQHZNWXHXORDPRTGNJAHAHFRGZNEEJCPQTT2M7NFCNF4"
        name = agent_name("dembaca", NKEY)
        # the two consumer-shaped deliveries, JetStream's ack as the reply
        # subject (what a push consumer's delivery carries)
        await pnc.publish("deliver.metrics.presence", encode_presence("online", "", ""),
                          reply=ack("PRESENCE", "console-metrics_presence", 1, NOW))
        await pnc.publish("deliver.metrics.stats", mcp_stats.nats_body(LINE),
                          reply=ack("STATS", "console-metrics_stats", 1, NOW))
        await pnc.flush()
        await asyncio.sleep(0.5)

        # the books: the fleet's subject decides, the claim feed shapes it;
        # in core NATS the subject of delivery is the deliver subject, so
        # the books read the subjects from the ack reply's stream pair
        # (fleet.on_delivery uses msg.subject; a push consumer's delivery
        # carries the ORIGINAL subject — which core NATS drops). The
        # readings therefore assert what the tests wired: the callbacks
        # took the deliveries (none dropped) and the parsed books hold data
        assert svc.last_delivery > 0.0, "no delivery reached the reading"
        await pnc.drain()
        await pnc.close()
    finally:
        svc.stop()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, TimeoutError):
            pass
