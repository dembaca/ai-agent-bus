"""The v1 bodies of people's posts onto NATS (RT-6.42), as docs/protocol.md shows them.

RT-6.42 is specified, not built: the bridge's step 2 and the MCP server's reader
bring their own tests (a Go test beside the bridge, mcp/tests for the decoder) and
replace this one. Until then it pins the contract the code packages start from,
so an edit of the examples that breaks a rule of the spec fails here: every body
is format v1 with a known kind, carries the Mattermost post id, has no `props`
and only the fields its kind allows.

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
    "post": {"v", "kind", "text", "name", "mm", "mm_root", "root", "files"},
    "reaction": {"v", "kind", "emoji", "name", "mm", "ref"},
    "unreaction": {"v", "kind", "emoji", "name", "mm", "ref"},
}
MM_ID = re.compile(r"[a-z0-9]{26}")


def bodies() -> list[dict]:
    text = PROTOCOL.read_text()
    section = text.split(SECTION, 1)[1].split("\n## ", 1)[0]
    block = section.split("```json\n", 1)[1].split("```", 1)[0]
    # One body per JSON object; an object may span lines.
    found, depth, start = [], 0, 0
    for i, ch in enumerate(block):
        if ch == "{":
            depth, start = depth + 1, (i if depth == 0 else start)
        elif ch == "}":
            depth -= 1
            if depth == 0:
                found.append(json.loads(block[start:i + 1]))
    return found


def test_every_kind_has_an_example():
    assert sorted(b["kind"] for b in bodies()) == ["post", "reaction", "unreaction"]


def test_bodies_keep_the_rules_of_the_spec():
    for body in bodies():
        assert body["v"] == 1
        assert set(body) <= ALLOWED[body["kind"]], body
        assert "props" not in body
        assert MM_ID.fullmatch(body["mm"])
        if "mm_root" in body:
            assert MM_ID.fullmatch(body["mm_root"])


def test_the_subject_is_its_own_and_names_the_person_last():
    section = PROTOCOL.read_text().split(SECTION, 1)[1].split("\n## ", 1)[0]
    assert "<bus>.hu.<home>.<anchor>" in section
