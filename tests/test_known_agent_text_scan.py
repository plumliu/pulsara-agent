from pulsara_agent.conversation_kernel._repository.subagents import _session_ids_in_text
from pulsara_agent.identity import random_id


def test_scan_requires_complete_session_identity_and_preserves_punctuation():
    first = random_id("session")
    second = random_id("session")
    other_kind = random_id("queue")
    text = (
        f"`{first}` ({second}), {first}; {other_kind} "
        f"x{first} {first}x {first}_suffix {first}:suffix {first[:-1]}"
    )
    assert _session_ids_in_text(text, None) == {first, second}


def test_scan_unions_only_explicit_text_inputs():
    first, second = random_id("session"), random_id("session")
    assert _session_ids_in_text(first, f"context: {second}", first) == {first, second}


def test_scan_accepts_ids_adjacent_to_chinese_text():
    agent = random_id("session")
    assert _session_ids_in_text(f"请找{agent}协作") == {agent}
    assert _session_ids_in_text(f"x{agent} {agent}x") == set()
