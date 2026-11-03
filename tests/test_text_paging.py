import pytest

from pulsara_agent.primitives.text_paging import TextPosition, paginate_text
from pulsara_agent.primitives.tool_result_budget import ToolResultPageBoundary


@pytest.mark.parametrize(
    "text", ["", "last", "first\nlast", "a\r\nb\rc\n", "\n\n", '中🙂\\"' * 100]
)
def test_text_pages_preserve_every_unicode_character_and_line_ending(text):
    position = TextPosition()
    pieces = []
    while True:
        page = paginate_text(
            text, position=position, max_chars=7, fits=lambda content, next: True
        )
        pieces.append(page.text)
        if page.next_position is None:
            break
        assert page.end_offset > page.start_offset
        position = page.next_position
    assert "".join(pieces) == text


def test_complete_lines_are_preferred_over_partial_following_line():
    page = paginate_text(
        "first\nsecond\nlast", fits=lambda content, next: len(content) <= 10
    )
    assert page.text == "first\n" and page.next_position == TextPosition(2, 0)


def test_single_long_line_uses_character_continuation():
    page = paginate_text(
        "🙂" * 20, fits=lambda content, next: len(content.encode()) <= 12
    )
    assert page.text == "🙂" * 3 and page.next_position == TextPosition(1, 3)


def test_complete_eof_is_tried_before_longer_continuation_metadata():
    page = paginate_text("abc", fits=lambda content, next: next is None)
    assert page.text == "abc" and page.next_position is None


def test_text_boundary_is_not_an_empty_success_with_unchanged_next():
    with pytest.raises(ToolResultPageBoundary):
        paginate_text("x", fits=lambda content, next: False)


def test_text_position_validation_and_past_eof():
    with pytest.raises(ValueError):
        TextPosition(True, 0)
    with pytest.raises(ValueError):
        paginate_text("abc", position=TextPosition(1, 4), fits=lambda *args: True)
    page = paginate_text("abc", position=TextPosition(9, 0), fits=lambda *args: True)
    assert page.text == "" and page.next_position is None
