"""The bus as a process, and the run that measures it.

These tests start the real MCP server, because a fake one would hide exactly
what this module exists to check: that the server's handshake, its tool list
and its two tools work against this rig. The process is bounded on every step
and reaped on every path; a test that finds the server hanging says so rather
than waiting longer.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
import tempfile
from pathlib import Path

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
from bus_testrig import bus as bus_module
from bus_testrig.proxy import GatewayProxy, ProxyFault
from bus_testrig.stub import MattermostStub, posted_frame


def _can_import() -> bool:
    try:
        interpreter()
    except RuntimeError:
        return False
    return True


# `BUS_TESTRIG_REQUIRE_BUS=1` (CI) turns the skip into a failure: with the
# switch on, the conftest refuses before this marker can skip, so a host without
# the bus fails with one sentence instead of a green report. A silent skip is
# what let eight bus tests stay green while the workflow ran them before
# `uv sync` (review point 1).
skip_no_bus = pytest.mark.skipif(
    not _can_import(),
    reason="mcp/ is not synced: `uv sync --directory mcp`")
pytestmark = pytest.mark.bus


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
async def test_a_cancelled_stop_still_kills_the_child():
    """Review point at bus.py:114: the wait and the kill are a `try/finally`, so
    a `stop` cancelled while it waits still takes the child down, and the next
    `stop` finds it gone."""
    async with MattermostStub() as stub:
        bus = BusProcess(stub.url, token=SENDER_TOKEN)
        await bus.start()
        proc = bus._proc
        assert proc is not None and proc.returncode is None
        stopper = asyncio.create_task(bus.stop())
        await asyncio.sleep(0)  # let it reach the wait
        stopper.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopper
        await bus.stop()
        assert proc.returncode is not None


@skip_no_bus
async def test_the_state_directory_is_not_left_behind(tmp_path, monkeypatch):
    """Review point at bus.py:176: `mkdtemp` came before the probe, so a probe
    that failed left a directory behind. The probe now runs first, and a failed
    start leaves nothing."""
    async with MattermostStub() as stub:
        bus = BusProcess(stub.url, token=SENDER_TOKEN)
        await bus.start()
        state = bus._state
        assert state and Path(state).is_dir()
        await bus.stop()
        assert not Path(state).exists(), state


@skip_no_bus
async def test_a_spawn_cancelled_after_the_temp_dir_still_removes_it(monkeypatch):
    """Follow-up review point 6: `mkdtemp` ran before the spawn, so a cancel
    during `create_subprocess_exec` left one directory per cancelled start.
    The spawn is interrupted here rather than raced; `start` is supposed to
    clean up on every way out, not only on `OSError`."""
    spawned: list[str] = []
    original = tempfile.mkdtemp

    def recording_mkdtemp(*args, **kwargs):
        path = original(*args, **kwargs)
        spawned.append(path)
        return path

    async def cancelled(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(tempfile, "mkdtemp", recording_mkdtemp)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", cancelled)
    async with MattermostStub() as stub:
        bus = BusProcess(stub.url, token=SENDER_TOKEN)
        with pytest.raises(asyncio.CancelledError):
            await bus.start()
    assert spawned, "the test did not reach the spawn"
    # Every directory it made is gone, and the instance holds none.
    assert [p for p in spawned if Path(p).exists()] == [], spawned
    assert bus._state == ""


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
async def test_stop_kills_the_whole_group_including_a_grandchild(tmp_path):
    """Follow-up review point 5: `stop` kills the process *group*, and the test
    for that only ever looked at the server itself. The server here is replaced
    by a small one that starts a child of its own — the grandchild — so killing
    the pid alone would leave a live process behind. Fast, and no CPU: both
    children sleep.

    The child writes its own pid, and the grandchild's, to a file before it
    sleeps: those pids are read from the file, not discovered by scanning the
    process table."""
    script = tmp_path / "child.py"
    pids = tmp_path / "pids"
    script.write_text(
        "import os, subprocess, sys, time\n"
        f"grand = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        f"open({str(pids)!r}, 'w').write(f'{{os.getpid()}} {{grand.pid}}')\n"
        "time.sleep(120)\n")
    bus = BusProcess("http://127.0.0.1:1", token=SENDER_TOKEN)
    # The probe is what `start` would do first; there is no bus here, so the
    # interpreter is pinned and the module the child runs is this script.
    bus._proc = await asyncio.create_subprocess_exec(
        str(sys.executable), str(script),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True)
    try:
        deadline = asyncio.get_running_loop().time() + 20
        while not pids.exists() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
        assert pids.exists(), "the child never wrote its pids"
        child, grand = (int(part) for part in pids.read_text().split())
        assert _alive(child) and _alive(grand), (child, grand)
        await bus.stop()
        # `stop` is bounded; give the kernel a moment to reap the grandchild.
        deadline = asyncio.get_running_loop().time() + 5
        while _alive(grand) and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
        assert not _alive(child), child
        assert not _alive(grand), f"the grandchild {grand} outlived the group"
    finally:
        if bus._proc is not None and bus._proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(os.getpgid(bus._proc.pid), signal.SIGKILL)


def _alive(pid: int) -> bool:
    """Whether pid is a running process. `kill(pid, 0)` answers for a zombie
    too, which is not: a killed child waits for its parent to reap it, and that
    parent is this test, not the group being checked."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/stat") as stat:  # Linux: a zombie is no process
            return stat.read().split()[2] != "Z"
    except FileNotFoundError:  # not Linux, or gone: the signal above is the test
        return True


@skip_no_bus
async def test_a_run_reports_the_bus_as_its_target():
    """`target: bus`, every measurand in the file, and the numbers the run's
    own formulas produce — not a second bookkeeping type.

    `relay` is filled even for the baseline (review point 4): the proxy is in
    front of the stub in every run, so the two runs' counters compare."""
    run = await bus_run(count=6)
    report = run.report()
    assert report["target"] == "bus" and report["run"] == "bus-run"
    assert report["sent_frames"] == 6
    assert report["connection_closed"] is None, report
    assert report["measured"]["loss"]["value"] == 0
    assert report["measured"]["latency_p50"]["value"] is not None
    assert report["relay"]["received"] > 0, report["relay"]
    assert report["relay"]["altered"] == 0, report["relay"]
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


@skip_no_bus
async def test_a_cut_is_marked_as_not_a_delivery_measurement():
    """`cut` and `halfopen` close the line rather than dropping frames one by
    one, and after a cut the bus reconnects and backfills (review point 3): what
    the run then counts is the catch-up read, not the socket. The report says so
    in `notes` instead of letting the number pass as a delivery loss."""
    run = await bus_run(count=6, fault=ProxyFault(kind="cut", after=3), send_interval=0.05)
    assert any("not a socket-delivery measurement" in note for note in run.notes), run.notes
    half = await bus_run(count=4, fault=ProxyFault(kind="halfopen", after=2))
    assert any("not a socket-delivery measurement" in note for note in half.notes), half.notes


@skip_no_bus
async def test_a_failed_send_is_neither_sent_nor_lost(monkeypatch):
    """Review point 5: a `post_message` that raises, times out or answers
    without an id never left the harness. The echo run pops the send
    (`runner.py:246`); the bus run must too, or a dead server reads as frames
    the channel lost."""
    original = BusProcess.call
    posts = {"n": 0}

    async def flaky(self, tool, **args):
        if tool == "post_message":
            posts["n"] += 1
            if posts["n"] == 3:
                raise McpError("post_message: the server refused")
        return await original(self, tool, **args)

    monkeypatch.setattr(BusProcess, "call", flaky)
    run = await bus_run(count=5)
    assert posts["n"] == 3, posts
    assert run.sent_frames == 2, (run.sent_frames, [s.seq for s in run.sent])
    assert len(run.sent) == run.sent_frames
    # Nothing that never left counts as lost: only the two real sends can be.
    assert run.measure()["loss"]["value"] == 0, run.measure()["loss"]
    assert "post_message failed" in run.closed, run.closed


@skip_no_bus
async def test_a_post_delivered_by_the_catch_up_read_is_counted_as_arrived(monkeypatch):
    """Follow-up review point 3: `seq_of` was filled only after `post_message`
    returned, so a post the receiver was handed while that call was still in
    flight — by the catch-up read after a reconnect — had no seq yet and was
    counted as lost.

    The first send is made to hold its answer while its socket is cut: the
    receiver's wait fails, reconnects and backfills, and the backfill hands over
    the post that was just created. With the id-only match that post arrived
    nowhere, so `loss` was 1 for a run that lost nothing."""
    from bus_testrig import bus as bus_module

    recorded: list[MattermostStub] = []

    class Recording(MattermostStub):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            recorded.append(self)

    original = BusProcess.call
    posted = {"n": 0}

    async def call(self, tool, **args):
        answer = await original(self, tool, **args)
        if tool == "post_message":
            posted["n"] += 1
            if posted["n"] == 2:
                # The post is in the stub and its frame may be on the wire; its
                # id is not in `seq_of` yet, because this call has not returned.
                for conn in list(recorded[0].conns):
                    conn.transport.abort()
                await asyncio.sleep(0.6)  # the receiver reconnects and backfills
        return answer

    monkeypatch.setattr(bus_module, "MattermostStub", Recording)
    monkeypatch.setattr(BusProcess, "call", call)
    run = await bus_run(count=4)
    assert posted["n"] == 4, posted
    assert run.sent_frames == 4, run.sent_frames
    assert run.measure()["loss"]["value"] == 0, (
        run.measure()["loss"], [s.seq for s in run.sent], sorted(run.received))


@skip_no_bus
async def test_the_server_environment_carries_no_proxy(monkeypatch):
    """Follow-up review point 4: the stub and the proxy are loopback, and the
    server's own `httpx` reads `HTTP(S)_PROXY`, so an inherited proxy sent the
    run's traffic to it and failed at `get_me`. The child is handed an
    environment without those variables, in either case."""
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
                 "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    env = bus_module._clean_env("http://127.0.0.1:1", SENDER_TOKEN, "/nonexistent/state")
    left = [name for name in env if name.upper() in
            ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")]
    assert left == [], left
    assert env["MM_CHAT_URL"] == "http://127.0.0.1:1"


@skip_no_bus
async def test_the_baseline_runs_through_the_proxy_too(monkeypatch):
    """Review point 4: the proxy used to be left out when `fault is None`, so
    the baseline and the faulted run took different paths. Every run now enters
    `GatewayProxy`, with `None` as the baseline's fault."""
    entered: list[object] = []

    class Recording(GatewayProxy):
        def __init__(self, upstream, fault=None):
            entered.append(fault)
            super().__init__(upstream, fault)

    monkeypatch.setattr("bus_testrig.bus.GatewayProxy", Recording)
    await bus_run(count=2)
    await bus_run(count=2, fault=ProxyFault(kind="drop", after=1))
    assert len(entered) == 2, entered
    assert entered[0] is None and isinstance(entered[1], ProxyFault), entered
