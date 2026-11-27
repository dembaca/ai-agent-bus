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
    # The synthetic run models a fault whose window closed and whose session
    # came back; a test that wants the lost sender sets it False.
    run.recovered = True
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
    run = _run(loss=20, fault="drop", closed="no open socket when a posted frame was pushed")
    run.relay = {"received": 21, "forwarded": 0, "altered": 20,
                 "posted_seen": 20, "posted_forwarded": 0, "posted_altered": 20}
    bus_run(run)
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


@pytest.mark.parametrize("fault", ("stall", "reorder", "duplicate"))
def test_a_passing_fault_that_delivers_nothing_exits_non_zero(bus_run, capsys, fault):
    """`stall`, `reorder` and `duplicate` let every frame through, so a run under
    one of them must deliver some.

    Delivering none means the path, not the fault (issue #47). Each of the three
    is checked: with only `stall` pinned, a mutation that treated `reorder` and
    `duplicate` as silencing faults stayed green (716d72's review of #78). The run
    is **built here**, not driven: the `bus_run` fixture replaces `bus_run` with a
    stand-in that returns this `Run` to `cli.main`, so the test exercises the
    decision and not a real run.

    `none` is not covered here: the baseline rule catches it first. `drop`,
    `blackhole`, `halfopen` and `cut` have rules of their own.
    """
    run = _run(loss=3, fault=fault, sent=3)
    run.received.clear()  # nothing arrived, though the fault lets it through
    run.relay = {"received": 4, "forwarded": 4, "altered": 0,
                 "posted_seen": 3, "posted_forwarded": 3, "posted_altered": 0}
    bus_run(run)
    assert _main(["bus-run", "--count", "3", "--fault", fault]) == 1
    assert "lets traffic through" in capsys.readouterr().err


def test_a_drop_run_that_discarded_every_posted_frame_stays_a_measurement(bus_run, capsys):
    """The measurement side of the rule: `drop` saw every posted frame and
    discarded every one, so the silence is the fault's - and the run **says** so
    (issue #47). Without the sentence the same numbers read as a healthy run."""
    run = _run(loss=3, fault="drop", sent=3)
    run.received.clear()
    run.relay = {"received": 4, "forwarded": 1, "altered": 3,
                 "posted_seen": 3, "posted_forwarded": 0, "posted_altered": 3}
    bus_run(run)
    assert _main(["bus-run", "--count", "3", "--fault", "drop", "--after", "1",
                  "--every", "1"]) == 0
    err = capsys.readouterr().err
    assert "nothing arrived" in err and "discards every frame it sees" in err, err
    assert "3 of 3 posted frame(s) discarded at the relay" in err, err


def test_a_drop_run_whose_frames_never_reached_the_relay_exits_non_zero(bus_run, capsys):
    """The criterion that tells the two apart (issue #47): `drop` discards what it
    sees, so a frame the relay never saw was lost **before** the injection point.
    Counting delivered frames alone reads identically; the relay's own counters do
    not (Fable's first case on #78)."""
    run = _run(loss=3, fault="drop", sent=3)
    run.received.clear()
    # the plain counters would say "all seen, all dropped" (status frames inflate
    # them); only the posted ones show that two of three never reached the relay
    run.relay = {"received": 4, "forwarded": 1, "altered": 3,
                 "posted_seen": 1, "posted_forwarded": 0, "posted_altered": 1}
    bus_run(run)
    assert _main(["bus-run", "--count", "3", "--fault", "drop", "--after", "1",
                  "--every", "1"]) == 1
    assert "lost before the injection point" in capsys.readouterr().err


def test_a_drop_run_that_dropped_only_some_exits_non_zero(bus_run, capsys):
    """The other half of the same criterion: the frames the fault *forwarded* also
    failed to arrive, so the silence is not the fault's alone (Fable on #78: a
    `--every 2` run exited 0 before this)."""
    run = _run(loss=3, fault="drop", sent=3)
    run.received.clear()
    run.relay = {"received": 3, "forwarded": 2, "altered": 1,
                 "posted_seen": 3, "posted_forwarded": 2, "posted_altered": 1}
    bus_run(run)
    assert _main(["bus-run", "--count", "3", "--fault", "drop", "--every", "2"]) == 1
    assert "the path lost the forwarded ones" in capsys.readouterr().err


def test_a_blackhole_that_discarded_every_posted_frame_stays_a_measurement(bus_run, capsys):
    """`blackhole` is the same discard as `drop` (the review of #78): it has to be
    read the same way, not taken as explained unconditionally."""
    run = _run(loss=3, fault="blackhole", sent=3)
    run.received.clear()
    run.relay = {"received": 3, "forwarded": 0, "altered": 3,
                 "posted_seen": 3, "posted_forwarded": 0, "posted_altered": 3}
    bus_run(run)
    assert _main(["bus-run", "--count", "3", "--fault", "blackhole", "--after", "1"]) == 0
    assert "stops delivery" in capsys.readouterr().err


def test_a_blackhole_that_never_fired_exits_non_zero(bus_run, capsys):
    """`blackhole --after 50` on a 3-frame run discards nothing: nothing arriving
    is then the path, not the fault (Fable's measured case)."""
    for fault in ("blackhole", "drop", "halfopen"):
        run = _run(loss=3, fault=fault, sent=3)
        run.received.clear()
        run.relay = {"received": 3, "forwarded": 3, "altered": 0,
                     "posted_seen": 3, "posted_forwarded": 3, "posted_altered": 0}
        bus_run(run)
        assert _main(["bus-run", "--count", "3", "--fault", fault]) == 1, fault
        err = capsys.readouterr().err
        assert "is the path" in err or "discarded 0 of 3" in err, (fault, err)


def test_a_halfopen_that_froze_before_a_posted_frame_stays_a_measurement(bus_run, capsys):
    """A frozen line carries nothing: if no posted frame had been delivered before
    the freeze, the silence is what the fault is for."""
    run = _run(loss=3, fault="halfopen", sent=3)
    run.received.clear()
    run.relay = {"received": 3, "forwarded": 0, "altered": 3,
                 "posted_seen": 3, "posted_forwarded": 0, "posted_altered": 3}
    bus_run(run)
    assert _main(["bus-run", "--count", "3", "--fault", "halfopen", "--after", "3"]) == 0
    assert "froze the line before a posted frame" in capsys.readouterr().err


def test_a_halfopen_that_delivered_a_posted_frame_first_exits_non_zero(bus_run, capsys):
    """A posted frame delivered before the freeze had to be carried by the path:
    if none arrived, the path lost it."""
    run = _run(loss=3, fault="halfopen", sent=3)
    run.received.clear()
    run.relay = {"received": 3, "forwarded": 1, "altered": 2,
                 "posted_seen": 3, "posted_forwarded": 1, "posted_altered": 2}
    bus_run(run)
    assert _main(["bus-run", "--count", "3", "--fault", "halfopen", "--after", "3"]) == 1
    assert "had been delivered, and none arrived" in capsys.readouterr().err


def test_a_cut_that_delivers_nothing_exits_non_zero(bus_run, capsys):
    """`cut` aborts the transport and the bus recovers by catch-up read (the
    review of #78): nothing arriving means the recovery recovered nothing, which
    is not the cut itself - it used to exit 0 as "the fault's own silence"."""
    run = _run(loss=4, fault="cut", sent=4, closed="ConnectionClosed: cut")
    run.received.clear()
    run.relay = {"received": 0, "forwarded": 0, "altered": 1,
                 "posted_seen": 0, "posted_forwarded": 0, "posted_altered": 1}
    bus_run(run)
    assert _main(["bus-run", "--count", "4", "--fault", "cut"]) == 1
    assert "recovered none of it" in capsys.readouterr().err


def test_a_silent_run_without_relay_counters_exits_non_zero(bus_run, capsys):
    """No counters is no evidence (Fable's fifth point): the fault cannot be shown
    to have caused the silence, so the run does not pass."""
    run = _run(loss=3, fault="drop", sent=3)
    run.received.clear()
    bus_run(run)
    assert _main(["bus-run", "--count", "3", "--fault", "drop"]) == 1
    assert "kept no relay counters" in capsys.readouterr().err


def test_expect_loss_fails_when_everything_was_lost(bus_run, capsys):
    """`loss == sent` is a dead path, not a fault: a receiver that never gets a
    frame and a fault that drops every frame look the same, and the flag meant
    to prove a fault works proved nothing while `loss > 0` was the test.
    Without this case the older condition kept the whole file green (review of
    #25)."""
    run = _run(loss=20, fault="drop", closed="inbox never opened")
    run.relay = {"received": 20, "forwarded": 0, "altered": 20,
                 "posted_seen": 20, "posted_forwarded": 0, "posted_altered": 20}
    bus_run(run)
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
