"""Process-local artifact views and execution ports."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, Literal, Protocol

from pulsara_agent.message import ToolResultArtifactRef
from pulsara_agent.message import ToolResultPreviewMetadata
from pulsara_agent.ports.tool_execution import ToolCall, ToolExecutionResult
from pulsara_agent.primitives.model_call import sha256_fingerprint

class ArtifactContentConflict(RuntimeError):
    """A deterministic artifact id already names different semantic content."""


class ArtifactWriteResultView(Protocol):
    id: str
    digest: str
    size_bytes: int


class ArtifactPutConfirmationView(Protocol):
    result: ArtifactWriteResultView


class ModelArtifactStore(Protocol):
    """Narrow artifact authority needed by model lifecycle recovery."""

    def get_text(
        self,
        blob_id: str,
        *,
        session_id: str | None = None,
        deadline_monotonic: float | None = None,
    ) -> str: ...

    def put_text_if_absent_or_confirm_identical(
        self,
        blob_id: str,
        content: str,
        *,
        session_id: str | None,
        run_id: str | None,
        media_type: str,
        semantic_metadata: dict[str, Any],
        deadline_monotonic: float | None = None,
    ) -> ArtifactPutConfirmationView: ...


class ToolArtifactMode(StrEnum):
    DEFAULT = "default"
    NEVER = "never"
    ALWAYS = "always"
    LARGE_OUTPUT = "large_output"
    STRUCTURED_JSON = "structured_json"


DEFAULT_TOOL_ARTIFACT_THRESHOLD_BYTES = 8_000
DEFAULT_COMPLETE_PREVIEW_BODY_CHARS = 32_000
DEFAULT_LARGE_PREVIEW_CHARS = 8_000
DEFAULT_HUGE_OUTPUT_CHARS = 200_000
DEFAULT_HUGE_PREVIEW_CHARS = 4_000
DEFAULT_STREAMING_LIVE_HEAD_CAP_CHARS = 2_600
_HEAD_RATIO = 0.65


@dataclass(frozen=True, slots=True)
class ToolResultArtifactOptions:
    archive_threshold_bytes: int = DEFAULT_TOOL_ARTIFACT_THRESHOLD_BYTES
    complete_preview_body_chars: int = DEFAULT_COMPLETE_PREVIEW_BODY_CHARS
    large_preview_chars: int = DEFAULT_LARGE_PREVIEW_CHARS
    huge_output_chars: int = DEFAULT_HUGE_OUTPUT_CHARS
    huge_preview_chars: int = DEFAULT_HUGE_PREVIEW_CHARS
    streaming_live_head_cap_chars: int = DEFAULT_STREAMING_LIVE_HEAD_CAP_CHARS

    def __post_init__(self) -> None:
        values = (
            self.archive_threshold_bytes,
            self.complete_preview_body_chars,
            self.large_preview_chars,
            self.huge_output_chars,
            self.huge_preview_chars,
            self.streaming_live_head_cap_chars,
        )
        if any(value < 1 for value in values):
            raise ValueError("artifact processing bounds must be positive")

    @property
    def effective_archive_threshold_bytes(self) -> int:
        return self.archive_threshold_bytes

    @property
    def effective_large_preview_chars(self) -> int:
        return self.large_preview_chars


@dataclass(frozen=True, slots=True)
class AdaptivePreview:
    text: str
    policy: str
    original_chars: int
    original_bytes: int
    preview_chars: int
    visible_head_chars: int
    visible_tail_chars: int
    omitted_middle_chars: int

    def to_metadata(
        self, *, artifact_id: str | None = None
    ) -> ToolResultPreviewMetadata:
        read_more: dict[str, object] = {
            "tool": "artifact_read",
            "suggested_offset_chars": self.visible_head_chars,
            "suggested_max_chars": 20_000,
        }
        if artifact_id is not None:
            read_more["artifact_id"] = artifact_id
        return ToolResultPreviewMetadata(
            preview_policy=self.policy,  # type: ignore[arg-type]
            preview_chars=self.preview_chars,
            original_chars=self.original_chars,
            original_bytes=self.original_bytes,
            omitted_middle_chars=self.omitted_middle_chars,
            visible_head_chars=self.visible_head_chars,
            visible_tail_chars=self.visible_tail_chars,
            read_more=read_more,
        )


def build_adaptive_preview(
    text: str, options: ToolResultArtifactOptions
) -> AdaptivePreview:
    original_chars = len(text)
    original_bytes = len(text.encode("utf-8"))
    if original_chars <= options.complete_preview_body_chars:
        return AdaptivePreview(
            text=text,
            policy="full",
            original_chars=original_chars,
            original_bytes=original_bytes,
            preview_chars=original_chars,
            visible_head_chars=original_chars,
            visible_tail_chars=0,
            omitted_middle_chars=0,
        )
    budget = (
        options.huge_preview_chars
        if original_chars > options.huge_output_chars
        else options.effective_large_preview_chars
    )
    budget = max(1, min(budget, original_chars))
    preliminary_head = max(1, int(budget * _HEAD_RATIO))
    preliminary_tail = max(0, budget - preliminary_head)
    notice = _preview_truncation_notice(
        max(0, original_chars - preliminary_head - preliminary_tail),
        preliminary_head,
    )
    content_budget = max(1, budget - len(notice))
    head_chars = max(1, int(content_budget * _HEAD_RATIO))
    tail_chars = max(0, content_budget - head_chars)
    if head_chars + tail_chars >= original_chars:
        return AdaptivePreview(
            text=text,
            policy="full",
            original_chars=original_chars,
            original_bytes=original_bytes,
            preview_chars=original_chars,
            visible_head_chars=original_chars,
            visible_tail_chars=0,
            omitted_middle_chars=0,
        )
    omitted = max(0, original_chars - head_chars - tail_chars)
    notice = _preview_truncation_notice(omitted, head_chars)
    text_preview = (
        text[:head_chars] + notice + (text[-tail_chars:] if tail_chars else "")
    )
    return AdaptivePreview(
        text=text_preview,
        policy=(
            "head_tail_huge"
            if original_chars > options.huge_output_chars
            else "head_tail"
        ),
        original_chars=original_chars,
        original_bytes=original_bytes,
        preview_chars=len(text_preview),
        visible_head_chars=head_chars,
        visible_tail_chars=tail_chars,
        omitted_middle_chars=omitted,
    )


def _preview_truncation_notice(omitted: int, suggested_offset_chars: int) -> str:
    return (
        f"\n\n[OUTPUT TRUNCATED / PREVIEW: omitted {omitted} chars from the middle. "
        f"Full retained output is available via artifact_read. Prefer reading from offset_chars={suggested_offset_chars} "
        "if you need content after the visible head.]\n\n"
    )


def effective_terminal_output_cap(raw: object) -> int | None:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        return None
    from pulsara_agent.ports.terminal import (
        DEFAULT_MAX_OUTPUT_CHARS,
        MIN_TERMINAL_OUTPUT_CHARS,
    )

    return max(MIN_TERMINAL_OUTPUT_CHARS, min(raw, DEFAULT_MAX_OUTPUT_CHARS))


@dataclass(frozen=True, slots=True)
class ToolArtifactRecordView:
    artifact_id: str
    role: str
    media_type: str
    size_bytes: int
    stored_complete: bool
    loss_reason: str | None
    content_digest: str | None
    record_view_fingerprint: str


@dataclass(frozen=True, slots=True)
class ToolArtifactInfoView:
    record: ToolArtifactRecordView
    stored_at_utc: str
    created_at_utc: str | None
    info_view_fingerprint: str


@dataclass(frozen=True, slots=True)
class ToolArtifactTextSliceView:
    info: ToolArtifactInfoView
    text: str
    offset_chars: int
    returned_chars: int
    total_chars: int | None
    has_more: bool
    slice_view_fingerprint: str


@dataclass(frozen=True, slots=True)
class ToolResultArtifactProcessingPolicy:
    descriptor_id: str
    descriptor_fingerprint: str
    artifact_mode: ToolArtifactMode
    source_reference_policy: Literal["none", "reuse_input_artifact"]
    fallback_media_type: Literal["text/plain; charset=utf-8", "application/json"]
    archive_threshold_bytes: int
    complete_preview_body_chars: int
    large_preview_chars: int
    huge_output_chars: int
    huge_preview_chars: int
    streaming_live_head_cap_chars: int
    max_inline_chars: int | None
    policy_contract_version: Literal["tool-result-artifact-processing:v1"]
    policy_fingerprint: str


def build_tool_artifact_record_view(
    *,
    artifact_id: str,
    role: str,
    media_type: str,
    size_bytes: int,
    stored_complete: bool,
    loss_reason: str | None,
    content_digest: str | None,
) -> ToolArtifactRecordView:
    payload = {
        "artifact_id": artifact_id,
        "role": role,
        "media_type": media_type,
        "size_bytes": size_bytes,
        "stored_complete": stored_complete,
        "loss_reason": loss_reason,
        "content_digest": content_digest,
    }
    return ToolArtifactRecordView(
        **payload,
        record_view_fingerprint=sha256_fingerprint(
            "tool-artifact-record-view:v1", payload
        ),
    )


def build_tool_artifact_info_view(
    *,
    record: ToolArtifactRecordView,
    stored_at_utc: str,
    created_at_utc: str | None,
) -> ToolArtifactInfoView:
    payload = {
        "record": asdict(record),
        "stored_at_utc": stored_at_utc,
        "created_at_utc": created_at_utc,
    }
    return ToolArtifactInfoView(
        record=record,
        stored_at_utc=stored_at_utc,
        created_at_utc=created_at_utc,
        info_view_fingerprint=sha256_fingerprint("tool-artifact-info-view:v1", payload),
    )


def build_tool_artifact_text_slice_view(
    *,
    info: ToolArtifactInfoView,
    text: str,
    offset_chars: int,
    returned_chars: int,
    total_chars: int | None,
    has_more: bool,
) -> ToolArtifactTextSliceView:
    payload = {
        "info_view_fingerprint": info.info_view_fingerprint,
        "text": text,
        "offset_chars": offset_chars,
        "returned_chars": returned_chars,
        "total_chars": total_chars,
        "has_more": has_more,
    }
    return ToolArtifactTextSliceView(
        info=info,
        text=text,
        offset_chars=offset_chars,
        returned_chars=returned_chars,
        total_chars=total_chars,
        has_more=has_more,
        slice_view_fingerprint=sha256_fingerprint(
            "tool-artifact-text-slice-view:v1", payload
        ),
    )


def build_tool_result_artifact_processing_policy(
    *,
    descriptor_id: str,
    descriptor_fingerprint: str,
    artifact_mode: ToolArtifactMode,
    source_reference_policy: Literal["none", "reuse_input_artifact"],
    archive_threshold_bytes: int,
    complete_preview_body_chars: int,
    large_preview_chars: int,
    huge_output_chars: int,
    huge_preview_chars: int,
    streaming_live_head_cap_chars: int,
    max_inline_chars: int | None,
) -> ToolResultArtifactProcessingPolicy:
    bounds = (
        archive_threshold_bytes,
        complete_preview_body_chars,
        large_preview_chars,
        huge_output_chars,
        huge_preview_chars,
        streaming_live_head_cap_chars,
    )
    if any(value < 1 for value in bounds):
        raise ValueError("artifact processing bounds must be positive")
    if max_inline_chars is not None and max_inline_chars < 1:
        raise ValueError("max_inline_chars must be positive when provided")
    fallback_media_type = (
        "application/json"
        if artifact_mode is ToolArtifactMode.STRUCTURED_JSON
        else "text/plain; charset=utf-8"
    )
    payload = {
        "descriptor_id": descriptor_id,
        "descriptor_fingerprint": descriptor_fingerprint,
        "artifact_mode": artifact_mode.value,
        "source_reference_policy": source_reference_policy,
        "fallback_media_type": fallback_media_type,
        "archive_threshold_bytes": archive_threshold_bytes,
        "complete_preview_body_chars": complete_preview_body_chars,
        "large_preview_chars": large_preview_chars,
        "huge_output_chars": huge_output_chars,
        "huge_preview_chars": huge_preview_chars,
        "streaming_live_head_cap_chars": streaming_live_head_cap_chars,
        "max_inline_chars": max_inline_chars,
        "policy_contract_version": "tool-result-artifact-processing:v1",
    }
    return ToolResultArtifactProcessingPolicy(
        descriptor_id=descriptor_id,
        descriptor_fingerprint=descriptor_fingerprint,
        artifact_mode=artifact_mode,
        source_reference_policy=source_reference_policy,
        fallback_media_type=fallback_media_type,
        archive_threshold_bytes=archive_threshold_bytes,
        complete_preview_body_chars=complete_preview_body_chars,
        large_preview_chars=large_preview_chars,
        huge_output_chars=huge_output_chars,
        huge_preview_chars=huge_preview_chars,
        streaming_live_head_cap_chars=streaming_live_head_cap_chars,
        max_inline_chars=max_inline_chars,
        policy_contract_version="tool-result-artifact-processing:v1",
        policy_fingerprint=sha256_fingerprint(
            "tool-result-artifact-processing-policy:v1", payload
        ),
    )


def resolve_tool_result_artifact_policy_for_call(
    *,
    base_policy: ToolResultArtifactProcessingPolicy,
    tool_call: ToolCall,
) -> ToolResultArtifactProcessingPolicy:
    """Derive the bounded call-local policy from one frozen surface policy."""

    if tool_call.name not in {"terminal", "terminal_process", "terminal_monitor"}:
        return base_policy
    cap = effective_terminal_output_cap(tool_call.arguments.get("max_output_chars"))
    if cap is None:
        return base_policy
    options = ToolResultArtifactOptions(
        archive_threshold_bytes=base_policy.archive_threshold_bytes,
        complete_preview_body_chars=min(base_policy.complete_preview_body_chars, cap),
        large_preview_chars=min(base_policy.large_preview_chars, cap),
        huge_output_chars=base_policy.huge_output_chars,
        huge_preview_chars=min(base_policy.huge_preview_chars, cap),
        streaming_live_head_cap_chars=1,
    )
    huge_head_cap = build_adaptive_preview(
        "x" * (base_policy.huge_output_chars + 1), options
    ).visible_head_chars
    return build_tool_result_artifact_processing_policy(
        descriptor_id=base_policy.descriptor_id,
        descriptor_fingerprint=base_policy.descriptor_fingerprint,
        artifact_mode=base_policy.artifact_mode,
        source_reference_policy=base_policy.source_reference_policy,
        archive_threshold_bytes=base_policy.archive_threshold_bytes,
        complete_preview_body_chars=min(base_policy.complete_preview_body_chars, cap),
        large_preview_chars=min(base_policy.large_preview_chars, cap),
        huge_output_chars=base_policy.huge_output_chars,
        huge_preview_chars=min(base_policy.huge_preview_chars, cap),
        streaming_live_head_cap_chars=max(
            1,
            min(base_policy.streaming_live_head_cap_chars, huge_head_cap),
        ),
        max_inline_chars=base_policy.max_inline_chars,
    )


class ToolArtifactReadPort(Protocol):
    def lookup(self, artifact_id: str) -> ToolArtifactRecordView | None: ...

    def info(self, artifact_id: str) -> ToolArtifactInfoView: ...

    def read_text(
        self, artifact_id: str, *, offset_chars: int, max_chars: int
    ) -> ToolArtifactTextSliceView: ...


class ToolResultArtifactProcessingPort(Protocol):
    def process_result(
        self,
        result: ToolExecutionResult,
        *,
        tool_call: ToolCall,
        policy: ToolResultArtifactProcessingPolicy,
    ) -> tuple[ToolExecutionResult, tuple[ToolResultArtifactRef, ...]]: ...


__all__ = [
    "AdaptivePreview",
    "ArtifactContentConflict",
    "ArtifactPutConfirmationView",
    "ArtifactWriteResultView",
    "ModelArtifactStore",
    "ToolArtifactInfoView",
    "ToolArtifactMode",
    "ToolArtifactReadPort",
    "ToolArtifactRecordView",
    "ToolArtifactTextSliceView",
    "ToolResultArtifactProcessingPolicy",
    "ToolResultArtifactProcessingPort",
    "ToolResultArtifactOptions",
    "build_adaptive_preview",
    "build_tool_artifact_info_view",
    "build_tool_artifact_record_view",
    "build_tool_artifact_text_slice_view",
    "build_tool_result_artifact_processing_policy",
    "effective_terminal_output_cap",
    "resolve_tool_result_artifact_policy_for_call",
]
