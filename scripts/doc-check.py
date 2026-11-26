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
  env-names  every environment name the code reads is in docs/env-names.md, and
             every old name that page maps is mentioned in the code

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


# --- environment names ------------------------------------------------------

ENV_PAGE = ROOT / "docs" / "env-names.md"
# The prefixes the project defines and the one it moves to (docs/env-names.md).
ENV_PREFIX = r"(?:MM|CB|AUTH|AUTHD|BRIDGE|CONSOLE|MATTERMOST)"
ENV_NAME = re.compile(rf"\b({ENV_PREFIX}_[A-Z0-9_]+)")
# A quoted literal: "MM_X", 'AUTH_Y', `CB_Z`, and a Go struct tag's value
# (`env:"AUTH_X,required"` — the name ends before the comma).
ENV_QUOTED = re.compile(rf"""["'`]({ENV_PREFIX}_[A-Z0-9_]+)(?=[,`"'])""")
# Dot access carries no quotes: process.env.MM_X, import.meta.env.CONSOLE_X.
ENV_DOT = re.compile(rf"(?:process|import\.meta)\.env\.({ENV_PREFIX}_[A-Z0-9_]+)")
# Code that is scanned, by how a name appears there: quoted literals in the
# languages, any occurrence in the shell and in configuration files.
ENV_QUOTED_SUFFIXES = (".py", ".go", ".ts", ".tsx", ".mjs", ".js")
ENV_ANY_SUFFIXES = (".sh", ".sh.example", ".yml", ".yaml", ".json", ".conf", ".service")
ENV_ANY_NAMES = ("Dockerfile",)  # exact file names, and the shell without a suffix
ENV_SKIP = ("docs/", "site/", "CHANGELOG.md")
# Tests may name a setting that no longer exists (a removed fallback, a
# fixture); the page-to-code direction must not be satisfied by them.
ENV_TEST = re.compile(r"(^|/)(tests?/|test_|.*_test\.(go|py)$)")
# The check's own two files quote names as examples; they must not count as reads.
ENV_SELF = ("scripts/doc-check.py", "tests/hooks/test_env_names.py")


def env_section(text: str, start: str) -> str:
    """The page's text from `start` to the next heading of the same or a higher
    level — the page puts tables under headings, and prose under them too."""
    i = text.find(start)
    if i < 0:
        return ""
    for m in re.finditer(r"^#{1,6} ", text[i + len(start):], re.MULTILINE):
        return text[i:i + len(start) + m.start()]
    return text[i:]


def env_page() -> dict[str, set[str]]:
    """What docs/env-names.md lists, by kind.

    ``old``/``new``: the mapping tables' two columns. ``registrar``: the names
    the page keeps because they go with the registrar. ``not_env``: the shell
    globals the page says are no environment. ``excluded``: the table of names
    that are not the project's to rename, each with its reason.
    """
    text = ENV_PAGE.read_text()
    if re.search(r"^(<<<<<<<|=======|>>>>>>>)", text, re.MULTILINE):
        raise ValueError("the page holds unresolved merge conflict markers")
    old: set[str] = set()
    new: set[str] = set()
    sections: dict[str, set[str]] = {}
    title = ""
    for line in text.splitlines():
        h = re.match(r"^#{3,4} (.+?)(?: \(\d+\))?$", line)
        if h:
            title = h.group(1)
            continue
        m = re.match(r"\|\s*`([A-Z][A-Z0-9_]*)`\s*\|\s*`([A-Z][A-Z0-9_]*)`\s*\|\s*"
                     r"(?:yes|no)\s*\|", line)
        if m:
            old.add(m.group(1))
            new.add(m.group(2))
            sections.setdefault(title, set()).add(m.group(1))
    registrar = set(re.findall(r"\|\s*`([A-Z][A-Z0-9_]+)`\s*\|",
                               env_section(text, "### Goes with the registrar")))
    not_env = set(ENV_NAME.findall(env_section(text, "#### Shell variables that are not environment")))
    not_env -= {"CB", "CB_MM"}  # the section names the two prefixes themselves
    excluded = set(re.findall(r"\|\s*`([A-Za-z][A-Za-z0-9_]+)`\s*\|",
                              env_section(text, "### Excluded names")))
    return {"old": old, "new": new, "registrar": registrar, "not_env": not_env,
            "excluded": excluded, "sections": sections}


def env_counts() -> list[tuple[str, int, int]]:
    """Each table's heading and the rows it really holds, for the page's own
    count in the heading: ``(heading, stated, parsed)``."""
    text = ENV_PAGE.read_text()
    out = []
    for m in re.finditer(r"^(#{3,4}) (.+?) \((\d+)\)$", text, re.MULTILINE):
        level, title, stated = m.group(1), m.group(2), int(m.group(3))
        body = env_section(text, f"{level} {title} ({stated})")
        rows = len(re.findall(r"^\|\s*`", body, re.MULTILINE))
        out.append((title, stated, rows))
    return out


def env_is_test(path: str) -> bool:
    return bool(ENV_TEST.search(path))


def env_scan_kind(path: str) -> str:
    """``quoted``, ``any`` or ``""`` (not scanned), by the file's suffix/name."""
    name = path.rsplit("/", 1)[-1]
    if "." not in name and (path.startswith(("bin/", "hooks/", "adapters/"))
                            or name in ENV_ANY_NAMES):
        return "any"  # the shell without a suffix, and the image files
    if name.endswith(ENV_QUOTED_SUFFIXES):
        return "quoted"
    if name.endswith(ENV_ANY_SUFFIXES) or name.startswith(ENV_ANY_NAMES):
        return "any"
    return ""


def env_code_names(files: list[str]) -> dict[str, str]:
    """The environment names the code reads, each with the file that reads it.

    A name counts as read where it can reach the environment: a quoted literal
    in the languages (`get("AUTH_X")`, `env.Get("CB_BRIDGE_X", "BRIDGE_X")`,
    `process.env["CONSOLE_X"]`, a Go tag's `env:"X,required"`), any occurrence in
    the shell (`$NAME`, `${NAME…}`, `export NAME=`, `unset NAME`, `read -r NAME`,
    `printenv NAME`, `jq`'s `env.NAME`) and in the configuration files the
    example ships (Dockerfiles, workflow YAML, `hooks.json`). A name that only
    the page's exclusion table keeps quiet is not read.
    """
    read: dict[str, str] = {}
    for path in files:
        if path.startswith(ENV_SKIP):
            continue
        kind = env_scan_kind(path)
        if not kind:
            continue
        try:
            text = (ROOT / path).read_text(errors="ignore")
        except OSError:
            continue
        found = (list(ENV_QUOTED.finditer(text)) + list(ENV_DOT.finditer(text))
                 if kind == "quoted" else list(ENV_NAME.finditer(text)))
        for m in found:
            name = m.group(1)
            if name.endswith("_") or path in ENV_SELF:
                continue  # a stem in a pattern, or the check's own examples
            read.setdefault(name, path)
    return read


def env_code_text(files: list[str]) -> tuple[str, str]:
    """The code files' text for the page-to-code direction: (production, all).

    A removed fallback must not be kept alive by a test that still names it, so
    the mapping tables are held against production code only; the "Tests and CI"
    table's names live in tests, so those are held against every code file.
    """
    prod: list[str] = []
    every: list[str] = []
    for path in files:
        if path.startswith(ENV_SKIP) or not env_scan_kind(path):
            continue
        try:
            text = (ROOT / path).read_text(errors="ignore")
        except OSError:
            continue
        every.append(text)
        if not env_is_test(path):
            prod.append(text)
    return "\n".join(prod), "\n".join(every)


def env_name_errors(page: dict[str, set[str]], read: dict[str, str],
                    code_text: tuple[str, str]) -> list[tuple[str, str]]:
    """Both directions between the page and the code, as ``(where, what)``.

    A name the code reads and the page does not list loses its `CB_` mapping
    when the rename comes; a mapping whose old name appears nowhere in the code
    (tests aside) is stale — a removed setting, or a typo in the table. Whole
    tokens only: `AUTH_LOBBY` must not be satisfied by `CB_AUTH_LOBBY`.
    """
    documented = set().union(page["old"], page["new"], page["registrar"],
                             page["not_env"], page["excluded"])
    out = [(path, f"reads {name}, which docs/env-names.md does not list")
           for name, path in sorted(read.items()) if name not in documented]
    prod, every = code_text
    tests = page["sections"].get("Tests and CI", set())
    for name in sorted(page["old"]):
        where = every if name in tests else prod
        if not re.search(rf"\b{re.escape(name)}\b", where):
            out.append((str(ENV_PAGE.relative_to(ROOT)), f"maps {name}, which no code mentions"))
    return out


def check_env_names() -> None:
    try:
        page = env_page()
    except ValueError as exc:
        error(ENV_PAGE, None, str(exc))
        return
    if len(page["old"]) < 100 or len(page["registrar"]) != 10:
        error(ENV_PAGE, None, f"parsed {len(page['old'])} mapped and "
                              f"{len(page['registrar'])} registrar names: the tables did not read")
        return
    for title, stated, parsed in env_counts():
        if stated != parsed:
            error(ENV_PAGE, None, f"{title}: the heading says {stated} names, the table holds {parsed}")
    files = repo_files()
    read = env_code_names(files)
    for where, what in env_name_errors(page, read, env_code_text(files)):
        error(where, None, what)


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
    check_env_names()

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
