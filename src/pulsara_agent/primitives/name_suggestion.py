"""Advisory name matching shared by tool and argument diagnostics."""
from collections.abc import Iterable
from difflib import SequenceMatcher, get_close_matches


def suggest_name(name: str, candidates: Iterable[str]) -> str | None:
    matches = get_close_matches(name, sorted(candidates), n=2, cutoff=0.75)
    if not matches or matches[0] == name:
        return None
    # Two results are enough to detect a tied best match without choosing one
    # by spelling order. difflib owns both ranking and similarity calculation.
    if len(matches) == 2 and (
        SequenceMatcher(None, matches[0], name).ratio()
        == SequenceMatcher(None, matches[1], name).ratio()
    ):
        return None
    return matches[0]
