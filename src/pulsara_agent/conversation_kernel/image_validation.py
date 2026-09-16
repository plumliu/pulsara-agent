"""Host-owned validation of raw prompt images in short-lived workers."""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from io import BytesIO
import multiprocessing
from multiprocessing.connection import Connection
from threading import Lock
from time import monotonic
from typing import Final
import warnings

from pulsara_agent.conversation_kernel.prompt_content import (
    MAXIMUM_PROMPT_MULTIPART_BYTES,
    freeze_canonical_prompt,
)
from pulsara_agent.llm.input import (
    FrozenPromptContent,
    LLMImagePart,
    LLMTextPart,
    MAXIMUM_PROMPT_IMAGE_PIXELS,
    PromptContent,
    PromptImagePart,
)


PNG_TEXT_CHUNK_LIMIT: Final = 1 << 20
PNG_TEXT_MEMORY_LIMIT: Final = 1 << 20
_FORMAT_TO_MEDIA_TYPE: Final = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
}

@dataclass(slots=True)
class _ValidationSlotWaiter:
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future[None]
    claimed: bool = False


class _HostImageValidationSlot:
    """One process slot that is safe across successive Host event loops."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._held = False
        self._waiters: deque[_ValidationSlotWaiter] = deque()

    async def acquire(self) -> None:
        loop = asyncio.get_running_loop()
        waiter = _ValidationSlotWaiter(loop=loop, future=loop.create_future())
        with self._lock:
            if not self._held:
                self._held = True
                return
            self._waiters.append(waiter)
        try:
            await waiter.future
        except BaseException:
            release_claim = False
            with self._lock:
                if waiter.claimed:
                    release_claim = True
                else:
                    with suppress(ValueError):
                        self._waiters.remove(waiter)
            waiter.future.cancel()
            if release_claim:
                self.release()
            raise

    def release(self) -> None:
        while True:
            with self._lock:
                if not self._held:
                    raise RuntimeError("image validation slot is not held")
                waiter = self._waiters.popleft() if self._waiters else None
                if waiter is None:
                    self._held = False
                    return
                waiter.claimed = True
            try:
                waiter.loop.call_soon_threadsafe(self._finish_claim, waiter)
                return
            except RuntimeError:
                # A loop can close while its cancelled waiter is being removed.
                # Retain ownership and offer the same slot to the next waiter.
                continue

    @staticmethod
    def _finish_claim(waiter: _ValidationSlotWaiter) -> None:
        if not waiter.future.done():
            waiter.future.set_result(None)

    def locked(self) -> bool:
        with self._lock:
            return self._held


# KernelHostCore is normally unique, but the physical decoder bound belongs to
# the Host process rather than a session or a core instance.
_HOST_IMAGE_VALIDATION_SLOT = _HostImageValidationSlot()


class PromptImageValidationError(ValueError):
    """A submitted image does not satisfy the frozen local image contract."""


def _validated_image_facts(
    payload: bytes, declared_mime: str | None
) -> tuple[str, int, int]:
    # Pillow owns decoding and its PNG text accounting.  These process globals
    # are set before the worker opens its first image and die with the worker.
    from PIL import Image, PngImagePlugin

    PngImagePlugin.MAX_TEXT_CHUNK = PNG_TEXT_CHUNK_LIMIT
    PngImagePlugin.MAX_TEXT_MEMORY = PNG_TEXT_MEMORY_LIMIT
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with Image.open(BytesIO(payload), formats=("PNG", "JPEG", "WEBP")) as header:
            actual_format = str(header.format or "").upper()
            media_type = _FORMAT_TO_MEDIA_TYPE.get(actual_format)
            if media_type is None:
                raise PromptImageValidationError("decoded image format is unsupported")
            if declared_mime is not None and media_type != declared_mime:
                raise PromptImageValidationError(
                    "declared image MIME does not match the decoded format"
                )
            if actual_format == "PNG" and header.get_format_mimetype() == "image/apng":
                raise PromptImageValidationError("APNG prompt images are not supported")
            if int(getattr(header, "n_frames", 1)) != 1:
                raise PromptImageValidationError(
                    "animated or multi-frame prompt images are not supported"
                )
            width, height = header.size
            if (
                isinstance(width, bool)
                or isinstance(height, bool)
                or not isinstance(width, int)
                or not isinstance(height, int)
                or width < 1
                or height < 1
                or width * height > MAXIMUM_PROMPT_IMAGE_PIXELS
            ):
                raise PromptImageValidationError(
                    "prompt image dimensions exceed the pixel boundary"
                )
            header.verify()

        with Image.open(BytesIO(payload), formats=(actual_format,)) as decoded:
            if str(decoded.format or "").upper() != actual_format:
                raise PromptImageValidationError("prompt image format changed on decode")
            if actual_format == "PNG" and decoded.get_format_mimetype() == "image/apng":
                raise PromptImageValidationError("APNG prompt images are not supported")
            if int(getattr(decoded, "n_frames", 1)) != 1:
                raise PromptImageValidationError(
                    "animated or multi-frame prompt images are not supported"
                )
            if decoded.size != (width, height):
                raise PromptImageValidationError("prompt image dimensions changed on decode")
            decoded.load()
    return media_type, width, height


def _validation_worker(
    connection: Connection,
    images: tuple[tuple[bytes, str | None], ...],
) -> None:
    try:
        facts = tuple(_validated_image_facts(payload, mime) for payload, mime in images)
        connection.send((True, facts))
    except BaseException as exc:
        # The parent receives a closed, bounded diagnostic rather than a remote
        # traceback or a partially validated result.
        with suppress(BaseException):
            connection.send((False, type(exc).__name__, str(exc)))
    finally:
        connection.close()


class HostPromptImageValidator:
    """One process-local physical validation slot shared by all Host sessions."""

    def __init__(self) -> None:
        self._slot = _HOST_IMAGE_VALIDATION_SLOT
        self._state_lock = asyncio.Lock()
        self._cleanup_lock = asyncio.Lock()
        self._active_process: multiprocessing.Process | None = None
        self._active_connection: Connection | None = None
        self._closed = False

    async def freeze(
        self,
        content: PromptContent,
        *,
        deadline_monotonic: float,
    ) -> FrozenPromptContent:
        if not isinstance(content, PromptContent):
            raise TypeError("Host prompt ingress requires PromptContent")
        async with self._state_lock:
            if self._closed:
                raise RuntimeError("prompt image validator is closed")
        # This is a necessary lower-bound check before starting a decoder.  The
        # exact canonical M check runs again after trusted dimensions exist.
        raw_bytes = sum(
            len(part.text.encode("utf-8"))
            if isinstance(part, LLMTextPart)
            else len(part.original_bytes)
            for part in content.parts
        )
        if raw_bytes > MAXIMUM_PROMPT_MULTIPART_BYTES:
            raise PromptImageValidationError("prompt multipart exceeds its input bound")
        images = tuple(
            (part.original_bytes, part.declared_mime)
            for part in content.parts
            if isinstance(part, PromptImagePart)
        )
        if not images:
            frozen = FrozenPromptContent(
                tuple(part for part in content.parts if isinstance(part, LLMTextPart))
            )
            freeze_canonical_prompt(frozen)
            return frozen

        facts = await self._validate_images(
            tuple((payload, mime) for payload, mime in images),
            deadline_monotonic=deadline_monotonic,
        )
        fact_index = 0
        parts: list[LLMTextPart | LLMImagePart] = []
        for part in content.parts:
            if isinstance(part, LLMTextPart):
                parts.append(part)
                continue
            media_type, width, height = facts[fact_index]
            fact_index += 1
            parts.append(
                LLMImagePart(
                    media_type=media_type,
                    immutable_bytes=part.original_bytes,
                    width=width,
                    height=height,
                )
            )
        frozen = FrozenPromptContent(tuple(parts))
        freeze_canonical_prompt(frozen)
        return frozen

    async def freeze_local_image(
        self,
        payload: bytes,
        *,
        deadline_monotonic: float,
    ) -> LLMImagePart:
        """Validate one bounded local-file payload without a declared MIME."""

        if not isinstance(payload, bytes) or not payload:
            raise PromptImageValidationError("local image payload is empty")
        if len(payload) > MAXIMUM_PROMPT_MULTIPART_BYTES:
            raise PromptImageValidationError("local image exceeds its input bound")
        facts = await self._validate_images(
            ((payload, None),), deadline_monotonic=deadline_monotonic
        )
        media_type, width, height = facts[0]
        image = LLMImagePart(
            media_type=media_type,
            immutable_bytes=payload,
            width=width,
            height=height,
        )
        freeze_canonical_prompt(
            FrozenPromptContent((LLMTextPart("Image loaded."), image))
        )
        return image

    async def _validate_images(
        self,
        images: tuple[tuple[bytes, str | None], ...],
        *,
        deadline_monotonic: float,
    ) -> tuple[tuple[str, int, int], ...]:
        remaining = deadline_monotonic - monotonic()
        if remaining <= 0:
            raise TimeoutError("prompt image validation deadline expired")
        try:
            await asyncio.wait_for(self._slot.acquire(), timeout=remaining)
        except TimeoutError:
            raise TimeoutError("prompt image validation deadline expired") from None
        parent: Connection | None = None
        child: Connection | None = None
        process: multiprocessing.Process | None = None
        recv_task: asyncio.Task[object] | None = None
        try:
            async with self._state_lock:
                if self._closed:
                    raise RuntimeError("prompt image validator is closed")
                context = multiprocessing.get_context("spawn")
                parent, child = context.Pipe(duplex=False)
                process = context.Process(
                    target=_validation_worker,
                    args=(child, images),
                    name="pulsara-prompt-image-validator",
                )
                try:
                    process.start()
                except BaseException:
                    with suppress(BaseException):
                        process.close()
                    raise
                # Own a started worker before any later operation can fail,
                # including closing the parent's copy of the sending pipe.
                self._active_process = process
                self._active_connection = parent
                child.close()
                child = None
            recv_task = asyncio.create_task(
                asyncio.to_thread(parent.recv),
                name="pulsara-prompt-image-validation-result",
            )
            remaining = deadline_monotonic - monotonic()
            if remaining <= 0:
                raise TimeoutError("prompt image validation deadline expired")
            result = await asyncio.wait_for(asyncio.shield(recv_task), timeout=remaining)
            exitcode = await self._reap_active(
                process,
                parent,
                terminate=False,
                deadline_monotonic=deadline_monotonic,
            )
            if exitcode != 0:
                raise PromptImageValidationError(
                    "image decoder worker exited abnormally"
                )
            if not isinstance(result, tuple) or not result or result[0] is not True:
                detail = (
                    "image decoder worker failed"
                    if not isinstance(result, tuple) or len(result) < 3
                    else f"{result[1]}: {result[2]}"
                )
                raise PromptImageValidationError(detail)
            facts = result[1]
            if not isinstance(facts, tuple) or len(facts) != len(images):
                raise PromptImageValidationError("image decoder returned an invalid result")
            return facts
        except BaseException:
            if process is not None and parent is not None:
                with suppress(asyncio.CancelledError):
                    await self._reap_active(process, parent, terminate=True)
            if recv_task is not None:
                await self._drain_task(recv_task)
            raise
        finally:
            if parent is not None:
                with suppress(OSError):
                    parent.close()
            if child is not None:
                with suppress(OSError):
                    child.close()
            self._slot.release()

    async def _reap_active(
        self,
        process: multiprocessing.Process,
        connection: Connection,
        *,
        terminate: bool,
        deadline_monotonic: float | None = None,
    ) -> int | None:
        cleanup = asyncio.create_task(
            self._cleanup_active(
                process,
                connection,
                terminate=terminate,
                deadline_monotonic=deadline_monotonic,
            ),
            name="pulsara-prompt-image-validation-cleanup",
        )
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
                # A reply does not prove the worker exited. Cancellation must
                # also stop a worker while normal cleanup is joining it.
                if self._active_process is process:
                    with suppress(ValueError, OSError):
                        if process.is_alive():
                            process.terminate()
        try:
            exitcode = cleanup.result()
        except BaseException:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
        if cancelled:
            raise asyncio.CancelledError
        return exitcode

    async def _cleanup_active(
        self,
        process: multiprocessing.Process,
        connection: Connection,
        *,
        terminate: bool,
        deadline_monotonic: float | None,
    ) -> int | None:
        async with self._cleanup_lock:
            async with self._state_lock:
                if self._active_process is not process:
                    return None
            try:
                timed_out = False
                if terminate:
                    with suppress(BaseException):
                        if process.is_alive():
                            process.terminate()
                    await asyncio.to_thread(process.join)
                else:
                    assert deadline_monotonic is not None
                    await asyncio.to_thread(
                        process.join,
                        max(0.0, deadline_monotonic - monotonic()),
                    )
                    timed_out = process.is_alive()
                    if timed_out:
                        process.terminate()
                        await asyncio.to_thread(process.join)
                exitcode = process.exitcode
                process.close()
                if timed_out:
                    raise TimeoutError("prompt image validation deadline expired")
                return exitcode
            finally:
                with suppress(BaseException):
                    connection.close()
                async with self._state_lock:
                    if self._active_process is process:
                        self._active_process = None
                        self._active_connection = None

    @staticmethod
    async def _drain_task(task: asyncio.Task[object]) -> None:
        while not task.done():
            try:
                await asyncio.shield(task)
            except BaseException:
                continue
        with suppress(BaseException):
            task.result()

    async def aclose(self) -> None:
        async with self._state_lock:
            self._closed = True
            process = self._active_process
            connection = self._active_connection
        if process is not None and connection is not None:
            await self._reap_active(process, connection, terminate=True)


__all__ = [
    "HostPromptImageValidator",
    "PNG_TEXT_CHUNK_LIMIT",
    "PNG_TEXT_MEMORY_LIMIT",
    "PromptImageValidationError",
]
