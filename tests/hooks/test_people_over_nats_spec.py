"""The v1 bodies of people's posts onto NATS (RT-6.42), as docs/protocol.md shows them.

The MCP server's reader is built and tested in mcp/tests/test_people_over_nats.py;
the bridge's step 2 brings a Go test beside the bridge. Until both stand, this one
pins the contract the code packages share,
so an edit of the examples that breaks a rule of the spec fails here: every body
is format v1 with a known kind, carries the Mattermost post id, has no `props`
and only the fields its kind allows, the field table lists exactly
those, and the examples hold no duplicate key.

Run where the other script tests run: uv run --directory mcp pytest ../tests/hooks
"""

from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PROTOCOL = ROOT / "docs" / "protocol.md"
SECTION = "## Over NATS: people's posts and reactions"

ALLOWED = {
    "post": {"v", "kind", "text", "name", "mm", "mm_root", "root", "files", "cut"},
    "reaction": {"v", "kind", "emoji", "name", "mm", "ref"},
    "unreaction": {"v", "kind", "emoji", "name", "mm", "ref"},
}
MM_ID = re.compile(r"[a-z0-9]{26}")


def section() -> str:
    return PROTOCOL.read_text().split(SECTION, 1)[1].split("\n## ", 1)[0]


def no_duplicates(pairs: list[tuple[str, object]]) -> dict:
    keys = [k for k, _ in pairs]
    assert len(keys) == len(set(keys)), f"duplicate key in {keys}"
    return dict(pairs)


def bodies() -> list[dict]:
    block = section().split("```json\n", 1)[1].split("```", 1)[0]
    # One body per JSON object; an object may span lines.
    found, depth, start = [], 0, 0
    for i, ch in enumerate(block):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                found.append(json.loads(block[start:i + 1], object_pairs_hook=no_duplicates))
    return found


def field_table() -> dict[str, set[str]]:
    """The protocol's field table: field -> the kinds it is "In"."""
    table: dict[str, set[str]] = {}
    for line in section().splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 3 and cells[0].startswith("`"):
            kinds = {k.strip() for k in cells[1].split(",")}
            table[cells[0].strip("`")] = set(ALLOWED) if kinds == {"all"} else kinds
    return table


def test_every_kind_has_an_example():
    assert sorted(b["kind"] for b in bodies()) == ["post", "reaction", "unreaction"]


def test_the_field_table_says_what_the_spec_allows():
    by_kind: dict[str, set[str]] = {k: set() for k in ALLOWED}
    for field, kinds in field_table().items():
        for kind in kinds:
            by_kind[kind].add(field)
    assert by_kind == ALLOWED


def test_a_duplicate_key_is_refused():
    try:
        json.loads('{"v": 1, "v": 1}', object_pairs_hook=no_duplicates)
    except AssertionError:
        return
    raise AssertionError("a duplicate key passed")


def test_bodies_keep_the_rules_of_the_spec():
    for body in bodies():
        assert body["v"] == 1
        assert set(body) <= ALLOWED[body["kind"]], body
        assert "props" not in body
        assert MM_ID.fullmatch(body["mm"])
        if "mm_root" in body:
            assert MM_ID.fullmatch(body["mm_root"])


def test_the_subject_is_its_own_and_names_the_person_last():
    assert "<bus>.hu.<home>.<anchor>" in section()
