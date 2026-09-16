"""Round 5B summary guidance, tolerant normalization and snapshot carrier."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from typing import Mapping

from pulsara_agent.conversation_kernel.compaction.contracts import (
    COMPACTION_SUMMARY_PROMPT_CONTRACT,
    CompactionActiveRequestLocation,
    CompactionContinuationMode,
    CompactionSnapshotCarrier,
    FrozenCompactionActiveRequest,
    FrozenCompactionSummary,
    FrozenRetainedHistoricalRequest,
)
from pulsara_agent.conversation_kernel.prompt_content import (
    CanonicalPromptBody,
    CanonicalPromptImageDescriptor,
    decode_canonical_prompt_body,
    hydrate_canonical_prompt_body,
)
from pulsara_agent.llm.input import (
    LLMImagePart,
)
from pulsara_agent.model_input.contracts import (
    CanonicalInputOriginKind,
    FrozenProviderInputItemKind,
    MAXIMUM_CANONICAL_PROVIDER_INPUT_BYTES,
    validate_compaction_request_shape,
)
from pulsara_agent.model_input.lowering import (
    compaction_snapshot_display_header,
    compaction_snapshot_provider_content,
    compaction_snapshot_sections,
    display_json,
)
from pulsara_agent.primitives.context import canonical_json_bytes, context_fingerprint


_SUMMARY_REQUEST_COMMON = """CRITICAL: Respond with text only. Do not call any tool.
This is a temporary summary-only call. For this response only, this checkpoint
request supersedes earlier requests to inspect, read, search, invoke, or otherwise
use a tool. Do not continue or execute task work in this response. You already
have the conversation material available to this request; tool calls will not be
executed and cannot provide additional context.

TEMPORAL HANDOFF RULES:
- The no-tool and no-execution restrictions above end when this summary response
  ends. They are not state of the user's task.
- Never say that a user request is queued, deferred, blocked, waiting for another
  agent, or awaiting a later turn merely because this summary-only call cannot
  execute it.
- Preserve the actual task status from the conversation. A later correction,
  cancellation, or replacement supersedes conflicting earlier work; do not turn
  superseded work back into a next step.

You are performing CONTEXT CHECKPOINT COMPACTION. Produce a faithful, compact but
sufficiently informative semantic handoff for the agent that will continue this same
task, so it does not have to guess what has been completed, what the user intends, or
what the current diagnosis is.

Cover the following where applicable. These are content guidelines, not required
headings or an output template:
- The user's current main goal, latest corrections, explicit constraints, scope
  boundaries, preferences, and decisions that must be respected.
- Completed work, key technical or architectural decisions, the reasons for those
  decisions, and verification that actually passed.
- File paths, symbols, commands, configuration, data, examples, or opaque handles
  genuinely needed to continue the work.
- Errors encountered, failed approaches, confirmed root causes, fixes attempted or
  completed, and hypotheses still being evaluated.
- Unfinished tasks, work in progress, the precise current diagnosis, and the most
  direct next step.

Accuracy and inheritance rules:
- Pay particular attention to the user's later corrections and feedback. Current
  canonical content takes precedence over earlier descriptions.
- If earlier compaction summaries are present, carry forward their still-relevant
  meaning so that important information is not lost through repeated compaction.
- retained_historical_requests is settled historical material. Incorporate it as
  history in the summary; do not reinstate it as the current task.
- Clearly distinguish completed, verified, merely attempted, failed, awaiting
  confirmation, and not-yet-executed work. Do not turn plans into facts or invent work
  the user did not request.
- Prioritize precise information that lets the next agent continue acting. Be
  economical: avoid long verbatim code, repeated messages, and irrelevant history,
  but do not omit key constraints or current status merely to be brief.

After the handoff, selected recent user quotes and tool results may be supplied
separately, along with available current information about tools, permissions, Plan,
MCP, Skills, memory, and running tasks. Therefore:
- Still preserve the user intent, constraints, conclusions, and tool outcomes needed
  to continue. Do not assume quotes or tool results will be retained separately in full.
- Extract the necessary information rather than repeating every user message or
  copying whole stretches of history.
- Do not enumerate or guess dynamic directories, catalogs, or current runtime state.
- Do not copy Skill bodies.
- Retain an exact previously seen, usable reference or handle only when continuing
  explicitly pending work genuinely requires it.
- Only when continuing the task requires viewing an image again, copy its previously
  seen image_ref unchanged and briefly state its purpose. Do not compute, guess, or
  enumerate all image references.
- Do not claim that a runtime status will still be current after the handoff.

Organize and check the information internally first; do not output analysis or your
reasoning process. Output only the semantic handoff body. You may naturally use short
paragraphs, bullets, or headings, but no headings, numbering, XML tags, or fixed format
are mandatory. Do not greet or answer the user, treat this instruction as a new user
request, or append closing remarks after the handoff.
"""

_SUMMARY_REQUEST_SUFFIX = """
CONTINUATION OWNERSHIP:
Runtime separately and mechanically owns whether work resumes immediately or a
later user message starts the next turn. Do not infer either lifecycle from this
summary-only response, do not instruct Runtime to wait or continue, and describe
work as pending only when the conversation itself actually left it pending.

REMINDER: Produce the checkpoint handoff now as text only. Do not resume task
work and do not call, inspect, retry, or request any tool in this summary response.
"""


def compaction_summary_request() -> str:
    return _SUMMARY_REQUEST_COMMON + _SUMMARY_REQUEST_SUFFIX


_LEADING_ANALYSIS = re.compile(r"\A\s*<analysis>.*?</analysis>\s*", re.DOTALL)
_OUTER_MARKDOWN_FENCE = re.compile(r"\A```[^\n]*\n(?P<body>.*)\n```\s*\Z", re.DOTALL)


def freeze_compaction_summary_output(
    raw_text: str,
    *,
    maximum_utf8_bytes: int,
) -> FrozenCompactionSummary:
    """Freeze one bounded handoff without imposing a prose schema.

    Provider terminal atomicity and the no-tool outcome are enforced before
    this factory is called.  XML/Markdown wrappers are presentation hints, not
    authority: a complete wrapper is unwrapped when present and otherwise the
    model's normalized text is retained verbatim.
    """

    if not isinstance(raw_text, str):
        raise TypeError("compaction summary output must be text")
    if maximum_utf8_bytes <= 0:
        raise ValueError("compaction summary byte bound must be positive")
    value = raw_text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    value = _LEADING_ANALYSIS.sub("", value, count=1).strip()
    fenced = _OUTER_MARKDOWN_FENCE.fullmatch(value)
    if fenced is not None:
        value = fenced.group("body").strip()
        value = _LEADING_ANALYSIS.sub("", value, count=1).strip()
    opening = value.find("<summary>")
    closing = value.rfind("</summary>")
    if opening >= 0 and closing > opening:
        parts = (
            value[:opening].strip(),
            value[opening + len("<summary>") : closing].strip(),
            value[closing + len("</summary>") :].strip(),
        )
        body = "\n\n".join(part for part in parts if part)
    else:
        body = value
    # Tags echoed as prose are data inside the JSON snapshot, not control.
    body = (
        body.replace("</summary>", "<\\/summary>")
        .replace("<summary>", "<\\summary>")
        .replace("</analysis>", "<\\/analysis>")
        .replace("<analysis>", "<\\analysis>")
        .strip()
    )
    encoded = body.encode("utf-8")
    if not encoded or len(encoded) > maximum_utf8_bytes:
        raise ValueError("compaction summary is empty or exceeds its byte bound")
    return FrozenCompactionSummary(
        body=body,
        body_utf8_bytes=len(encoded),
        body_digest=context_fingerprint(
            "pulsara.frozen-compaction-summary.v2-guided-freeform", body
        ),
    )


def build_compaction_snapshot_carrier(
    *,
    summary: FrozenCompactionSummary,
    recent_human_requests: tuple[FrozenRetainedHistoricalRequest, ...],
    continuation_mode: CompactionContinuationMode,
    active_request: FrozenCompactionActiveRequest | None,
    retained_historical_requests: tuple[FrozenRetainedHistoricalRequest, ...] = (),
) -> CompactionSnapshotCarrier:
    handoff_instruction = compaction_handoff_instruction(
        continuation_mode=continuation_mode,
        active_request=active_request,
    )
    body = canonical_json_bytes(
        {
            "continuation": {
                "mode": continuation_mode.value,
                "instruction": handoff_instruction,
                "active_request": (
                    None if active_request is None else active_request.canonical_value()
                ),
            },
            "earlier_context_summary": summary.body,
            "recent_human_requests": tuple(
                request.canonical_value() for request in recent_human_requests
            ),
            "retained_historical_requests": tuple(
                request.canonical_value() for request in retained_historical_requests
            ),
        }
    )
    carrier = CompactionSnapshotCarrier(
        continuation_mode=continuation_mode,
        handoff_instruction=handoff_instruction,
        active_request=active_request,
        earlier_context_summary=summary.body,
        recent_human_requests=recent_human_requests,
        body=body,
        content_digest="sha256:" + sha256(body).hexdigest(),
        retained_historical_requests=retained_historical_requests,
    )
    compaction_snapshot_canonical_expanded_bytes(carrier.body)
    return carrier


def compaction_handoff_instruction(
    *,
    continuation_mode: CompactionContinuationMode,
    active_request: FrozenCompactionActiveRequest | None,
) -> str:
    if continuation_mode is CompactionContinuationMode.AWAIT_NEXT_USER:
        if active_request is not None:
            raise ValueError("idle handoff cannot carry an active request")
        return (
            "HANDOFF COMPLETE / AWAIT NEXT USER. Compaction is durably complete "
            "while no turn is active. No model response or task execution is due "
            "solely because this snapshot exists. This instruction survives a "
            "process restart. When a later canonical user message arrives, it "
            "drives the next normal call; use the summary only as advisory history. "
            "Do not resume completed or superseded work merely because the summary "
            "calls it pending, and do not treat the summary call's temporary "
            "no-tool restriction as task state."
        )
    if continuation_mode is not CompactionContinuationMode.RESUME_ACTIVE_TURN:
        raise ValueError("unsupported compaction continuation mode")
    if active_request is None:
        raise ValueError("active handoff lacks its active request")
    if active_request.location is CompactionActiveRequestLocation.SNAPSHOT_EXACT:
        request_location = (
            "The section=active retained content below is the exact mechanically "
            "identified request driving this turn."
        )
    else:
        request_location = (
            "The exact mechanically identified active request remains as a later "
            "canonical user-role message after this snapshot; it is not duplicated "
            "inside continuation.active_request."
        )
    return (
        "HANDOFF COMPLETE / RESUME NOW. Compaction is complete. This is a normal "
        "continuation call, not the summary-only call, so the summarizer's temporary "
        "no-tool and no-execution restrictions have ended. "
        + request_location
        + " Resume that request now, subject to current system policy, permissions, "
        "and available tools. This RESUME directive describes the turn for which "
        "the snapshot was adopted: if a newer canonical turn activation or user "
        "request appears after the mechanically identified request, the newer "
        "request supersedes this directive. Apply later user corrections or "
        "cancellations and current Runtime facts. A summary statement that work was "
        "queued, deferred, or not executed because of compaction is descriptive "
        "noise, not an instruction to wait. The summary is advisory; do not repeat "
        "completed or superseded work merely to verify it."
    )


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("snapshot carrier JSON contains a duplicate key")
        value[key] = item
    return value


@dataclass(frozen=True, slots=True)
class _DecodedRequest:
    item_kind: FrozenProviderInputItemKind
    input_origin: CanonicalInputOriginKind | None
    body: CanonicalPromptBody
    encoded_body: bytes


@dataclass(frozen=True, slots=True)
class _DecodedActiveRequest:
    entry_id: str
    entry_sequence: int
    location: CompactionActiveRequestLocation
    item_kind: FrozenProviderInputItemKind
    input_origin: CanonicalInputOriginKind | None
    request: _DecodedRequest | None


@dataclass(frozen=True, slots=True)
class _DecodedCarrier:
    encoded: bytes
    mode: CompactionContinuationMode
    instruction: str
    active: _DecodedActiveRequest | None
    summary: str
    recent: tuple[_DecodedRequest, ...]
    historical: tuple[_DecodedRequest, ...]


def _decode_request(value: object, *, active: bool) -> _DecodedRequest:
    if not isinstance(value, Mapping) or set(value) != {
        "item_kind",
        "input_origin",
        "content",
    }:
        raise ValueError("snapshot request fields do not match its contract")
    try:
        item_kind = FrozenProviderInputItemKind(value["item_kind"])
        input_origin = (
            None
            if value["input_origin"] is None
            else CanonicalInputOriginKind(value["input_origin"])
        )
    except (TypeError, ValueError) as error:
        raise ValueError("snapshot request kind/origin is invalid") from error
    encoded_body = canonical_json_bytes(value["content"])
    body = decode_canonical_prompt_body(encoded_body)
    validate_compaction_request_shape(
        item_kind=item_kind,
        input_origin=input_origin,
        active=active,
        has_image=any(
            isinstance(part, CanonicalPromptImageDescriptor) for part in body.parts
        ),
    )
    return _DecodedRequest(item_kind, input_origin, body, encoded_body)


def _decode_compaction_snapshot_carrier(value: str | bytes) -> _DecodedCarrier:
    encoded = value.encode("utf-8") if isinstance(value, str) else value
    if not isinstance(encoded, bytes):
        raise TypeError("snapshot carrier must be immutable UTF-8 bytes")
    try:
        raw = json.loads(encoded, object_pairs_hook=_unique_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("snapshot carrier is not valid UTF-8 JSON") from error
    if not isinstance(raw, dict) or set(raw) != {
        "continuation",
        "earlier_context_summary",
        "recent_human_requests",
        "retained_historical_requests",
    }:
        raise ValueError("snapshot carrier fields do not match its contract")
    continuation = raw["continuation"]
    if not isinstance(continuation, dict) or set(continuation) != {
        "mode",
        "instruction",
        "active_request",
    }:
        raise ValueError("snapshot continuation fields do not match its contract")
    try:
        mode = CompactionContinuationMode(continuation["mode"])
    except (TypeError, ValueError) as error:
        raise ValueError("snapshot continuation mode is invalid") from error
    active_value = continuation["active_request"]
    active: _DecodedActiveRequest | None
    if active_value is None:
        active = None
    else:
        if not isinstance(active_value, dict) or set(active_value) != {
            "entry_id",
            "entry_sequence",
            "location",
            "item_kind",
            "input_origin",
            "content",
        }:
            raise ValueError("snapshot active-request fields do not match its contract")
        entry_sequence = active_value["entry_sequence"]
        if isinstance(entry_sequence, bool) or not isinstance(entry_sequence, int):
            raise ValueError("snapshot active-request sequence is invalid")
        try:
            location = CompactionActiveRequestLocation(active_value["location"])
        except (TypeError, ValueError) as error:
            raise ValueError("snapshot active-request location is invalid") from error
        entry_id = active_value["entry_id"]
        if not isinstance(entry_id, str):
            raise ValueError("snapshot active-request values are invalid")
        try:
            item_kind = FrozenProviderInputItemKind(active_value["item_kind"])
            input_origin = (
                None
                if active_value["input_origin"] is None
                else CanonicalInputOriginKind(active_value["input_origin"])
            )
        except (TypeError, ValueError) as error:
            raise ValueError("snapshot active-request kind/origin is invalid") from error
        content_value = active_value["content"]
        decoded_request = (
            None
            if content_value is None
            else _decode_request(
                {
                    "item_kind": item_kind.value,
                    "input_origin": (
                        None if input_origin is None else input_origin.value
                    ),
                    "content": content_value,
                },
                active=True,
            )
        )
        if (
            not entry_id
            or entry_sequence < 0
            or (
                location is CompactionActiveRequestLocation.SNAPSHOT_EXACT
            )
            != (decoded_request is not None)
        ):
            raise ValueError("snapshot active-request shape is invalid")
        if decoded_request is None:
            validate_compaction_request_shape(
                item_kind=item_kind,
                input_origin=input_origin,
                active=True,
                has_image=False,
            )
        active = _DecodedActiveRequest(
            entry_id=entry_id,
            entry_sequence=entry_sequence,
            location=location,
            item_kind=item_kind,
            input_origin=input_origin,
            request=decoded_request,
        )
    instruction = continuation["instruction"]
    historical = raw["retained_historical_requests"]
    if not isinstance(historical, list):
        raise ValueError("retained historical requests must be an ordered list")
    retained: list[_DecodedRequest] = []
    for request in historical:
        retained.append(_decode_request(request, active=False))
    summary = raw["earlier_context_summary"]
    recent_value = raw["recent_human_requests"]
    if (
        not isinstance(instruction, str)
        or not isinstance(summary, str)
        or not isinstance(recent_value, list)
    ):
        raise ValueError("snapshot carrier values are invalid")
    recent = tuple(_decode_request(request, active=False) for request in recent_value)
    if any(
        request.item_kind is not FrozenProviderInputItemKind.USER
        or request.input_origin
        not in {
            CanonicalInputOriginKind.HUMAN_MESSAGE,
            CanonicalInputOriginKind.HUMAN_STEER,
        }
        for request in recent
    ):
        raise ValueError("snapshot recent requests are not human requests")
    if (mode is CompactionContinuationMode.RESUME_ACTIVE_TURN) != (active is not None):
        raise ValueError("snapshot continuation/active-request union is invalid")
    canonical = canonical_json_bytes(raw)
    if encoded != canonical:
        raise ValueError("snapshot carrier is not canonical JSON")
    return _DecodedCarrier(
        encoded=canonical,
        mode=mode,
        instruction=instruction,
        active=active,
        summary=summary,
        recent=recent,
        historical=tuple(retained),
    )


def _request_image_descriptors(
    request: _DecodedRequest,
) -> tuple[CanonicalPromptImageDescriptor, ...]:
    return tuple(
        part
        for part in request.body.parts
        if isinstance(part, CanonicalPromptImageDescriptor)
    )


def compaction_snapshot_image_descriptors(
    value: str | bytes,
) -> tuple[CanonicalPromptImageDescriptor, ...]:
    """Traverse carrier image occurrences in the frozen owner-local order."""

    decoded = _decode_compaction_snapshot_carrier(value)
    return _decoded_image_descriptors(decoded)


def _decoded_image_descriptors(
    decoded: _DecodedCarrier,
) -> tuple[CanonicalPromptImageDescriptor, ...]:
    requests = (
        (
            ()
            if decoded.active is None or decoded.active.request is None
            else (decoded.active.request,)
        )
        + decoded.recent
        + decoded.historical
    )
    return tuple(
        descriptor
        for request in requests
        for descriptor in _request_image_descriptors(request)
    )


def compaction_snapshot_canonical_expanded_bytes(value: str | bytes) -> int:
    """Return the descriptor-derived C charge before image hydration."""

    decoded = _decode_compaction_snapshot_carrier(value)
    charge = len(decoded.encoded) + sum(
        descriptor.encoded_bytes
        for descriptor in _decoded_image_descriptors(decoded)
    )
    if charge > MAXIMUM_CANONICAL_PROVIDER_INPUT_BYTES:
        raise ValueError("snapshot carrier exceeds the canonical expanded bound")
    return charge


def _hydrate_request(
    request: _DecodedRequest,
    payloads: tuple[bytes, ...],
    offset: int,
) -> tuple[FrozenRetainedHistoricalRequest, int]:
    count = len(_request_image_descriptors(request))
    prompt = hydrate_canonical_prompt_body(
        body=request.encoded_body,
        image_payloads=payloads[offset : offset + count],
    )
    return (
        FrozenRetainedHistoricalRequest(
            item_kind=request.item_kind,
            input_origin=request.input_origin,
            content=prompt.content,
        ),
        offset + count,
    )


def parse_compaction_snapshot_carrier(
    value: str | bytes,
    *,
    image_payloads: tuple[bytes, ...] = (),
) -> CompactionSnapshotCarrier:
    """Strictly decode and hydrate one v4 carrier from ordered ref payloads."""

    if not isinstance(image_payloads, tuple) or any(
        not isinstance(payload, bytes) for payload in image_payloads
    ):
        raise TypeError("snapshot image payloads must be immutable bytes")
    decoded = _decode_compaction_snapshot_carrier(value)
    descriptors = _decoded_image_descriptors(decoded)
    charge = len(decoded.encoded) + sum(item.encoded_bytes for item in descriptors)
    if charge > MAXIMUM_CANONICAL_PROVIDER_INPUT_BYTES:
        raise ValueError("snapshot carrier exceeds the canonical expanded bound")
    expected_count = len(descriptors)
    if len(image_payloads) != expected_count:
        raise ValueError("snapshot image refs are incomplete or excessive")
    offset = 0
    active_request: FrozenCompactionActiveRequest | None = None
    if decoded.active is not None:
        active_content = None
        if decoded.active.request is not None:
            hydrated, offset = _hydrate_request(
                decoded.active.request, image_payloads, offset
            )
            active_content = hydrated.content
        active_request = FrozenCompactionActiveRequest(
            entry_id=decoded.active.entry_id,
            entry_sequence=decoded.active.entry_sequence,
            location=decoded.active.location,
            item_kind=decoded.active.item_kind,
            input_origin=decoded.active.input_origin,
            content=active_content,
        )
    recent: list[FrozenRetainedHistoricalRequest] = []
    for request in decoded.recent:
        hydrated, offset = _hydrate_request(request, image_payloads, offset)
        recent.append(hydrated)
    historical: list[FrozenRetainedHistoricalRequest] = []
    for request in decoded.historical:
        hydrated, offset = _hydrate_request(request, image_payloads, offset)
        historical.append(hydrated)
    if offset != len(image_payloads):
        raise ValueError("snapshot image traversal did not consume every payload")
    expected_instruction = compaction_handoff_instruction(
        continuation_mode=decoded.mode,
        active_request=active_request,
    )
    if decoded.instruction != expected_instruction:
        raise ValueError("snapshot handoff instruction differs from its contract")
    return CompactionSnapshotCarrier(
        continuation_mode=decoded.mode,
        handoff_instruction=decoded.instruction,
        active_request=active_request,
        earlier_context_summary=decoded.summary,
        recent_human_requests=tuple(recent),
        body=decoded.encoded,
        content_digest="sha256:" + sha256(decoded.encoded).hexdigest(),
        retained_historical_requests=tuple(historical),
    )


def compaction_snapshot_image_parts(
    carrier: CompactionSnapshotCarrier,
) -> tuple[LLMImagePart, ...]:
    requests = (
        (
            ()
            if carrier.active_request is None
            or carrier.active_request.content is None
            else (carrier.active_request.content,)
        )
        + tuple(request.content for request in carrier.recent_human_requests)
        + tuple(request.content for request in carrier.retained_historical_requests)
    )
    return tuple(
        part
        for content in requests
        for part in content.parts
        if isinstance(part, LLMImagePart)
    )


def summary_request_fingerprint(summary_request: str) -> str:
    if not summary_request:
        raise ValueError("summary request is empty")
    return context_fingerprint(
        COMPACTION_SUMMARY_PROMPT_CONTRACT,
        {"request": summary_request},
    )


__all__ = [
    "build_compaction_snapshot_carrier",
    "compaction_handoff_instruction",
    "compaction_snapshot_canonical_expanded_bytes",
    "compaction_snapshot_display_header",
    "compaction_snapshot_image_descriptors",
    "compaction_snapshot_image_parts",
    "compaction_snapshot_provider_content",
    "compaction_snapshot_sections",
    "compaction_summary_request",
    "display_json",
    "freeze_compaction_summary_output",
    "parse_compaction_snapshot_carrier",
    "summary_request_fingerprint",
]
