"""The doc map in docs/doc-map.toml, and how scripts/doc-check.py reads it.

The rules decide which PRs must touch a doc, so they gate every merge. This file
pins what the map says and what the matcher does, so a rule that is widened by
accident shows up as a failing test rather than as a surprising label request on
some later PR.

Run where the other script tests run: uv run --directory mcp pytest ../tests/hooks
"""

from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DOC_MAP = ROOT / "docs" / "doc-map.toml"


def _doc_check():
    spec = importlib.util.spec_from_file_location("doc_check", ROOT / "scripts" / "doc-check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


doc_check = _doc_check()


def rules() -> dict[str, dict]:
    """The doc map by rule name, so a test names a rule and not its position."""
    return {r["name"]: r for r in tomllib.loads(DOC_MAP.read_text())["rule"]}


def hits(rule: dict, *changed: str) -> list[str]:
    return doc_check.doc_map_hits(rule, set(changed))


# --- the matcher --------------------------------------------------------------

def test_a_rule_answers_for_its_paths():
    rule = {"name": "t", "paths": ["bin/*"], "docs": ["docs/x.md"]}
    assert hits(rule, "bin/mm-agent-lib.sh") == ["bin/mm-agent-lib.sh"]
    assert hits(rule, "docs/arc42/06-runtime-view.md") == []


def test_a_star_covers_slashes_too():
    """The documented behaviour of the globs: `*` also matches `/`, so one rule
    covers a whole tree without a `**`."""
    rule = {"name": "t", "paths": ["services/auth/*"], "docs": ["docs/x.md"]}
    assert hits(rule, "services/auth/cmd/authd/main.go") == ["services/auth/cmd/authd/main.go"]
    assert hits(rule, "services/auth/internal/token/verify.go") == [
        "services/auth/internal/token/verify.go"
    ]


def test_the_hits_are_sorted_and_distinct():
    rule = {"name": "t", "paths": ["bin/*", "hooks/*"], "docs": ["docs/x.md"]}
    assert hits(rule, "hooks/b.sh", "bin/z.sh", "bin/a.sh") == [
        "bin/a.sh",
        "bin/z.sh",
        "hooks/b.sh",
    ]


def test_exclude_takes_a_file_back_out_of_the_rule():
    rule = {"name": "t", "paths": ["services/auth/*"], "docs": ["docs/x.md"],
            "exclude": ["*_test.go"]}
    assert hits(rule, "services/auth/cmd/authd/signal_test.go") == []
    assert hits(rule, "services/auth/cmd/authd/main.go") == ["services/auth/cmd/authd/main.go"]


def test_exclude_only_applies_after_paths():
    """A file the rule never claimed stays out, whatever exclude says."""
    rule = {"name": "t", "paths": ["bin/*"], "docs": ["docs/x.md"], "exclude": ["*_test.go"]}
    assert hits(rule, "tests/hooks/test_x.py") == []


def test_several_excludes():
    rule = {"name": "t", "paths": ["services/auth/*"], "docs": ["docs/x.md"],
            "exclude": ["*_test.go", "*/testdata/*"]}
    assert hits(rule, "services/auth/a/b_test.go") == []
    assert hits(rule, "services/auth/a/testdata/keys.json") == []
    assert hits(rule, "services/auth/a/b.go") == ["services/auth/a/b.go"]


def test_a_rule_without_exclude_still_works():
    """The key is optional: every rule written before it existed keeps its meaning."""
    rule = {"name": "t", "paths": ["bin/*"], "docs": ["docs/x.md"]}
    assert hits(rule, "bin/mm-agent-sweep.sh") == ["bin/mm-agent-sweep.sh"]


# --- the map that ships -------------------------------------------------------

def test_every_rule_is_well_formed():
    for name, rule in rules().items():
        assert rule["paths"], name
        assert rule["docs"], name


def test_every_rule_has_a_doc_that_exists():
    """A rule pointing at a doc that was renamed never asks for an update."""
    for name, rule in rules().items():
        for doc in rule["docs"]:
            assert (ROOT / doc).is_file(), f"{name} names {doc}, which does not exist"


def test_the_login_service_rule_ignores_its_own_tests():
    """The finding from PR #12: a PR that only changes Go tests under
    services/auth/ must not be asked for a doc change. Its product code still is."""
    rule = rules()["login service: configuration, health, image"]
    assert hits(rule, "services/auth/cmd/authd/signal_test.go") == []
    assert hits(rule, "services/auth/internal/token/verify_test.go") == []
    assert hits(rule, "services/auth/internal/token/testdata/keys.json") == []
    assert hits(rule, "services/auth/cmd/authd/signal_test.go",
                 "services/auth/internal/config/config.go") == [
        "services/auth/internal/config/config.go"
    ]


def test_the_other_rules_keep_their_meaning():
    """Only the login service needed the exclusion: the harness plugins' tests sit
    in the same tree as the code they drive and stay covered, as do the shell and
    MCP rules."""
    by_name = rules()
    assert "exclude" not in by_name["OpenCode plugin: turn cycle in OpenCode sessions"]
    assert "exclude" not in by_name["pi extension: turn cycle in pi sessions"]
    assert "exclude" not in by_name["shell CLI"]
    assert "exclude" not in by_name["MCP server: tools, inbox, instructions"]
    assert hits(by_name["shell CLI"], "bin/mm-agent-lib.sh")
    assert hits(by_name["MCP server: tools, inbox, instructions"],
                "mcp/src/mattermost_ai_agent_bus/server.py")


def test_the_version_rule_answers_for_the_plugin_json():
    rule = rules()["version bump"]
    assert hits(rule, ".claude-plugin/plugin.json") == [".claude-plugin/plugin.json"]
    assert rule["docs"] == ["CHANGELOG.md"]
