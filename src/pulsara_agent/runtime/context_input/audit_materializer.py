"""Best-effort context-input audit source preparation and materialization."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum, StrEnum
from tempfile import TemporaryFile
from time import monotonic
from typing import BinaryIO

from pydantic import BaseModel

from pulsara_agent.event import ModelCallStartEvent, ProviderInputAppendCommittedEvent
from pulsara_agent.ports.mcp_secret import (
    SealedMcpContinuationSecretBase,
    assert_not_mcp_secret,
)
from pulsara_agent.primitives._context_base import (
    ContextEventReferenceFact,
    FrozenJsonArrayFact,
    FrozenJsonObjectFact,
    canonical_json_bytes,
    context_fingerprint,
)
from pulsara_agent.primitives.context_input_audit_storage import (
    ContextInputAuditComponentKind,
    ContextInputAuditComponentOwnership,
    ContextInputAuditComponentReferenceFact,
    ContextInputAuditMaterializationPlanFact,
    ContextInputAuditPageFact,
    ContextInputAuditRootFact,
    ContextInputAuditStoredArtifactReferenceFact,
    MAX_AUDIT_COMPONENT_REFERENCES,
    MAX_AUDIT_INLINE_ITEM_BYTES,
    MAX_AUDIT_PAGE_CANONICAL_BYTES,
    MAX_AUDIT_PAGES,
    MAX_AUDIT_TOTAL_INLINE_BYTES,
    MAX_AUDIT_TOTAL_PAGE_CANONICAL_BYTES,
)
from pulsara_agent.primitives.context_input_commit import (
    ContextCompileInputCommitFact,
    ContextInputAuditExpectationFact,
)
from pulsara_agent.primitives.frozen import FrozenRuntimeStateBase
from pulsara_agent.primitives.storage_frozen import build_frozen_storage_fact
from pulsara_agent.primitives.storage_frozen import FrozenStorageFactBase
from pulsara_agent.runtime.context_input.audit_storage import (
    ContextInputAuditArtifactRepository,
    expected_audit_artifact_reference,
)
from pulsara_agent.runtime.context_input.commit import (
    context_input_audit_component_ownership,
)
from pulsara_agent.runtime.context_input.event_slice import (
    event_reference_from_stored,
)
from pulsara_agent.runtime.context_engine.types import (
    CompiledContextSection,
    CompiledToolSpecUnit,
    ContextDiagnostic,
)


MAX_PREPARED_AUDIT_SOURCE_RESIDENT_CHARGE = 32 * 1024 * 1024
MAX_PREPARED_AUDIT_SOURCE_CANONICAL_BYTES = 16 * 1024 * 1024
AUDIT_OPERATION_DEADLINE_SECONDS = 30.0
_TARGET_FRAGMENT_UTF8_BYTES = 220 * 1024
_MAX_STREAMED_MAPPING_ENTRIES = 65_536


class ContextInputAuditMaterializationDisposition(StrEnum):
    MATERIALIZED = "materialized"
    SKIPPED_SOURCE_CAPTURE = "skipped_source_capture"
    SKIPPED_PHYSICAL_BOUND = "skipped_physical_bound"
    FAILED_OPERATIONALLY = "failed_operationally"


@dataclass(frozen=True, slots=True)
class PreparedContextInputAuditComponent:
    kind: ContextInputAuditComponentKind
    value: object
    ownership: ContextInputAuditComponentOwnership = (
        ContextInputAuditComponentOwnership.PAGE_OWNED_DETAIL
    )

    def __post_init__(self) -> None:
        if isinstance(self.value, (dict, list, set)):
            raise TypeError("audit source component must be recursively immutable")
        if self.ownership is not context_input_audit_component_ownership(self.kind):
            raise ValueError("audit component ownership differs from closed registry")
        assert_not_mcp_secret(self.value, sink="PreparedContextInputAuditSource")


@dataclass(frozen=True, slots=True)
class PreparedContextInputAuditCaptureComponent:
    """One borrowed source consumed only by the closed streaming collector.

    The carrier stores existing process-local authority by reference.  It does
    not run caller code, recursively freeze the value, or claim resident bytes
    before the best-effort permit is held.
    """

    kind: ContextInputAuditComponentKind
    source: object
    ownership: ContextInputAuditComponentOwnership

    def __post_init__(self) -> None:
        if self.ownership is not context_input_audit_component_ownership(self.kind):
            raise ValueError("audit capture ownership differs from closed registry")


@dataclass(frozen=True, slots=True)
class PreparedContextInputAuditSourceBudgetQuote:
    known_canonical_bytes: int
    maximum_canonical_bytes: int
    resident_charge_bytes: int

    def __post_init__(self) -> None:
        if self.known_canonical_bytes < 0:
            raise ValueError("audit known byte quote cannot be negative")
        if self.maximum_canonical_bytes < self.known_canonical_bytes:
            raise ValueError("audit maximum byte quote is below known bytes")
        if self.maximum_canonical_bytes > 16 * 1024 * 1024:
            raise ValueError("audit canonical source quote exceeds 16 MiB")
        if self.resident_charge_bytes < 1:
            raise ValueError("audit resident quote must be positive")
        expected_charge = min(
            MAX_PREPARED_AUDIT_SOURCE_RESIDENT_CHARGE,
            2 * self.maximum_canonical_bytes + 64 * 1024,
        )
        if self.resident_charge_bytes != expected_charge:
            raise ValueError("audit resident quote does not use the frozen formula")
        if self.resident_charge_bytes > MAX_PREPARED_AUDIT_SOURCE_RESIDENT_CHARGE:
            raise ValueError("audit resident quote exceeds 32 MiB")


@dataclass(frozen=True, slots=True)
class PreparedContextInputAuditSourceBasis:
    semantic_commit: ContextCompileInputCommitFact
    expectation: ContextInputAuditExpectationFact
    components: tuple[PreparedContextInputAuditComponent, ...]
    source_budget_quote: PreparedContextInputAuditSourceBudgetQuote

    def __post_init__(self) -> None:
        if (
            self.expectation.semantic_commit_fingerprint
            != self.semantic_commit.commit_fingerprint
        ):
            raise ValueError("audit source expectation/commit mismatch")
        kinds = tuple(item.kind for item in self.components)
        if len(kinds) != len(set(kinds)):
            raise ValueError("audit source component kinds must be unique")
        if ContextInputAuditComponentKind.MODEL_START_ATTRIBUTION in kinds:
            raise ValueError("ModelStart attribution belongs to the binding factory")


@dataclass(frozen=True, slots=True)
class OversizedContextInputAuditSourceBasis:
    """Bounded rejection carrier for a source already known to exceed V1.

    It deliberately drops the full component tuple.  The ModelStart owner can
    still obtain the typed ``skipped_physical_bound`` offer result without
    retaining an oversized optional source or starting a worker.
    """

    semantic_commit: ContextCompileInputCommitFact
    expectation: ContextInputAuditExpectationFact
    known_canonical_bytes: int

    def __post_init__(self) -> None:
        if (
            self.expectation.semantic_commit_fingerprint
            != self.semantic_commit.commit_fingerprint
        ):
            raise ValueError("oversized audit source expectation/commit mismatch")
        if self.known_canonical_bytes <= MAX_PREPARED_AUDIT_SOURCE_CANONICAL_BYTES:
            raise ValueError("oversized audit source does not exceed 16 MiB")


PreparedContextInputAuditSource = (
    PreparedContextInputAuditSourceBasis | OversizedContextInputAuditSourceBasis
)


@dataclass(frozen=True, slots=True)
class PreparedContextInputAuditSourceCapture:
    """Process-local borrowed sources for the closed streaming collector."""

    semantic_commit: ContextCompileInputCommitFact
    expectation: ContextInputAuditExpectationFact
    components: tuple[PreparedContextInputAuditCaptureComponent, ...]

    def __post_init__(self) -> None:
        if (
            self.expectation.semantic_commit_fingerprint
            != self.semantic_commit.commit_fingerprint
        ):
            raise ValueError("audit source capture expectation/commit mismatch")
        kinds = tuple(item.kind for item in self.components)
        if len(kinds) != len(set(kinds)):
            raise ValueError("audit capture component kinds must be unique")
        if ContextInputAuditComponentKind.MODEL_START_ATTRIBUTION in kinds:
            raise ValueError("ModelStart attribution belongs to the binding factory")


@dataclass(frozen=True, slots=True)
class PreparedContextInputAuditCaptureMaterialization:
    source_capture: PreparedContextInputAuditSourceCapture
    model_start_reference: ContextEventReferenceFact
    provider_input_append_reference: ContextEventReferenceFact

    def __post_init__(self) -> None:
        commit = self.source_capture.semantic_commit
        if (
            self.model_start_reference.runtime_session_id != commit.runtime_session_id
            or self.provider_input_append_reference.runtime_session_id
            != commit.runtime_session_id
        ):
            raise ValueError("audit capture event owner mismatch")


@dataclass(frozen=True, slots=True)
class PreparedContextInputAuditMaterialization:
    source_basis: PreparedContextInputAuditSourceBasis
    model_start_reference: ContextEventReferenceFact
    provider_input_append_reference: ContextEventReferenceFact

    def __post_init__(self) -> None:
        commit = self.source_basis.semantic_commit
        if (
            self.model_start_reference.runtime_session_id != commit.runtime_session_id
            or self.provider_input_append_reference.runtime_session_id
            != commit.runtime_session_id
        ):
            raise ValueError("audit materialization event owner mismatch")


@dataclass(frozen=True, slots=True)
class ContextInputAuditMaterializationResult:
    disposition: ContextInputAuditMaterializationDisposition
    root_reference: ContextInputAuditStoredArtifactReferenceFact | None
    page_count: int
    total_page_canonical_bytes: int
    diagnostic_code: str | None


def prepare_context_input_audit_source_basis(
    *,
    semantic_commit: ContextCompileInputCommitFact,
    expectation: ContextInputAuditExpectationFact,
    components: tuple[PreparedContextInputAuditComponent, ...],
    known_canonical_bytes: int,
    maximum_canonical_bytes: int | None = None,
) -> PreparedContextInputAuditSource:
    # This eager helper remains for explicit legacy fixtures and offline
    # callers. Normal model calls do not construct or materialize an audit
    # source. Explicit callers still use the bounded canonical encoder and skip
    # an underestimated quote.
    if known_canonical_bytes > MAX_PREPARED_AUDIT_SOURCE_CANONICAL_BYTES:
        return OversizedContextInputAuditSourceBasis(
            semantic_commit=semantic_commit,
            expectation=expectation,
            known_canonical_bytes=known_canonical_bytes,
        )
    maximum = (
        MAX_PREPARED_AUDIT_SOURCE_CANONICAL_BYTES
        if maximum_canonical_bytes is None
        else maximum_canonical_bytes
    )
    if maximum > MAX_PREPARED_AUDIT_SOURCE_CANONICAL_BYTES:
        raise ValueError("audit maximum canonical source bound exceeds 16 MiB")
    return PreparedContextInputAuditSourceBasis(
        semantic_commit=semantic_commit,
        expectation=expectation,
        components=components,
        source_budget_quote=PreparedContextInputAuditSourceBudgetQuote(
            known_canonical_bytes=known_canonical_bytes,
            maximum_canonical_bytes=maximum,
            resident_charge_bytes=min(
                MAX_PREPARED_AUDIT_SOURCE_RESIDENT_CHARGE,
                2 * maximum + 64 * 1024,
            ),
        ),
    )


def materialize_captured_context_input_audit(
    *,
    capture_materialization: PreparedContextInputAuditCaptureMaterialization,
    repository: ContextInputAuditArtifactRepository,
    deadline_monotonic: float,
) -> ContextInputAuditMaterializationResult:
    """Stream borrowed sources behind the best-effort process permit."""

    capture = capture_materialization.source_capture
    return _materialize_context_input_audit_components(
        semantic_commit=capture.semantic_commit,
        expectation=capture.expectation,
        components=capture.components,
        model_start_reference=capture_materialization.model_start_reference,
        provider_input_append_reference=(
            capture_materialization.provider_input_append_reference
        ),
        repository=repository,
        deadline_monotonic=deadline_monotonic,
    )


def bind_context_input_audit_materialization(
    *,
    source_basis: PreparedContextInputAuditSourceBasis,
    runtime_session_id: str,
    committed_start_batch: tuple[object, ...],
) -> PreparedContextInputAuditMaterialization:
    starts = tuple(
        item for item in committed_start_batch if isinstance(item, ModelCallStartEvent)
    )
    appends = tuple(
        item
        for item in committed_start_batch
        if isinstance(item, ProviderInputAppendCommittedEvent)
    )
    if len(starts) != 1 or len(appends) != 1:
        raise ValueError("audit materialization requires one Start and one append")
    start = starts[0]
    append = appends[0]
    commit = source_basis.semantic_commit
    if (
        start.resolved_call.resolved_model_call_id != commit.resolved_model_call_id
        or append.resolved_model_call_id != commit.resolved_model_call_id
        or append.semantic_commit_fingerprint != commit.commit_fingerprint
        or start.provider_input_reference is None
        or start.provider_input_reference.semantic_commit_fingerprint
        != commit.commit_fingerprint
        or start.provider_input_reference.provider_input_plan_fingerprint
        != commit.canonical_provider_input_plan_fingerprint
    ):
        raise ValueError("audit materialization Start/append semantic join mismatch")
    return PreparedContextInputAuditMaterialization(
        source_basis=source_basis,
        model_start_reference=event_reference_from_stored(
            start, runtime_session_id=runtime_session_id
        ),
        provider_input_append_reference=event_reference_from_stored(
            append, runtime_session_id=runtime_session_id
        ),
    )


def bind_context_input_audit_capture_materialization(
    *,
    source_capture: PreparedContextInputAuditSourceCapture,
    runtime_session_id: str,
    committed_start_batch: tuple[object, ...],
) -> PreparedContextInputAuditCaptureMaterialization:
    starts = tuple(
        item for item in committed_start_batch if isinstance(item, ModelCallStartEvent)
    )
    appends = tuple(
        item
        for item in committed_start_batch
        if isinstance(item, ProviderInputAppendCommittedEvent)
    )
    if len(starts) != 1 or len(appends) != 1:
        raise ValueError("audit capture requires one Start and one append")
    start = starts[0]
    append = appends[0]
    commit = source_capture.semantic_commit
    if (
        start.resolved_call.resolved_model_call_id != commit.resolved_model_call_id
        or append.resolved_model_call_id != commit.resolved_model_call_id
        or append.semantic_commit_fingerprint != commit.commit_fingerprint
        or start.provider_input_reference is None
        or start.provider_input_reference.semantic_commit_fingerprint
        != commit.commit_fingerprint
        or start.provider_input_reference.provider_input_plan_fingerprint
        != commit.canonical_provider_input_plan_fingerprint
    ):
        raise ValueError("audit capture Start/append semantic join mismatch")
    return PreparedContextInputAuditCaptureMaterialization(
        source_capture=source_capture,
        model_start_reference=event_reference_from_stored(
            start, runtime_session_id=runtime_session_id
        ),
        provider_input_append_reference=event_reference_from_stored(
            append, runtime_session_id=runtime_session_id
        ),
    )


def _sha256(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _ordered_accumulator(domain: str, values: tuple[str, ...]) -> str:
    accumulator = context_fingerprint(f"{domain}:empty", ())
    for value in values:
        accumulator = context_fingerprint(f"{domain}:step", (accumulator, value))
    return accumulator


class _AuditPhysicalBound(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _EagerCanonicalAuditValue:
    """Compatibility carrier for deterministic offline/test materialization."""

    value: object


@dataclass(frozen=True, slots=True)
class _AuditSpoolRange:
    offset: int
    length: int


@dataclass(frozen=True, slots=True)
class _AuditPageSpoolEntry:
    page_ordinal: int
    component_kind: ContextInputAuditComponentKind
    component_ordinal: int
    fragment_ordinal: int
    fragment_count: int
    source_range: _AuditSpoolRange


@dataclass(frozen=True, slots=True)
class _AuditComponentSpoolEntry:
    component_kind: ContextInputAuditComponentKind
    component_ownership: ContextInputAuditComponentOwnership
    component_ordinal: int
    source_range: _AuditSpoolRange
    canonical_payload_sha256: str
    page_ordinals: tuple[int, ...]
    inline_canonical_json: str | None


@dataclass(slots=True)
class _PreparedAuditSpool:
    spool: BinaryIO
    plan: ContextInputAuditMaterializationPlanFact
    pages: tuple[_AuditPageSpoolEntry, ...]
    semantic_commit: ContextCompileInputCommitFact
    expectation: ContextInputAuditExpectationFact

    def close(self) -> None:
        self.spool.close()


def _iter_json_string(value: str) -> Iterable[str]:
    yield '"'
    for start in range(0, len(value), 4096):
        escaped = json.dumps(value[start : start + 4096], ensure_ascii=False)
        yield escaped[1:-1]
    yield '"'


def _model_items(value: BaseModel) -> Iterable[tuple[str, object]]:
    fields = type(value).model_fields
    for name, field in fields.items():
        if field.exclude is True:
            continue
        key = field.serialization_alias or field.alias or name
        yield key, getattr(value, name)
    for name, field in type(value).model_computed_fields.items():
        key = field.alias or name
        yield key, getattr(value, name)


def _iter_json_mapping(items: Iterable[tuple[str, object]]) -> Iterable[str]:
    ordered = sorted(items, key=lambda pair: pair[0])
    if any(not isinstance(key, str) for key, _ in ordered):
        raise TypeError("canonical JSON object keys must be strings")
    yield "{"
    for ordinal, (key, value) in enumerate(ordered):
        if ordinal:
            yield ","
        yield from _iter_json_string(key)
        yield ":"
        yield from _iter_canonical_json(value)
    yield "}"


def _iter_json_array(values: Iterable[object]) -> Iterable[str]:
    yield "["
    for ordinal, value in enumerate(values):
        if ordinal:
            yield ","
        yield from _iter_canonical_json(value)
    yield "]"


def _iter_canonical_json(value: object) -> Iterable[str]:
    if isinstance(
        value,
        (
            SealedMcpContinuationSecretBase,
            FrozenStorageFactBase,
            FrozenRuntimeStateBase,
        ),
    ):
        raise TypeError("canonical audit stream rejects private/storage runtime facts")
    if isinstance(value, FrozenJsonArrayFact):
        yield from _iter_json_array(value.items)
        return
    if isinstance(value, FrozenJsonObjectFact):
        if len(value.entries) > _MAX_STREAMED_MAPPING_ENTRIES:
            raise _AuditPhysicalBound("audit mapping entry bound exceeded")
        yield from _iter_json_mapping(
            (entry.key, entry.value) for entry in value.entries
        )
        return
    if isinstance(value, BaseModel):
        yield from _iter_json_mapping(_model_items(value))
        return
    if type(value) is dict:
        if len(value) > _MAX_STREAMED_MAPPING_ENTRIES:
            raise _AuditPhysicalBound("audit mapping entry bound exceeded")
        yield from _iter_json_mapping(value.items())
        return
    if type(value) in {list, tuple}:
        yield from _iter_json_array(value)
        return
    if isinstance(value, Enum):
        yield from _iter_canonical_json(value.value)
        return
    if isinstance(value, datetime):
        rendered = value.isoformat()
        if value.tzinfo is not None and value.utcoffset() == timezone.utc.utcoffset(
            value
        ):
            rendered = rendered.removesuffix("+00:00") + "Z"
        yield from _iter_json_string(rendered)
        return
    if isinstance(value, date):
        yield from _iter_json_string(value.isoformat())
        return
    if isinstance(value, str):
        yield from _iter_json_string(value)
        return
    if value is None:
        yield "null"
        return
    if value is True:
        yield "true"
        return
    if value is False:
        yield "false"
        return
    if isinstance(value, int):
        yield str(value)
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical JSON floats must be finite")
        yield json.dumps(value, allow_nan=False, separators=(",", ":"))
        return
    raise TypeError(f"unsupported canonical audit value: {type(value).__name__}")


def _candidate_entry_value(entry: object) -> dict[str, object]:
    candidate = entry.candidate
    return {
        "candidate_id": candidate.candidate_id,
        "source_id": candidate.source_id.value,
        "source_instance_id": candidate.source_instance_id,
        "semantic_fingerprint": candidate.semantic_fingerprint,
        "lifecycle": entry.lifecycle,
    }


def _iter_candidate_set_json(source: object) -> Iterable[str]:
    yield "{"
    yield '"candidate_set_fingerprint":'
    yield from _iter_canonical_json(source.candidate_set_fingerprint)
    yield ',"collection_decisions":'
    yield from _iter_json_array(source.collection_decisions)
    yield ',"entries":['
    for ordinal, item in enumerate(source.entries):
        if ordinal:
            yield ","
        yield from _iter_canonical_json(_candidate_entry_value(item))
    yield "]"
    yield ',"invalidations":'
    yield from _iter_json_array(source.invalidations)
    yield ',"policy":'
    yield from _iter_canonical_json(source.policy)
    yield "}"


_EVENT_VALUE_SEQUENCE_KINDS = frozenset(
    {
        ContextInputAuditComponentKind.COMPILED_SECTIONS,
        ContextInputAuditComponentKind.COMPILED_TOOL_SPECS,
        ContextInputAuditComponentKind.COMPILED_DIAGNOSTICS,
    }
)


def _compiled_event_value(
    kind: ContextInputAuditComponentKind,
    item: object,
) -> dict[str, object]:
    if kind is ContextInputAuditComponentKind.COMPILED_SECTIONS:
        if type(item) is not CompiledContextSection:
            raise TypeError("audit section source has an invalid closed type")
        return {
            "id": item.id,
            "source_id": item.source_id,
            "channel": item.channel,
            "render_mode": item.render_mode,
            "included": item.included,
            "estimated_tokens": item.estimated_tokens,
            "lifecycle_status": item.lifecycle_status,
            "lifecycle_reason": item.lifecycle_reason,
            "dependency_fingerprint": item.dependency_fingerprint,
            "cache_key_scope": item.cache_key_scope,
            "provenance": item.provenance,
            "metadata": item.metadata,
        }
    if kind is ContextInputAuditComponentKind.COMPILED_TOOL_SPECS:
        if type(item) is not CompiledToolSpecUnit:
            raise TypeError("audit tool-spec source has an invalid closed type")
        return {
            "name": item.name,
            "descriptor_id": item.descriptor_id,
            "schema_chars": item.schema_chars,
            "estimated_tokens": item.estimated_tokens,
            "included": item.included,
            "metadata": item.metadata,
        }
    if kind is ContextInputAuditComponentKind.COMPILED_DIAGNOSTICS:
        if type(item) is not ContextDiagnostic:
            raise TypeError("audit diagnostic source has an invalid closed type")
        return {
            "severity": item.severity,
            "code": item.code,
            "message": item.message,
            "section_id": item.section_id,
            "metadata": item.metadata,
        }
    raise TypeError("audit event-value source kind is unsupported")


def _iter_capture_component_json(
    component: PreparedContextInputAuditCaptureComponent,
) -> Iterable[str]:
    if isinstance(
        component.source,
        (
            SealedMcpContinuationSecretBase,
            FrozenStorageFactBase,
            FrozenRuntimeStateBase,
        ),
    ):
        raise TypeError("context input audit capture rejects private/storage facts")
    if type(component.source) is _EagerCanonicalAuditValue:
        yield from _iter_canonical_json(component.source.value)
        return
    if component.kind is ContextInputAuditComponentKind.PREPARED_CANDIDATE_SET:
        yield from _iter_candidate_set_json(component.source)
        return
    if component.kind in _EVENT_VALUE_SEQUENCE_KINDS:
        yield "["
        for ordinal, item in enumerate(component.source):
            if ordinal:
                yield ","
            yield from _iter_canonical_json(_compiled_event_value(component.kind, item))
        yield "]"
        return
    yield from _iter_canonical_json(component.source)


def _write_component_to_spool(
    *,
    spool: BinaryIO,
    component: PreparedContextInputAuditCaptureComponent,
    total_source_bytes: int,
    maximum_source_bytes: int,
) -> tuple[_AuditSpoolRange, str, int]:
    start = spool.tell()
    digest = hashlib.sha256()
    length = 0
    for text in _iter_capture_component_json(component):
        encoded = text.encode("utf-8")
        if total_source_bytes + length + len(encoded) > maximum_source_bytes:
            raise _AuditPhysicalBound(
                "context input audit canonical source bound exceeded"
            )
        spool.write(encoded)
        digest.update(encoded)
        length += len(encoded)
    return (
        _AuditSpoolRange(offset=start, length=length),
        f"sha256:{digest.hexdigest()}",
        total_source_bytes + length,
    )


def _split_utf8_ranges(
    spool: BinaryIO,
    source_range: _AuditSpoolRange,
    *,
    semantic_commit: ContextCompileInputCommitFact,
    expectation: ContextInputAuditExpectationFact,
    component_kind: ContextInputAuditComponentKind,
) -> tuple[_AuditSpoolRange, ...]:
    ranges: list[_AuditSpoolRange] = []
    cursor = source_range.offset
    remaining = source_range.length
    while remaining:
        requested = min(_TARGET_FRAGMENT_UTF8_BYTES, remaining)
        spool.seek(cursor)
        data = spool.read(requested)
        if len(data) != requested:
            raise OSError("context input audit spool read was truncated")
        safe = len(data)
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            if exc.reason != "unexpected end of data" or exc.start < 1:
                raise
            safe = exc.start
        text = data[:safe].decode("utf-8")
        if (
            _provisional_page_canonical_bytes(
                semantic_commit=semantic_commit,
                expectation=expectation,
                component_kind=component_kind,
                fragment=text,
            )
            > MAX_AUDIT_PAGE_CANONICAL_BYTES
        ):
            low = 0
            high = len(text)
            while low < high:
                midpoint = (low + high + 1) // 2
                if (
                    _provisional_page_canonical_bytes(
                        semantic_commit=semantic_commit,
                        expectation=expectation,
                        component_kind=component_kind,
                        fragment=text[:midpoint],
                    )
                    <= MAX_AUDIT_PAGE_CANONICAL_BYTES
                ):
                    low = midpoint
                else:
                    high = midpoint - 1
            if low < 1:
                raise _AuditPhysicalBound(
                    "context input audit page wrapper exceeds its physical bound"
                )
            safe = len(text[:low].encode("utf-8"))
        ranges.append(_AuditSpoolRange(offset=cursor, length=safe))
        cursor += safe
        remaining -= safe
        if len(ranges) > MAX_AUDIT_PAGES:
            raise _AuditPhysicalBound("context input audit page-count bound exceeded")
    return tuple(ranges)


def _provisional_page_canonical_bytes(
    *,
    semantic_commit: ContextCompileInputCommitFact,
    expectation: ContextInputAuditExpectationFact,
    component_kind: ContextInputAuditComponentKind,
    fragment: str,
) -> int:
    """Measure the final storage wrapper with worst-case bounded ordinals.

    The own fingerprint has fixed width.  Using the largest legal ordinal and
    component ordinal makes this an upper bound for the eventual page while
    retaining the exact JSON escaping behaviour of the storage carrier.
    """

    encoded = fragment.encode("utf-8")
    return len(
        canonical_json_bytes(
            {
                "schema_version": "context_input_audit_page.v1",
                "source_runtime_session_id": semantic_commit.runtime_session_id,
                "source_run_id": semantic_commit.run_id,
                "materialization_key": expectation.materialization_key,
                "page_ordinal": MAX_AUDIT_PAGES - 1,
                "component_kind": component_kind.value,
                "component_ordinal": MAX_AUDIT_COMPONENT_REFERENCES - 1,
                "fragment_ordinal": MAX_AUDIT_PAGES - 1,
                "fragment_count": MAX_AUDIT_PAGES,
                "canonical_json_fragment": fragment,
                "canonical_payload_sha256": "sha256:" + "0" * 64,
                "canonical_payload_bytes": len(encoded),
                "page_storage_fingerprint": "sha256:" + "0" * 64,
            }
        )
    )


def _read_spool_text(spool: BinaryIO, source_range: _AuditSpoolRange) -> str:
    spool.seek(source_range.offset)
    content = spool.read(source_range.length)
    if len(content) != source_range.length:
        raise OSError("context input audit spool read was truncated")
    return content.decode("utf-8")


def _build_page(
    *,
    semantic_commit: ContextCompileInputCommitFact,
    expectation: ContextInputAuditExpectationFact,
    entry: _AuditPageSpoolEntry,
    fragment: str,
) -> ContextInputAuditPageFact:
    encoded = fragment.encode("utf-8")
    return build_frozen_storage_fact(
        ContextInputAuditPageFact,
        schema_version="context_input_audit_page.v1",
        source_runtime_session_id=semantic_commit.runtime_session_id,
        source_run_id=semantic_commit.run_id,
        materialization_key=expectation.materialization_key,
        page_ordinal=entry.page_ordinal,
        component_kind=entry.component_kind,
        component_ordinal=entry.component_ordinal,
        fragment_ordinal=entry.fragment_ordinal,
        fragment_count=entry.fragment_count,
        canonical_json_fragment=fragment,
        canonical_payload_sha256=_sha256(encoded),
        canonical_payload_bytes=len(encoded),
    )


def _page_artifact_id(
    *,
    materialization_key: str,
    page: ContextInputAuditPageFact,
    audit_contract_fingerprint: str,
) -> str:
    digest = context_fingerprint(
        "context-input-audit-page-id:v1",
        (
            materialization_key,
            page.page_ordinal,
            page.canonical_payload_sha256,
            audit_contract_fingerprint,
        ),
    ).removeprefix("sha256:")
    return f"context-input-audit-page:{digest}"


def _prepare_streaming_plan(
    *,
    semantic_commit: ContextCompileInputCommitFact,
    expectation: ContextInputAuditExpectationFact,
    components: tuple[PreparedContextInputAuditCaptureComponent, ...],
    model_start_reference: ContextEventReferenceFact,
    provider_input_append_reference: ContextEventReferenceFact,
    maximum_source_bytes: int,
) -> _PreparedAuditSpool:
    registry_ordinal = {
        kind: ordinal for ordinal, kind in enumerate(ContextInputAuditComponentKind)
    }
    ordered_components = (
        *tuple(
            sorted(
                components,
                key=lambda item: registry_ordinal[item.kind],
            )
        ),
        PreparedContextInputAuditCaptureComponent(
            kind=ContextInputAuditComponentKind.MODEL_START_ATTRIBUTION,
            source=(
                model_start_reference,
                provider_input_append_reference,
            ),
            ownership=(
                ContextInputAuditComponentOwnership.EXISTING_AUTHORITY_REFERENCE
            ),
        ),
    )
    spool = TemporaryFile(mode="w+b")
    try:
        component_entries: list[_AuditComponentSpoolEntry] = []
        page_entries: list[_AuditPageSpoolEntry] = []
        total_inline = 0
        total_source_bytes = 0
        for component_ordinal, component in enumerate(ordered_components):
            source_range, payload_sha, total_source_bytes = _write_component_to_spool(
                spool=spool,
                component=component,
                total_source_bytes=total_source_bytes,
                maximum_source_bytes=maximum_source_bytes,
            )
            can_inline = (
                source_range.length <= MAX_AUDIT_INLINE_ITEM_BYTES
                and total_inline + source_range.length <= MAX_AUDIT_TOTAL_INLINE_BYTES
            )
            if (
                component.ownership
                is ContextInputAuditComponentOwnership.EXISTING_AUTHORITY_REFERENCE
                and not can_inline
            ):
                raise _AuditPhysicalBound(
                    "existing audit authority reference exceeds 8 KiB: "
                    f"{component.kind.value}:{source_range.length}"
                )
            if can_inline:
                page_ordinals: tuple[int, ...] = ()
                inline = _read_spool_text(spool, source_range)
                total_inline += source_range.length
            else:
                ranges = _split_utf8_ranges(
                    spool,
                    source_range,
                    semantic_commit=semantic_commit,
                    expectation=expectation,
                    component_kind=component.kind,
                )
                if len(page_entries) + len(ranges) > MAX_AUDIT_PAGES:
                    raise _AuditPhysicalBound(
                        "context input audit page-count bound exceeded"
                    )
                start_page_ordinal = len(page_entries)
                page_ordinals = tuple(
                    range(start_page_ordinal, start_page_ordinal + len(ranges))
                )
                for fragment_ordinal, fragment_range in enumerate(ranges):
                    page_entries.append(
                        _AuditPageSpoolEntry(
                            page_ordinal=start_page_ordinal + fragment_ordinal,
                            component_kind=component.kind,
                            component_ordinal=component_ordinal,
                            fragment_ordinal=fragment_ordinal,
                            fragment_count=len(ranges),
                            source_range=fragment_range,
                        )
                    )
                inline = None
            component_entries.append(
                _AuditComponentSpoolEntry(
                    component_kind=component.kind,
                    component_ownership=component.ownership,
                    component_ordinal=component_ordinal,
                    source_range=source_range,
                    canonical_payload_sha256=payload_sha,
                    page_ordinals=page_ordinals,
                    inline_canonical_json=inline,
                )
            )

        page_references = []
        page_fingerprints: dict[int, str] = {}
        total_page_bytes = 0
        for entry in page_entries:
            page = _build_page(
                semantic_commit=semantic_commit,
                expectation=expectation,
                entry=entry,
                fragment=_read_spool_text(spool, entry.source_range),
            )
            reference = expected_audit_artifact_reference(
                artifact_id=_page_artifact_id(
                    materialization_key=expectation.materialization_key,
                    page=page,
                    audit_contract_fingerprint=(expectation.audit_contract_fingerprint),
                ),
                fact=page,
            )
            page_references.append(reference)
            page_fingerprints[entry.page_ordinal] = page.page_storage_fingerprint
            page_canonical_bytes = len(
                canonical_json_bytes(page.model_dump(mode="json"))
            )
            if page_canonical_bytes > MAX_AUDIT_PAGE_CANONICAL_BYTES:
                raise _AuditPhysicalBound(
                    "context input audit page wrapper exceeds its physical bound"
                )
            total_page_bytes += page_canonical_bytes
            if total_page_bytes > MAX_AUDIT_TOTAL_PAGE_CANONICAL_BYTES:
                raise _AuditPhysicalBound(
                    "context input audit total page-byte bound exceeded"
                )

        component_references = tuple(
            build_frozen_storage_fact(
                ContextInputAuditComponentReferenceFact,
                schema_version="context_input_audit_component_reference.v1",
                component_kind=entry.component_kind,
                component_ownership=entry.component_ownership,
                component_ordinal=entry.component_ordinal,
                canonical_payload_sha256=entry.canonical_payload_sha256,
                canonical_payload_bytes=entry.source_range.length,
                storage_kind=("inline" if not entry.page_ordinals else "paged"),
                inline_canonical_json=entry.inline_canonical_json,
                page_ordinals=entry.page_ordinals,
                ordered_page_accumulator=_ordered_accumulator(
                    "context-input-audit-component-pages:v1",
                    tuple(page_fingerprints[item] for item in entry.page_ordinals),
                ),
            )
            for entry in component_entries
        )
        page_references_tuple = tuple(page_references)
        plan = build_frozen_storage_fact(
            ContextInputAuditMaterializationPlanFact,
            schema_version="context_input_audit_materialization_plan.v1",
            source_runtime_session_id=semantic_commit.runtime_session_id,
            source_run_id=semantic_commit.run_id,
            source_context_id=semantic_commit.context_id,
            source_resolved_model_call_id=semantic_commit.resolved_model_call_id,
            semantic_commit_fingerprint=semantic_commit.commit_fingerprint,
            expectation_fingerprint=expectation.expectation_fingerprint,
            materialization_key=expectation.materialization_key,
            expected_root_artifact_id=expectation.expected_root_artifact_id,
            expected_root_semantic_fingerprint=(
                expectation.expected_root_semantic_fingerprint
            ),
            audit_contract_fingerprint=expectation.audit_contract_fingerprint,
            components=component_references,
            page_references=page_references_tuple,
            component_count=len(component_references),
            page_count=len(page_references_tuple),
            total_inline_bytes=total_inline,
            total_page_canonical_bytes=total_page_bytes,
            ordered_component_accumulator=_ordered_accumulator(
                "context-input-audit-components:v1",
                tuple(
                    item.component_reference_fingerprint
                    for item in component_references
                ),
            ),
            ordered_page_accumulator=_ordered_accumulator(
                "context-input-audit-pages:v1",
                tuple(item.reference_fingerprint for item in page_references_tuple),
            ),
        )
        return _PreparedAuditSpool(
            spool=spool,
            plan=plan,
            pages=tuple(page_entries),
            semantic_commit=semantic_commit,
            expectation=expectation,
        )
    except BaseException:
        spool.close()
        raise


def materialize_context_input_audit(
    *,
    materialization: PreparedContextInputAuditMaterialization,
    repository: ContextInputAuditArtifactRepository,
    deadline_monotonic: float,
) -> ContextInputAuditMaterializationResult:
    if isinstance(materialization.source_basis, OversizedContextInputAuditSourceBasis):
        return ContextInputAuditMaterializationResult(
            disposition=(
                ContextInputAuditMaterializationDisposition.SKIPPED_PHYSICAL_BOUND
            ),
            root_reference=None,
            page_count=0,
            total_page_canonical_bytes=0,
            diagnostic_code="audit_source_physical_bound",
        )
    components = tuple(
        PreparedContextInputAuditCaptureComponent(
            kind=item.kind,
            source=_EagerCanonicalAuditValue(item.value),
            ownership=item.ownership,
        )
        for item in materialization.source_basis.components
    )
    return _materialize_context_input_audit_components(
        semantic_commit=materialization.source_basis.semantic_commit,
        expectation=materialization.source_basis.expectation,
        components=components,
        model_start_reference=materialization.model_start_reference,
        provider_input_append_reference=(
            materialization.provider_input_append_reference
        ),
        maximum_source_bytes=(
            materialization.source_basis.source_budget_quote.maximum_canonical_bytes
        ),
        repository=repository,
        deadline_monotonic=deadline_monotonic,
    )


def _materialize_context_input_audit_components(
    *,
    semantic_commit: ContextCompileInputCommitFact,
    expectation: ContextInputAuditExpectationFact,
    components: tuple[PreparedContextInputAuditCaptureComponent, ...],
    model_start_reference: ContextEventReferenceFact,
    provider_input_append_reference: ContextEventReferenceFact,
    repository: ContextInputAuditArtifactRepository,
    deadline_monotonic: float,
    maximum_source_bytes: int = MAX_PREPARED_AUDIT_SOURCE_CANONICAL_BYTES,
) -> ContextInputAuditMaterializationResult:
    """Write plan first and rebuild at most one bounded page at a time."""

    try:
        prepared = _prepare_streaming_plan(
            semantic_commit=semantic_commit,
            expectation=expectation,
            components=components,
            model_start_reference=model_start_reference,
            provider_input_append_reference=provider_input_append_reference,
            maximum_source_bytes=maximum_source_bytes,
        )
    except _AuditPhysicalBound:
        return ContextInputAuditMaterializationResult(
            disposition=(
                ContextInputAuditMaterializationDisposition.SKIPPED_PHYSICAL_BOUND
            ),
            root_reference=None,
            page_count=0,
            total_page_canonical_bytes=0,
            diagnostic_code="audit_source_physical_bound",
        )
    except Exception:
        return ContextInputAuditMaterializationResult(
            disposition=(
                ContextInputAuditMaterializationDisposition.SKIPPED_SOURCE_CAPTURE
            ),
            root_reference=None,
            page_count=0,
            total_page_canonical_bytes=0,
            diagnostic_code="audit_source_capture_skipped",
        )
    plan = prepared.plan
    try:
        if monotonic() >= deadline_monotonic:
            return ContextInputAuditMaterializationResult(
                disposition=(
                    ContextInputAuditMaterializationDisposition.FAILED_OPERATIONALLY
                ),
                root_reference=None,
                page_count=0,
                total_page_canonical_bytes=0,
                diagnostic_code="audit_deadline_before_plan",
            )
        plan_reference = repository.put_exact(
            artifact_id=expectation.expected_plan_artifact_id,
            fact=plan,
            deadline_monotonic=deadline_monotonic,
        )
        for entry, reference in zip(prepared.pages, plan.page_references, strict=True):
            if monotonic() >= deadline_monotonic:
                raise TimeoutError("context input audit page deadline exceeded")
            page = _build_page(
                semantic_commit=semantic_commit,
                expectation=expectation,
                entry=entry,
                fragment=_read_spool_text(prepared.spool, entry.source_range),
            )
            confirmed = repository.put_exact(
                artifact_id=reference.artifact_id,
                fact=page,
                deadline_monotonic=deadline_monotonic,
            )
            if confirmed != reference:
                raise ValueError("context input audit page confirmation drifted")
        for reference in plan.page_references:
            if monotonic() >= deadline_monotonic:
                raise TimeoutError("context input audit verification deadline exceeded")
            repository.get_exact(
                reference=reference,
                source_runtime_session_id=plan.source_runtime_session_id,
                source_run_id=plan.source_run_id,
                fact_type=ContextInputAuditPageFact,
                deadline_monotonic=deadline_monotonic,
            )
        root = build_frozen_storage_fact(
            ContextInputAuditRootFact,
            schema_version="context_input_audit_root.v1",
            source_runtime_session_id=plan.source_runtime_session_id,
            source_run_id=plan.source_run_id,
            source_context_id=plan.source_context_id,
            source_resolved_model_call_id=plan.source_resolved_model_call_id,
            semantic_commit_fingerprint=plan.semantic_commit_fingerprint,
            materialization_key=plan.materialization_key,
            plan_artifact_reference=plan_reference,
            component_count=plan.component_count,
            page_count=plan.page_count,
            ordered_component_accumulator=plan.ordered_component_accumulator,
            ordered_page_accumulator=plan.ordered_page_accumulator,
            materialization_contract_fingerprint=(plan.audit_contract_fingerprint),
            root_semantic_fingerprint=(expectation.expected_root_semantic_fingerprint),
        )
        if monotonic() >= deadline_monotonic:
            raise TimeoutError("context input audit root deadline exceeded")
        root_reference = repository.put_exact(
            artifact_id=expectation.expected_root_artifact_id,
            fact=root,
            deadline_monotonic=deadline_monotonic,
        )
    except Exception:
        return ContextInputAuditMaterializationResult(
            disposition=(
                ContextInputAuditMaterializationDisposition.FAILED_OPERATIONALLY
            ),
            root_reference=None,
            page_count=len(prepared.pages),
            total_page_canonical_bytes=plan.total_page_canonical_bytes,
            diagnostic_code="audit_materialization_failed",
        )
    else:
        return ContextInputAuditMaterializationResult(
            disposition=ContextInputAuditMaterializationDisposition.MATERIALIZED,
            root_reference=root_reference,
            page_count=len(prepared.pages),
            total_page_canonical_bytes=plan.total_page_canonical_bytes,
            diagnostic_code=None,
        )
    finally:
        prepared.close()


def hydrate_context_input_audit_components(
    *,
    plan: ContextInputAuditMaterializationPlanFact,
    pages: tuple[ContextInputAuditPageFact, ...],
) -> tuple[tuple[ContextInputAuditComponentKind, object], ...]:
    by_ordinal = {page.page_ordinal: page for page in pages}
    if len(by_ordinal) != len(pages):
        raise ValueError("context input audit pages repeat an ordinal")
    hydrated: list[tuple[ContextInputAuditComponentKind, object]] = []
    for component in plan.components:
        if component.storage_kind == "inline":
            assert component.inline_canonical_json is not None
            canonical = component.inline_canonical_json
        else:
            selected = tuple(by_ordinal[item] for item in component.page_ordinals)
            if any(
                page.component_kind != component.component_kind
                or page.component_ordinal != component.component_ordinal
                or page.fragment_ordinal != ordinal
                or page.fragment_count != len(selected)
                for ordinal, page in enumerate(selected)
            ):
                raise ValueError("context input audit page/component join mismatch")
            canonical = "".join(item.canonical_json_fragment for item in selected)
        encoded = canonical.encode("utf-8")
        if (
            len(encoded) != component.canonical_payload_bytes
            or _sha256(encoded) != component.canonical_payload_sha256
        ):
            raise ValueError("context input audit hydrated component mismatch")
        hydrated.append((component.component_kind, json.loads(canonical)))
    return tuple(hydrated)


__all__ = [
    "AUDIT_OPERATION_DEADLINE_SECONDS",
    "ContextInputAuditMaterializationDisposition",
    "ContextInputAuditMaterializationResult",
    "MAX_PREPARED_AUDIT_SOURCE_CANONICAL_BYTES",
    "MAX_PREPARED_AUDIT_SOURCE_RESIDENT_CHARGE",
    "OversizedContextInputAuditSourceBasis",
    "PreparedContextInputAuditCaptureComponent",
    "PreparedContextInputAuditComponent",
    "PreparedContextInputAuditCaptureMaterialization",
    "PreparedContextInputAuditSourceCapture",
    "PreparedContextInputAuditMaterialization",
    "PreparedContextInputAuditSource",
    "PreparedContextInputAuditSourceBasis",
    "PreparedContextInputAuditSourceBudgetQuote",
    "bind_context_input_audit_materialization",
    "bind_context_input_audit_capture_materialization",
    "hydrate_context_input_audit_components",
    "materialize_context_input_audit",
    "materialize_captured_context_input_audit",
    "prepare_context_input_audit_source_basis",
]
