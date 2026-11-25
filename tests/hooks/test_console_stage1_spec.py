"""The rest of the console's stage 1 (RT-6.43), as the console decision's
addition of 2026-11-25 (ADR-015) and docs/protocol.md specify it.

Of RT-6.43 the login service's rights and the STATS stream (services/auth)
and the MCP server's and the console's records, decoders and views
(mcp/tests/test_console_records.py, console/server/tests/test_asks_stats.py)
are built, and the login service's person credential and consumers
(services/auth); the console's use of that credential is not. This test pins the spec: the addition holds fifteen points, numbered
once each, every one with a recommendation and an alternative, and keeps its
bounds (200 characters, 20 asks in 4 KiB, 15 minutes, 30 days and 500 per
subject, the console's own `azp`); the ask record's example bodies carry
exactly the fields of its table and no duplicate key; the STATS example keeps
the shape and the string patterns that let no free text ride along; each
subject ends with the agent's name, where NATS checks it; and the scenario
and its risk (RISK-22) exist.

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

RECORD_FIELDS = {"v", "asks", "more", "iat", "exp"}
ASK_FIELDS = {"mm", "kind", "human", "title", "asked", "answered"}
STATS_STRINGS = {
    "v": r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,6}",
    "sid": r"[0-9a-f]{12}",
    "rt": r"claude-code|cursor|opencode|codex|pi|other",
    "kind": r"session|autonomous",
}
ANCHOR = re.compile(r"-|[a-z0-9][a-z0-9~_-]{0,63}")
MM_ID = re.compile(r"[a-z0-9]{26}")


def addition() -> str:
    text = ADR.read_text()
    assert text.count(ADDITION) == 1
    return text.split(ADDITION, 1)[1].split("\n---", 1)[0]


def points() -> dict[int, str]:
    found: dict[int, str] = {}
    parts = re.split(r"^ ?(\d+)\. \*\*", addition(), flags=re.M)
    for number, body in zip(parts[1::2], parts[2::2], strict=True):
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


def field_table() -> dict[str, set[str]]:
    """The ask record's field table: where each field is ("record" or "ask")."""
    table: dict[str, set[str]] = {"record": set(), "ask": set()}
    for line in section().splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 3 and cells[0].startswith("`") and cells[1] in table:
            table[cells[1]].add(cells[0].strip("`"))
    return table


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
    records, _ = json_blocks()
    assert field_table() == {"record": RECORD_FIELDS, "ask": ASK_FIELDS}
    assert [len(r["asks"]) for r in records] == [2, 0]
    for record in records:
        assert set(record) == RECORD_FIELDS, record
        assert record["v"] == 1 and record["exp"] - record["iat"] == 1800
        assert len(json.dumps(record).encode()) <= 4096 and len(record["asks"]) <= 20
        ids = [a["mm"] for a in record["asks"]]
        assert len(ids) == len(set(ids))
        for ask in record["asks"]:
            assert set(ask) == ASK_FIELDS, ask
            assert ask["kind"] in {"approval", "question"}
            assert len(ask["title"]) <= 200
            assert MM_ID.fullmatch(ask["mm"]) and ANCHOR.fullmatch(ask["human"])
            assert isinstance(ask["answered"], bool)


def _numbers_only(value: object) -> bool:
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return True
    if isinstance(value, list):
        return all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in value)
    if isinstance(value, dict):
        return all(_numbers_only(v) for v in value.values())
    return False


def test_a_stats_line_has_no_string_but_four_checked_ones():
    _, lines = json_blocks()
    assert len(lines) == 1
    text = section()
    for key in ("v", "sid"):
        assert f"| `{key}` | `{STATS_STRINGS[key]}` |" in text, key
    for key in ("rt", "kind"):
        assert all(f"`{word}`" in text for word in STATS_STRINGS[key].split("|")), key
    for line in lines:
        assert len(json.dumps(line).encode()) <= 8192
        for key, value in line.items():
            if isinstance(value, str):
                assert key in STATS_STRINGS and re.fullmatch(STATS_STRINGS[key], value), key
            else:
                assert _numbers_only(value), key


def test_the_subjects_end_with_the_agents_name():
    text = section()
    assert "<presence>.ask.<home>.<name>   " in text
    assert "<stats>.<name>   " in text
    assert "<presence>.ask.<home>.<name>." not in text


def test_the_bounds_are_pinned():
    found = points()
    assert "at most 200" in found[10] and "at most 20 asks" in found[10] and "4 KiB" in found[10]
    assert "15 minutes" in found[6] and "`azp`" in found[2]
    assert "device" in found[2] and "standard flow" in found[2]
    assert "30 days" in found[12] and "500" in found[12]
    assert "one subject per agent" in found[9]


def test_the_scenario_and_its_risk_exist():
    assert "## 6.43 The console on the bus as its operator" in (ROOT / "docs" / "arc42" / "06-runtime-view.md").read_text()
    assert "| RISK-22 |" in (ROOT / "docs" / "arc42" / "11-risks-and-technical-debt.md").read_text()
    assert "RT-6.43" in addition()


def test_a_dotted_anchor_counts_in_its_subject_form():
    assert ANCHOR.fullmatch("jane.doe".replace(".", "~"))
    assert not ANCHOR.fullmatch("jane.doe")
    assert "with every `.` written as `~`" in section()
