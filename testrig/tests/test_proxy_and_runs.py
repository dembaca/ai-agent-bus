"""The harness's own tests: the faults do what the file says, and nothing else.

What is checked against the real thing: `websockets` is the library the bus
dialled in production, and these tests run the proxy in front of a real
WebSocket server of that same library, not in front of a stand-in. A fake
socket would have hidden exactly the kind of error that broke production
twice before: an attribute or a behaviour the real object does not have.
"""

from __future__ import annotations

import asyncio

import pytest
import websockets

from bus_testrig import measurands
from bus_testrig.proxy import FrameProxy, ProxyFault
from bus_testrig.runner import Run, dry_run


async def _relay(kind: str | None, count: int = 10, **kw) -> tuple[list[str], FrameProxy]:
    """Send `count` numbers through a real echo server and the proxy, return
    what the client received, in order."""

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
                    await asyncio.sleep(0.01)
                got: list[str] = []
                try:
                    for _ in range(count * 3 + 5):
                        got.append(await asyncio.wait_for(client.recv(), timeout=0.3))
                except asyncio.TimeoutError:
                    pass
    finally:
        server.close()
    return got, proxy


async def test_no_fault_delivers_everything_in_order():
    got, _ = await _relay(None)
    assert got == [str(i) for i in range(10)]


async def test_drop_loses_frames_and_keeps_the_rest_in_order():
    got, _ = await _relay("drop", after=1)
    assert len(got) < 10 and "0" in got
    assert got == sorted(got)  # dropping delays nothing: the order holds


async def test_duplicate_repeats_without_losing_or_reordering():
    got, _ = await _relay("duplicate", after=1)
    assert len(got) > 10
    assert set(got) == {str(i) for i in range(10)}  # every frame, exactly once or more
    assert got == sorted(got)


async def test_reorder_changes_the_order_without_losing_or_repeating():
    got, _ = await _relay("reorder", after=1, reorder_max_wait=0.05)
    assert len(got) == 10 and len(set(got)) == 10  # nothing lost, nothing doubled
    assert got != sorted(got)  # and the order really changed


async def test_stall_delays_and_loses_nothing():
    loop = asyncio.get_running_loop()
    began = loop.time()
    got, _ = await _relay("stall", count=5, after=1, stall_sec=0.05)
    assert got == [str(i) for i in range(5)]
    assert loop.time() - began >= 0.05  # it really waited


async def test_every_n_lets_the_rest_through():
    """`every=2` faults every second frame: the run can measure a mixed share."""
    got, _ = await _relay("drop", count=10, after=0, every=2)
    assert 0 < len(got) < 10


def test_an_unknown_fault_is_refused():
    with pytest.raises(ValueError, match="unknown fault"):
        ProxyFault(kind="explode")


async def test_dry_run_needs_no_lab_and_reports_every_measurand():
    """Condition 4 of the approval: runnable with no Lab, no test channel and
    no bots. It reports every measurand in the file, measured or named."""
    run = await dry_run(count=10)
    measured = run.measure()
    assert set(measured) == set(measurands.names())
    assert measured["loss"]["value"] == 0 and measured["duplicates"]["value"] == 0
    assert measured["latency_p50_ms"]["value"] is not None
    # Not measurable on today's bus, and said so rather than guessed:
    assert measured["device_approvals"]["status"] == measurands.NOT_YET
    assert measured["device_approvals"]["value"] is None
    assert measured["device_approvals"]["why"]


async def test_dry_run_with_a_fault_measures_it():
    run = await dry_run(count=10, fault=ProxyFault(kind="drop", after=1))
    measured = run.measure()
    assert measured["loss"]["value"] > 0


def test_a_run_cannot_report_a_name_the_file_does_not_declare():
    """The names come from the file alone: a run with a used measurand the file
    lacks would be a bug, and the report is built from the file, so an
    undeclared name cannot leak in."""
    run = Run(name="x")
    run.send(0)
    run.arrive(0)
    measured = run.measure()
    assert set(measured) == set(measurands.names())


def test_the_file_is_well_formed():
    rows = measurands.load()
    assert rows, "the file declares no measurand"
    measured = [m for m in rows if m.measurable]
    assert measured, "nothing is measurable on today's bus"
    for m in rows:
        assert m.source, f"{m.name} does not cite its acceptance row"
