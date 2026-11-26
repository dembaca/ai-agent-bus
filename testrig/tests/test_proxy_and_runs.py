"""The harness's own tests: the faults do what the file says, and nothing else.

Checked against the real thing: `websockets` is the library the bus dialled in
production, and these tests run the proxy in front of a real WebSocket server of
that same library. The process fault is a real subprocess killed with a real
signal. A stand-in would have hidden exactly the error that broke production
twice before: a behaviour the real object does not have.
"""

from __future__ import annotations

import asyncio
import json
import sys

import pytest
import websockets

from bus_testrig import measurands, proc
from bus_testrig.proxy import ConnState, FaultRelay, FrameProxy, ProxyFault
from bus_testrig.runner import Run, _pct, echo_run


async def _relay(kind: str | None, count: int = 10, pace: float = 0.01, **kw):
    """Send `count` numbers through a real echo server and the proxy; return
    what the client received (in order) and the proxy, for its counters."""

    async def echo(conn):
        async for frame in conn:
            await conn.send(frame)

    server = await websockets.serve(echo, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    fault = None if kind is None else ProxyFault(kind=kind, **kw)
    proxy = FrameProxy(f"ws://127.0.0.1:{port}", fault)
    try:
        async with proxy:
            async with websockets.connect(proxy.url) as client:
                for i in range(count):
                    await client.send(str(i))
                    await asyncio.sleep(pace)
                got: list[str] = []
                limit = 5.0 if kind == "stall" else 0.4
                try:
                    while True:
                        got.append(await asyncio.wait_for(client.recv(), timeout=limit))
                except asyncio.TimeoutError:
                    pass
    finally:
        server.close()
    return got, proxy


async def test_no_fault_delivers_everything_in_order():
    got, proxy = await _relay(None)
    assert got == [str(i) for i in range(10)]
    assert (proxy.received, proxy.forwarded, proxy.altered) == (10, 10, 0)


async def test_drop_loses_exactly_the_faulted_frames():
    """`after=1` lets frame 0 pass without counting it; `every=2` then faults
    the sent frames 2, 4, 6 and 8, so 0, 1, 3, 5, 7, 9 arrive, in order."""
    got, proxy = await _relay("drop", after=1, every=2)
    assert got == ["0", "1", "3", "5", "7", "9"]
    assert (proxy.received, proxy.forwarded, proxy.altered) == (10, 6, 4)


async def test_duplicate_doubles_exactly_the_faulted_frames():
    got, proxy = await _relay("duplicate", after=1, every=2)
    assert got == ["0", "1", "2", "2", "3", "4", "4", "5", "6", "6", "7", "8", "8", "9"]
    assert len(got) == 14  # 10 frames, 4 of them doubled
    assert sorted(got) == got  # duplicating changes how often, not the order
    assert (proxy.received, proxy.altered) == (10, 4)


async def test_reorder_swaps_without_losing_or_repeating():
    got, proxy = await _relay("reorder", after=1, pace=0.001, reorder_max_wait=0.05)
    assert sorted(got) == [str(i) for i in range(10)]  # nothing lost, nothing doubled
    assert got != sorted(got)  # the order really changed
    assert (proxy.received, proxy.altered) == (10, 9)


async def test_reorder_releases_a_trailing_frame_by_its_timer():
    """With no successor, the held frame goes out on the timer: delayed, not lost.

    Frame 0 passes (`after=1`) and frame 1, the last one, is held. No frame
    follows it, so nothing but its timer can release it while the server is
    still connected (#84: with a third frame paced as long as the timer, which
    of the two released the held frame depended on scheduling)."""
    got, proxy = await _relay("reorder", count=2, after=1, pace=0, reorder_max_wait=0.05)
    assert got == ["0", "1"]
    # held once (altered), sent once: the timer released it, nothing doubled
    assert (proxy.received, proxy.forwarded, proxy.altered) == (2, 2, 1)


async def test_stall_delays_everything_and_loses_nothing():
    loop = asyncio.get_running_loop()
    began = loop.time()
    got, proxy = await _relay("stall", count=5, after=0, pace=0.0, stall_sec=0.02)
    assert got == [str(i) for i in range(5)]
    assert loop.time() - began >= 0.02 * 5 * 0.5
    assert (proxy.received, proxy.forwarded) == (5, 5)


async def test_stall_counts_a_forwarded_posted_frame():
    """A stalled frame is delivered after the delay, not discarded: the posted
    counter has to say so, or a real stall run reads "seen 3, forwarded 0,
    altered 3", which is a discard (Fable's review of #78)."""
    sent: list[object] = []

    async def send(frame: object) -> None:
        sent.append(frame)

    relay = FaultRelay(ProxyFault(kind="stall", stall_sec=0.001))
    frame = json.dumps({"event": "posted", "data": {"post": "x"}})
    await relay.deliver(frame, ConnState(), send=send)
    assert sent == [frame]
    assert (relay.forwarded, relay.altered) == (1, 1)
    assert (relay.posted_seen, relay.posted_forwarded, relay.posted_altered) == (1, 1, 1)


async def test_reorder_timer_releases_the_held_frame_and_rearms():
    """`FaultRelay.deliver` alone, without sockets (#84): a held frame goes out
    on its timer, and a frame after that release is held again and released by
    a timer of its own. Holding a frame never yields, so the timer cannot fire
    before the asserts right after `deliver`."""
    sent: list[object] = []

    async def send(frame: object) -> None:
        sent.append(frame)

    relay = FaultRelay(ProxyFault(kind="reorder", after=1, reorder_max_wait=0.05))
    state = ConnState()
    await relay.deliver("0", state, send=send)
    await relay.deliver("1", state, send=send)
    assert sent == ["0"]
    assert state.held == "1"
    await asyncio.sleep(0.2)
    assert sent == ["0", "1"]
    assert state.held is None and state.release is None
    assert relay.forwarded == 2

    await relay.deliver("2", state, send=send)  # held again: the timer re-arms
    assert sent == ["0", "1"]
    assert state.held == "2" and state.release is not None
    await asyncio.sleep(0.2)
    assert sent == ["0", "1", "2"]
    assert state.held is None
    assert (relay.forwarded, relay.altered) == (3, 2)


async def test_blackhole_keeps_the_socket_and_delivers_nothing():
    got, proxy = await _relay("blackhole", count=5, after=0)
    assert got == []
    assert proxy.received == 5 and proxy.forwarded == 0


async def test_blackhole_counts_each_frame_once():
    """Review point: `_applies` ran twice for blackhole, so `after=3` let only
    frame 0 through. With one call it lets exactly 0, 1 and 2 through."""
    got, proxy = await _relay("blackhole", count=6, after=3)
    assert got == ["0", "1", "2"]
    assert (proxy.received, proxy.forwarded, proxy.altered) == (6, 3, 3)


async def test_a_cut_aborts_without_a_close_frame():
    """Review point 3/5: a network cut is not a clean close. Only
    `ConnectionClosedError` may arrive, and it must carry no close frame on
    either side."""
    import websockets.exceptions as wx

    async def echo(conn):
        async for frame in conn:
            await conn.send(frame)

    server = await websockets.serve(echo, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        async with FrameProxy(f"ws://127.0.0.1:{port}", ProxyFault(kind="cut", after=1)) as proxy:
            async with websockets.connect(proxy.url) as client:
                await client.send("0")
                assert await asyncio.wait_for(client.recv(), 1) == "0"
                await client.send("1")
                with pytest.raises(wx.ConnectionClosedError) as caught:
                    for _ in range(5):
                        await asyncio.wait_for(client.recv(), 1)
                assert caught.value.rcvd is None and caught.value.sent is None
    finally:
        server.close()


async def test_echo_run_survives_a_cut_and_counts_what_it_lost():
    """Review point 1 and third re-check point 2: `--fault cut` used to crash,
    and then reported 0 losses. It survives, names the closed connection **and**
    counts the frames that never arrived — that number is what the acceptance
    compares (10 sent, 3 arrived, 7 lost)."""
    run = await echo_run(count=10, fault=ProxyFault(kind="cut", after=3))
    report = run.report()
    assert report["connection_closed"], report
    assert report["sent_frames"] == 10, report
    assert report["measured"]["loss"]["value"] == 7, report["measured"]["loss"]


async def test_a_failed_write_is_not_counted_as_lost():
    """Fourth re-check: a frame whose write failed never left the harness, so
    it is neither sent nor lost. With a pace and a cut the loss is exactly
    `sent_frames - arrived`, and 1 for this run."""
    run = await echo_run(count=10, fault=ProxyFault(kind="cut", after=3), send_interval=0.05)
    m = run.measure()
    assert m["loss"]["value"] == run.sent_frames - len(run.received), (m["loss"], run.sent_frames,
                                                                      len(run.received))
    assert m["loss"]["value"] == 1, (m["loss"], run.sent_frames, len(run.received))


async def test_a_slow_sender_invents_no_loss():
    """Review point 2: with `--send-interval 0.3` the settle window used to run
    during sending and report 4 of 5 frames as lost."""
    run = await echo_run(count=5, send_interval=0.3)
    assert run.measure()["loss"]["value"] == 0
    assert run.sent_frames == 5


async def test_a_stall_delays_application_frames_only():
    """Review point 11, answered honestly: `stall` delays application frames.
    Control frames are handled inside each `websockets` connection, so a
    heartbeat does **not** see a stall, and this harness does not claim it
    does. "The peer notices the connection is gone" is `cut`, tested separately.
    """
    async def echo(conn):
        async for frame in conn:
            await conn.send(frame)

    server = await websockets.serve(echo, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        async with FrameProxy(f"ws://127.0.0.1:{port}",
                              ProxyFault(kind="stall", after=1, stall_sec=0.3)) as proxy:
            async with websockets.connect(proxy.url, ping_interval=0.1,
                                          ping_timeout=0.2) as client:
                await client.send("0")
                assert await asyncio.wait_for(client.recv(), 1) == "0"
                began = asyncio.get_running_loop().time()
                await client.send("1")
                # Still connected across the stall: the heartbeat kept working.
                assert await asyncio.wait_for(client.recv(), 2) == "1"
                assert asyncio.get_running_loop().time() - began >= 0.3
    finally:
        server.close()


async def test_the_close_code_travels_from_the_server_to_the_client():
    """Review point 10: the server closes with 4001, the client must see 4001,
    not the proxy's own 1011."""

    async def refusing(conn):
        await conn.close(4001, "wrong key")

    server = await websockets.serve(refusing, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        async with FrameProxy(f"ws://127.0.0.1:{port}") as proxy:
            async with websockets.connect(proxy.url) as client:
                with pytest.raises(websockets.exceptions.ConnectionClosed) as caught:
                    await asyncio.wait_for(client.recv(), 2)
                assert caught.value.rcvd.code == 4001
                assert caught.value.rcvd.reason == "wrong key"
    finally:
        server.close()


async def test_only_faults_frames_the_predicate_accepts():
    """Review point 14: `only` decides which frames fault, and `every` counts
    those, not all frames."""
    async def echo(conn):
        async for frame in conn:
            await conn.send(frame)

    def is_posted(frame):
        return isinstance(frame, str) and frame.startswith("posted")

    server = await websockets.serve(echo, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        async with FrameProxy(f"ws://127.0.0.1:{port}",
                              ProxyFault(kind="drop", after=0, every=1, only=is_posted)) as proxy:
            async with websockets.connect(proxy.url) as client:
                for frame in ("typing", "posted-1", "hello", "posted-2"):
                    await client.send(frame)
                got = []
                try:
                    for _ in range(6):
                        got.append(await asyncio.wait_for(client.recv(), 0.3))
                except asyncio.TimeoutError:
                    pass
                assert got == ["typing", "hello"]  # only the two `posted` frames dropped
    finally:
        server.close()


async def test_two_clients_do_not_share_reorder_state():
    """The held frame belongs to its connection (review point 9)."""

    async def echo(conn):
        async for frame in conn:
            await conn.send(frame)

    server = await websockets.serve(echo, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        async with FrameProxy(f"ws://127.0.0.1:{port}",
                              ProxyFault(kind="reorder", after=0, reorder_max_wait=5.0)) as proxy:
            async with websockets.connect(proxy.url) as a, websockets.connect(proxy.url) as b:
                await a.send("a0")
                await b.send("b0")
                await a.send("a1")
                await b.send("b1")
                await asyncio.sleep(0.1)
                assert await asyncio.wait_for(a.recv(), 1) == "a1"
                assert await asyncio.wait_for(a.recv(), 1) == "a0"
                assert await asyncio.wait_for(b.recv(), 1) == "b1"
                assert await asyncio.wait_for(b.recv(), 1) == "b0"
    finally:
        server.close()


async def test_a_client_leaving_closes_the_proxy_connection():
    """A connection is over when either end says so (review point 10)."""

    async def echo(conn):
        async for frame in conn:
            await conn.send(frame)

    server = await websockets.serve(echo, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        async with FrameProxy(f"ws://127.0.0.1:{port}") as proxy:
            ws = await websockets.connect(proxy.url)
            await ws.send("x")
            assert await asyncio.wait_for(ws.recv(), 1) == "x"
            await ws.close()
            await asyncio.sleep(0.1)  # the handler must end, not hang
    finally:
        server.close()


async def test_a_killed_server_reaches_the_client_as_an_abort(tmp_path):
    """Third re-check, point 3: the proxy swallowed ConnectionClosed, so a
    killed server looked like a clean close with 1000. The client must see
    ConnectionClosedError with no close frame instead."""
    import subprocess
    import sys

    script = tmp_path / "server.py"
    script.write_text(
        "import asyncio, websockets\n"
        "async def echo(c):\n"
        "    async for f in c: await c.send(f)\n"
        "async def main():\n"
        "    async with websockets.serve(echo, '127.0.0.1', 0) as srv:\n"
        "        port = srv.sockets[0].getsockname()[1]\n"
        "        print(port, flush=True)\n"
        "        await asyncio.Future()\n"
        "asyncio.run(main())\n")
    srv = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, text=True)
    try:
        port = int(await asyncio.to_thread(srv.stdout.readline))
        async with FrameProxy(f"ws://127.0.0.1:{port}") as proxy:
            async with websockets.connect(proxy.url) as client:
                await client.send("x")
                assert await asyncio.wait_for(client.recv(), 2) == "x"
                srv.kill()
                srv.wait(timeout=5)
                with pytest.raises(websockets.exceptions.ConnectionClosedError) as caught:
                    while True:
                        await asyncio.wait_for(client.recv(), 5)
                assert caught.value.rcvd is None
    finally:
        if srv.poll() is None:
            srv.kill()


def test_the_percentile_is_nearest_rank():
    """n = 1 gives its only value; n = 100 at p95 gives index 94 (review point 6)."""
    assert _pct([7.0], 95) == 7.0
    twenty = [float(i) for i in range(1, 21)]
    assert _pct(twenty, 95) == 19.0
    hundred = [float(i) for i in range(1, 101)]
    assert _pct(hundred, 95) == 95.0
    assert _pct(hundred, 50) == 50.0


def test_an_unknown_fault_is_refused():
    with pytest.raises(ValueError, match="unknown fault"):
        ProxyFault(kind="explode")


async def test_echo_run_needs_no_lab_and_reports_every_measurand():
    run = await echo_run(count=10)
    report = run.report()
    assert set(report["measured"]) == set(measurands.ids())
    assert report["target"] == "echo" and report["duration_s"] is not None
    assert report["measured"]["loss"]["value"] == 0
    assert report["measured"]["duplicates"]["value"] == 0
    assert report["measured"]["latency_p50"]["value"] is not None
    # Not measurable here, and said so rather than guessed as zero:
    assert report["measured"]["takeover_p95"] == {
        "name": "Coordinator take-over, 95th percentile", "value": None, "unit": "s",
        "status": measurands.NOT_YET,
        "why": "The harness drives one bus and its own posts; a coordinator take-over "
               "needs two roles and a hard stop of one, which no run here does."}


async def test_a_stall_is_not_a_loss():
    """Review point 5: a stall longer than a short read timeout used to read as
    a loss. Every frame arrives, only later."""
    run = await echo_run(count=5, fault=ProxyFault(kind="stall", after=0, stall_sec=0.2))
    assert run.measure()["loss"]["value"] == 0
    assert run.measure()["latency_p95"]["value"] >= 200


async def test_echo_run_with_a_fault_measures_it():
    run = await echo_run(count=10, fault=ProxyFault(kind="drop", after=1))
    assert run.measure()["loss"]["value"] > 0


def test_a_run_cannot_report_a_name_the_file_does_not_declare():
    run = Run(name="x")
    run.send(0)
    run.arrive(0)
    assert set(run.measure()) == set(measurands.ids())


def test_a_measured_id_without_a_formula_is_an_error(tmp_path):
    """Renaming must be safe; declaring a `measured` id the harness cannot
    compute must raise, not report 0 (review point 8)."""
    src = measurands.FILE.read_text()
    path = tmp_path / "measurands.toml"
    path.write_text(src.replace('id = "loss"', 'id = "verlust"'))
    assert "verlust" in [m.id for m in measurands.load(path)]
    run = Run(name="x")
    run.send(0)
    run.arrive(0)
    assert set(run.measure()) == set(measurands.ids())
    with pytest.raises(RuntimeError, match="no formula"):
        run.measure(path)


def test_the_file_is_well_formed():
    rows = measurands.load()
    assert rows
    assert [m for m in rows if m.measurable]
    for m in rows:
        assert m.source, f"{m.id} does not cite its acceptance row"
        assert m.id.replace("_", "").isalnum(), m.id


def test_kill_really_ends_a_process():
    """Review point 3: a real subprocess, a real signal, and the victim gone."""
    import subprocess

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        result = proc.kill_child(child)
        assert result.returncode == -9
        assert child.poll() == -9  # really dead, not merely signalled
    finally:
        if child.poll() is None:
            child.kill()


def test_no_namespace_cut_is_claimed():
    """The harness must not advertise a capability it does not have (review
    point 3): it offers the proxy's cut and says the namespace one is absent."""
    status = proc.network_cut_supported()
    assert "--fault cut" in status
    assert "not implemented" in status
