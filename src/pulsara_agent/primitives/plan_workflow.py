"""Closed Plan workflow values and canonical content extraction."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
import json

from pulsara_agent.primitives.context import FrozenJsonObjectFact, thaw_json
from pulsara_agent.primitives.permission import PermissionMode


# Native Plan interactions wait ten minutes, independent of task lifetime.
PLAN_INTERACTION_WAIT_SECONDS = 10 * 60
MAXIMUM_PLAN_QUESTION_BYTES = 16 * 1024
MAXIMUM_PLAN_OPTION_LABEL_BYTES = 256
MAXIMUM_PLAN_OPTION_DESCRIPTION_BYTES = 2 * 1024
MAXIMUM_PLAN_DRAFT_BYTES = 1 * 1024 * 1024
MAXIMUM_PLAN_RESPONSE_BYTES = 32 * 1024
MINIMUM_PLAN_CHUNK_BYTES = 4
MAXIMUM_PLAN_CHUNK_BYTES = 64 * 1024


class PlanWorkflowStatus(StrEnum):
    ACTIVE = "ACTIVE"
    EXITED = "EXITED"
    APPROVED = "APPROVED"
    CANCELLED = "CANCELLED"
    FORCE_EXITED = "FORCE_EXITED"


class PlanWorkflowEnteredBy(StrEnum):
    USER = "USER"
    AGENT = "AGENT"


class PlanInteractionKind(StrEnum):
    QUESTION = "QUESTION"
    DRAFT_REVIEW = "DRAFT_REVIEW"


class PlanHandoffKind(StrEnum):
    ENTERED_PLAN = "ENTERED_PLAN"
    EXITED_PLAN = "EXITED_PLAN"
    REVISION_REQUESTED = "REVISION_REQUESTED"
    APPROVED_PLAN = "APPROVED_PLAN"
    CANCELLED_PLAN = "CANCELLED_PLAN"
    FORCE_EXITED_PLAN = "FORCE_EXITED_PLAN"


class PlanQuestionAnswerKind(StrEnum):
    OPTION = "OPTION"
    FREE_TEXT = "FREE_TEXT"


class PlanDraftDecision(StrEnum):
    APPROVE = "APPROVE"
    REVISE = "REVISE"
    CANCEL = "CANCEL"



def validate_plan_entry_arguments(*, arguments: FrozenJsonObjectFact) -> None:
    if thaw_json(arguments) != {}:
        raise ValueError("enter_plan 只接受空对象 {}。")


def plan_enter_schema():
    return {"type": "object", "properties": {}, "additionalProperties": False}


def plan_question_schema():
    return {"type": "object", "additionalProperties": False, "required": ["question"], "properties": {
        "question": {"type": "string", "minLength": 1, "maxLength": MAXIMUM_PLAN_QUESTION_BYTES},
        "options": {"type": "array", "items": {"type": "object", "additionalProperties": False,
            "required": ["label"], "properties": {
                "label": {"type": "string", "minLength": 1, "maxLength": MAXIMUM_PLAN_OPTION_LABEL_BYTES},
                "description": {"type": "string", "maxLength": MAXIMUM_PLAN_OPTION_DESCRIPTION_BYTES},
                "recommended": {"type": "boolean"},
            }}, "oneOf": [{"maxItems": 0}, {"minItems": 2, "maxItems": 3}]},
    }}


def plan_exit_schema():
    return {"type": "object", "additionalProperties": False, "properties": {
        "plan": {"type": ["string", "null"], "maxLength": MAXIMUM_PLAN_DRAFT_BYTES},
        "summary": {"type": "string"},
    }}


@dataclass(frozen=True, slots=True)
class PlanQuestionOption:
    ordinal: int
    label: str
    description: str
    recommended: bool


@dataclass(frozen=True, slots=True)
class PlanQuestionContent:
    interaction_id: str
    question: str
    options: tuple[PlanQuestionOption, ...]
    typed_content_fingerprint: str


@dataclass(frozen=True, slots=True)
class PlanDraftContentIdentity:
    interaction_id: str
    assistant_entry_id: str
    tool_call_id: str
    request_semantic_digest: str
    plan_utf8_size: int
    plan_utf8_digest: str


@dataclass(frozen=True, slots=True)
class PlanDraftTextChunk:
    identity: PlanDraftContentIdentity
    offset_utf8_bytes: int
    body: str
    next_offset_utf8_bytes: int
    eof: bool


@dataclass(frozen=True, slots=True)
class ExtractedPlanDraft:
    identity: PlanDraftContentIdentity
    exact_plan_utf8: bytes
    summary: str | None


def _bounded_text(
    value: object,
    *,
    name: str,
    maximum_bytes: int,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    encoded = value.encode("utf-8")
    if (not allow_empty and not encoded) or len(encoded) > maximum_bytes:
        raise ValueError(f"{name} is outside its UTF-8 bound")
    return value


def _canonical_fingerprint(domain: str, value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + sha256(domain.encode("utf-8") + b"\0" + encoded).hexdigest()


def plan_draft_utf8_digest(body: bytes) -> str:
    return "sha256:" + sha256(
        b"pulsara:plan-draft-utf8:v1\0"
        + len(body).to_bytes(8, "big", signed=False)
        + body
    ).hexdigest()


def parse_plan_question_arguments(
    *, arguments: FrozenJsonObjectFact,
) -> tuple[str, tuple[PlanQuestionOption, ...]]:
    """Validate content before a workflow or interaction exists."""
    raw = thaw_json(arguments)
    if not isinstance(raw, dict):
        raise ValueError("Plan question arguments must be an object")
    if set(raw) - {"question", "options"}:
        raise ValueError("Plan question arguments contain unknown fields")
    question = _bounded_text(
        raw.get("question"),
        name="question",
        maximum_bytes=MAXIMUM_PLAN_QUESTION_BYTES,
    )
    option_values = raw.get("options", [])
    if not isinstance(option_values, list) or len(option_values) not in {0, 2, 3}:
        raise ValueError("Plan question must provide zero or two-to-three options")
    options: list[PlanQuestionOption] = []
    labels: set[str] = set()
    for ordinal, item in enumerate(option_values):
        if not isinstance(item, dict) or set(item) - {
            "label",
            "description",
            "recommended",
        }:
            raise ValueError("Plan question option is invalid")
        label = _bounded_text(
            item.get("label"),
            name="option label",
            maximum_bytes=MAXIMUM_PLAN_OPTION_LABEL_BYTES,
        )
        if label in labels:
            raise ValueError("Plan question option labels must be unique")
        labels.add(label)
        description = _bounded_text(
            item.get("description", ""),
            name="option description",
            maximum_bytes=MAXIMUM_PLAN_OPTION_DESCRIPTION_BYTES,
            allow_empty=True,
        )
        recommended = item.get("recommended", False)
        if not isinstance(recommended, bool):
            raise ValueError("Plan question recommended flag must be boolean")
        options.append(
            PlanQuestionOption(ordinal, label, description, recommended)
        )
    if sum(item.recommended for item in options) > 1:
        raise ValueError("Plan question options allow at most one recommendation")
    return question, tuple(options)


def extract_plan_question(
    *,
    interaction_id: str,
    arguments: FrozenJsonObjectFact,
) -> PlanQuestionContent:
    question, options = parse_plan_question_arguments(
        arguments=arguments
    )
    payload = {
        "interaction_id": interaction_id,
        "question": question,
        "options": [
            {
                "ordinal": item.ordinal,
                "label": item.label,
                "description": item.description,
                "recommended": item.recommended,
            }
            for item in options
        ],
    }
    return PlanQuestionContent(
        interaction_id=interaction_id,
        question=question,
        options=tuple(options),
        typed_content_fingerprint=_canonical_fingerprint(
            "pulsara:plan-question-content:v1", payload
        ),
    )


def parse_plan_exit_arguments(
    *,
    arguments: FrozenJsonObjectFact,
) -> tuple[bytes | None, str | None]:
    """A missing or blank proposal exits planning without a review interaction."""
    raw = thaw_json(arguments)
    if not isinstance(raw, dict) or set(raw) - {"plan", "summary"}:
        raise ValueError("Plan draft arguments are invalid")
    value = raw.get("plan")
    plan = (
        None
        if value is None
        else _bounded_text(
            value,
            name="plan",
            maximum_bytes=MAXIMUM_PLAN_DRAFT_BYTES,
            allow_empty=True,
        )
    )
    summary = raw.get("summary")
    if "summary" in raw and not isinstance(summary, str):
        raise ValueError("summary must be a string")
    body = None if plan is None or not plan.strip() else plan.encode("utf-8")
    return body, summary


def parse_plan_draft_arguments(
    *,
    arguments: FrozenJsonObjectFact,
) -> tuple[bytes, str | None]:
    body, summary = parse_plan_exit_arguments(arguments=arguments)
    if body is None:
        raise ValueError("Plan draft requires a nonblank proposal")
    return body, summary


def extract_plan_draft(
    *,
    interaction_id: str,
    assistant_entry_id: str,
    tool_call_id: str,
    request_semantic_digest: str,
    arguments: FrozenJsonObjectFact,
) -> ExtractedPlanDraft:
    body, summary = parse_plan_draft_arguments(arguments=arguments)
    identity = PlanDraftContentIdentity(
        interaction_id=interaction_id,
        assistant_entry_id=assistant_entry_id,
        tool_call_id=tool_call_id,
        request_semantic_digest=request_semantic_digest,
        plan_utf8_size=len(body),
        plan_utf8_digest=plan_draft_utf8_digest(body),
    )
    return ExtractedPlanDraft(identity, body, summary)


def read_plan_draft_chunk(
    draft: ExtractedPlanDraft,
    *,
    offset_utf8_bytes: int,
    limit_bytes: int,
) -> PlanDraftTextChunk:
    body = draft.exact_plan_utf8
    if not MINIMUM_PLAN_CHUNK_BYTES <= limit_bytes <= MAXIMUM_PLAN_CHUNK_BYTES:
        raise ValueError("Plan draft chunk limit is invalid")
    if not 0 <= offset_utf8_bytes <= len(body):
        raise ValueError("Plan draft offset is invalid")
    try:
        body[:offset_utf8_bytes].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Plan draft offset is not a UTF-8 boundary") from exc
    end = min(len(body), offset_utf8_bytes + limit_bytes)
    while end > offset_utf8_bytes:
        try:
            text = body[offset_utf8_bytes:end].decode("utf-8")
            break
        except UnicodeDecodeError:
            end -= 1
    else:
        text = ""
    if end == offset_utf8_bytes and end < len(body):
        raise ValueError("Plan draft chunk limit cannot contain one code point")
    return PlanDraftTextChunk(
        identity=draft.identity,
        offset_utf8_bytes=offset_utf8_bytes,
        body=text,
        next_offset_utf8_bytes=end,
        eof=end == len(body),
    )


def project_plan_continuation_for_provider(
    *,
    storage_text: str,
    transition: str,
    workflow_id: str,
    interaction_id: str | None,
) -> str:
    """Validate one canonical continuation carrier and render its provider value."""

    if transition not in {"ENTERED_PLAN", "APPROVED_PLAN", "EXITED_PLAN"}:
        raise ValueError("Plan continuation transition is invalid")
    try:
        value = json.loads(storage_text)
    except json.JSONDecodeError as exc:
        raise ValueError("Plan continuation storage carrier is invalid") from exc
    if not isinstance(value, dict) or str(value.get("transition") or "") != transition:
        raise ValueError("Plan continuation storage transition conflicts")
    if (
        value.get("workflow_id") is not None
        and str(value["workflow_id"]) != workflow_id
    ):
        raise ValueError("Plan continuation workflow conflicts")
    if value.get("interaction_id") is not None and str(value["interaction_id"]) != str(
        interaction_id
    ):
        raise ValueError("Plan continuation interaction conflicts")

    projected: dict[str, object] = {
        "status": {
            "APPROVED_PLAN": "APPROVED",
            "ENTERED_PLAN": "ACTIVE",
            "EXITED_PLAN": "EXITED",
        }[transition],
        "transition": transition,
    }
    if transition == "EXITED_PLAN":

        projected["resume_permission_mode"] = PermissionMode(
            value.get("resume_permission_mode")
        ).value
    if transition == "APPROVED_PLAN" and not isinstance(
        value.get("approved_plan"), dict
    ):
        raise ValueError("approved Plan identity carrier is invalid")
    return json.dumps(
        {"pulsara_plan_continuation": projected},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


__all__ = [
    "ExtractedPlanDraft",
    "MAXIMUM_PLAN_RESPONSE_BYTES",
    "PlanDraftContentIdentity",
    "PlanDraftDecision",
    "PlanDraftTextChunk",
    "PlanHandoffKind",
    "PlanInteractionKind",
    "PlanQuestionAnswerKind",
    "PlanQuestionContent",
    "PlanQuestionOption",
    "PlanWorkflowEnteredBy",
    "PlanWorkflowStatus",
    "validate_plan_entry_arguments",
    "plan_enter_schema",
    "plan_question_schema",
    "plan_exit_schema",
    "extract_plan_draft",
    "extract_plan_question",
    "plan_draft_utf8_digest",
    "project_plan_continuation_for_provider",
    "read_plan_draft_chunk",
]
