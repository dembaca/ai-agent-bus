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


def test_a_malformed_rule_is_reported():
    """A string where a list belongs would be iterated character by character, and
    the rule would silently claim nothing. Reported, not tolerated."""
    good = {"name": "t", "paths": ["bin/*"], "docs": ["docs/x.md"]}
    assert doc_check.doc_map_rule_error(good) == ""
    assert doc_check.doc_map_rule_error({**good, "exclude": ["*_test.go"]}) == ""
    assert doc_check.doc_map_rule_error({**good, "exclude": "*_test.go"}) == (
        "exclude must be a list of strings"
    )
    assert doc_check.doc_map_rule_error({**good, "exclude": [1]}) == (
        "exclude must be a list of strings"
    )
    assert doc_check.doc_map_rule_error({**good, "paths": "bin/*"}) == (
        "paths must be a list of strings"
    )
    assert doc_check.doc_map_rule_error({**good, "docs": "docs/x.md"}) == (
        "docs must be a list of strings"
    )


def test_a_rule_missing_a_required_key_is_reported_by_name():
    """Missing keys used to pass validation and then die with a bare KeyError
    further down, without the rule's name."""
    good = {"name": "t", "paths": ["bin/*"], "docs": ["docs/x.md"]}
    for missing in ("name", "paths", "docs"):
        rule = {k: v for k, v in good.items() if k != missing}
        assert doc_check.doc_map_rule_error(rule) == f"{missing} is missing", missing


def test_an_unknown_key_is_reported():
    """A misspelled optional key (`excludes`) leaves the rule claiming what it
    should exempt, and nothing would say so."""
    good = {"name": "t", "paths": ["bin/*"], "docs": ["docs/x.md"]}
    msg = doc_check.doc_map_rule_error({**good, "excludes": ["*_test.go"]})
    assert msg.startswith("unknown key excludes") and "allowed:" in msg
    assert doc_check.doc_map_rule_error({**good, "naem": "x"}).startswith("unknown key naem")


def test_an_empty_paths_or_docs_is_reported():
    """An empty list makes the rule claim nothing while looking well formed."""
    good = {"name": "t", "paths": ["bin/*"], "docs": ["docs/x.md"]}
    assert doc_check.doc_map_rule_error({**good, "paths": []}) == "paths must not be empty"
    assert doc_check.doc_map_rule_error({**good, "docs": []}) == "docs must not be empty"
    assert doc_check.doc_map_rule_error({**good, "name": ""}) == "name must be a non-empty string"


# --- the wiring in check_doc_map ------------------------------------------------

def _run_check(monkeypatch, tmp_path, toml: str, changed: list[str]):
    """Run the real check_doc_map on a map we write, with git answering `changed`.
    Proves the validator is called and its verdict reaches the exit state, not
    only that the function returns the right string."""
    doc_map = tmp_path / "doc-map.toml"
    doc_map.write_text(toml)
    (tmp_path / "docs").mkdir(exist_ok=True)
    (tmp_path / "docs" / "x.md").write_text("x")
    monkeypatch.setattr(doc_check, "DOC_MAP", doc_map)
    monkeypatch.setattr(doc_check, "ROOT", tmp_path)

    def fake_git(*args: str) -> str:
        if args[:1] == ("merge-base",):
            return "base\n"
        return "\n".join(changed)

    monkeypatch.setattr(doc_check, "git", fake_git)
    saved, doc_check.errors = doc_check.errors, []
    try:
        doc_check.check_doc_map("origin/main")
        return list(doc_check.errors)
    finally:
        doc_check.errors = saved


def test_check_doc_map_fails_on_a_malformed_rule(monkeypatch, tmp_path):
    """The mutation the review named: a validator that only warns, or one never
    called, would leave this green."""
    errs = _run_check(monkeypatch, tmp_path, """
[[rule]]
name = "t"
paths = ["bin/*"]
docs = ["docs/x.md"]
exclude = "*_test.go"
""", ["bin/a.sh"])
    assert errs, "check_doc_map accepted a malformed rule"
    assert "t" in errs[0][2] and "exclude must be a list of strings" in errs[0][2]


def test_check_doc_map_fails_on_a_rule_missing_docs(monkeypatch, tmp_path):
    """Previously a KeyError inside check_doc_map instead of a named error."""
    errs = _run_check(monkeypatch, tmp_path, """
[[rule]]
name = "no docs here"
paths = ["bin/*"]
""", ["bin/a.sh"])
    assert errs and "no docs here" in errs[0][2] and "docs is missing" in errs[0][2]


def test_check_doc_map_still_passes_a_good_rule(monkeypatch, tmp_path):
    """Counter-check: a well-formed rule whose doc changed records nothing."""
    errs = _run_check(monkeypatch, tmp_path, """
[[rule]]
name = "t"
paths = ["bin/*"]
docs = ["docs/x.md"]
""", ["bin/a.sh", "docs/x.md"])
    assert errs == [], errs


def test_check_doc_map_still_asks_for_a_doc(monkeypatch, tmp_path):
    """And the rule's own purpose still works: code changed, no doc, one error."""
    errs = _run_check(monkeypatch, tmp_path, """
[[rule]]
name = "t"
paths = ["bin/*"]
docs = ["docs/x.md"]
""", ["bin/a.sh"])
    assert len(errs) == 1 and "bin/a.sh changed" in errs[0][2], errs


# --- the map that ships -------------------------------------------------------

def test_every_rule_is_well_formed():
    for name, rule in rules().items():
        assert rule["paths"], name
        assert rule["docs"], name
        assert doc_check.doc_map_rule_error(rule) == "", name


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


def test_product_code_with_test_in_its_name_stays_covered():
    """The exclusion must not be widened into "*test*": that would silently drop
    services/auth/internal/testrealm/realm.go, real code that happens to live in a
    directory with 'test' in its name. Pinned so a later loosening fails here."""
    rule = rules()["login service: configuration, health, image"]
    real_code = [
        "services/auth/internal/testrealm/realm.go",   # exists
        "services/auth/internal/contest/handler.go",   # shape of the same trap
        "services/auth/internal/latest/reader.go",     # ditto
    ]
    for path in real_code:
        assert hits(rule, path) == [path], path

    # The same paths under an over-broad exclude would fall out, which is what this
    # guards against: prove the test would notice.
    broad = {**rule, "exclude": ["*test*"]}
    assert hits(broad, "services/auth/internal/testrealm/realm.go") == []


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


def test_the_testrig_rule_points_at_adr_014():
    """The fault-run rig measures ADR-014's phase 1 and 4 rows: a change to it asks for
    the decision record, its own tests and README do not."""
    rule = rules()["fault-run rig: transports, faults, measurands"]
    assert rule["docs"] == ["docs/arc42/09-architecture-decisions.md"]
    assert hits(rule, "testrig/src/bus_testrig/natsrig.py") == ["testrig/src/bus_testrig/natsrig.py"]
    assert hits(rule, "testrig/measurands.toml") == ["testrig/measurands.toml"]
    assert hits(rule, "testrig/tests/test_natsrig.py") == []
    assert hits(rule, "testrig/README.md") == []
