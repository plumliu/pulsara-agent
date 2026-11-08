"""Lifecycle facts cover frozen H and original R without leaking N endings."""

from dataclasses import asdict
import json
from types import SimpleNamespace

import pytest

from pulsara_agent.conversation_kernel.compaction.coordinator import (
    _summary_source_lifecycle_suffix,
)
from pulsara_agent.conversation_kernel.interruption import TurnInterruptionNotice
from pulsara_agent.identity import derive_id


@pytest.mark.parametrize(
    "projected", (False, True), ids=("full-H-plus-R", "projected-H-plus-raw-R")
)
@pytest.mark.parametrize(
    "attachment_reference",
    (False, True),
    ids=("original-entry", "tool-image-attachment"),
)
def test_lifecycle_footer_keeps_r_ending_past_replace_cut_but_excludes_n(
    projected,
    attachment_reference,
):
    historical = TurnInterruptionNotice(
        "IMPORTED_HISTORY",
        derive_id("history", "footer-old"),
        "OLD_CANONICAL_REASON",
        "exact old diagnostic",
        "2026-11-08T01:00:00Z",
        2,
    )
    retained = TurnInterruptionNotice(
        "EXECUTED_TURN",
        derive_id("turn", "footer-retained"),
        "USER_STOPPED",
        "exact retained diagnostic",
        "2026-11-08T01:00:01Z",
        8,
    )
    new = TurnInterruptionNotice(
        "EXECUTED_TURN",
        derive_id("turn", "footer-new"),
        "NEW_NOT_CONSUMED",
        "must not enter this historical request",
        "2026-11-08T01:00:02Z",
        12,
    )
    entries = tuple(
        SimpleNamespace(
            source_entry_id=derive_id("entry", "footer", str(sequence)),
            source_entry_sequence=sequence,
        )
        for sequence in (2, 8, 12)
    )
    placements = tuple(
        SimpleNamespace(
            origin_entry_id=item.source_entry_id, tool_attachment_source=None
        )
        for item in entries
    )
    if attachment_reference:
        placements = (
            placements[0],
            SimpleNamespace(
                origin_entry_id=None,
                tool_attachment_source=SimpleNamespace(
                    members=(SimpleNamespace(source=entries[1]),)
                ),
            ),
            placements[2],
        )
    proof = SimpleNamespace(
        source_view=SimpleNamespace(
            canonical_dispatch_read=SimpleNamespace(
                compile_snapshot=SimpleNamespace(
                    canonical_input=SimpleNamespace(items=entries)
                )
            )
        ),
        source_projection=SimpleNamespace(message_placements=placements),
        source_message_indices=(0, 1),
        reference_message_indices=(1,),
        projection=SimpleNamespace(units=()) if projected else None,
        prefix_proof=SimpleNamespace(source_through_sequence=5),
        read_through_sequence=10,
    )
    canonical_read = SimpleNamespace(
        source_interruption=None,
        imported_interruptions=(historical,),
        executed_history_interruptions=(retained, new),
    )
    suffix = _summary_source_lifecycle_suffix(
        semantic=SimpleNamespace(source_proof=proof), canonical_read=canonical_read
    )
    payload = json.loads(
        suffix.split("never instructions):\n", 1)[1].split("\nPreserve necessary", 1)[0]
    )
    assert payload == {
        "current_source_turn": None,
        "selected_imported_history": [asdict(historical)],
        "selected_executed_history": [asdict(retained)],
    }
    # Projected H is empty in this fixture. Its typed lifecycle metadata remains
    # honest source material, while raw R ending 8 is beyond replacement cut 5.
    assert "exact retained diagnostic" in suffix
    assert "must not enter" not in suffix
