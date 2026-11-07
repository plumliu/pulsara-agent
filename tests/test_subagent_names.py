from pulsara_agent.conversation_kernel.subagents.names import (
    SCIENTIST_NAMES,
    choose_agent_name,
)


def test_scientist_pool_has_100_distinct_short_names():
    assert len(SCIENTIST_NAMES) == 100
    assert len({name.casefold() for name in SCIENTIST_NAMES}) == 100
    assert all(name == name.strip() and 1 <= len(name.split()) <= 2 for name in SCIENTIST_NAMES)


def test_names_do_not_repeat_until_pool_is_exhausted_and_do_not_cap_work():
    used = set()
    for _ in SCIENTIST_NAMES:
        name = choose_agent_name(used)
        assert name in SCIENTIST_NAMES and name not in used
        used.add(name)
    assert choose_agent_name(used) in SCIENTIST_NAMES
