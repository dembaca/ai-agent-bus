"""`bus-testrig`'s exit code: whether a run's numbers can be compared.

Review point 2: `main` returned 0 on every path after a run, so "20 of 20
lost" and a brand-new endpoint the stub could not answer both stayed green. The
report still prints — the numbers are how a failure is read — and the code
says whether to believe them.

These drive `cli.main` in-process, with `bus_run` replaced, so what is checked
is the decision and the printed report, not a second real run per case.
"""

from __future__ import annotations

import json

import pytest

from bus_testrig import cli, runner


def _run(*, loss: int = 0, closed: str = "", fault: str = "none",
         unknown: list[tuple[str, str]] | None = None,
         unmeasured: str = "", sent: int = 20) -> runner.Run:
    run = runner.Run(name="bus-run", count=sent, target="bus", fault=fault)
    for seq in range(sent):
        run.send(seq)
        if seq >= loss:
            run.arrive(seq)
    run.sent_frames = sent
    run.closed = closed
    run.unknown = list(unknown or [])
    run.unmeasured = unmeasured
    if run.unknown:
        run.notes.append(f"the stub did not know {len(run.unknown)} call(s): {run.unknown[:5]}")
    run.started = run.ended = 0.0
    # measure() needs the clock for nothing, but a duration of 0 reads as
    # "never ran"; a report is still what the CLI prints.
    run.ended = 1.0
    return run


@pytest.fixture
def bus_run(monkeypatch):
    """Hand `cli.main` a run the test built, and record what it was asked for."""
    seen: dict[str, object] = {}

    async def fake(*, count, fault, send_interval):
        seen["count"], seen["fault"] = count, fault
        return seen["result"]

    monkeypatch.setattr("bus_testrig.bus.bus_run", fake)

    def give(result: runner.Run) -> runner.Run:
        seen["result"] = result
        return result

    return give


def _main(argv: list[str]) -> int:
    return cli.main(argv)


def _json_of(capsys) -> dict:
    """The report printed after the JSON dump, leaving stderr readable."""
    return json.loads(capsys.readouterr().out)


def test_a_clean_baseline_exits_zero(bus_run, capsys):
    bus_run(_run(loss=0))
    assert _main(["bus-run", "--count", "20"]) == 0
    report = _json_of(capsys)
    assert report["measured"]["loss"]["value"] == 0


def test_a_baseline_that_lost_a_frame_exits_non_zero(bus_run, capsys):
    """"20 of 20 lost" must not be green: the rig itself is broken, and a
    number from it compares with nothing."""
    bus_run(_run(loss=2, closed="the socket closed"))
    assert _main(["bus-run", "--count", "20"]) == 1
    captured = capsys.readouterr()
    # The report is still printed, so the loss can be read.
    assert json.loads(captured.out)["measured"]["loss"]["value"] == 2
    assert "baseline lost 2" in captured.err


def test_a_closed_baseline_exits_non_zero_even_without_loss(bus_run, capsys):
    bus_run(_run(loss=0, closed="inbox never opened"))
    assert _main(["bus-run", "--count", "20"]) == 1


def test_an_unknown_call_exits_non_zero_whatever_the_fault(bus_run, capsys, monkeypatch):
    """A brand-new endpoint the stub does not know is a gap in the rig, not a
    delivery result; a fault run does not excuse it."""
    bus_run(_run(loss=0, fault="drop",
                 unknown=[("GET", "/api/v4/a/new/endpoint")]))
    assert _main(["bus-run", "--count", "20", "--fault", "drop"]) == 1
    assert "/api/v4/a/new/endpoint" in capsys.readouterr().err


def test_a_fault_runs_own_loss_is_not_a_failure(bus_run, capsys):
    """A faulted run's loss is its measurement: `--fault drop` is expected to
    lose, and the exit code must not call that a broken rig."""
    bus_run(_run(loss=20, fault="drop", closed="no open socket when a posted frame was pushed"))
    assert _main(["bus-run", "--count", "20", "--fault", "drop", "--after", "0"]) == 0
    assert _json_of(capsys)["measured"]["loss"]["value"] == 20


def test_a_run_that_sent_nothing_exits_non_zero(bus_run, capsys):
    """Follow-up review point 2: a fault run whose sender broke before its
    first post has `loss == 0`, nothing unknown and nothing unmeasured, so it
    passed. It measured a sender that never ran."""
    bus_run(_run(loss=0, fault="drop", sent=0,
                 closed="post_message failed: McpError: the server refused"))
    assert _main(["bus-run", "--count", "20", "--fault", "drop", "--after", "1"]) == 1
    err = capsys.readouterr().err
    assert "sent no frame" in err
    assert "the server refused" in err


def test_expect_loss_fails_a_fault_run_that_lost_nothing(bus_run, capsys):
    """`--fault drop --every 1000` may fault no frame at all: the run is then a
    clean baseline wearing a fault's name, and `--expect-loss` says so."""
    bus_run(_run(loss=0, fault="drop"))
    assert _main(["bus-run", "--count", "20", "--fault", "drop",
                  "--every", "1000", "--expect-loss"]) == 1
    assert "--expect-loss" in capsys.readouterr().err


def test_expect_loss_passes_a_fault_run_that_lost(bus_run, capsys):
    """The flag checks the claim, it does not forbid the loss: a run that lost
    what the fault promised exits 0."""
    bus_run(_run(loss=4, fault="drop", closed="no open socket when a posted frame was pushed"))
    assert _main(["bus-run", "--count", "20", "--fault", "drop", "--after", "1",
                  "--expect-loss"]) == 0
    assert _json_of(capsys)["measured"]["loss"]["value"] == 4


def test_a_passing_fault_that_delivers_nothing_exits_non_zero(bus_run, capsys):
    """`stall` lets every frame through, so a run under it must deliver some.

    Delivering none means the path, not the fault (issue #47). The run is built
    through `bus_run` with a stub that accepts each pushed frame and hands none
    to the receiver: the relay counts it as forwarded, the run arrives nowhere —
    exactly the case a built report could not show. `drop` is not covered here:
    a `drop` run may lose everything by design and is a measurement.
    """
    run = _run(loss=3, fault="stall", sent=3)
    run.received.clear()  # nothing arrived, though the fault lets it through
    run.relay = {"received": 4, "forwarded": 4, "altered": 0}
    bus_run(run)
    assert _main(["bus-run", "--count", "3", "--fault", "stall"]) == 1
    assert "lets traffic through" in capsys.readouterr().err


def test_a_drop_run_that_delivers_nothing_stays_a_measurement(bus_run, capsys):
    """The other side of the rule: `drop` is meant to discard, so a run under it
    that delivers nothing and exits 0 is the measurement, not a broken path."""
    run = _run(loss=3, fault="drop", sent=3)
    run.received.clear()
    run.relay = {"received": 4, "forwarded": 1, "altered": 3}
    bus_run(run)
    assert _main(["bus-run", "--count", "3", "--fault", "drop", "--after", "1",
                  "--every", "1"]) == 0


def test_expect_loss_fails_when_everything_was_lost(bus_run, capsys):
    """`loss == sent` is a dead path, not a fault: a receiver that never gets a
    frame and a fault that drops every frame look the same, and the flag meant
    to prove a fault works proved nothing while `loss > 0` was the test.
    Without this case the older condition kept the whole file green (review of
    #25)."""
    bus_run(_run(loss=20, fault="drop", closed="inbox never opened"))
    assert _main(["bus-run", "--count", "20", "--fault", "drop", "--after", "1",
                  "--expect-loss"]) == 1
    assert "loses some and carries the rest" in capsys.readouterr().err


def test_a_fault_run_that_measured_nothing_exits_non_zero(bus_run, capsys):
    """The inbox never opened: the loss says "everything", and every
    comparison against it would be against nothing (the `drop --after 0` run
    the review measured at 5 / 5 lost and exit 0)."""
    bus_run(_run(loss=5, fault="drop", unmeasured="the receiver's inbox never opened"))
    assert _main(["bus-run", "--count", "5", "--fault", "drop", "--after", "0"]) == 1
    assert "measured nothing" in capsys.readouterr().err


def test_an_echo_run_still_exits_zero_on_its_own_loss(monkeypatch, capsys):
    """The exit-code rule is about the bus run; the echo run's own cut test
    lives in `ci_fault_check.py` and must not start failing on its loss."""
    async def fake(*, count, fault, send_interval):
        run = runner.Run(name="echo-run", count=2, target="echo",
                         fault="cut" if fault else "none")
        run.send(0)
        run.sent_frames = 1
        run.closed = "ConnectionClosed"
        run.ended = 1.0
        return run

    monkeypatch.setattr("bus_testrig.runner.echo_run", fake)
    assert _main(["echo-run", "--fault", "cut"]) == 0


def test_the_missing_interpreter_is_a_start_failure(monkeypatch, capsys):
    """No interpreter, no run: 2, with the sentence that says what to do, not a
    traceback (and not 0, which the review found on every path)."""
    async def boom(*, count, fault, send_interval):
        raise RuntimeError("no interpreter here imports mattermost_ai_agent_bus: "
                           "run `uv sync --directory mcp` first")

    monkeypatch.setattr("bus_testrig.bus.bus_run", boom)
    assert _main(["bus-run", "--count", "1"]) == 2
    assert "uv sync" in capsys.readouterr().err.lower()
