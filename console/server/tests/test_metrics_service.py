"""The unattended metrics page (#158, RT-6.43 step 10): the scrape token
gates the console's own reading — the service nkey's two consumers feed a
view of its own, no session, no person, no anchor — and the settings pair
the token with the seed (both or neither). Asked through the raw asyncio
httpd as test_http.py; the service reading itself is seam-tested, its
deliveries fed straight into the callbacks it subscribes.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from console_server import config
from console_server.app import Console
from console_server.metrics_service import MetricsService
from console_server.nats_read import View

from test_metrics import CONSOLE_FAMILIES, AGENT_FAMILIES, ack, page  # the house helpers


def settings(tmp_path, *, token="tok-123456", seed=True) -> config.Settings:
    tok = tmp_path / "token"
    tok.write_text(token)
    return config.Settings(public_url="http://127.0.0.1:8747", login_off=True,
                          nats_url="", metrics_token_file=str(tok),
                          metrics_nkey_file=str(tmp_path / "seed"),
                          metrics_listen_port=0)


async def get_console(console: Console, path: str, bearer: str | None = None
                     ) -> tuple[int, dict[str, str], bytes]:
    port = console._http.bound_port
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    head = f"GET {path} HTTP/1.1\r\nHost: x\r\n"
    if bearer is not None:
        head += f"Authorization: Bearer {bearer}\r\n"
    writer.write((head + "\r\n").encode())
    answer = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5.0)
    lines = answer.decode("latin-1").split("\r\n")
    headers = {k.strip().lower(): v.strip() for k, _, v in (l.partition(":")
                                                            for l in lines[1:-1])}
    body = await reader.readexactly(int(headers.get("content-length", "0")))
    writer.close()
    return int(lines[0].split()[1]), headers, body


async def test_the_scrape_token_gates_the_service_page(tmp_path) -> None:
    """The unattended page's listener of its own (#158): the token page
    lives there alone - a missing or wrong Bearer answers 401 with no
    operator fallback (this listener has no sessions), the right token
    serves the service reading's zero state; every other path 404s. The
    main listener's /metrics needs the operator's session as before."""
    base = settings(tmp_path)
    console = Console(config.Settings(**{**base.__dict__, "login_off": False}))
    await console.start()
    try:
        port = console._metrics_http.bound_port
        assert console._http.bound_port != port

        async def bare(bearer: str | None = None, path: str = "/metrics"
                      ) -> tuple[int, bytes]:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            head = f"GET {path} HTTP/1.1\r\nHost: x\r\n"
            if bearer is not None:
                head += f"Authorization: Bearer {bearer}\r\n"
            writer.write((head + "\r\n").encode())
            answer = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5.0)
            lines = answer.decode("latin-1").split("\r\n")
            headers = {k.strip().lower(): v.strip() for k, _, v in
                       (l.partition(":") for l in lines[1:-1])}
            body = await reader.readexactly(int(headers.get("content-length", "0")))
            writer.close()
            return int(lines[0].split()[1]), body

        status, body = await bare()
        assert (status, body) == (401, b"unauthorized")
        status, _b = await bare(bearer="wrong")
        assert status == 401
        status, _b = await bare(path="/anything")
        assert status == 404
        status, body = await bare(bearer="tok-123456")
        assert status == 200
        families, series = page(body.decode())
        assert families == set(CONSOLE_FAMILIES) | set(AGENT_FAMILIES)
        assert not any(k.startswith("choirbus_bus_") for k in series)
        assert series["choirbus_console_bus_connects_total"] == 0.0
        # fed right through the service view with its reading live once,
        # the series appear — review finding 2's fix
        from mattermost_ai_agent_bus.natsbus import encode_presence
        console.metrics.last_delivery = 1.0    # its reading delivered
        console.service.fleet._see(1764061800.0)
        name2 = agent_name("dembaca", "UDXU4RCSJNZOIQHZNWXHXORDPRTGNJAHAHFRGZNEEJCPQTT2M7NFCNF4")
        assert console.service.fleet.on_delivery(
            f"presence.dembaca.{name2}", encode_presence("online", "", ""),
            ack("PRESENCE", "console-metrics_presence", 2, 1764061800.0),
            "PRESENCE", "console-metrics_presence")
        status, body = await bare(bearer="tok-123456")
        _families, series = page(body.decode())
        assert series['choirbus_bus_agents{state="online",harness="other"}'] == 1.0
        # the main listener: the operator's page, a Bearer goes nowhere
        status, _h, _b = await get_console(console, "/metrics", bearer="tok-123456")
        assert (status, _b) == (401, b'{"error": "operator login required"}')
    finally:
        await console.stop()


async def test_without_a_token_file_the_service_path_stays_closed(tmp_path) -> None:
    """An unset scrape token (the default): the token path matches nothing
    and never serves; the operator's page stands as before."""
    console = Console(config.Settings(public_url="http://127.0.0.1:8747",
                                      login_off=True, nats_url=""))
    await console.start()
    try:
        assert console._scrape_token == ""
        assert console.metrics is None      # the reading is off with it
        status, _h, body = await get_console(console, "/metrics", bearer="tok-123456")
        assert status == 200
        _families, series = page(body.decode())
        assert not any(k.startswith("choirbus_bus_") for k in series)
    finally:
        await console.stop()


def test_the_settings_pair_the_token_with_the_seed(tmp_path, monkeypatch) -> None:
    """Both or neither: a half-configured page reads nothing and gates
    nothing, so the start refuses it and names both settings."""
    tok = tmp_path / "token"
    tok.write_text("t")
    monkeypatch.setenv("CONSOLE_PUBLIC_URL", "http://127.0.0.1:8747")
    monkeypatch.setenv("CONSOLE_LOGIN_OFF", "1")
    monkeypatch.setenv("CONSOLE_METRICS_TOKEN_FILE", str(tok))
    monkeypatch.delenv("CONSOLE_METRICS_NKEY_SEED_FILE", raising=False)
    with pytest.raises(config.ConfigError) as err:
        config.find()
    assert "CONSOLE_METRICS_NKEY_SEED_FILE" in str(err.value)


class FakeConn:
    """The seam of MetricsService._serve: it collects the callbacks the
    reading subscribes, so a test can drive deliveries into the view."""

    def __init__(self) -> None:
        self.cbs: dict[str, object] = {}

    async def subscribe(self, subject: str, cb: object = None) -> None:
        self.cbs[subject] = cb

    async def flush(self) -> None:
        return None

    async def drain(self) -> None:
        return None


async def test_the_deliver_subjects_feed_the_service_view() -> None:
    """The wiring: a presence claim reaches the fleet, a STATS line the
    book, and each delivery stamps the reading's liveness."""
    from mattermost_ai_agent_bus import stats
    from mattermost_ai_agent_bus.natsbus import agent_name, encode_presence

    view = View("presence", "stats")
    svc = MetricsService(_settings_unit(), view, connect=None)
    conn = FakeConn()
    task = asyncio.create_task(svc._serve(conn, asyncio.Event()))
    await asyncio.sleep(0)      # let the subscriptions happen
    assert set(conn.cbs) == {"deliver.metrics.presence", "deliver.metrics.stats"}

    NKEY = "UDXU4RCSJNZOIQHZNWXHXORDPRTGNJAHAHFRGZNEEJCPQTT2M7NFCNF4"
    name = agent_name("dembaca", NKEY)
    now = 1764061800.0
    msg = SimpleNamespace(subject=f"presence.dembaca.{name}",
                          data=encode_presence("online", "", ""), reply=ack(
                              "PRESENCE", "console-metrics_presence", 1, now))
    view.fleet._see(now)   # the fleet's clock floor: the claims need it
    await conn.cbs["deliver.metrics.presence"](msg)          # type: ignore[operator]
    assert view.fleet.cards()[0]["state"] == "online"

    line = {"v": "0.101.0", "sid": "aaaaaaaaaaaa", "part": 1, "rt": "opencode",
            "kind": "session", "min": 1, "tok": {"req": 1, "all": {"in": 10, "out": 1}}}
    body = stats.nats_body(line)
    assert body is not None
    smsg = SimpleNamespace(subject=f"stats.{name}", data=body, reply=ack(
        "STATS", "console-metrics_stats", 1, now))
    await conn.cbs["deliver.metrics.stats"](smsg)            # type: ignore[operator]
    assert view.stats_lines == 1
    assert svc.last_delivery > 0.0

    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, TimeoutError):
        pass


async def test_a_refused_connect_ends_the_reading(tmp_path, caplog) -> None:
    """A refusal (the account gone, the nkey revoked) is terminal: the
    reading ends with its warning, the loop does not retry it all day."""

    async def refused(seed: str, lost: object = None) -> object:
        raise RuntimeError("nats: authorization violation")

    seed = tmp_path / "seed"                 # a readable seed: it dials
    seed.write_text("SUAKEYINTESTSONLYNOTAREALONE00")
    svc = MetricsService(_settings_unit(tmp_path, seed=str(seed)),
                         View("presence", "stats"), connect=refused)
    svc.start()
    await asyncio.wait_for(svc._task, 5.0)   # type: ignore[arg-type]
    assert "was refused" in caplog.text


async def test_a_bad_seed_file_never_connects(tmp_path) -> None:
    """A seed that is not one key: the reading never dials, and the view
    stays what it was (n/a on the page, never a 0)."""
    seed = tmp_path / "seed"
    seed.write_text("not a key")
    s = _settings_unit(tmp_path, seed=str(seed))

    async def never(seed: str) -> object:
        raise AssertionError("must not dial")

    svc = MetricsService(s, View("presence", "stats"), connect=never)
    svc.start()
    await asyncio.wait_for(svc._task, 5.0)   # type: ignore[arg-type]


def _settings_unit(tmp_path=None, seed: str | None = None) -> config.Settings:
    """Unit settings with the service on; the seed file may not exist (the
    reading's own checks decide)."""
    return config.Settings(public_url="http://127.0.0.1:8747", login_off=True,
                          nats_url="nats://127.0.0.1:4222",
                          metrics_nkey_file=seed or "/nonexistent/seed",
                          metrics_token_file="/nonexistent/token")
