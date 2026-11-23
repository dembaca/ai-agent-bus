"""The bus as a process, and the run that measures it.

These tests start the real MCP server, because a fake one would hide exactly
what this module exists to check: that the server's handshake, its tool list
and its two tools work against this rig. The process is bounded on every step
and reaped on every path; a test that finds the server hanging says so rather
than waiting longer.
"""

from __future__ import annotations

import pytest

from bus_testrig.bus import (
    RECEIVER_ID,
    RECEIVER_TOKEN,
    SENDER_ID,
    SENDER_TOKEN,
    BusProcess,
    McpError,
    bus_run,
    interpreter,
)
from bus_testrig.proxy import ProxyFault
from bus_testrig.stub import MattermostStub, posted_frame


def _can_import() -> bool:
    try:
        interpreter()
    except RuntimeError:
        return False
    return True


skip_no_bus = pytest.mark.skipif(not _can_import(),
                                 reason="mcp/ is not synced: `uv sync --directory mcp`")


@skip_no_bus
async def test_the_server_speaks_mcp_and_lists_the_bus_tools():
    """The acceptance: the real server starts, `initialize` and `tools/list`
    succeed, and the tool list is the bus's own — not an empty one a broken
    handshake could also produce."""
    async with MattermostStub() as stub:
        async with BusProcess(stub.url, token=SENDER_TOKEN) as bus:
            assert "get_me" in bus.tools
            assert "post_message" in bus.tools
            assert "wait_for_events" in bus.tools


@skip_no_bus
async def test_a_read_only_tool_answers_without_a_traceback():
    """`get_me` is the harmless one: it reads the stub's own `/users/me` and
    needs no registered bot (the server hides `session_start` when a token is
    in its environment). The stderr tail is where a failure would show."""
    async with MattermostStub() as stub:
        stub.identify(SENDER_TOKEN, SENDER_ID, "agent-sender")
        async with BusProcess(stub.url, token=SENDER_TOKEN) as bus:
            me = await bus.call("get_me")
            assert me == {"id": SENDER_ID, "username": "agent-sender"}
            assert bus.errors == [], bus.errors
        assert stub.unknown == []


@skip_no_bus
async def test_the_stub_tells_two_tokens_apart_by_the_bearer():
    """Two servers, one stub: the token each presents decides the bot, as
    Mattermost's bearer does. Without this both would be `agent-probe`, and a
    post would reach its own author and be dropped as its own."""
    async with MattermostStub() as stub:
        stub.identify(SENDER_TOKEN, SENDER_ID, "agent-sender")
        stub.identify(RECEIVER_TOKEN, RECEIVER_ID, "agent-receiver")
        async with BusProcess(stub.url, token=SENDER_TOKEN) as sender:
            async with BusProcess(stub.url, token=RECEIVER_TOKEN) as receiver:
                assert (await sender.call("get_me"))["id"] == SENDER_ID
                assert (await receiver.call("get_me"))["id"] == RECEIVER_ID


@skip_no_bus
async def test_a_tool_error_is_raised_not_returned():
    """The server wraps a failure in `isError`; a run that read that as a
    result would report frames it never sent (review point 8)."""
    async with MattermostStub() as stub:
        async with BusProcess(stub.url, token=SENDER_TOKEN) as bus:
            with pytest.raises(McpError, match="Unknown tool"):
                await bus.call("no_such_tool")


@skip_no_bus
async def test_stop_reaps_the_child():
    """No orphan is left behind: after `stop` the process is gone, and a second
    `stop` is a no-op rather than a signal to an unrelated pid."""
    async with MattermostStub() as stub:
        bus = BusProcess(stub.url, token=SENDER_TOKEN)
        await bus.start()
        proc = bus._proc
        assert proc is not None and proc.returncode is None
        await bus.stop()
        assert proc.returncode is not None
        await bus.stop()  # already reaped: nothing to signal


@skip_no_bus
async def test_the_stub_carries_a_created_post_into_its_frame():
    """The frame a run pushes is the post the bus asked the stub to create, so
    it is the server's own JSON, not one the harness made up. The tool answers
    the slim post (`author`, not `user_id`); the frame carries the full one."""
    async with MattermostStub() as stub:
        stub.identify(SENDER_TOKEN, SENDER_ID, "agent-sender")
        async with BusProcess(stub.url, token=SENDER_TOKEN) as bus:
            await bus.call("get_me")
            created = await bus.call("post_message", message="hello from the bus")
            assert created["message"] == "hello from the bus"
            carried = stub.post_of(created["id"])
            assert carried["user_id"] == SENDER_ID
            frame = posted_frame(carried)
            assert frame["event"] == "posted"
            assert '"hello from the bus"' in frame["data"]["post"]


@skip_no_bus
async def test_a_run_reports_the_bus_as_its_target():
    """`target: bus`, every measurand in the file, and the numbers the run's
    own formulas produce — not a second bookkeeping type."""
    run = await bus_run(count=6)
    report = run.report()
    assert report["target"] == "bus" and report["run"] == "bus-run"
    assert report["sent_frames"] == 6
    assert report["connection_closed"] is None, report
    assert report["measured"]["loss"]["value"] == 0
    assert report["measured"]["latency_p50"]["value"] is not None
    assert report["notes"] == [], report["notes"]


@skip_no_bus
async def test_a_drop_on_the_socket_measures_loss():
    """The acceptance for `--fault drop`: the loss is greater than zero and the
    numbers come from the same formulas. `after=1` lets the socket's handshake
    through; `after=0` eats it, and the run then loses everything — both are
    real runs, and the second one says why in `connection_closed`."""
    run = await bus_run(count=8, fault=ProxyFault(kind="drop", after=1))
    assert run.measure()["loss"]["value"] > 0
    wired = await bus_run(count=8, fault=ProxyFault(kind="drop"))
    assert wired.measure()["loss"]["value"] == wired.sent_frames
    assert wired.closed, "a run that lost every frame must say why"


@skip_no_bus
async def test_a_duplicate_on_the_wire_is_handed_over_once():
    """The bus's inbox dedups by post id, so the tool boundary sees each post
    once although the relay forwarded the frame twice. Both numbers are in the
    report: the wire's in `relay`, the bus's in `measured`."""
    run = await bus_run(count=6, fault=ProxyFault(kind="duplicate", after=1))
    assert run.measure()["duplicates"]["value"] == 0
    assert run.relay["forwarded"] > run.relay["received"]
    assert any("more than once" in note for note in run.notes), run.notes


@skip_no_bus
async def test_a_stall_delays_the_bus_without_losing():
    run = await bus_run(count=4, fault=ProxyFault(kind="stall", after=1, stall_sec=0.1))
    assert run.measure()["loss"]["value"] == 0
    assert run.measure()["latency_p95"]["value"] >= 100
