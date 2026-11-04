"""Core-lifetime physical resources for joined, already-authorized ASR work.

Subtitle/frame operations stay with each tool port. Shared recognition owns its
cloned source, decoder, cache IO, frozen settings and initial absolute deadline;
closing its initiating session cannot close another caller's accepted work.
"""

from __future__ import annotations

from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.media.transcript_work import TranscriptWork
from pulsara_agent.media.worker import MediaWorker


class MediaAsrRuntime:
    def __init__(self):
        self.work = TranscriptWork()
        self._worker = MediaWorker()
        self._io = KernelSessionIO()

    async def run_media(self, operation, *args, deadline_monotonic, **kwargs):
        kwargs.pop("cancelled", None)
        return await self._worker.run(
            operation, *args, deadline_monotonic=deadline_monotonic, **kwargs
        )

    async def run_cache(self, operation, *args, deadline_monotonic, **kwargs):
        return await self._io.run(
            operation, *args, deadline_monotonic=deadline_monotonic, **kwargs
        )

    @property
    def physical_close_complete(self):
        return (
            self.work.physical_close_complete
            and self._worker.physical_close_complete
            and self._io.physical_close_complete
        )

    async def aclose(self, *, deadline_monotonic):
        # Settle shared tasks while their decoder/cache dependencies are alive.
        # The last session waiter normally already did this; Core also owns the
        # force-close path for any remaining work after session shutdown.
        try:
            await self.work.aclose()
        finally:
            try:
                await self._worker.aclose()
            finally:
                await self._io.aclose(deadline_monotonic=deadline_monotonic)
