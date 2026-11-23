#!/usr/bin/env python3
"""Keep code, runtime texts and docs/arc42 in step.

    python3 scripts/doc-check.py                     # static checks only
    python3 scripts/doc-check.py --base origin/main  # plus the doc map
    python3 scripts/doc-check.py --base origin/main --skip-doc-map

Complements the vendored arc42 linter (scripts/arc42-lint/), which checks the
docs against each other. This checks what it cannot see:

  doc-map    a changed code path has its doc changed too (docs/doc-map.toml)
  blocks     Section 6 participants are building blocks from Section 5
  rt-ids     every RT-6.x referenced anywhere exists as a Section 6 scenario,
             and every scenario is referenced by at least one test in tests/, mcp/tests/ or a Go test under services/
  changelog  the plugin version has an entry in CHANGELOG.md, and its
             `Needs:` and `Withdrawn:` lines keep their grammar (RT-6.30)
  versions   the MCP package (pyproject.toml, __version__) has the plugin version

Exit 0 when clean, 1 on any error. Stdlib only.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARC42 = ROOT / "docs" / "arc42"
SEC5 = ARC42 / "05-building-block-view.md"
SEC6 = ARC42 / "06-runtime-view.md"
DOC_MAP = ROOT / "docs" / "doc-map.toml"
PLUGIN = ROOT / ".claude-plugin" / "plugin.json"
CHANGELOG = ROOT / "CHANGELOG.md"
PYPROJECT = ROOT / "mcp" / "pyproject.toml"
PKG_INIT = ROOT / "mcp" / "src" / "mattermost_ai_agent_bus" / "__init__.py"

# Files that may mention RT ids without being a reference (the checker itself).
RT_SCAN_EXCLUDE = ("scripts/doc-check.py",)

RT_REF = re.compile(r"\bRT-(6\.\d+)\b")

errors: list[tuple[str, int | None, str]] = []


def error(path: Path | str, line: int | None, msg: str) -> None:
    rel = str(Path(path).resolve().relative_to(ROOT)) if isinstance(path, Path) else path
    errors.append((rel, line, msg))


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout


def repo_files() -> list[str]:
    out = git("ls-files", "--cached", "--others", "--exclude-standard")
    return [f for f in out.splitlines() if (ROOT / f).is_file()]


# --- doc-map ----------------------------------------------------------------

DOC_MAP_REQUIRED = ("name", "paths", "docs")
DOC_MAP_OPTIONAL = ("exclude",)
DOC_MAP_KEYS = DOC_MAP_REQUIRED + DOC_MAP_OPTIONAL


def doc_map_rule_error(rule: dict) -> str:
    """What is wrong with a rule's shape, '' when nothing is.

    A malformed rule is reported rather than tolerated: `exclude` as a string
    instead of a list would be iterated character by character, so `"*_test.go"`
    excludes nothing specific and the rule silently stops claiming any file — a
    check that never fires is worse than one that complains.

    Required keys are checked here too, not left to fail later: a rule without
    `paths` or `docs` used to pass validation and then die with a bare `KeyError`
    somewhere below, without the rule's name. An empty `paths`, an unknown key
    and a misspelled optional one (`excludes`) are refused for the same reason:
    each one makes the rule claim nothing while looking healthy.
    """
    for key in DOC_MAP_REQUIRED:
        if key not in rule:
            return f"{key} is missing"
    unknown = sorted(set(rule) - set(DOC_MAP_KEYS))
    if unknown:
        return f"unknown key {', '.join(unknown)} (allowed: {', '.join(DOC_MAP_KEYS)})"
    if not isinstance(rule["name"], str) or not rule["name"]:
        return "name must be a non-empty string"
    for key in ("paths", "docs", "exclude"):
        if key not in rule:
            continue
        if not isinstance(rule[key], list) or not all(isinstance(v, str) for v in rule[key]):
            return f"{key} must be a list of strings"
    if not rule["paths"]:
        return "paths must not be empty"
    if not rule["docs"]:
        return "docs must not be empty"
    return ""


def doc_map_hits(rule: dict, changed: set[str]) -> list[str]:
    """The changed files a rule answers for, sorted.

    A file is in when its path matches one of the rule's `paths` and none of its
    optional `exclude` patterns. Both are fnmatch globs on repo-relative paths,
    as docs/doc-map.toml says: `*` also matches `/`. `exclude` is per rule, so a
    tree can exempt its tests without loosening another rule's claim on the same
    kind of file (the harness plugins' tests stay covered).
    """
    exclude = rule.get("exclude", [])
    return sorted(
        f for f in changed
        if any(fnmatch.fnmatch(f, p) for p in rule["paths"])
        and not any(fnmatch.fnmatch(f, x) for x in exclude)
    )


def check_doc_map(base: str) -> None:
    merge_base = git("merge-base", base, "HEAD").strip()
    changed = set(git("diff", "--name-only", merge_base).splitlines())
    changed |= set(git("ls-files", "--others", "--exclude-standard").splitlines())
    rules = tomllib.loads(DOC_MAP.read_text())["rule"]
    for rule in rules:
        shape = doc_map_rule_error(rule)
        if shape:
            error(DOC_MAP, None, f"doc-map [{rule.get('name', '?')}]: {shape}")
            continue
        hits = doc_map_hits(rule, changed)
        if hits and not any(d in changed for d in rule["docs"]):
            error(
                hits[0], None,
                f"doc-map [{rule['name']}]: {', '.join(hits)} changed, but none of "
                f"{', '.join(rule['docs'])} — update the doc, or mark the change "
                "with the no-doc-change label",
            )


# --- blocks -------------------------------------------------------------------

def section5_blocks() -> set[str]:
    names, in_table = set(), False
    for line in SEC5.read_text().splitlines():
        if re.match(r"^\|\s*Name\s*\|", line):
            in_table = True
            continue
        if in_table:
            if not line.startswith("|"):
                break
            cell = line.split("|")[1].strip()
            if cell and not set(cell) <= set("-: "):
                names.add(cell)
    return names


def check_blocks() -> None:
    blocks = section5_blocks()
    for n, line in enumerate(SEC6.read_text().splitlines(), 1):
        m = re.match(r"^\*\*Participants:\*\*\s*(.+)$", line)
        if not m:
            continue
        internal = m.group(1).split(";")[0]
        for name in (p.strip() for p in internal.split(",")):
            if name and name not in blocks:
                error(SEC6, n, f'participant "{name}" is not a Section 5 building block '
                               f"({', '.join(sorted(blocks))}); put externals after ';'")


# --- rt-ids -------------------------------------------------------------------

def scenarios() -> dict[str, int]:
    found = {}
    for n, line in enumerate(SEC6.read_text().splitlines(), 1):
        m = re.match(r"^##\s+(6\.\d+)\s", line)
        if m:
            found[m.group(1)] = n
    return found


def check_rt_ids() -> None:
    defined = scenarios()
    tested: set[str] = set()
    for f in repo_files():
        if f in RT_SCAN_EXCLUDE or f.startswith("scripts/arc42-lint/"):
            continue
        try:
            text = (ROOT / f).read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for ref in RT_REF.findall(line):
                if ref not in defined:
                    error(f, n, f"RT-{ref} is not a scenario in {SEC6.relative_to(ROOT)}")
                elif f.startswith(("tests/", "mcp/tests/")) or (
                        f.startswith("services/") and f.endswith("_test.go")):
                    # Go services keep their tests beside the code.
                    tested.add(ref)
    for ref, n in defined.items():
        if ref not in tested:
            error(SEC6, n, f"scenario RT-{ref} has no test referencing it in tests/, mcp/tests/ or a services/ *_test.go")


# --- changelog ------------------------------------------------------------------

def check_changelog() -> None:
    version = json.loads(PLUGIN.read_text())["version"]
    if not CHANGELOG.exists() or not re.search(
        rf"^##\s+\[?{re.escape(version)}\]?(\s|$)", CHANGELOG.read_text(), re.MULTILINE
    ):
        error(CHANGELOG, None, f"no entry for plugin version {version}")


# The release rules (ADR-011 point 3, RT-6.30; CLAUDE.md, "Releasing"): a
# `Needs:` line at the start of a line in a version's entry says what that
# release needs first, since it reaches every agent's context, in the grammar
# the server checks it with: NEEDS_PATTERN and NEEDS_MAX of release.py, read
# from its source (this script imports nothing of the package). A
# `Withdrawn:` line names older releases.
RELEASE_PY = ROOT / "mcp" / "src" / "mattermost_ai_agent_bus" / "release.py"
_release_src = RELEASE_PY.read_text()
NEEDS_RE = re.compile(re.search(r'^NEEDS_PATTERN = r"(.*)"$', _release_src, re.MULTILINE)[1])
NEEDS_MAX = int(re.search(r"^NEEDS_MAX = (\d+)$", _release_src, re.MULTILINE)[1])
WITHDRAWN_RE = re.compile(r"^Withdrawn: (v\d+\.\d+\.\d+(?:, v\d+\.\d+\.\d+){0,9})$")


def needs_ok(text: str) -> bool:
    return 0 < len(text) <= NEEDS_MAX and NEEDS_RE.fullmatch(text) is not None


def _vkey(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def check_release_lines(text: str | None = None) -> None:
    entry = ""
    for n, line in enumerate((CHANGELOG.read_text() if text is None else text).splitlines(), 1):
        if line.startswith("## "):
            m = re.match(r"^## \[?(\d+\.\d+\.\d+)\]?\s*$", line)
            entry = m.group(1) if m else ""
        elif line.startswith("Needs:"):
            if not entry:
                error(CHANGELOG, n, "a Needs: line outside a version's entry")
            elif not line.startswith("Needs: ") or not needs_ok(line[7:]):
                error(CHANGELOG, n, "Needs: takes MM_* variables, versions x.y.z and lowercase "
                      f"words of at most 15 letters (each with , or .), at most {NEEDS_MAX} characters")
        elif line.startswith("Withdrawn:"):
            m = WITHDRAWN_RE.match(line)
            if not entry:
                error(CHANGELOG, n, "a Withdrawn: line outside a version's entry")
            elif not m:
                error(CHANGELOG, n, "Withdrawn: takes v<x.y.z>[, v<x.y.z>…] (at most ten)")
            elif any(_vkey(v[1:]) >= _vkey(entry) for v in m.group(1).split(", ")):
                error(CHANGELOG, n, f"Withdrawn: in {entry} names a release not older than {entry}")


# --- versions -------------------------------------------------------------------

def check_versions() -> None:
    version = json.loads(PLUGIN.read_text())["version"]
    pyproject = tomllib.loads(PYPROJECT.read_text())["project"]["version"]
    if pyproject != version:
        error(PYPROJECT, None, f"version {pyproject}, plugin is {version}")
    m = re.search(r'^__version__ = "([^"]+)"', PKG_INIT.read_text(), re.MULTILINE)
    if not m or m.group(1) != version:
        error(PKG_INIT, None, f"__version__ {m.group(1) if m else 'missing'}, plugin is {version}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", help="git ref to diff against for the doc map")
    ap.add_argument("--skip-doc-map", action="store_true",
                    help="the change needs no doc update (no-doc-change)")
    args = ap.parse_args()

    if args.base and not args.skip_doc_map:
        check_doc_map(args.base)
    check_blocks()
    check_rt_ids()
    check_changelog()
    check_release_lines()
    check_versions()

    gh = os.environ.get("GITHUB_ACTIONS") == "true"
    for path, line, msg in errors:
        if gh:
            loc = f"file={path}" + (f",line={line}" if line else "")
            print(f"::error {loc}::{msg}")
        else:
            print(f"{path}{f':{line}' if line else ''}: {msg}")
    if errors:
        print(f"doc-check: {len(errors)} error(s)", file=sys.stderr)
        return 1
    print("doc-check: ok", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
