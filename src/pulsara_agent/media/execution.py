"""Media-specific execution within the existing tool authority and resources.

The tool port owns dispatch, cancellation and settlement. This module composes
source, decoder, transcript and image owners into one atomic result candidate.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from time import monotonic

from pulsara_agent.conversation_kernel.io import PhysicalToolInvocationDisposition
from pulsara_agent.conversation_kernel.limits import STAGE2_LIMITS
from pulsara_agent.conversation_kernel.prompt_content import bind_prompt_images
from pulsara_agent.llm.input import (
    FrozenPromptContent,
    LLMTextPart,
    MAXIMUM_PROMPT_IMAGE_PIXELS,
    PromptContent,
    PromptImagePart,
)
from pulsara_agent.ports.tool_execution import (
    ToolOutputSourceFormatHint,
    encode_tool_public_json,
)
from pulsara_agent.tools.builtins.media import validate_media_arguments
from .asr_cache import AsrMediaKey
from .frames import FrameBudget, read_video_frames
from .source import MediaError, MediaSource, probe_media, validate_range
from .transcript import TranscriptService


@dataclass(frozen=True, slots=True)
class MediaExecutionResult:
    state: str
    content: bytes | FrozenPromptContent
    public_format: ToolOutputSourceFormatHint = ToolOutputSourceFormatHint.JSON


async def execute_media(
    tool,
    arguments,
    context,
    binding,
    *,
    io,
    worker,
    image_validator,
    cache,
    read_settings,
    asr_runtime,
    memory_domain_id,
    deadline,
    page_budget,
    reserve_read,
    observe_read,
) -> MediaExecutionResult:
    validate_media_arguments(tool.name, arguments)
    source = None

    def result(state, body):
        return MediaExecutionResult(state=state, content=encode_tool_public_json(body))

    reserved = False

    def reserve_once():
        nonlocal reserved
        if not reserved:
            reserve_read(
                canonical_path=str(source.path), file_mtime_ns=str(source.modified_ns)
            )
            reserved = True

    def begin(a, b):
        observe_read(
            canonical_path=str(source.path),
            file_mtime_ns=str(source.modified_ns),
            from_seconds=a,
            to_seconds=b,
        )

    try:
        if (
            tool.name == "read_video_frames"
            and context.input_modalities is not None
            and "image" not in context.input_modalities
        ):
            return result(
                "APPLICATION_ERROR", {"error": "MODEL_IMAGE_INPUT_UNSUPPORTED"}
            )
        opened = await io.run_tool_invocation(
            _open_media_source, tool, arguments, deadline_monotonic=deadline
        )
        if opened.disposition is PhysicalToolInvocationDisposition.RAISED:
            raise opened.error
        source = opened.value
        if opened.caller_cancelled:
            raise asyncio.CancelledError
        info = await worker.run(
            probe_media,
            source,
            display_facts=tool.name == "read_video_frames",
            deadline_monotonic=deadline,
        )
        span = validate_range(
            arguments.get("from"),
            arguments.get("to"),
            info.duration,
            annotation=tool.name == "annotate_media",
        )
        if tool.name == "annotate_media":
            source.assert_unchanged()
            return result(
                "SUCCESS",
                {
                    "media_annotation": "added",
                    "canonical_path": str(source.path),
                    "file_mtime_ns": str(source.modified_ns),
                },
            )
        if tool.name == "read_transcript":

            async def run_media(operation, *args, **kwargs):
                kwargs.pop("cancelled", None)
                kwargs.pop("deadline_monotonic", None)
                return await worker.run(
                    operation,
                    *args,
                    deadline_monotonic=deadline,
                    on_started=reserve_once,
                    **kwargs,
                )

            async def run_cache(operation, *args, **kwargs):
                kwargs.pop("deadline_monotonic", None)
                return await io.run(
                    operation, *args, deadline_monotonic=deadline, **kwargs
                )

            if asr_runtime is None or read_settings is None:
                return result(
                    "APPLICATION_ERROR", {"error": "MEDIA_RUNTIME_UNAVAILABLE"}
                )
            settings = await io.run(
                _read_media_settings, read_settings, deadline_monotonic=deadline
            )
            service = TranscriptService(
                work=asr_runtime.work,
                cache=cache,
                asr_config=settings.asr,
                run_media=run_media,
                run_cache=run_cache,
                run_asr_media=asr_runtime.run_media,
                run_asr_cache=asr_runtime.run_cache,
                credential_scrub_set=settings.credential_scrub_set(),
                maximum_upload_bytes=STAGE2_LIMITS.canonical_blob_hard_bytes,
                maximum_response_bytes=STAGE2_LIMITS.tool_result_hard_bytes,
                deadline_monotonic=deadline,
            )
            page = await service.read(
                source,
                info,
                arguments,
                page_budget,
                memory_domain_id=memory_domain_id,
                on_started=begin,
            )
            source.assert_unchanged()
            return result(
                "APPLICATION_ERROR" if page.is_error else "SUCCESS", page.payload
            )
        if info.video_stream_index is None:
            return result("APPLICATION_ERROR", {"error": "NO_VIDEO_TRACK"})
        allowance = context.image_resource_allowance
        if (
            allowance is None
            or allowance.tool_call_id != context.tool_call_id
            or allowance.executor_binding_fingerprint
            != binding.executor_binding_fingerprint
        ):
            raise RuntimeError("video image allowance does not exact-join")
        if image_validator is None:
            raise RuntimeError("video image validation owner is unavailable")
        maximum_bytes = min(
            allowance.canonical_bytes,
            allowance.logical_bytes,
            3 * (allowance.wire_bytes // 4),
        )
        if maximum_bytes <= 0 or allowance.input_tokens <= 0:
            return result("APPLICATION_ERROR", {"error": "IMAGE_RESOURCE_EXCEEDED"})
        # Existing image pixel boundary bounds this call's encoded/raster
        # working set. Scan capacity derives from that physical allocation.
        pixels = MAXIMUM_PROMPT_IMAGE_PIXELS
        budget = FrameBudget(
            maximum_bytes=maximum_bytes,
            image_pixels=pixels,
            maximum_images=max(1, pixels // (160 * 90)),
            analysis_frames=max(3, pixels // (256 * 256)),
        )
        touched = False
        first_span = None

        def begin_frames_range(span):
            nonlocal first_span
            # A smaller-image quote retry belongs to the same logical read.
            if first_span is None:
                first_span = span
                begin(*span)

        async def begin_frames():
            nonlocal touched
            reserve_once()
            if cache is not None and not touched:
                touched = True
                try:
                    await io.run(
                        cache.touch,
                        AsrMediaKey(
                            memory_domain_id,
                            str(source.path),
                            source.modified_ns,
                            source.size,
                        ),
                        deadline_monotonic=deadline,
                    )
                except Exception:
                    # Cache recency grants no source authority and cannot
                    # prevent local image evidence from being read.
                    pass

        from pulsara_agent.tools.builtins.media import parse_image_tool_source

        image_source = parse_image_tool_source(tool.name, arguments)
        while True:
            candidate = await worker.run(
                read_video_frames,
                source,
                info,
                start=span.start,
                end=span.end,
                mode=arguments.get("mode", "overview"),
                crop=arguments.get("crop"),
                budget=budget,
                deadline_monotonic=deadline,
                on_progress=begin_frames_range,
                on_started=begin_frames,
            )
            content = await image_validator.freeze(
                PromptContent(
                    (
                        LLMTextPart(candidate.text),
                        *(
                            PromptImagePart(image.data, image.mime)
                            for image in candidate.images
                        ),
                    )
                ),
                deadline_monotonic=deadline,
            )
            content = bind_prompt_images(content, workspace_id=context.workspace_id)
            quote = allowance.quote_owner.quote(
                tool_call_id=context.tool_call_id,
                tool_name=tool.name,
                source=image_source,
                content=content,
            )
            if all(
                getattr(quote, key) <= getattr(allowance, key)
                for key in (
                    "canonical_bytes",
                    "logical_bytes",
                    "wire_bytes",
                    "input_tokens",
                )
            ):
                source.assert_unchanged()
                if candidate.processed_range != first_span:
                    begin(*candidate.processed_range)
                return MediaExecutionResult(
                    state="SUCCESS",
                    content=content,
                    public_format=ToolOutputSourceFormatHint.TEXT,
                )
            # A quote is authoritative. Rebuild all evidence together at a
            # smaller output allocation; no partial text/image commit.
            if budget.image_pixels <= 160 * 90 or budget.maximum_bytes <= 1:
                return result("APPLICATION_ERROR", {"error": "IMAGE_RESOURCE_EXCEEDED"})
            budget = replace(
                budget,
                image_pixels=max(1, budget.image_pixels // 2),
                maximum_bytes=max(1, budget.maximum_bytes // 2),
                maximum_images=max(1, budget.maximum_images // 2),
            )
    except TimeoutError as exc:
        return result(
            "APPLICATION_ERROR",
            {"error": "MEDIA_PROCESSING_DEADLINE_EXPIRED", "message": str(exc)},
        )
    except (MediaError, OSError, ValueError) as exc:
        code = getattr(
            exc,
            "code",
            "MEDIA_NOT_FOUND"
            if isinstance(exc, FileNotFoundError)
            else "INVALID_ARGUMENTS",
        )
        return result("APPLICATION_ERROR", {"error": code, "message": str(exc)})
    finally:
        if source is not None:
            source.close()


def _open_media_source(tool, arguments, *, deadline_monotonic):
    from pulsara_agent.tools.builtins.filesystem import path_target_arg

    if monotonic() >= deadline_monotonic:
        raise TimeoutError("Media source admission deadline expired")
    return MediaSource.open(tool._resolve_read_path(path_target_arg(arguments)))


def _read_media_settings(read_settings, *, deadline_monotonic):
    if monotonic() >= deadline_monotonic:
        raise TimeoutError("Media settings read deadline expired")
    return read_settings()
