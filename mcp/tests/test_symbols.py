"""One meaning, one name (model.Mark, Answer, StatusMark), and the characters byte for byte (arc42 §5.1)."""

import pytest

from mattermost_ai_agent_bus import a2a, symbols
from mattermost_ai_agent_bus.model import MAX_CHOICES, Answer, Mark, StatusMark

KEYCAP = chr(0xFE0F) + chr(0x20E3)


def test_no_meaning_has_two_names():
    """An answer is no text mark, a coordinator state is no crown mark: the enums share no member name."""
    names = [{m.name for m in e} for e in (Mark, Answer, StatusMark)]
    assert not (names[0] & names[1]) and not (names[0] & names[2])
    assert names[1] & names[2] == set()


def test_every_member_has_its_characters():
    assert {m for m in Mark} == set(symbols._MARKS)
    assert {a for a in Answer} == set(symbols._ANSWERS)
    assert {s for s in StatusMark} == set(symbols._STATUS)


def test_a_choice_is_an_answer():
    assert [Answer.choice(n) for n in (1, 2, 3, 4)] == [Answer.ONE, Answer.TWO, Answer.THREE, Answer.FOUR]
    for bad in (0, -1, MAX_CHOICES + 1):
        with pytest.raises(ValueError):
            Answer.choice(bad)


def test_the_wire_form_is_unchanged():
    assert a2a.symbol(Mark.CROWN) == "\U0001F451" and a2a.symbol(Mark.HEADER) == "\U0001F68C"
    assert a2a.answer_symbol(Answer.YES) == "\U0001F44D" and a2a.answer_symbol(Answer.NO) == "\U0001F44E"
    assert a2a.answer_marks(2) == ["\U0001F44D", "\U0001F44E"]
    assert a2a.answer_marks(4) == [f"{n}{KEYCAP}" for n in "1234"]
    assert a2a.status_emoji(StatusMark.COORDINATOR) == "crown" and a2a.status_emoji(StatusMark.BUS) == "satellite_antenna"


CROWN = "\U0001F451"


@pytest.mark.parametrize("text", ["| a | b |\n|---|---|\n| 1 | 2 |", "- one\n- two", "1. one", "# Title", "```\ncode\n```", "> quote"])
def test_crown_before_a_markdown_block_gets_its_own_line(text):
    """RT-6.6 step 3: a 👑 on the first line would stop a table or list rendering."""
    crowned = symbols.with_crown(text)
    assert crowned == f"{CROWN}\n\n{text}"
    assert symbols.strip_crown(crowned) == text
    assert symbols.parse_head(crowned).crowned and symbols.parse_head(crowned).body == text


def test_crown_before_plain_text_stays_on_the_line():
    assert symbols.with_crown("ACK got it") == f"{CROWN} ACK got it"
    assert symbols.strip_crown(f"{CROWN} ACK got it") == "ACK got it"
    m = symbols.status_head(symbols.with_crown("TASK do it"))
    assert m is not None and m[3] == "TASK"


def test_an_already_crowned_message_is_left_alone():
    for text in (f"{CROWN} | a |", f"{CROWN}\n\n| a |", f"{CROWN} x"):
        assert symbols.with_crown(text) == text


@pytest.mark.parametrize("body", ["| a | b |\n|---|---|\n| 1 | 2 |", "- one\n- two"])
def test_a_status_word_stands_alone_before_a_block(body):
    """RT-6.14 step 2: `RESULT · | a |` would break the table; the block starts below."""
    led = a2a.lead(body, "RESULT")
    assert led == f"RESULT\n\n{body}"
    assert a2a.styled(led) == f"`RESULT`\n\n{body}"
    assert a2a.styled(symbols.with_crown(led)) == f"{CROWN} `RESULT`\n\n{body}"
    assert a2a.lead("plain text", "RESULT") == "RESULT · plain text"
