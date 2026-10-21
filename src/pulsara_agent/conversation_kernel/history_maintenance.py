"""Core-owned, best-effort history maintenance over existing physical I/O.

Only small process-local hints/cursors live here. Canonical dependency reads,
construction and atomic publication belong to HistoryProjectionRepository.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
import logging
from time import monotonic

from pulsara_agent.conversation_kernel.history_publication import (
    HistoryProjectionRepository,
    HistoryQuestionKey,
)
from pulsara_agent.conversation_kernel.io import KernelSessionIO, _drain_physical_task
from pulsara_agent.conversation_kernel.limits import STAGE2_LIMITS

# Maintenance cadence, not a session/work lifetime cap or freshness SLA. See
# PULSARA_DEFERRED_HISTORY_SEARCH_IMPLEMENTATION_SPEC.zh.md.
HISTORY_COALESCE_SECONDS = 0.250
HISTORY_RETRY_SECONDS = 1.0
HISTORY_RECONCILE_SECONDS = 30.0


class HistoryProjectionMaintainer:
    def __init__(self, provider) -> None:
        self._store = HistoryProjectionRepository(provider)
        self._io = KernelSessionIO(maximum_concurrency=1)
        self._hints: OrderedDict[str, dict[str, float]] = OrderedDict()
        self._pending: OrderedDict[HistoryQuestionKey, float] = OrderedDict()
        self._retry_after: dict[HistoryQuestionKey, float] = {}
        self._scan_page: deque[HistoryQuestionKey] = deque()
        self._scan_after = ""
        self._scan_due = monotonic()
        self._page_done: bool | None = None
        self._rescan_requested = False
        self._prefer_scan = False
        self._resolve_after = 0.0
        self._closing = False
        self._wake = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="kernel-history-maintenance")

    def offer_entries(self, session_id: str, entry_ids: tuple[str, ...]) -> None:
        if self._closing or not entry_ids:
            return
        due = monotonic() + HISTORY_COALESCE_SECONDS
        entries = self._hints.setdefault(session_id, {})
        for entry_id in entry_ids:
            entries.setdefault(entry_id, due)
        self._wake.set()

    def offer_session(self, session_id: str) -> None:
        """Fork has no ordinary entry tap; accelerate the next complete sweep.

        Do not reset a running keyset cursor: repeated forks must not prevent
        the rest of the corpus from ever being visited.
        """
        if self._closing:
            return
        now = monotonic()
        if not self._scan_after and self._page_done is None and self._scan_due > now:
            self._scan_due = now
            self._rescan_requested = False
        else:
            self._rescan_requested = True
        self._wake.set()

    def _enqueue(self, question: HistoryQuestionKey, due: float) -> None:
        old = self._pending.get(question)
        self._pending[question] = due if old is None else min(old, due)

    async def _physical(self, operation, *args):
        return await self._io.run(
            operation,
            *args,
            deadline_monotonic=monotonic()
            + STAGE2_LIMITS.foreground_io_timeout_ms / 1_000,
        )

    async def _resolve_hints(self) -> None:
        if not self._hints or monotonic() < self._resolve_after:
            return
        session_id, entries = self._hints.popitem(last=False)
        selected = tuple(entries)[: STAGE2_LIMITS.history_page_default_entries]
        hints = {entry_id: entries.pop(entry_id) for entry_id in selected}
        if entries:
            self._hints[session_id] = entries
        try:
            questions = await self._physical(
                self._store.resolve_history_questions,
                session_id,
                selected,
            )
        except Exception:
            logging.getLogger(__name__).warning(
                "History hint resolution failed", exc_info=True
            )
            restored = self._hints.setdefault(session_id, {})
            for entry_id, due in hints.items():
                restored[entry_id] = min(due, restored.get(entry_id, due))
            self._resolve_after = monotonic() + HISTORY_RETRY_SECONDS
            return
        if not self._closing:
            first_due = min(hints.values())
            for question in questions:
                self._enqueue(question, first_due)

    def _finish_page(self) -> None:
        if self._scan_page or self._page_done is None:
            return
        if self._page_done:
            self._scan_after = ""
            self._scan_due = monotonic() + (
                0.0 if self._rescan_requested else HISTORY_RECONCILE_SECONDS
            )
            self._rescan_requested = False
        self._page_done = None

    async def _load_page(self) -> None:
        self._finish_page()
        if self._scan_page or monotonic() < self._scan_due:
            return
        try:
            page = await self._physical(
                self._store.scan_history_questions,
                self._scan_after,
                STAGE2_LIMITS.history_page_default_entries,
            )
        except Exception:
            logging.getLogger(__name__).warning(
                "History reconciliation page failed", exc_info=True
            )
            self._scan_due = monotonic() + HISTORY_RETRY_SECONDS
            return
        if self._closing:
            return
        self._scan_after = page.next_cursor
        self._page_done = page.done
        self._scan_page.extend(page.questions)
        self._finish_page()

    def _ready_live(self) -> HistoryQuestionKey | None:
        now = monotonic()
        return next(
            (
                question
                for question, due in self._pending.items()
                if max(due, self._retry_after.get(question, 0.0)) <= now
            ),
            None,
        )

    async def _run(self) -> None:
        while not self._closing:
            self._wake.clear()
            await self._resolve_hints()
            if self._closing:
                break
            await self._load_page()
            if self._closing:
                break
            live = self._ready_live()
            if self._scan_page and (self._prefer_scan or live is None):
                question = self._scan_page.popleft()
                self._prefer_scan = False
                # If a failed live build is backing off, this sweep must not
                # bypass that backoff. Leave it in the pending tail instead.
                if self._retry_after.get(question, 0.0) > monotonic():
                    self._enqueue(question, monotonic())
                    continue
                self._pending.pop(question, None)
            elif live is not None:
                question = live
                self._pending.pop(question)
                self._prefer_scan = True
                # A page may also contain it; consume that duplicate without
                # removing hints which arrive during the following await.
                self._scan_page = deque(q for q in self._scan_page if q != question)
            else:
                if self._wake.is_set():
                    continue
                deadlines = [self._scan_due]
                deadlines.extend(
                    max(due, self._retry_after.get(q, 0.0))
                    for q, due in self._pending.items()
                )
                if self._hints:
                    deadlines.append(self._resolve_after)
                timeout = max(0.0, min(deadlines) - monotonic())
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=timeout)
                except TimeoutError:
                    pass
                continue
            try:
                status = await self._physical(
                    self._store.maintain_question,
                    question.session_id,
                    question.q1_entry_id,
                )
                failed = status == "build_failed"
            except Exception:
                logging.getLogger(__name__).warning(
                    "History maintenance failed for %s",
                    question,
                    exc_info=True,
                )
                failed = True
            if failed and not self._closing:
                self._retry_after[question] = monotonic() + HISTORY_RETRY_SECONDS
                self._enqueue(question, self._retry_after[question])
            else:
                self._retry_after.pop(question, None)
            # No completion deletes an offer delivered while this build ran.
            # Its fixed cut has been published; new hints will resolve to a
            # later pending tail item on the next pass.

    async def aclose(self, *, deadline_monotonic: float) -> None:
        self._closing = True
        self._hints.clear()
        self._pending.clear()
        self._scan_page.clear()
        self._retry_after.clear()
        self._wake.set()
        # Reuse the I/O owner's cancellation-resistant physical join. Stop
        # claims, not the currently executing thread; do not drain backlog.
        await _drain_physical_task(self._task)
        io_close = asyncio.create_task(
            self._io.aclose(deadline_monotonic=deadline_monotonic)
        )
        await _drain_physical_task(io_close)
        io_close.result()
        self._task.result()
