"""#182: build.json as the image build wrote it, and the ways it can be wrong."""
from __future__ import annotations

import json
import logging

from console_server import build


def test_no_dir_no_file_is_no_build(tmp_path, caplog) -> None:
    with caplog.at_level(logging.WARNING):
        assert build.read("") == build.Build()
        assert build.read(str(tmp_path)) == build.Build()
    assert not caplog.records


def test_values_are_checked_one_by_one(tmp_path) -> None:
    (tmp_path / "build.json").write_text(
        json.dumps({"version": "bad value", "revision": "ABCDEF1"}))
    assert build.read(str(tmp_path)) == build.Build()
    (tmp_path / "build.json").write_text(
        json.dumps({"version": "1.2.3-rc.1+x", "revision": 5}))
    assert build.read(str(tmp_path)) == build.Build(version="1.2.3-rc.1+x")


def test_unreadable_warns_once(tmp_path, caplog) -> None:
    for text in ("not json", "[1]"):
        (tmp_path / "build.json").write_text(text)
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            assert build.read(str(tmp_path)) == build.Build()
        assert len(caplog.records) == 1
