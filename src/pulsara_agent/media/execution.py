"""Acquire complete media business values before native delivery or JS return.

PyAV/Pillow and the existing worker, cache and ASR work owners retain physical
execution. This adapter owns public ordered content, not display pagination.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from time import monotonic

from pulsara_agent.conversation_kernel.content_parts import ResultPart
from pulsara_agent.conversation_kernel.io import PhysicalToolInvocationDisposition
from pulsara_agent.conversation_kernel.limits import STAGE2_LIMITS
from pulsara_agent.conversation_kernel.prompt_content import (
    MAXIMUM_PROMPT_MULTIPART_BYTES,
    bind_prompt_images,
)
from pulsara_agent.llm.input import (
    FrozenPromptContent,
    LLMImagePart,
    MAXIMUM_PROMPT_IMAGE_PIXELS,
)
from pulsara_agent.ports.tool_execution import (
    FrozenToolJsonDict,
    ToolBusinessValue,
    freeze_tool_json_object,
)
from pulsara_agent.tools.builtins.filesystem import (
    _relpath,
    file_target,
    path_target_arg,
)
from pulsara_agent.tools.builtins.media import validate_media_arguments
from .frames import (
    FRAME_ANALYSIS_FRAMES,
    FrameBudget,
    frame_page_fits_geometry,
    prepare_frame_plan,
    render_frame_page,
)
from .source import MediaError, MediaSource, probe_media, validate_range
from .transcript import TranscriptService


@dataclass(frozen=True, slots=True)
class MediaExecutionResult:
    state: str
    content: ToolBusinessValue
    result_facts: FrozenToolJsonDict = field(default_factory=FrozenToolJsonDict)
    pending_images: tuple[LLMImagePart, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "result_facts", freeze_tool_json_object(self.result_facts)
        )


async def execute_media(
    tool,
    arguments,
    *,
    io,
    worker,
    image_validator,
    cache,
    read_settings,
    asr_runtime,
    memory_domain_id,
    deadline,
    reserve_read,
    observe_read,
) -> MediaExecutionResult:
    source = None
    parts: list[ResultPart] = []
    images: list[LLMImagePart] = []
    fields = {}
    reserved = False

    def finish(state, message=None, *, facts=None):
        if message is not None:
            parts.append(ResultPart("text", message))
        referenced = {part.value["image_ref"] for part in parts if part.type == "image"}
        return MediaExecutionResult(
            state,
            ToolBusinessValue(fields, tuple(parts)),
            result_facts=facts or {},
            pending_images=tuple(
                image for image in images if image.image_ref in referenced
            ),
        )

    def reserve_once():
        nonlocal reserved
        if not reserved:
            reserve_read(
                canonical_path=str(source.path), file_mtime_ns=str(source.modified_ns)
            )
            reserved = True

    def begin(a, b):
        reserve_once()
        observe_read(
            canonical_path=str(source.path),
            file_mtime_ns=str(source.modified_ns),
            from_seconds=a,
            to_seconds=b,
        )

    try:
        validate_media_arguments(tool.name, arguments)
        opened = await io.run_tool_invocation(
            _open_media_source, tool, arguments, deadline_monotonic=deadline
        )
        if opened.disposition is PhysicalToolInvocationDisposition.RAISED:
            raise opened.error
        source = opened.value
        if opened.caller_cancelled:
            raise asyncio.CancelledError
        fields["target"] = file_target(_relpath(source.path, tool.workspace_root))
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
        start, end = span.start, span.end
        if tool.name == "annotate_media":
            source.assert_unchanged()
            return finish(
                "SUCCESS",
                "已添加时间标注。",
                facts={
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

            async def load_asr_settings():
                return await io.run(
                    _read_media_settings, read_settings, deadline_monotonic=deadline
                )

            service = TranscriptService(
                work=asr_runtime.work if asr_runtime is not None else None,
                cache=cache,
                asr_config=None,
                run_media=run_media,
                run_cache=run_cache,
                run_asr_media=asr_runtime.run_media
                if asr_runtime is not None
                else run_media,
                run_asr_cache=asr_runtime.run_cache
                if asr_runtime is not None
                else run_cache,
                load_asr_settings=load_asr_settings
                if read_settings is not None
                else None,
                # These remain the existing per-upload/response ASR I/O bounds,
                # independent of complete transcript length or model delivery.
                maximum_upload_bytes=STAGE2_LIMITS.canonical_blob_hard_bytes,
                maximum_response_bytes=STAGE2_LIMITS.tool_result_hard_bytes,
                deadline_monotonic=deadline,
            )
            value = await service.read(
                source,
                info,
                arguments,
                memory_domain_id=memory_domain_id,
                on_started=begin,
            )
            meta = {}
            if value.source is not None:
                meta["source"] = value.source
            if value.language is not None:
                meta["language"] = value.language
            fields["meta"] = meta
            parts.append(
                ResultPart("text", f"请求 [{start:g}, {end:g}) 秒的原始时间文字。\n")
            )
            parts.extend(
                ResultPart("object", item.public_value((start, end)))
                for item in value.entries
            )
            parts.extend(
                ResultPart("text", str(message) + "\n") for message in value.diagnostics
            )
            for error in value.errors:
                parts.append(
                    ResultPart(
                        "text",
                        f"文字读取未完成：{error.get('message', error.get('error'))}\n",
                    )
                )
            if value.errors and value.processed_to < end:
                parts.append(
                    ResultPart(
                        "text",
                        f"已取得连续范围 [{start:g}, {value.processed_to:g}) 秒；后续至 {end:g} 秒未完成。\n",
                    )
                )
            if not value.entries:
                parts.append(
                    ResultPart("text", "未取得文字；这不证明没有声音或语音。\n")
                )
                parts.extend(
                    ResultPart(
                        "text",
                        f"实际音频观察 [{item['from']:g}, {item['to']:g}) 秒：{item['state']}。\n",
                    )
                    for item in value.sound_observations
                )
            return finish("APPLICATION_ERROR" if value.errors else "SUCCESS")
        if info.video_stream_index is None:
            raise MediaError("NO_VIDEO_TRACK", "This source has no usable video track")
        if image_validator is None:
            raise RuntimeError("video image validation owner is unavailable")
        action, crop = arguments.get("action", "overview"), arguments.get("crop")
        description = {
            "detail": "首、中、终点之前三个时间目标",
            "overview": (
                "稀疏代表状态。representative_range 是按代表帧时间划分的导航区间，"
                "端点与分镜切点无关"
            ),
            "motion": "按真实时间顺序观察",
        }[action]
        heading = f"{action}：请求 [{start:g}, {end:g}) 秒；{description}。"
        parts.append(ResultPart("text", heading + "\n"))
        # Selection/render batches bound the live raster working set only. They
        # never stop the request or create a source continuation for the model.
        budget = FrameBudget(
            MAXIMUM_PROMPT_MULTIPART_BYTES,
            MAXIMUM_PROMPT_IMAGE_PIXELS,
            max(1, MAXIMUM_PROMPT_IMAGE_PIXELS // (160 * 90)),
            FRAME_ANALYSIS_FRAMES,
        )
        window = None
        previous_t = None
        while True:
            plan = await worker.run(
                prepare_frame_plan,
                source,
                info,
                start=start,
                end=end,
                mode=action,
                crop=crop,
                window_start=window,
                analysis_frames=budget.analysis_frames,
                on_progress=lambda span: begin(*span),
                deadline_monotonic=deadline,
            )
            plan = replace(
                plan, priority=tuple(sorted(plan.priority, key=lambda item: item.t))
            )
            offset = 0
            while offset < plan.observation_count:
                count = plan.observation_count - offset
                while not frame_page_fits_geometry(
                    plan, offset=offset, count=count, budget=budget
                ):
                    if count == 1:
                        raise MediaError(
                            "RESOURCE_BOUNDARY",
                            "One readable frame cannot fit the physical render allocation",
                        )
                    count -= 1
                while True:
                    try:
                        rendered = await worker.run(
                            render_frame_page,
                            source,
                            info,
                            plan,
                            offset=offset,
                            count=count,
                            budget=budget,
                            deadline_monotonic=deadline,
                        )
                        break
                    except MediaError as exc:
                        if exc.code != "RESOURCE_BOUNDARY" or count == 1:
                            raise
                        count -= 1
                bound = {}

                async def validated_image(number):
                    if number not in bound:
                        image = rendered.images[number - 1]
                        validated = await image_validator.freeze_local_image(
                            image.data, deadline_monotonic=deadline
                        )
                        validated = bind_prompt_images(
                            FrozenPromptContent((validated,))
                        ).parts[0]
                        bound[number] = validated
                        images.append(validated)
                    return bound[number]

                if not rendered.index:
                    raise MediaError(
                        "MEDIA_FRAME_UNAVAILABLE", "Rendering produced no observations"
                    )
                if action == "detail":
                    for item in rendered.index:
                        image = await validated_image(item["image"])
                        index = {
                            key: value
                            for key, value in item.items()
                            if key in {"t", "visible_range", "dt"}
                        }
                        if previous_t is not None:
                            index["dt"] = item["t"] - previous_t
                        previous_t = item["t"]
                        parts.extend(
                            (
                                ResultPart("object", index),
                                ResultPart(
                                    "image",
                                    {
                                        "kind": "IMAGE_REF",
                                        "image_ref": image.image_ref,
                                    },
                                ),
                            )
                        )
                else:
                    for number in range(1, len(rendered.images) + 1):
                        image = await validated_image(number)
                        cells = []
                        for item in rendered.index:
                            if item["image"] != number:
                                continue
                            cell = {
                                key: value
                                for key, value in item.items()
                                if key != "image"
                            }
                            cell["n"] = len(cells) + 1
                            if previous_t is not None:
                                cell["dt"] = item["t"] - previous_t
                            previous_t = item["t"]
                            cells.append(cell)
                        parts.extend(
                            (
                                ResultPart("object", {"frames": cells}),
                                ResultPart(
                                    "image",
                                    {"kind": "IMAGE_REF", "image_ref": image.image_ref},
                                ),
                            )
                        )
                offset += len(rendered.index)
            if info.video_start is not None and info.video_start > start:
                message = "视频轨从请求起点之后开始；开头缺少可验证画面。\n"
                if not any(
                    part.type == "text" and part.value == message for part in parts
                ):
                    parts.append(ResultPart("text", message))
            if plan.video_ended and plan.complete:
                video_end = float(plan.processed_range[1])
                parts.append(
                    ResultPart(
                        "text",
                        f"视频轨在 {video_end} 秒结束；请求尾部 "
                        f"[{video_end},{end}) 秒没有可验证画面。\n",
                    )
                )
            if plan.complete:
                break
            following = plan.next_window_start
            if action != "motion" or following is None or following == window:
                raise MediaError(
                    "MEDIA_FRAME_UNAVAILABLE",
                    "Video analysis could not advance to its next exact frame",
                )
            window = following
        source.assert_unchanged()
        if action == "motion":
            frame_count = sum(
                len(part.value["frames"]) for part in parts if part.type == "object"
            )
            parts[0] = ResultPart(
                "text",
                f"{heading}完整结果包含 {frame_count} 帧、{len(images)} 张拼图，"
                f"最后一帧起于 {previous_t:.6f} 秒。\n",
            )
        return finish("SUCCESS")
    except asyncio.CancelledError:
        if parts:
            return finish("CANCELLED", "媒体读取已取消；以上为实际已取得的内容。\n")
        raise
    except (MediaError, OSError, ValueError, TimeoutError) as exc:
        if "target" not in fields:
            try:
                fields["target"] = file_target(
                    _relpath(
                        tool._resolve_read_path(path_target_arg(arguments)),
                        tool.workspace_root,
                    )
                )
            except (ValueError, OSError):
                pass
        return finish("APPLICATION_ERROR", f"媒体读取未完成：{exc}\n")
    finally:
        if source is not None:
            source.close()


def _open_media_source(tool, arguments, *, deadline_monotonic):
    if monotonic() >= deadline_monotonic:
        raise TimeoutError("Media source admission deadline expired")
    return MediaSource.open(tool._resolve_read_path(path_target_arg(arguments)))


def _read_media_settings(read_settings, *, deadline_monotonic):
    if monotonic() >= deadline_monotonic:
        raise TimeoutError("Media settings read deadline expired")
    return read_settings()
