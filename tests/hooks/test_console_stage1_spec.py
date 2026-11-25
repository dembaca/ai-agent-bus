"""The rest of the console's stage 1 (RT-6.43), as the console decision's
addition of 2026-11-25 (ADR-015) and docs/protocol.md specify it.

Nothing of RT-6.43 is built yet. This test pins the spec until the code's own
tests reference the scenario: the addition holds fifteen points, numbered
once each, every one with a recommendation and an alternative; the ask
record's example bodies carry exactly the fields of its table and no
duplicate key; the STATS example keeps the shape that lets no text ride
along; and the subjects name the agent last, where NATS checks it.

Run where the other script tests run: uv run --directory mcp pytest ../tests/hooks
"""

from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ADR = ROOT / "docs" / "arc42" / "09-architecture-decisions.md"
PROTOCOL = ROOT / "docs" / "protocol.md"
ADDITION = "**Addition, proposed on 2026-11-25: the rest of stage 1.**"
SECTION = "## Over NATS: the console's asks and STATS lines"

ASK_FIELDS = {"v", "kind", "state", "human", "title", "asked", "answered", "mm", "iat", "exp"}
STATS_STRINGS = {
    "v": re.compile(r"\d+\.\d+\.\d+"),
    "sid": re.compile(r"[0-9a-f]{1,32}"),
    "rt": re.compile(r"[a-z0-9-]{1,32}"),
    "kind": re.compile(r"session|autonomous"),
}
SHORT_KEY = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
MM_ID = re.compile(r"[a-z0-9]{26}")


def addition() -> str:
    text = ADR.read_text()
    assert text.count(ADDITION) == 1
    return text.split(ADDITION, 1)[1].split("\n---", 1)[0]


def points() -> dict[int, str]:
    found: dict[int, str] = {}
    parts = re.split(r"^ ?(\d+)\. \*\*", addition(), flags=re.M)
    for number, body in zip(parts[1::2], parts[2::2]):
        n = int(number)
        assert n not in found, f"point {n} twice"
        found[n] = body
    return found


def section() -> str:
    return PROTOCOL.read_text().split(SECTION, 1)[1].split("\n## ", 1)[0]


def no_duplicates(pairs: list[tuple[str, object]]) -> dict:
    keys = [k for k, _ in pairs]
    assert len(keys) == len(set(keys)), f"duplicate key in {keys}"
    return dict(pairs)


def objects(block: str) -> list[dict]:
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


def json_blocks() -> list[list[dict]]:
    return [objects(b.split("```", 1)[0]) for b in section().split("```json\n")[1:]]


def field_table() -> set[str]:
    fields = set()
    for line in section().splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 3 and cells[0].startswith("`"):
            fields.add(cells[0].strip("`"))
    return fields


def test_fifteen_points_each_with_a_recommendation_and_an_alternative():
    found = points()
    assert sorted(found) == list(range(1, 16))
    for n, body in found.items():
        assert "Recommended" in body, f"point {n} has no recommendation"
        assert "Alternative" in body, f"point {n} has no alternative"


def test_stage_1_answers_no_ask_and_the_person_publishes_nothing():
    found = points()
    assert "does not answer" in found[8]
    assert "allows no publish" in found[5]


def test_a_duplicate_key_is_refused():
    try:
        json.loads('{"v": 1, "v": 1}', object_pairs_hook=no_duplicates)
    except AssertionError:
        return
    raise AssertionError("a duplicate key passed")


def test_ask_records_carry_exactly_the_fields_of_the_table():
    asks, _ = json_blocks()
    assert field_table() == ASK_FIELDS
    assert sorted(a["state"] for a in asks) == ["ended", "open"]
    for ask in asks:
        assert set(ask) == ASK_FIELDS, ask
        assert ask["v"] == 1 and ask["kind"] in {"approval", "question"}
        assert len(ask["title"]) <= 200
        assert MM_ID.fullmatch(ask["mm"])
        assert ask["exp"] - ask["iat"] == 1800
        assert isinstance(ask["answered"], bool)


def _numbers_only(value: object) -> bool:
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return True
    if isinstance(value, list):
        return all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in value)
    if isinstance(value, dict):
        return all(SHORT_KEY.fullmatch(k) and _numbers_only(v) for k, v in value.items())
    return False


def test_a_stats_line_has_no_string_but_four_checked_ones():
    _, lines = json_blocks()
    assert len(lines) == 1
    for line in lines:
        assert len(json.dumps(line).encode()) <= 8192
        for key, value in line.items():
            if isinstance(value, str):
                assert key in STATS_STRINGS and STATS_STRINGS[key].fullmatch(value), key
            else:
                assert _numbers_only(value), key


def test_the_subjects_name_the_agent_where_nats_checks_it():
    text = section()
    assert "<presence>.ask.<home>.<name>.<ask>" in text
    assert "<stats>.<name>" in text
