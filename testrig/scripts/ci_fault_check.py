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
    assert measured["takeover_p95"]["status"] == "not_yet_measurable", measured["takeover_p95"]
    assert measured["takeover_p95"]["value"] is None, measured["takeover_p95"]
    print("faulted run reports a loss and names what it cannot measure yet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
