"""The models' empty defaults (#96): a shared, read-only mapping taken through
default_factory. A frozen dataclass's default must be hashable on Python 3.11 —
a mappingproxy only is from 3.12 on — so `default=_EMPTY` made the whole package
fail at import on 3.11, which `pyproject.toml` allows. The CI job on 3.11 runs
this module and the suite; this test fails if the plain default comes back.
"""

import dataclasses

from mattermost_ai_agent_bus import model


def test_empty_defaults_are_factories_not_plain_defaults() -> None:
    fields = (
        (model.Me, "raw"),
        (model.Channel, "raw"),
        (model.ChannelUser, "custom_status"),
        (model.Post, "props"),
        (model.Post, "raw"),
    )
    for cls, name in fields:
        f = next(f for f in dataclasses.fields(cls) if f.name == name)
        assert f.default_factory is not dataclasses.MISSING, (
            f"{cls.__name__}.{name} takes a plain default; Python 3.11 refuses an "
            f"unhashable one (the package would not import)"
        )
        assert f.default is dataclasses.MISSING, f"{cls.__name__}.{name} has both a default and a factory"


def test_the_empty_default_is_the_one_shared_mapping() -> None:
    assert model.Me().raw is model._EMPTY
    assert model.Channel().raw is model._EMPTY
    assert model.ChannelUser().custom_status is model._EMPTY
    assert model.Post().props is model._EMPTY and model.Post().raw is model._EMPTY
    # read-only: a caller cannot change every other model's default through it
    try:
        model._EMPTY["x"] = 1  # type: ignore[index]
    except TypeError:
        pass
    else:
        raise AssertionError("the empty default is writable")
