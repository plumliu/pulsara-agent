"""Media-specific execution within the existing tool authority and resources.

The tool port owns dispatch, cancellation and settlement. This module composes
source, decoder, transcript and image owners into one atomic result candidate.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from time import monotonic

from pulsara_agent.conversation_kernel.io import PhysicalToolInvocationDisposition
from pulsara_agent.conversation_kernel.limits import STAGE2_LIMITS
from pulsara_agent.conversation_kernel.prompt_content import (
    MAXIMUM_PROMPT_MULTIPART_BYTES,
    bind_prompt_images,
)
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
from .frames import (
    FRAME_ANALYSIS_FRAMES,
    FRAME_SELECTION_REVISION,
    FrameBudget,
    frame_page_fits_geometry,
    prepare_frame_plan,
    render_frame_page,
)
from .frames_paging import (
    FramePagePosition,
    read_frame_continuation,
    trim_detail_page,
    with_frame_page_continuation,
)
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
        validate_media_arguments(tool.name, arguments)
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
        mode = arguments.get("mode", "overview")
        crop = arguments.get("crop")
        position = (
            read_frame_continuation(
                arguments["continuation"], source=source, info=info,
                start=span.start, end=span.end, mode=mode, crop=crop,
                selection_revision=FRAME_SELECTION_REVISION,
            ) if "continuation" in arguments else FramePagePosition()
        )
        # Selection is owned by the fixed quality/physical analysis profile.
        # Provider allowance may split its delivery, never change the plan.
        pixels = MAXIMUM_PROMPT_IMAGE_PIXELS
        budget = FrameBudget(
            maximum_bytes=MAXIMUM_PROMPT_MULTIPART_BYTES,
            image_pixels=pixels,
            maximum_images=max(1, pixels // (160 * 90)),
            analysis_frames=FRAME_ANALYSIS_FRAMES,
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
        plan = await worker.run(
            prepare_frame_plan, source, info,
            start=span.start, end=span.end, mode=mode, crop=crop,
            window_start=position.window_start,
            analysis_frames=FRAME_ANALYSIS_FRAMES,
            deadline_monotonic=deadline,
            on_progress=begin_frames_range,
            on_started=begin_frames,
        )
        if position.next_observation >= plan.observation_count:
            raise MediaError("INVALID_ARGUMENTS", "Frame continuation is outside the selection plan")
        content = await render_prepared_frame_page(
            source=source, info=info, plan=plan, position=position,
            budget=budget, allowance=allowance, page_budget=page_budget,
            image_source=image_source, workspace_id=context.workspace_id,
            worker=worker, image_validator=image_validator, deadline=deadline,
            refresh_image_resources=context.refresh_image_resources,
        )
        source.assert_unchanged()
        processed_range = tuple(map(float, plan.processed_range))
        if processed_range != first_span:
            begin(*processed_range)
        return MediaExecutionResult(
            state="SUCCESS", content=content,
            public_format=ToolOutputSourceFormatHint.TEXT,
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


async def render_prepared_frame_page(
    *, source, info, plan, position, budget, allowance, page_budget,
    image_source, workspace_id, worker, image_validator, deadline,
    refresh_image_resources=None,
) -> FrozenPromptContent:
    """Quote complete slices of one prepared plan; never reselect or reanalyse.

    Detail retains one physically admitted encoded candidate and projects its
    prefixes for joint quotes. Other modes may render smaller slices because
    their changing collage geometry makes encoded cost non-monotone. Every
    mode can reach the actual indexed singleton boundary without reselecting.
    """
    offset = position.next_observation
    count = plan.observation_count - offset
    if count < 1:
        raise MediaError("INVALID_ARGUMENTS", "Frame continuation has no remaining observation")
    floor = False
    refresh_attempted = False
    raw_detail_candidate = None
    while True:
        source.assert_unchanged()
        if not frame_page_fits_geometry(
            plan, offset=offset, count=count,
            budget=budget, floor=floor,
        ):
            if plan.mode == "detail":
                raise MediaError("RESOURCE_BOUNDARY", "The minimum detail geometry exceeds the physical allocation")
            if count > 1:
                count -= 1
                continue
            if plan.mode == "overview" and not floor:
                floor = True
                continue
            raise MediaError("RESOURCE_BOUNDARY", "The minimum frame geometry exceeds the physical allocation")
        try:
            candidate = (
                trim_detail_page(raw_detail_candidate, count)
                if raw_detail_candidate is not None else
                await worker.run(
                    render_frame_page, source, info, plan,
                    offset=offset, count=count, budget=budget,
                    floor=floor, deadline_monotonic=deadline,
                )
            )
        except MediaError as exc:
            if exc.code != "RESOURCE_BOUNDARY":
                raise
            if plan.mode == "detail":
                # A worker-wide memory/serialization boundary may happen before
                # the renderer can return its admitted prefix. Try the actual
                # singleton atom rather than rerendering every trailing removal.
                if count > 1:
                    count = 1
                    continue
                raise
            if count > 1:
                count -= 1
                continue
            if plan.mode == "overview" and not floor:
                floor = True
                continue
            raise
        if plan.mode == "detail":
            # Exact RGB sharing determines physical image cost only while
            # rendering. A full requested slice may therefore yield its
            # actually admitted prefix; quote and continue from that prefix.
            rendered = json.loads(candidate.text).get("frames")
            if not isinstance(rendered, list) or not 1 <= len(rendered) <= count:
                raise ValueError("Rendered detail page is not a nonempty requested prefix")
            count = len(rendered)
            if raw_detail_candidate is None:
                raw_detail_candidate = candidate
        candidate = with_frame_page_continuation(
            candidate, source=source, info=info, plan=plan, position=position,
            count=count, selection_revision=FRAME_SELECTION_REVISION,
        )
        content = await image_validator.freeze(
            PromptContent((LLMTextPart(candidate.text), *(
                PromptImagePart(image.data, image.mime) for image in candidate.images
            ))), deadline_monotonic=deadline,
        )
        content = bind_prompt_images(content, workspace_id=workspace_id)
        logical_fits = page_budget.fits_logical(candidate.text)
        if allowance.accepts_page(
            content=content, source=image_source, tool_name="read_video_frames",
            page_budget=page_budget,
        ):
            source.assert_unchanged()
            return content
        if logical_fits and refresh_image_resources is not None and not refresh_attempted:
            # The batch owner prepares only the closed-prefix successor here;
            # it does not adopt a new epoch while physical tools are running.
            # Keep this page and fixed plan unaccepted until its fresh quote.
            refresh_attempted = True
            refreshed = await refresh_image_resources()
            source.assert_unchanged()
            if refreshed is not None:
                fresh_allowance, fresh_budget = refreshed
                if (
                    fresh_allowance.tool_call_id != allowance.tool_call_id
                    or fresh_allowance.call_ordinal != allowance.call_ordinal
                    or fresh_allowance.executor_binding_fingerprint
                    != allowance.executor_binding_fingerprint
                    or fresh_budget.tool_call_id != page_budget.tool_call_id
                ):
                    raise RuntimeError("Refreshed frame resources do not own this exact invocation")
                allowance, page_budget = fresh_allowance, fresh_budget
                # Earlier larger slices failed physical geometry/bytes or the
                # immutable wrapped 40k limit. This is still the largest viable
                # candidate; retest it before removing any selected observation.
                if allowance.accepts_page(
                    content=content, source=image_source,
                    tool_name="read_video_frames", page_budget=page_budget,
                ):
                    source.assert_unchanged()
                    return content
        # Detail retries project its one admitted raw encoded prefix. Other
        # modes release rejected pages before the next child renders.
        del content, candidate
        if count > 1:
            count -= 1
        elif plan.mode == "overview" and not floor:
            floor = True
        else:
            raise MediaError(
                "RESOURCE_BOUNDARY",
                "One minimum readable frame, its complete index and continuation do not fit this call",
            )


def _open_media_source(tool, arguments, *, deadline_monotonic):
    from pulsara_agent.tools.builtins.filesystem import path_target_arg

    if monotonic() >= deadline_monotonic:
        raise TimeoutError("Media source admission deadline expired")
    return MediaSource.open(tool._resolve_read_path(path_target_arg(arguments)))


def _read_media_settings(read_settings, *, deadline_monotonic):
    if monotonic() >= deadline_monotonic:
        raise TimeoutError("Media settings read deadline expired")
    return read_settings()
