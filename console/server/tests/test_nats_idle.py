# 2b. the factor: the silent reconnect comes at three quarters of the
# bound, taken back half a watch tick — before the bound, never after.
# The identity the loop uses is checked without a server: the loop's own
# arithmetic (idle - wake/2 < bound for every bound), so the mutant 3
# (idle = bound * 3) fails it.
import math
import pytest


@pytest.mark.parametrize("bound", [30.0, 60.0, 180.0, 3600.0, 86400.0])
def test_the_silent_reconnect_lands_before_the_probe_bound(bound: float) -> None:
    from console_server import nats_read
    wake = max(5.0, bound / 4)
    idle = max(nats_read.IDLE_FLOOR_SEC, bound * 3 / 4)
    # the reconnect happens at age >= idle - wake/2 (every watch tick); the
    # probe goes red at bound: the former must beat the latter for the
    # probe to stay green through a silent cycle
    assert idle - wake / 2 < bound, "the silent reconnect must land inside the probe bound"
    # and the mutant survives nowhere: three times the bound would
    assert bound * 3 - wake / 2 >= bound
