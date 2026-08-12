"""Closed Plan workflow values and the sole historical content extractor."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
import json

from pulsara_agent.primitives.context import FrozenJsonObjectFact, thaw_json


MAXIMUM_PLAN_ENTRY_REASON_BYTES = 4 * 1024
MAXIMUM_PLAN_QUESTION_BYTES = 16 * 1024
MAXIMUM_PLAN_OPTION_LABEL_BYTES = 256
MAXIMUM_PLAN_OPTION_DESCRIPTION_BYTES = 2 * 1024
MAXIMUM_PLAN_DRAFT_BYTES = 1 * 1024 * 1024
MAXIMUM_PLAN_SUMMARY_BYTES = 8 * 1024
MAXIMUM_PLAN_RESPONSE_BYTES = 32 * 1024
MINIMUM_PLAN_CHUNK_BYTES = 4
MAXIMUM_PLAN_CHUNK_BYTES = 64 * 1024


class PlanWorkflowStatus(StrEnum):
    ACTIVE = "ACTIVE"
    APPROVED = "APPROVED"
    CANCELLED = "CANCELLED"
    FORCE_EXITED = "FORCE_EXITED"


class PlanWorkflowEnteredBy(StrEnum):
    USER = "USER"
    AGENT = "AGENT"


class PlanInteractionKind(StrEnum):
    QUESTION = "QUESTION"
    DRAFT_REVIEW = "DRAFT_REVIEW"


class PlanInteractionStatus(StrEnum):
    OPEN = "OPEN"
    ANSWERED = "ANSWERED"
    APPROVED = "APPROVED"
    REVISION_REQUESTED = "REVISION_REQUESTED"
    CANCELLED = "CANCELLED"
    ABORTED = "ABORTED"


class PlanHandoffKind(StrEnum):
    ENTERED_PLAN = "ENTERED_PLAN"
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


class PlanApprovedMaterializationDisposition(StrEnum):
    PIN_EXISTING_CANONICAL_BLOCK = "PIN_EXISTING_CANONICAL_BLOCK"
    MATERIALIZE_REFERENCED_BLOCK = "MATERIALIZE_REFERENCED_BLOCK"


# Closed historical decoder registry.  These identities are copied from the
# tool surface that was actually frozen for the originating model call.  A
# replacement binary may add another explicitly supported tuple, but it must
# never guess a historical payload from a current tool name.
PLAN_INTERACTION_CONTRACTS: dict[tuple[str, str, str], str] = {
    (
        "pulsara.workflow.ask_plan_question",
        "v1",
        "sha256:d19178a8bc3eaa69f03ce3151b1803b760be6afa55f5f58f489e58e45d5420b9",
    ): "ask_plan_question",
    (
        "pulsara.workflow.exit_plan",
        "v1",
        "sha256:98849b9f64ec6170cb509cdc9c4ff1292f99c0b98c657d518117340466dab336",
    ): "exit_plan",
}
PLAN_ENTRY_CONTRACT = (
    "pulsara.workflow.enter_plan",
    "v1",
    "sha256:d53f9ac13a86f6bdfc72812e1eb5693b3bb17e19d143c8a573b426d2f094dbaa",
)


@dataclass(frozen=True, slots=True)
class PlanInteractionBinding:
    contract_id: str
    contract_version: str
    contract_fingerprint: str

    def __post_init__(self) -> None:
        if not self.contract_id or not self.contract_version:
            raise ValueError("Plan interaction binding is incomplete")
        if not self.contract_fingerprint.startswith("sha256:"):
            raise ValueError("Plan interaction binding fingerprint is invalid")

    @property
    def identity(self) -> tuple[str, str, str]:
        return (
            self.contract_id,
            self.contract_version,
            self.contract_fingerprint,
        )


def require_plan_interaction_contract(
    binding: PlanInteractionBinding,
    *,
    expected_tool_name: str,
) -> None:
    if PLAN_INTERACTION_CONTRACTS.get(binding.identity) != expected_tool_name:
        raise ValueError("Plan interaction contract is unavailable")


def extract_plan_entry_reason(
    *,
    binding: PlanInteractionBinding,
    arguments: FrozenJsonObjectFact,
) -> str:
    """Validate and extract the bounded semantic payload of ``enter_plan``."""

    if binding.identity != PLAN_ENTRY_CONTRACT:
        raise ValueError("Plan entry contract is unavailable")
    raw = thaw_json(arguments)
    if not isinstance(raw, dict) or set(raw) - {"reason"}:
        raise ValueError("Plan entry arguments are invalid")
    return _bounded_text(
        raw.get("reason", ""),
        name="reason",
        maximum_bytes=MAXIMUM_PLAN_ENTRY_REASON_BYTES,
        allow_empty=True,
    )


@dataclass(frozen=True, slots=True)
class PlanQuestionOption:
    ordinal: int
    label: str
    description: str
    recommended: bool


@dataclass(frozen=True, slots=True)
class PlanQuestionContent:
    interaction_id: str
    request_contract_id: str
    request_contract_version: str
    request_contract_fingerprint: str
    question: str
    options: tuple[PlanQuestionOption, ...]
    allow_free_text: bool
    typed_content_fingerprint: str


@dataclass(frozen=True, slots=True)
class PlanDraftContentIdentity:
    interaction_id: str
    assistant_entry_id: str
    tool_call_id: str
    request_contract_id: str
    request_contract_version: str
    request_contract_fingerprint: str
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


def extract_plan_question(
    *,
    interaction_id: str,
    binding: PlanInteractionBinding,
    arguments: FrozenJsonObjectFact,
) -> PlanQuestionContent:
    require_plan_interaction_contract(
        binding, expected_tool_name="ask_plan_question"
    )
    raw = thaw_json(arguments)
    if not isinstance(raw, dict):
        raise ValueError("Plan question arguments must be an object")
    if set(raw) - {"question", "options", "allow_free_text", "reason"}:
        raise ValueError("Plan question arguments contain unknown fields")
    question = _bounded_text(
        raw.get("question"),
        name="question",
        maximum_bytes=MAXIMUM_PLAN_QUESTION_BYTES,
    )
    if "reason" in raw:
        _bounded_text(
            raw["reason"],
            name="reason",
            maximum_bytes=MAXIMUM_PLAN_ENTRY_REASON_BYTES,
            allow_empty=True,
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
    allow_free_text = raw.get("allow_free_text")
    if not isinstance(allow_free_text, bool):
        raise ValueError("Plan question free-text flag must be boolean")
    if not options and not allow_free_text:
        raise ValueError("optionless Plan question must allow free text")
    payload = {
        "interaction_id": interaction_id,
        "request_contract_id": binding.contract_id,
        "request_contract_version": binding.contract_version,
        "request_contract_fingerprint": binding.contract_fingerprint,
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
        "allow_free_text": allow_free_text,
    }
    return PlanQuestionContent(
        interaction_id=interaction_id,
        request_contract_id=binding.contract_id,
        request_contract_version=binding.contract_version,
        request_contract_fingerprint=binding.contract_fingerprint,
        question=question,
        options=tuple(options),
        allow_free_text=allow_free_text,
        typed_content_fingerprint=_canonical_fingerprint(
            "pulsara:plan-question-content:v1", payload
        ),
    )


def extract_plan_draft(
    *,
    interaction_id: str,
    assistant_entry_id: str,
    tool_call_id: str,
    binding: PlanInteractionBinding,
    request_semantic_digest: str,
    arguments: FrozenJsonObjectFact,
) -> ExtractedPlanDraft:
    require_plan_interaction_contract(binding, expected_tool_name="exit_plan")
    raw = thaw_json(arguments)
    if not isinstance(raw, dict) or set(raw) - {"plan", "summary"}:
        raise ValueError("Plan draft arguments are invalid")
    plan = _bounded_text(
        raw.get("plan"),
        name="plan",
        maximum_bytes=MAXIMUM_PLAN_DRAFT_BYTES,
    )
    summary_value = raw.get("summary")
    summary = (
        None
        if summary_value is None
        else _bounded_text(
            summary_value,
            name="summary",
            maximum_bytes=MAXIMUM_PLAN_SUMMARY_BYTES,
            allow_empty=True,
        )
    )
    body = plan.encode("utf-8")
    identity = PlanDraftContentIdentity(
        interaction_id=interaction_id,
        assistant_entry_id=assistant_entry_id,
        tool_call_id=tool_call_id,
        request_contract_id=binding.contract_id,
        request_contract_version=binding.contract_version,
        request_contract_fingerprint=binding.contract_fingerprint,
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


__all__ = [
    "ExtractedPlanDraft",
    "MAXIMUM_PLAN_RESPONSE_BYTES",
    "PLAN_ENTRY_CONTRACT",
    "PLAN_INTERACTION_CONTRACTS",
    "PlanApprovedMaterializationDisposition",
    "PlanDraftContentIdentity",
    "PlanDraftDecision",
    "PlanDraftTextChunk",
    "PlanHandoffKind",
    "PlanInteractionBinding",
    "PlanInteractionKind",
    "PlanInteractionStatus",
    "PlanQuestionAnswerKind",
    "PlanQuestionContent",
    "PlanQuestionOption",
    "PlanWorkflowEnteredBy",
    "PlanWorkflowStatus",
    "extract_plan_entry_reason",
    "extract_plan_draft",
    "extract_plan_question",
    "plan_draft_utf8_digest",
    "read_plan_draft_chunk",
    "require_plan_interaction_contract",
]
