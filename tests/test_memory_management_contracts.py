"""Current memory text, selection, timestamp and page contracts."""

from datetime import datetime, timezone
import pytest
from pulsara_agent.identity import random_id
from pulsara_agent.conversation_kernel.memory.contracts import (
    memory_title,
    memory_statement,
    memory_timestamp,
    canonical_memory_recorded_at,
)
from pulsara_agent.conversation_kernel.memory.management import (
    MemoryManagementSelection,
    normalized_search,
    encode_cursor,
    decode_cursor,
    page_size,
)


def test_selection_and_cursor_bind_exact_query():
    MemoryManagementSelection("project", random_id("workspace"))
    with pytest.raises(ValueError):
        MemoryManagementSelection("global", random_id("workspace"))
    filters = {"search": "中" * 8000, "context": "ctx:global"}
    cursor = encode_cursor(
        filters, ("2026-09-05T00:00:00.123456Z", random_id("memory"))
    )
    assert decode_cursor(cursor, filters, 2)[0].endswith(".123456Z")
    with pytest.raises(ValueError):
        decode_cursor(cursor, {"search": "different"}, 2)
    assert normalized_search("  100%_\\  ") == "100%_\\"
    assert normalized_search("字" * 8000) == "字" * 8000
    for size in (0, 101, True):
        with pytest.raises(ValueError):
            page_size(size)


def test_current_text_and_microsecond_contract():
    assert memory_title("  Cafe\u0301  ") == "Café"
    assert (
        memory_statement("FACT", " \r\n  body\r\n  indented\r\n ") == "body\n  indented"
    )
    assert memory_statement("RESPONSE_PREFERENCE", "字" * 90000) == ""
    for title in ("a\nb", "a" * 129, " "):
        with pytest.raises(ValueError):
            memory_title(title)
    stamp = "2026-09-05T00:00:00.123456Z"
    assert canonical_memory_recorded_at(memory_timestamp(stamp)) == stamp
    assert memory_timestamp(stamp) == datetime(
        2026, 9, 5, 0, 0, 0, 123456, tzinfo=timezone.utc
    )
