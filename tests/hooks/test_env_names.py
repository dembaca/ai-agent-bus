"""The environment-name guard in scripts/doc-check.py (docs/env-names.md, #169).

The page is the mapping every component's rename follows, so a name the code
reads that is missing there would lose its `CB_` name when the rename comes,
and a row nothing reads is a stale setting. This file pins both directions and
the reader forms the check knows, so a weakened guard shows up as a failing
test.

Run where the other script tests run: uv run --directory mcp pytest ../tests/hooks
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _doc_check():
    spec = importlib.util.spec_from_file_location("doc_check", ROOT / "scripts" / "doc-check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


doc_check = _doc_check()


def test_the_page_parses_into_its_five_lists() -> None:
    page = doc_check.env_page()
    assert len(page["old"]) == 158          # the mapping tables' left column
    assert len(page["registrar"]) == 10     # kept names, their own table
    assert not page["old"] & page["registrar"]
    # the two exclusion lists the guard has to accept
    assert {"MM_ANSWER", "MM_CROWN", "MM_INBOX"} <= page["not_env"]
    assert {"HOME", "PATH", "CLAUDE_PROJECT_DIR", "MM_A"} <= page["excluded"]
    # the new names are part of the page too: the tables' right column
    assert {"CB_NATS_URL", "CB_MM_BOT_TOKEN", "CB_AUTH_X".replace("X", "NATS_URL")} <= page["new"]


def test_every_reader_form_is_seen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The names are put together here, so this file's own text holds no read the
    # guard would find (it scans the test tree too, and it must find nothing).
    py_one, py_two = "MM_PY_" + "ONE", "MM_PY_" + "TWO"
    py_const = "MM_PY_" + "CONST"
    go_one, go_two = "AUTH_GO_" + "ONE", "BRIDGE_GO_" + "TWO"
    ts_one, ts_two = "CONSOLE_TS_" + "ONE", "MM_TS_" + "TWO"
    sh_one, sh_two = "MM_SH_" + "ONE", "MM_SH_" + "TWO"
    (tmp_path / "a.py").write_text(
        "import os\n"
        f'X = os.environ.get("{py_one}", "")\n'
        f'Y = _env("{py_two}")\n'
        f'CONST = "{py_const}"\n')
    (tmp_path / "b.go").write_text(
        f'v := os.Getenv("{go_one}")\n'
        f'type c struct {{ U string `env:"{go_two}"` }}\n')
    (tmp_path / "c.ts").write_text(
        f"const a = process.env.{ts_one}\n"
        f'const b = process.env["{ts_two}"]\n')
    (tmp_path / "d.sh").write_text(f'x="${{{sh_one}:-}}" && y=${sh_two}\n')
    (tmp_path / "ignored.md").write_text("MM_NOT_CODE\n")
    monkeypatch.setattr(doc_check, "ROOT", tmp_path)
    names = doc_check.env_code_names(["a.py", "b.go", "c.ts", "d.sh", "ignored.md"])
    assert {py_one, py_two, py_const, go_one, go_two, ts_one, ts_two,
            sh_one, sh_two} <= set(names)
    assert "MM_NOT_CODE" not in names
    # the file that reads it is named, for the error message
    assert names[py_one] == "a.py" and names[sh_two] == "d.sh"


def test_both_directions_fail() -> None:
    page = {"old": {"MM_KNOWN", "MM_STALE"}, "new": {"CB_KNOWN", "CB_STALE"},
            "registrar": {"MM_REG_SECRET"}, "not_env": {"MM_GLOBAL"}, "excluded": {"HOME"}}
    read = {"MM_KNOWN": "x.py", "MM_GLOBAL": "y.sh", "MM_REG_SECRET": "z.py",
            "MM_UNKNOWN": "w.py"}
    got = doc_check.env_name_errors(page, read, "MM_KNOWN MM_GLOBAL MM_REG_SECRET")
    assert got == [
        ("w.py", "reads MM_UNKNOWN, which docs/env-names.md does not list"),
        ("docs/env-names.md", "maps MM_STALE, which no code mentions"),
    ]


def test_nothing_is_reported_when_both_sides_agree() -> None:
    page = {"old": {"MM_KNOWN"}, "new": {"CB_KNOWN"}, "registrar": set(),
            "not_env": set(), "excluded": set()}
    assert doc_check.env_name_errors(page, {"MM_KNOWN": "x.py"}, "MM_KNOWN") == []


def test_the_repository_passes_the_guard() -> None:
    """The real tree, both directions: this is what CI runs."""
    doc_check.errors.clear()
    doc_check.check_env_names()
    assert doc_check.errors == []
