"""The environment-name guard in scripts/doc-check.py (docs/env-names.md, #169).

The page is the mapping every component's rename follows, so a name the code
reads that is missing there would lose its `CB_` name when the rename comes,
and a row nothing reads is a stale setting. This file pins both directions, the
reader forms the check knows and the page's own counts, so a weakened guard
shows up as a failing test.

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


def test_the_page_parses_into_its_lists() -> None:
    page = doc_check.env_page()
    assert len(page["old"]) == 183          # the mapping tables' left column (incl. CB_SESSION_NONCE, CB_COPIED_FROM, CB_STATE_KEY, CB_PLANB_TEST, CB_RECORDS_URL, CB_BRIDGE_MM_BOT_KEEP, CB_AUTH_LDAP_ID_CLAIM, the bridge commands' 10, the release record's 2 and the two request prefixes of plan B's wave 0)
    assert len(page["registrar"]) == 10     # kept names, their own table
    assert not page["old"] & page["registrar"]
    # the two exclusion lists the guard accepts
    assert {"CB_ANSWER", "CB_CROWN", "CB_INBOX", "CB_MM_API_BODY", "BUS_KEY"} <= page["not_env"]
    assert {"HOME", "PATH", "CLAUDE_PROJECT_DIR", "MM_A", "BRIDGE_TTL_SEC"} <= page["excluded"]
    # the tables' right column, and the per-section membership the test table needs
    assert {"CB_NATS_URL", "CB_MM_BOT_TOKEN", "CB_AUTH_NATS_URL"} <= page["new"]
    assert page["sections"]["Tests and CI"] == {
        "MM_NATS_SERVER", "MM_TEST_NATS", "MM_TEST_KEYCLOAK", "MM_TEST_FD_TOKEN",
        "BUS_TESTRIG_PYTHON", "BUS_TESTRIG_REQUIRE_BUS"}


def test_the_headings_count_what_their_tables_hold() -> None:
    for title, stated, parsed in doc_check.env_counts():
        assert stated == parsed, f"{title}: heading says {stated}, table holds {parsed}"


def test_every_reader_form_is_seen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Names are put together here, so this file's own text holds no read the
    # guard would find (it scans the test tree too, and must find nothing there).
    py_one, py_two = "MM_PY_" + "ONE", "MM_PY_" + "TWO"
    py_const, go_one = "MM_PY_" + "CONST", "AUTH_GO_" + "ONE"
    go_two, ts_one = "BRIDGE_GO_" + "TWO", "CONSOLE_TS_" + "ONE"
    ts_two, sh_one = "MM_TS_" + "TWO", "MM_SH_" + "ONE"
    sh_two = "MM_SH_" + "TWO"
    (tmp_path / "a.py").write_text(
        "import os\n"
        f'X = os.environ.get("{py_one}", "")\n'     # a call's argument
        f'Y = env(\n    "{py_two}",\n)\n'            # a call split over lines
        f'CONST = "{py_const}"\n')                    # a module constant
    (tmp_path / "b.go").write_text(
        f'v := get("{go_one}")\n'                     # the service's own helper
        f'type c struct {{ U string `env:"{go_two},required"` }}\n')
    (tmp_path / "c.ts").write_text(
        f"const a = process.env.{ts_one}\n"
        f'const b = process.env["{ts_two}"]\n')
    (tmp_path / "d.sh").write_text(
        f'x="${{{sh_one}:-}}" && y=${{{sh_two}}} && export {sh_one}=1\n'
        'unset MM_SH_THREE 2>/dev/null; read -r MM_SH_FOUR <<<""\n')
    (tmp_path / "Dockerfile").write_text("ARG CONSOLE_DOCKER_ONE\n")
    (tmp_path / "ignored.md").write_text("MM_NOT_CODE\n")
    monkeypatch.setattr(doc_check, "ROOT", tmp_path)
    names = doc_check.env_code_names(
        ["a.py", "b.go", "c.ts", "d.sh", "Dockerfile", "ignored.md"])
    assert {py_one, py_two, py_const, go_one, go_two, ts_one, ts_two,
            sh_one, sh_two, "MM_SH_THREE", "MM_SH_FOUR",
            "CONSOLE_DOCKER_ONE"} <= set(names)
    assert "MM_NOT_CODE" not in names
    assert names[py_one] == "a.py" and names[sh_two] == "d.sh"
    assert "CONSOLE_DOCKER_ONE" in names  # an ARG in a Dockerfile


def test_a_stem_and_the_guards_own_files_are_no_reads(tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "e.sh").write_text('x="${MM_REL_}${k}"\n')       # a stem in a pattern
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "doc-check.py").write_text('X = "MM_EXAMPLE_ONLY"\n')
    (tmp_path / "tests" / "hooks").mkdir(parents=True)
    (tmp_path / "tests" / "hooks" / "test_env_names.py").write_text('X = "MM_EXAMPLE_TOO"\n')
    monkeypatch.setattr(doc_check, "ROOT", tmp_path)
    names = doc_check.env_code_names(
        ["e.sh", "scripts/doc-check.py", "tests/hooks/test_env_names.py"])
    assert names == {}


def test_both_directions_fail() -> None:
    page = {"old": {"MM_KNOWN", "MM_STALE", "MM_NATS_SERVER"},
            "new": {"CB_KNOWN", "CB_STALE", "CB_NATS_SERVER"},
            "registrar": {"MM_REG_SECRET"}, "not_env": {"MM_GLOBAL"}, "excluded": {"HOME"},
            "sections": {"Tests and CI": {"MM_NATS_SERVER"}}}
    read = {"MM_KNOWN": "x.py", "MM_GLOBAL": "y.sh", "MM_REG_SECRET": "z.py",
            "MM_UNKNOWN": "w.py"}
    # MM_NATS_SERVER is a test setting: only the "every" text holds it
    got = doc_check.env_name_errors(page, read, ("MM_KNOWN MM_GLOBAL MM_REG_SECRET", "MM_NATS_SERVER"))
    assert got == [
        ("w.py", "reads MM_UNKNOWN, which docs/env-names.md does not list"),
        ("docs/env-names.md", "maps MM_STALE, which no code mentions"),
    ]


def test_a_whole_token_is_required() -> None:
    """`AUTH_LOBBY` must not be satisfied by `CB_AUTH_LOBBY` or by a comment's
    look-alike (#177 review: the substring test hid a removed fallback)."""
    page = {"old": {"AUTH_LOBBY"}, "new": {"CB_AUTH_LOBBY"}, "registrar": set(),
            "not_env": set(), "excluded": set(), "sections": {}}
    assert doc_check.env_name_errors(page, {}, ("CB_AUTH_LOBBY", "CB_AUTH_LOBBY")) != []
    assert doc_check.env_name_errors(page, {}, ("AUTH_LOBBY", "AUTH_LOBBY")) == []


def test_nothing_is_reported_when_both_sides_agree() -> None:
    page = {"old": {"MM_KNOWN"}, "new": {"CB_KNOWN"}, "registrar": set(),
            "not_env": set(), "excluded": set(), "sections": {}}
    assert doc_check.env_name_errors(page, {"MM_KNOWN": "x.py"}, ("MM_KNOWN", "")) == []


def test_the_repository_passes_the_guard() -> None:
    """The real tree, both directions and the counts: this is what CI runs."""
    doc_check.errors.clear()
    doc_check.check_env_names()
    assert doc_check.errors == []


def test_a_page_with_conflict_markers_is_refused(tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    bad = tmp_path / "env-names.md"
    bad.write_text("<<<<<<< HEAD\n| `MM_A` | `CB_A` | no | x | |\n=======\n>>>>>>> main\n")
    monkeypatch.setattr(doc_check, "ENV_PAGE", bad)
    with pytest.raises(ValueError, match="conflict markers"):
        doc_check.env_page()


def test_import_meta_env_is_seen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    name = "VITE_" + "CONSOLE_PROBE"
    (tmp_path / "vite.config.ts").write_text(f"const u = import.meta.env.{name}\n")
    monkeypatch.setattr(doc_check, "ROOT", tmp_path)
    assert name in doc_check.env_code_names(["vite.config.ts"])


def test_a_tests_fixture_is_no_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fixtures name fake settings (`CB_AUTH_TYPO`); a rename PR must not have to
    park them as exclusion rows (#177 review)."""
    (tmp_path / "a.test.ts").write_text('const x = "CB_TEST_FAKE"\n')
    (tmp_path / "test_probe.py").write_text('X = getenv("CB_AUTH_TYPO", "")\n')
    (tmp_path / "real.py").write_text('X = getenv("CB_REAL_SETTING", "")\n')
    monkeypatch.setattr(doc_check, "ROOT", tmp_path)
    names = doc_check.env_code_names(["a.test.ts", "test_probe.py", "real.py"])
    assert names == {"CB_REAL_SETTING": "real.py"}


def _page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> list[str]:
    page = tmp_path / "env-names.md"
    page.write_text(body)
    monkeypatch.setattr(doc_check, "ENV_PAGE", page)
    return doc_check.env_page()["problems"]


def test_a_row_the_guard_cannot_read_is_reported(tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    problems = _page(tmp_path, monkeypatch, """### The MCP server and the plugin (2)

| Old name | New name | Mattermost | Where it is read | Note |
|---|---|---|---|---|
| `MM_GOOD` | `CB_GOOD` | no | x | |
| `MM_BAD` | `CB_BAD` | maybe | x | |
""")
    assert any("MM_BAD" in p or "a row is not" in p for p in problems)


def test_a_reasonless_exclusion_is_reported(tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    problems = _page(tmp_path, monkeypatch, """### Excluded names

| Name | Why it is not renamed |
|---|---|
| `HOME` | another product's |
| `MM_NOWHY` |  |
""")
    assert any("MM_NOWHY" in p and "no reason" in p for p in problems)


def test_a_heading_count_is_checked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    page = tmp_path / "env-names.md"
    page.write_text("### The MCP server and the plugin (3)\n\n"
                    "| `MM_ONE` | `CB_ONE` | no | x | |\n")
    monkeypatch.setattr(doc_check, "ENV_PAGE", page)
    assert doc_check.env_counts() == [("The MCP server and the plugin", 3, 1)]


def test_every_cb_name_the_page_lists_is_known_to_the_servers_unknown_name_warning() -> None:
    """#261: the MCP server warns about a CB_ name it does not know; a name that
    docs/env-names.md lists must never be one of them (the server's own list is in
    config.py: RENAMED, INTERNAL, OTHER and the other components' prefixes)."""
    from mattermost_ai_agent_bus import config

    page = doc_check.env_page()
    missing = sorted(n for n in page["new"] | page["not_env"]
                     if n.startswith("CB_") and n != "CB_MM_" and not config.known(n))
    assert missing == []
