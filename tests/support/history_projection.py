"""Explicitly drive one production reconciliation pass in repository tests.

Repository writes intentionally do not maintain derived search state. Tests
without a Core must settle that state explicitly through the same owner.
"""

from time import monotonic

from pulsara_agent.conversation_kernel.history_publication import (
    HistoryProjectionRepository,
)


def maintain_history(repository, *, allow_build_failure=False):
    store = HistoryProjectionRepository(repository._provider)
    after = ""
    while True:
        page = store.scan_history_questions(
            after,
            128,
            deadline_monotonic=monotonic() + 30,
        )
        for question in page.questions:
            status = store.maintain_question(
                question.session_id,
                question.q1_entry_id,
                deadline_monotonic=monotonic() + 30,
            )
            if not allow_build_failure:
                assert status != "build_failed", question
        if page.done:
            return
        after = page.next_cursor
