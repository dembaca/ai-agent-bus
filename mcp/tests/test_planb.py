"""Plan B's switches (RT-6.39 step 5): planb.on follows the attached source."""

from collections.abc import Iterator

import pytest

from mattermost_ai_agent_bus import planb


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(planb.TEST_ENV, raising=False)
    planb._source = None
    planb._warned.clear()
    yield
    planb._source = None
    planb._warned.clear()


def _src(channel: str) -> frozenset[int]:
    return frozenset({1, 3}) if channel == "home" else frozenset()


def test_off_without_a_source() -> None:
    """RT-6.39 step 5: no source (the home is not over NATS) means every piece off."""
    assert not planb.on(1, "home")


def test_follows_the_source_per_channel() -> None:
    """RT-6.39 step 5: a piece is on in the channels whose record lists it."""
    planb.attach(_src)
    assert planb.on(1, "home") and planb.on(3, "home")
    assert not planb.on(2, "home") and not planb.on(1, "other")


@pytest.mark.parametrize("piece", [0, 14, -1, True, "1"])
def test_a_bad_piece_is_off(piece: object) -> None:
    """RT-6.39 step 5: only integers 1 to 13 are switches."""
    planb.attach(lambda _c: frozenset(range(0, 20)))
    assert not planb.on(piece, "home")  # type: ignore[arg-type]


def test_a_raising_source_is_off() -> None:
    """RT-6.39 step 5: a source that fails gives off."""
    def boom(_c: str) -> frozenset[int]:
        raise RuntimeError("x")
    planb.attach(boom)
    assert not planb.on(1, "home")


def test_detach_of_another_source_keeps_the_attached_one() -> None:
    """RT-6.39 step 5: only the attached source detaches."""
    planb.attach(_src)
    planb.detach(lambda _c: frozenset())
    assert planb.on(1, "home")
    planb.detach(_src)
    assert not planb.on(1, "home")


def test_the_test_env_switches_pieces_on_everywhere(monkeypatch: pytest.MonkeyPatch,
                                                    caplog: pytest.LogCaptureFixture) -> None:
    """RT-6.39 step 5: CB_PLANB_TEST is on in every channel, without a source; bad entries are ignored."""
    monkeypatch.setenv(planb.TEST_ENV, "1, 3,x,14")
    assert planb.on(1, "any") and planb.on(3, "other")
    assert not planb.on(2, "any") and not planb.on(14, "any")
    assert planb.on(1, "any")
    assert sum("'x'" in r.getMessage() for r in caplog.records) == 1


def test_the_test_env_outside_a_test_is_ignored(monkeypatch: pytest.MonkeyPatch,
                                                caplog: pytest.LogCaptureFixture) -> None:
    """RT-6.39 step 5: set outside a test, CB_PLANB_TEST counts for nothing."""
    monkeypatch.setenv(planb.TEST_ENV, "1")
    monkeypatch.delenv("PYTEST_CURRENT_TEST")
    assert not planb.on(1, "home") and not planb.on(1, "home")
    assert sum("for tests only" in r.getMessage() for r in caplog.records) == 1
