"""The NATS transport of the rig (ADR-014 phase 1): the byte proxy, the summary, the
coordinator run's crown-log reading, the login service's copied parts, and a gated
live run against a real nats-server.

The live tests need a nats-server (`CB_NATS_SERVER`, `MM_NATS_SERVER` or `PATH`) and
the bus synced (`uv sync --directory mcp`); without either they skip, and with
`CB_TESTRIG_REQUIRE_BUS=1` a missing bus fails as in the other bus tests.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from bus_testrig import coord, natsrig, proc, runner
from bus_testrig.bus import REPO, interpreter
from bus_testrig.proxy import NATS_FAULTS, ByteProxy


async def _echo_server() -> tuple[asyncio.base_events.Server, int]:
    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def _roundtrip(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                     data: bytes, timeout: float = 2.0) -> bytes:
    writer.write(data)
    await writer.drain()
    return await asyncio.wait_for(reader.readexactly(len(data)), timeout)


# --- the byte proxy -------------------------------------------------------------------


async def test_byte_proxy_passes_bytes_and_counts_them():
    server, port = await _echo_server()
    async with ByteProxy("127.0.0.1", port) as relay:
        reader, writer = await asyncio.open_connection("127.0.0.1", relay.port)
        assert await _roundtrip(reader, writer, b"PING\r\n") == b"PING\r\n"
        writer.close()
        assert relay.bytes_up == 6 and relay.bytes_down == 6 and relay.connections == 1
        assert relay.url == f"nats://127.0.0.1:{relay.port}"
    server.close()


async def test_byte_proxy_cut_aborts_open_and_new_connections():
    server, port = await _echo_server()
    async with ByteProxy("127.0.0.1", port) as relay:
        reader, writer = await asyncio.open_connection("127.0.0.1", relay.port)
        await _roundtrip(reader, writer, b"a")
        window = asyncio.create_task(relay.inject("cut", 0.3))
        await asyncio.sleep(0.05)
        # The open connection is gone: EOF or a reset, never an answer.
        with pytest.raises((asyncio.IncompleteReadError, ConnectionError)):
            await _roundtrip(reader, writer, b"b", timeout=1.0)
        # A connection made inside the window is aborted as it arrives.
        reader2, writer2 = await asyncio.open_connection("127.0.0.1", relay.port)
        with pytest.raises((asyncio.IncompleteReadError, ConnectionError)):
            await _roundtrip(reader2, writer2, b"c", timeout=1.0)
        await window
        # After the window the line carries again.
        reader3, writer3 = await asyncio.open_connection("127.0.0.1", relay.port)
        assert await _roundtrip(reader3, writer3, b"d") == b"d"
        assert relay.aborted >= 2
        writer3.close()
    server.close()


async def test_byte_proxy_blackhole_discards_then_aborts():
    server, port = await _echo_server()
    async with ByteProxy("127.0.0.1", port) as relay:
        reader, writer = await asyncio.open_connection("127.0.0.1", relay.port)
        window = asyncio.create_task(relay.inject("blackhole", 0.4))
        await asyncio.sleep(0.05)
        writer.write(b"lost")
        await writer.drain()
        # Silence, and the line stays open while the window lasts.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(reader.read(10), 0.2)
        await window
        assert relay.discarded == 4
        # A stream that lost bytes cannot go on: the window's end aborts it.
        assert await asyncio.wait_for(reader.read(10), 1.0) == b""
    server.close()


async def test_byte_proxy_halfopen_delays_and_loses_nothing():
    server, port = await _echo_server()
    async with ByteProxy("127.0.0.1", port) as relay:
        reader, writer = await asyncio.open_connection("127.0.0.1", relay.port)
        await _roundtrip(reader, writer, b"x")
        window = asyncio.create_task(relay.inject("halfopen", 0.4))
        await asyncio.sleep(0.05)
        began = time.monotonic()
        writer.write(b"frozen")
        await writer.drain()
        assert await asyncio.wait_for(reader.readexactly(6), 2.0) == b"frozen"
        assert time.monotonic() - began >= 0.3  # held until the window ended
        await window
        assert relay.discarded == 0 and relay.aborted == 0
    server.close()


async def test_byte_proxy_stall_delays_each_chunk_in_the_window():
    server, port = await _echo_server()
    async with ByteProxy("127.0.0.1", port, stall_sec=0.2) as relay:
        reader, writer = await asyncio.open_connection("127.0.0.1", relay.port)
        window = asyncio.create_task(relay.inject("stall", 0.5))
        await asyncio.sleep(0.01)
        began = time.monotonic()
        assert await _roundtrip(reader, writer, b"s") == b"s"
        assert time.monotonic() - began >= 0.2
        await window
        began = time.monotonic()
        await _roundtrip(reader, writer, b"t")
        assert time.monotonic() - began < 0.2  # the window is closed
    server.close()


async def test_byte_proxy_hands_the_connect_to_the_hook_before_the_server():
    """The login service's part: the CONNECT reaches the hook first, then the server,
    together with whatever followed it in the same write."""
    server, port = await _echo_server()
    seen: list[bytes] = []

    async def hook(payload: bytes) -> None:
        seen.append(payload)

    async with ByteProxy("127.0.0.1", port, on_connect=hook) as relay:
        reader, writer = await asyncio.open_connection("127.0.0.1", relay.port)
        line = b'CONNECT {"nkey":"U1"}\r\nPING\r\n'
        assert await _roundtrip(reader, writer, line) == line
        assert seen == [b'{"nkey":"U1"}']
    server.close()


async def test_byte_proxy_refuses_when_the_server_is_down():
    async with ByteProxy("127.0.0.1", natsrig._free_port()) as relay:
        reader, writer = await asyncio.open_connection("127.0.0.1", relay.port)
        assert await asyncio.wait_for(reader.read(10), 1.0) == b""
        assert relay.refused == 1


async def test_byte_proxy_rejects_a_frame_fault():
    async with ByteProxy("127.0.0.1", 9) as relay:
        with pytest.raises(ValueError):
            await relay.inject("duplicate", 0.1)


def test_the_nats_faults_are_the_phase_1_list():
    assert NATS_FAULTS == ("restart", "hang", "kill-client", "cut", "blackhole", "stall", "halfopen")


# --- pause and resume -----------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_pause_hangs_a_process_and_resume_ends_it(tmp_path: Path):
    """A hung process does no work: a child that appends a tick every 20 ms stops
    appending while paused and goes on once resumed."""
    ticks = tmp_path / "ticks"
    child = subprocess.Popen([sys.executable, "-c",
                              "import sys, time\n"
                              "while True:\n"
                              "    open(sys.argv[1], 'a').write('.')\n"
                              "    time.sleep(0.02)\n", str(ticks)])
    try:
        deadline = time.monotonic() + 10
        while (not ticks.exists() or ticks.stat().st_size < 3) and time.monotonic() < deadline:
            time.sleep(0.02)
        proc.pause(child.pid)
        time.sleep(0.1)  # a tick under way finishes
        frozen = ticks.stat().st_size
        time.sleep(0.3)
        assert ticks.stat().st_size == frozen
        proc.resume(child.pid)
        time.sleep(0.3)
        assert ticks.stat().st_size > frozen
    finally:
        proc.kill_child(child)


# --- the summary ----------------------------------------------------------------------


def _run(fault: str, arrivals: dict[int, float], sent: int = 3) -> runner.Run:
    run = runner.Run(name="bus-run", target="bus-nats", fault=fault)
    for seq in range(sent):
        run.sent.append(runner.Sent(seq=seq, at=100.0))
    run.sent_frames = sent
    for seq, delay in arrivals.items():
        run.arrive(seq, at=100.0 + delay)
    return run


def test_summary_has_a_row_per_fault_with_p50_p95_max_loss_duplicates():
    base = _run("none", {0: 0.010, 1: 0.020, 2: 0.030})
    cut = _run("cut", {0: 0.010, 2: 2.0})
    cut.arrive(2, at=101.0)  # a second copy
    rows = runner.summary_rows([base, cut])
    assert rows[0] == ("bus-nats", "none", "3", "0", "0", "20.0", "30.0", "30.0", "-", "-")
    assert rows[1][:5] == ("bus-nats", "cut", "3", "1", "1")
    assert rows[1][7] == "2000.0"
    table = runner.summary([base, cut]).splitlines()
    assert table[0].split()[:4] == ["target", "fault", "sent", "loss"]
    assert len(table) == 3


def test_summary_of_a_coordinator_run_shows_the_take_over():
    run = coord.CoordRun(name="coord-run", target="bus-nats", fault="coordinator-hard-stop")
    run.takeovers = [91.0, 118.0, 95.0]
    run.sent_frames = 3
    row = runner.summary_rows([run])[0]
    assert row[3:5] == ("-", "-")  # no delivery numbers for a run that sent nothing
    assert row[8:] == ("118.0", "118.0")
    assert run.measure()["takeover_p95"]["value"] == 118.0


def test_a_delivery_run_names_where_the_take_over_is_measured():
    entry = _run("none", {0: 0.01}, sent=1).measure()["takeover_p95"]
    assert entry["value"] is None and entry["status"] == "measured"
    assert "coord-run" in entry["why"]


# --- the coordinator run's reading of the crown log -----------------------------------


def test_crown_log_lines_and_who_holds(tmp_path: Path):
    uid = "u" * 26
    log = tmp_path / "ai-agent-bus" / f"crown-{uid}.log"
    log.parent.mkdir()
    log.write_text('{"t": "2026-11-26T10:00:00Z", "event": "gained", "way": "nats"}\n'
                   "not json\n"
                   '{"t": "2026-11-26T10:01:30Z", "event": "lost", "way": "nats"}\n')
    lines = coord.crown_lines(str(tmp_path), uid)
    assert [e["event"] for e in lines] == ["gained", "lost"]
    assert not coord.holds(lines)
    assert coord.holds(lines[:1])
    assert coord.crown_lines("", uid) == []
    assert coord._utc("2026-11-26T10:01:30Z") - coord._utc("2026-11-26T10:00:00Z") == 90.0


def test_a_stop_without_a_successor_has_no_take_over():
    stop = coord.Stop(killed="c0", killed_at=1000.0)
    assert stop.takeover_s is None and stop.row()["takeover_s"] is None
    stop.gained_at = 1093.0
    assert stop.row()["takeover_s"] == 93.0


def test_presence_fresh_sec_is_read_from_the_bus():
    assert coord.presence_fresh_sec() == 90.0


# --- the login service's parts, against their originals -------------------------------


def test_user_entry_grants_what_perms_go_grants():
    """perms.go is the original: a right added there and not here makes the rig's
    sessions fail on a refused publish. Count its publish rights, and check the
    entry carries the same subjects for the home channel."""
    perms = (REPO / "services" / "auth" / "internal" / "callout" / "perms.go").read_text()
    body = perms[perms.index("func (s Subjects) permissions("):]
    body = body[:body.index("\n}\n")]
    assert len(re.findall(r"p\.Pub\.Allow\.Add\(", body)) == 7
    assert len(re.findall(r"p\.Sub\.Allow\.Add\(", body)) == 1
    entry = natsrig.user_entry("UKEY", "agent-x")
    for subject in ("bus.ch.agents.agent-x", "presence.crown.agents.agent-x",
                    "presence.ask.agents.agent-x", "presence.member.agents.agent-x",
                    "bus.dm.*.agent-x", "presence.testrig.agent-x", "stats.agent-x",
                    "deliver.agent-x.>"):
        assert f'"{subject}"' in entry


def test_consumer_specs_follow_since():
    specs = {name: cfg for _, name, cfg in natsrig.consumer_specs("a", ["agents"],
                                                                 {"agents": 7, "@dm": 3})}
    assert specs["a_ch_agents"]["opt_start_seq"] == 8
    assert specs["a_dm"]["opt_start_seq"] == 4
    assert specs["a_presence"]["deliver_policy"] == "last_per_subject"
    assert "opt_start_seq" not in natsrig.consumer_specs("a", ["agents"])[0][2]


@pytest.mark.bus
def test_agent_name_is_the_bus_s_own():
    """The copy against natsbus.agent_name, run by the bus's interpreter."""
    try:
        argv = interpreter()
    except RuntimeError as exc:
        pytest.skip(str(exc))
    who = natsrig.identity("probe")
    out = subprocess.run(argv + ["-c", "import sys; from mattermost_ai_agent_bus.natsbus import agent_name; "
                                       "print(agent_name(sys.argv[1], sys.argv[2]))",
                                 natsrig.OWNER, who.public_key],
                         capture_output=True, text=True, check=True,
                         env={"PYTHONPATH": str(REPO / "mcp" / "src"), "PATH": "/usr/bin:/bin"})
    assert out.stdout.strip() == who.name


# --- live, against a real nats-server --------------------------------------------------


def _live() -> None:
    if natsrig.server_binary() is None:
        pytest.skip("no nats-server: set CB_NATS_SERVER to run the NATS transport live")
    try:
        interpreter()
    except RuntimeError as exc:
        pytest.skip(str(exc))


@pytest.mark.bus
async def test_a_short_nats_run_delivers_everything():
    _live()
    run = await natsrig.nats_bus_run(count=5)
    report = run.report()
    assert report["target"] == "bus-nats" and report["sent_frames"] == 5, report
    assert report["measured"]["loss"]["value"] == 0, report
    assert report["measured"]["duplicates"]["value"] == 0
    assert run.relay["bytes_down"] > 0  # the traffic went through the relay, over NATS
    assert not run.unknown and not any("stub holds" in n for n in run.notes), run.notes


@pytest.mark.bus
async def test_a_nats_restart_is_survived():
    _live()
    # Paced, so that sends after the window exist: the bus reconnects with back-off.
    run = await natsrig.nats_bus_run(count=6, fault="restart", fault_sec=0.5, send_interval=1.0,
                                     settle_sec=6.0)
    assert run.relay["server_restarts"] == 1
    assert run.received, run.report()  # the bus came back and carried posts after the restart
    assert max(run.received) >= 3, sorted(run.received)


@pytest.mark.bus
def test_the_cli_runs_the_nats_transport_with_a_summary():
    _live()
    out = subprocess.run([sys.executable, "-m", "bus_testrig.cli", "bus-run", "--transport", "nats",
                          "--count", "3", "--summary"], capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stderr[-2000:]
    table = out.stdout[out.stdout.rindex("\ntarget") + 1:].splitlines()
    report = json.loads(out.stdout[:out.stdout.rindex("\ntarget")])
    assert report["target"] == "bus-nats"
    assert table[1].split()[:4] == ["bus-nats", "none", "3", "0"]
