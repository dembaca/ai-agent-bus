"""What every test module here shares: the bus tests' skip, and the probe cache.

`BUS_TESTRIG_REQUIRE_BUS=1` turns the bus tests' skip into a failure, and CI
sets it (review point 1): a workflow that ran the tests before `uv sync` skipped
all eight bus tests and reported green, and nothing said the module was
unchecked. The run is refused once, at collection, with the sentence that names
the missing sync — thirteen tracebacks would say the same thing worse.

A skip is still right locally, where the echo run is the proof the harness works
without the bus.

`forget_interpreter` runs around every test because the probe's verdict is
cached for the process: a test that points the probe at another interpreter
would otherwise inherit a previous test's answer.
"""

from __future__ import annotations

import os

import pytest

from bus_testrig.bus import forget_interpreter, interpreter


@pytest.fixture(autouse=True)
def _forget_probed_interpreter():
    forget_interpreter()
    yield
    forget_interpreter()


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "bus: starts the real MCP server, so it needs `uv sync --directory mcp`")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """A skip is a failed job where the bus is required (review point 1)."""
    if os.environ.get("BUS_TESTRIG_REQUIRE_BUS") != "1":
        return
    if not any(item.get_closest_marker("bus") for item in items):
        return
    try:
        interpreter()
    except RuntimeError as exc:
        pytest.exit(f"BUS_TESTRIG_REQUIRE_BUS=1 and the bus cannot be imported: {exc}",
                    returncode=1)
