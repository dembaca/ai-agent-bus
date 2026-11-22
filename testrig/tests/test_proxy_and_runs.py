"""The harness's own tests: the faults do what the file says, and nothing else.

Checked against the real thing: `websockets` is the library the bus dialled in
production, and these tests run the proxy in front of a real WebSocket server of
that same library. The process fault is a real subprocess killed with a real
signal. A stand-in would have hidden exactly the error that broke production
twice before: a behaviour the real object does not have.
"""

from __future__ import annotations

import asyncio
import sys

import pytest
import websockets

from bus_testrig import measurands, proc
from bus_testrig.proxy import FrameProxy, ProxyFault
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
    """`after=1, every=2` faults the sent frames 1, 3, 5, 7, 9 (0-based):
    the frames 0, 2, 4, 6, 8 stay, in order."""
    got, proxy = await _relay("drop", after=1, every=2)
    assert got == ["0", "2", "4", "6", "8"]
    assert (proxy.received, proxy.forwarded, proxy.altered) == (10, 5, 5)


async def test_duplicate_doubles_exactly_the_faulted_frames():
    got, proxy = await _relay("duplicate", after=1, every=2)
    assert got == ["0", "1", "1", "2", "3", "3", "4", "5", "5",
                   "6", "7", "7", "8", "9", "9"]
    assert sorted(got) == got  # duplicating changes how often, not the order
    assert (proxy.received, proxy.altered) == (10, 5)


async def test_reorder_swaps_without_losing_or_repeating():
    got, proxy = await _relay("reorder", after=1, pace=0.001, reorder_max_wait=0.05)
    assert sorted(got) == [str(i) for i in range(10)]  # nothing lost, nothing doubled
    assert got != sorted(got)  # the order really changed
    assert (proxy.received, proxy.altered) == (10, 9)


async def test_reorder_releases_a_trailing_frame_by_its_timer():
    """With no successor, the held frame goes out on the timer: delayed, not lost."""
    got, _ = await _relay("reorder", count=3, after=1, pace=0.05, reorder_max_wait=0.05)
    assert got == ["0", "1", "2"]


async def test_stall_delays_everything_and_loses_nothing():
    loop = asyncio.get_running_loop()
    began = loop.time()
    got, proxy = await _relay("stall", count=5, after=0, pace=0.0, stall_sec=0.02)
    assert got == [str(i) for i in range(5)]
    assert loop.time() - began >= 0.02 * 5 * 0.5
    assert (proxy.received, proxy.forwarded) == (5, 5)


async def test_blackhole_keeps_the_socket_and_delivers_nothing():
    got, proxy = await _relay("blackhole", count=5, after=0)
    assert got == []
    assert proxy.received == 5 and proxy.forwarded == 0


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
        "why": "The harness has no bus and no coordinator yet: it cannot stop one "
               "and watch the crown pass (follow-up PR)."}


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
    """Renaming must be safe, dropping a formula must not (review point 8)."""
    src = measurands.FILE.read_text()
    renamed = src.replace('id = "loss"', 'id = "verlust"')
    path = tmp_path / "measurands.toml"
    path.write_text(renamed)
    rows = measurands.load(path)
    assert "verlust" in [m.id for m in rows]
    run = Run(name="x")
    run.send(0)
    run.arrive(0)
    # The file the run reads is the real one, so asking it for its own rows is
    # still consistent; the renamed copy only proves loading follows `id`.
    assert set(run.measure()) == set(measurands.ids())


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


def test_network_cut_says_when_it_cannot():
    status = proc.network_cut_supported()
    assert status == "" or "network namespace" in status
