# The silent stream's timing — pinned on the one function the loop uses,
# so no mutant of the arithmetic survives: the factor, the subtraction
# and the waking interval all live here.
import pytest

from console_server import nats_read


@pytest.mark.parametrize("bound", [30.0, 60.0, 180.0, 3600.0, 86400.0])
def test_the_silent_reconnect_lands_inside_the_bound(bound: float) -> None:
    wake, threshold = nats_read.idle_timing(bound)
    # the tick that fires the reconnect lands between 5/8 and 7/8 of the
    # bound — inside it, for every bound (the 15 s floor lifts the low ones)
    assert wake == max(5.0, bound / 4)
    assert threshold < bound, "the reconnect must land inside the probe bound"
    assert threshold >= bound * 5 / 8 - wake
    # the factor-3 and no-subtraction mutants fail it
    assert not threshold >= bound
    assert threshold < bound * 3 / 4 + 0.001


def test_the_floor_lifts_the_low_bounds() -> None:
    wake, threshold = nats_read.idle_timing(30.0)
    # three quarters of 30 s is 22.5 s, above the 15 s floor either way;
    # the floor shows for bounds a human should not set (guarded to 30 s)
    assert threshold >= nats_read.IDLE_FLOOR_SEC - wake / 2
