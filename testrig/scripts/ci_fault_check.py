"""The CI check that a fault really changes the numbers.

Run by `testrig.yml` after the plain echo run: a faulted run must report a
loss, and a measurand this bus cannot produce must be named as such rather than
reported as a zero (review points 4 and 15). Kept as a file, not a heredoc in
YAML, so it can be run and debugged locally exactly as CI runs it.
"""

from __future__ import annotations

import json
import subprocess
import sys


def main() -> int:
    out = subprocess.run(
        [sys.executable, "-m", "bus_testrig.cli", "echo-run",
         "--fault", "drop", "--after", "1", "--count", "10"],
        capture_output=True, text=True, check=True,
    ).stdout
    report = json.loads(out)
    measured = report["measured"]
    assert report["target"] == "echo", f"the harness reported a different target: {report['target']}"
    assert measured["loss"]["value"] > 0, f"a dropping run reported no loss: {measured['loss']}"
    # Measured by `coord-run` only: an echo run names that instead of a zero.
    assert measured["takeover_p95"]["value"] is None, measured["takeover_p95"]
    assert "coord-run" in measured["takeover_p95"]["why"], measured["takeover_p95"]
    assert measured["device_approvals"]["status"] == "not_yet_measurable", measured["device_approvals"]

    # A cut ends the connection and must not crash the run nor count as a loss.
    out = subprocess.run(
        [sys.executable, "-m", "bus_testrig.cli", "echo-run",
         "--fault", "cut", "--after", "1", "--count", "5"],
        capture_output=True, text=True, check=True,
    )
    if "Traceback" in out.stderr:
        raise AssertionError(f"a cut printed a traceback:\n{out.stderr[-500:]}")
    cut = json.loads(out.stdout)
    assert cut["connection_closed"], cut
    # A cut loses whatever it prevented: the report names the closed
    # connection *and* the count, never a zero that hides it.
    loss = cut["measured"]["loss"]["value"]
    assert loss is not None and loss > 0, cut["measured"]["loss"]

    # A failed write is neither sent nor lost (fourth re-check): loss must be
    # exactly the frames sent minus the frames that arrived.
    paced = subprocess.run(
        [sys.executable, "-m", "bus_testrig.cli", "echo-run",
         "--fault", "cut", "--after", "3", "--count", "10", "--send-interval", "0.05"],
        capture_output=True, text=True, check=True,
    )
    p = json.loads(paced.stdout)
    assert p["measured"]["loss"]["value"] == 1, p["measured"]["loss"]

    # `bus-run` says whether its own run can be compared (review point 2): a
    # baseline must exit 0, and a run that ate the whole socket must exit
    # non-zero even though it lost every frame. Both are subprocesses, so this
    # also checks the exit code the shell sees, not only the JSON.
    clean = subprocess.run(
        [sys.executable, "-m", "bus_testrig.cli", "bus-run", "--count", "3"],
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert clean.returncode == 0, (clean.returncode, clean.stderr[-500:])
    assert json.loads(clean.stdout)["measured"]["loss"]["value"] == 0

    broken = subprocess.run(
        [sys.executable, "-m", "bus_testrig.cli", "bus-run", "--count", "3",
         "--fault", "drop", "--after", "0"],
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert broken.returncode != 0, (broken.returncode, broken.stderr[-500:])
    # Its report is still printed: the numbers are how the failure is read.
    assert json.loads(broken.stdout)["sent_frames"] == 3

    print("faulted run reports its loss; a cut reports the closed connection and what it lost; "
          "bus-run's exit code says whether its numbers can be compared")
    return 0


if __name__ == "__main__":
    sys.exit(main())
