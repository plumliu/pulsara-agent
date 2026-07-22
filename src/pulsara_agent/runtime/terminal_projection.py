"""Session-owned lossless terminal projections for tool observations."""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from threading import RLock
from time import monotonic
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from pulsara_agent.event import (
    AgentEvent,
    ExternalExecutionResultEvent,
    RequireExternalExecutionEvent,
    ToolResultDataDeltaEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTerminalProjectionCommittedEvent,
    ToolResultTextDeltaEvent,
    utc_now,
)
from pulsara_agent.event_log.serialization import canonical_event_payload_bytes
from pulsara_agent.llm.terminal_projection import (
    TERMINAL_PROJECTION_MEDIA_TYPE,
    TerminalProjectionContractBundle,
    build_terminal_inline_content,
    normalize_data_media_type,
    stable_event_identity,
)
from pulsara_agent.message import (
    DataBlock,
    TextBlock,
    ToolResultArtifactRef,
    ToolResultBlock,
    ToolResultState,
)
from pulsara_agent.primitives import context_fingerprint, freeze_json
from pulsara_agent.primitives.context import canonical_json_bytes, thaw_json
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.terminal_projection import (
    CanonicalToolResultBlockFact,
    CanonicalToolResultBlockSemanticFact,
    CanonicalToolResultContentBlockFact,
    CanonicalToolResultDataBlockSemanticFact,
    CanonicalToolResultTextBlockSemanticFact,
    TerminalArtifactContentReferenceFact,
    TerminalContentFact,
    TerminalContentSemanticFact,
    TerminalProjectionDocumentFact,
    TerminalProjectionReferenceFact,
    ToolResultSemanticSourceFact,
    ToolResultTerminalProjectionEndReferenceFact,
    ToolTerminalProjectionPayloadFact,
    ToolTerminalProjectionSemanticFact,
    ToolTerminalProjectionSemanticJoinFact,
)
from pulsara_agent.primitives.tool_result import (
    ContextToolResultArtifactRefFact,
    ContextToolResultPreviewFact,
    ExternalToolResultIngressFact,
    ToolResultExecutionSemanticsFact,
    ToolResultStateFact,
)
from pulsara_agent.primitives.tool_observation import ToolObservationTimingFact
from pulsara_agent.primitives.terminal_observation import (
    TerminalProcessMonitorCancellationSemanticFact,
    TerminalProcessMonitorRegistrationSemanticFact,
    TerminalProcessObservationReceiptFact,
)

if TYPE_CHECKING:
    from pulsara_agent.runtime.session import RuntimeSession


TOOL_TERMINAL_PROJECTION_REDUCER_CONTRACT_FINGERPRINT = context_fingerprint(
    "tool-terminal-projection-reducer-contract:v1",
    {
        "source": "typed-start-delta-terminal:v1",
        "text": "consecutive-text-delta-coalescing:v1",
        "data": "one-durable-data-delta-per-block:v1",
        "terminal": "projection-before-end-exact-reference:v1",
    },
)
TOOL_RESULT_SEMANTIC_DOMAIN_CONTRACT_FINGERPRINT = context_fingerprint(
    "tool-result-semantic-domain-contract:v1",
    (
        "TOOL_RESULT_START",
        "TOOL_RESULT_TEXT_DELTA",
        "TOOL_RESULT_DATA_DELTA",
        "TOOL_RESULT_TERMINAL_PROJECTION_COMMITTED",
        "TOOL_RESULT_END",
    ),
)
EXTERNAL_TOOL_RESULT_REDUCER_CONTRACT_FINGERPRINT = context_fingerprint(
    "external-tool-terminal-projection-reducer-contract:v1",
    "committed-requirement+typed-external-ingress+canonical-result-block",
)
EXTERNAL_TOOL_RESULT_SEMANTIC_DOMAIN_CONTRACT_FINGERPRINT = context_fingerprint(
    "external-tool-result-semantic-domain-contract:v1",
    (
        "REQUIRE_EXTERNAL_EXECUTION",
        "TOOL_RESULT_TERMINAL_PROJECTION_COMMITTED",
        "EXTERNAL_EXECUTION_RESULT",
    ),
)


@dataclass(frozen=True, slots=True)
class _PendingToolResultSource:
    start: ToolResultStartEvent
    deltas: tuple[ToolResultTextDeltaEvent | ToolResultDataDeltaEvent, ...]


class ToolTerminalProjectionStateStore:
    """Incremental committed source state for non-terminal tool results."""

    def __init__(
        self,
        events: Sequence[AgentEvent] = (),
        *,
        through_sequence: int = 0,
    ) -> None:
        self._lock = RLock()
        self._pending: dict[tuple[str, str], _PendingToolResultSource] = {}
        self._through_sequence = 0
        for event in events:
            if event.sequence is None:
                raise ValueError("tool projection bootstrap requires stored events")
            self._apply_one(event)
        if events and through_sequence < max(event.sequence or 0 for event in events):
            raise ValueError("tool projection bootstrap high-water precedes source")
        self._through_sequence = through_sequence

    @property
    def through_sequence(self) -> int:
        with self._lock:
            return self._through_sequence

    def snapshot(self) -> dict[tuple[str, str], _PendingToolResultSource]:
        with self._lock:
            return dict(self._pending)

    def apply_committed(self, events: tuple[AgentEvent, ...]) -> None:
        with self._lock:
            for event in events:
                if event.sequence is None:
                    raise ValueError("tool projection store requires committed events")
                if event.sequence != self._through_sequence + 1:
                    raise ValueError("tool projection store sequence is not contiguous")
                self._apply_one(event)
                self._through_sequence = event.sequence

    def rebuild(self, events: tuple[AgentEvent, ...]) -> None:
        with self._lock:
            self._pending.clear()
            self._through_sequence = 0
        self.apply_committed(events)

    def _apply_one(self, event: AgentEvent) -> None:
        key = (event.run_id, getattr(event, "tool_call_id", ""))
        if isinstance(event, ToolResultStartEvent):
            if key in self._pending:
                raise ValueError("duplicate pending ToolResultStart")
            self._pending[key] = _PendingToolResultSource(start=event, deltas=())
        elif isinstance(event, (ToolResultTextDeltaEvent, ToolResultDataDeltaEvent)):
            pending = self._pending.get(key)
            if pending is None:
                raise ValueError("tool result delta has no pending Start")
            self._pending[key] = _PendingToolResultSource(
                start=pending.start,
                deltas=(*pending.deltas, event),
            )
        elif isinstance(event, ToolResultEndEvent):
            if key not in self._pending:
                raise ValueError("ToolResultEnd has no pending Start")
            self._pending.pop(key)


@dataclass(frozen=True, slots=True)
class _ContentArtifactCandidate:
    artifact_id: str
    text: str
    media_type: str
    sha256: str
    byte_count: int


@dataclass(frozen=True, slots=True)
class PreparedToolTerminalProjection:
    document: TerminalProjectionDocumentFact
    canonical_document_bytes: bytes
    projection_reference: TerminalProjectionReferenceFact
    committed_event: ToolResultTerminalProjectionCommittedEvent
    end_reference: ToolResultTerminalProjectionEndReferenceFact
    content_artifacts: tuple[_ContentArtifactCandidate, ...]


@dataclass(frozen=True, slots=True)
class ToolResultEndCandidate:
    """Process-local tool terminal candidate; never valid EventLog input."""

    id: str
    run_id: str
    turn_id: str
    reply_id: str
    created_at: str
    tool_call_id: str
    state: ToolResultState
    artifacts: tuple[ToolResultArtifactRef, ...]
    observation_timing: ToolObservationTimingFact
    execution_semantics: ToolResultExecutionSemanticsFact
    metadata: Mapping[str, Any]
    terminal_process_observation_receipt: (
        TerminalProcessObservationReceiptFact | None
    ) = None
    terminal_process_monitor_registration: (
        TerminalProcessMonitorRegistrationSemanticFact | None
    ) = None
    terminal_process_monitor_cancellation: (
        TerminalProcessMonitorCancellationSemanticFact | None
    ) = None

    def __post_init__(self) -> None:
        if not all((self.id, self.run_id, self.turn_id, self.reply_id, self.tool_call_id)):
            raise ValueError("tool terminal candidate identity cannot be empty")
        if self.observation_timing.tool_call_id not in (None, self.tool_call_id):
            raise ValueError("tool terminal candidate timing identity mismatch")
        if self.execution_semantics.result_state.value != self.state.value:
            raise ValueError("tool terminal candidate semantics state mismatch")
        if (
            self.terminal_process_observation_receipt is not None
            and self.terminal_process_observation_receipt.origin_tool_call_id
            != self.tool_call_id
        ):
            raise ValueError("tool terminal candidate receipt call mismatch")
        if (
            self.terminal_process_monitor_registration is not None
            and self.terminal_process_monitor_cancellation is not None
        ):
            raise ValueError(
                "tool terminal candidate monitor actions are mutually exclusive"
            )
        if (
            self.terminal_process_monitor_cancellation is not None
            and self.terminal_process_monitor_cancellation.cancel_intent.origin_cancel_tool_call_id
            != self.tool_call_id
        ):
            raise ValueError("tool terminal candidate cancellation call mismatch")

    @property
    def render_profile(self):
        return self.execution_semantics.render_profile

    @property
    def essential_capture_policy(self):
        return self.execution_semantics.essential_capture_policy

    @property
    def essential_result(self):
        return self.execution_semantics.essential_result

    @property
    def terminal_payload_timing(self):
        return self.execution_semantics.terminal_payload_timing

    @property
    def rollup_semantics(self):
        return self.execution_semantics.rollup_semantics

    def bind_projection(
        self,
        projection: ToolResultTerminalProjectionEndReferenceFact,
    ) -> ToolResultEndEvent:
        return ToolResultEndEvent(
            id=self.id,
            run_id=self.run_id,
            turn_id=self.turn_id,
            reply_id=self.reply_id,
            created_at=self.created_at,
            metadata=dict(self.metadata),
            tool_call_id=self.tool_call_id,
            state=self.state,
            artifacts=list(self.artifacts),
            observation_timing=self.observation_timing,
            render_profile=self.render_profile,
            essential_capture_policy=self.essential_capture_policy,
            essential_result=self.essential_result,
            terminal_payload_timing=self.terminal_payload_timing,
            rollup_semantics=self.rollup_semantics,
            terminal_process_observation_receipt=(
                self.terminal_process_observation_receipt
            ),
            terminal_process_monitor_registration=(
                self.terminal_process_monitor_registration
            ),
            terminal_process_monitor_cancellation=(
                self.terminal_process_monitor_cancellation
            ),
            terminal_projection=projection,
        )


@dataclass(frozen=True, slots=True)
class ExternalExecutionResultCandidate:
    """Process-local external result candidate awaiting terminal projections."""

    id: str
    run_id: str
    turn_id: str
    reply_id: str
    external_results: tuple[ExternalToolResultIngressFact, ...]
    created_at: str = field(default_factory=utc_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        ids = tuple(item.result_block.tool_call_id for item in self.external_results)
        if not all((self.id, self.run_id, self.turn_id, self.reply_id)):
            raise ValueError("external result candidate identity cannot be empty")
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("external result candidate call IDs must be non-empty and unique")

    def bind_projections(
        self,
        projections: tuple[ToolResultTerminalProjectionEndReferenceFact, ...],
    ) -> ExternalExecutionResultEvent:
        return ExternalExecutionResultEvent(
            id=self.id,
            run_id=self.run_id,
            turn_id=self.turn_id,
            reply_id=self.reply_id,
            created_at=self.created_at,
            metadata=dict(self.metadata),
            external_results=self.external_results,
            terminal_projections=projections,
        )


class ToolTerminalProjectionService:
    """Prepare projection artifacts before entering the critical event writer."""

    def __init__(
        self,
        *,
        runtime_session: RuntimeSession,
        state_store: ToolTerminalProjectionStateStore,
        contracts: TerminalProjectionContractBundle,
    ) -> None:
        self._runtime_session = runtime_session
        self._state_store = state_store
        self._contracts = contracts

    async def prepare_batch(
        self,
        events: Sequence[AgentEvent | ToolResultEndCandidate],
        *,
        deadline_monotonic: float | None = None,
    ) -> tuple[AgentEvent, ...]:
        prepared = tuple(
            event
            if isinstance(event, ToolResultEndCandidate)
            else self._runtime_session.prepare_event_for_write(event)
            for event in events
        )
        if not any(isinstance(event, ToolResultEndCandidate) for event in prepared):
            return prepared
        deadline = deadline_monotonic or monotonic() + 30.0
        pending = self._state_store.snapshot()
        output: list[AgentEvent] = []
        for event in prepared:
            key = (event.run_id, getattr(event, "tool_call_id", ""))
            if isinstance(event, ToolResultStartEvent):
                if key in pending:
                    raise ValueError("tool terminal batch duplicates pending Start")
                pending[key] = _PendingToolResultSource(event, ())
                output.append(event)
                continue
            if isinstance(event, (ToolResultTextDeltaEvent, ToolResultDataDeltaEvent)):
                source = pending.get(key)
                if source is None:
                    raise ValueError("tool terminal batch delta has no Start")
                pending[key] = _PendingToolResultSource(
                    source.start,
                    (*source.deltas, event),
                )
                output.append(event)
                continue
            if not isinstance(event, ToolResultEndCandidate):
                output.append(event)
                continue
            source = pending.get(key)
            if source is None:
                raise ValueError("ToolResultEnd has no projection source")
            projection = self._build_projection(source=source, terminal=event)
            projection = self._bind_projection(projection)
            await self._persist_projection(
                projection,
                run_id=event.run_id,
                deadline_monotonic=deadline,
            )
            self._runtime_session.transcript_projection_document_registry.register(
                projection.projection_reference,
                projection.document,
            )
            terminal = event.bind_projection(projection.end_reference)
            output.extend((projection.committed_event, terminal))
            pending.pop(key, None)
        return tuple(output)

    async def prepare_external_result_batch(
        self,
        *,
        requirement: RequireExternalExecutionEvent,
        result: ExternalExecutionResultCandidate,
        deadline_monotonic: float,
    ) -> tuple[AgentEvent, ...]:
        """Materialize one projection per typed external result before settlement."""

        if requirement.sequence is None:
            raise ValueError("external projection requires a committed requirement")
        requirements = {
            item.tool_call_id: item for item in requirement.external_tool_calls
        }
        output: list[AgentEvent] = []
        end_references: list[ToolResultTerminalProjectionEndReferenceFact] = []
        for ingress in result.external_results:
            tool_call_id = ingress.result_block.tool_call_id
            expected = requirements.get(tool_call_id)
            if expected is None or (
                ingress.requirement_ref.requirement_fingerprint
                != expected.requirement_fingerprint
            ):
                raise ValueError("external projection requirement identity mismatch")
            projection = self._build_external_projection(
                requirement=requirement,
                result=result,
                ingress=ingress,
            )
            projection = self._bind_projection(projection)
            await self._persist_projection(
                projection,
                run_id=result.run_id,
                deadline_monotonic=deadline_monotonic,
            )
            self._runtime_session.transcript_projection_document_registry.register(
                projection.projection_reference,
                projection.document,
            )
            output.append(projection.committed_event)
            end_references.append(projection.end_reference)
        output.append(
            result.bind_projections(tuple(end_references))
        )
        return tuple(output)

    def validate_tool_document(
        self,
        *,
        terminal: ToolResultEndEvent,
        document: TerminalProjectionDocumentFact,
        batch_prefix: Sequence[AgentEvent] = (),
    ) -> None:
        """Rebuild one live tool projection and require exact document equality."""

        source = self._state_store.snapshot().get(
            (terminal.run_id, terminal.tool_call_id)
        )
        for event in batch_prefix:
            if (
                event.run_id != terminal.run_id
                or getattr(event, "tool_call_id", None) != terminal.tool_call_id
            ):
                continue
            if isinstance(event, ToolResultStartEvent):
                if source is not None:
                    raise ValueError("tool terminal validation found duplicate Start")
                source = _PendingToolResultSource(event, ())
            elif isinstance(event, (ToolResultTextDeltaEvent, ToolResultDataDeltaEvent)):
                if source is None:
                    raise ValueError("tool terminal validation delta lacks Start")
                source = _PendingToolResultSource(
                    source.start,
                    (*source.deltas, event),
                )
        if source is None:
            raise ValueError("tool terminal projection validation lost its Start")
        expected = self._build_projection(source=source, terminal=terminal).document
        if expected != document:
            raise ValueError("tool terminal projection document drifted from End facts")

    def validate_external_document(
        self,
        *,
        requirement: RequireExternalExecutionEvent,
        result: ExternalExecutionResultEvent | ExternalExecutionResultCandidate,
        ingress,
        document: TerminalProjectionDocumentFact,
    ) -> None:
        """Rebuild one external projection from its committed requirement and ingress."""

        expected = self._build_external_projection(
            requirement=requirement,
            result=result,
            ingress=ingress,
        ).document
        if expected != document:
            raise ValueError(
                "external terminal projection document drifted from ingress facts"
            )

    def _build_external_projection(
        self,
        *,
        requirement: RequireExternalExecutionEvent,
        result: ExternalExecutionResultEvent | ExternalExecutionResultCandidate,
        ingress,
    ) -> PreparedToolTerminalProjection:
        block = ToolResultBlock.model_validate(
            thaw_json(ingress.result_block.canonical_block_payload)
        )
        content_blocks, content_artifacts = self._external_content_blocks(
            run_id=result.run_id,
            block=block,
        )
        artifact_refs = tuple(_artifact_fact(item) for item in block.artifacts)
        canonical_semantic = build_frozen_fact(
            CanonicalToolResultBlockSemanticFact,
            schema_version="canonical_tool_result_block_semantic.v1",
            tool_call_id=block.id,
            model_tool_name=block.name,
            result_state=ToolResultStateFact(block.state.value),
            ordered_content_semantic_fingerprints=tuple(
                item.semantic_identity.semantic_fingerprint for item in content_blocks
            ),
            artifact_content_fingerprints=tuple(
                item.ref_fingerprint for item in artifact_refs
            ),
        )
        canonical_block = build_frozen_fact(
            CanonicalToolResultBlockFact,
            schema_version="canonical_tool_result_block.v1",
            semantic_identity=canonical_semantic,
            content_blocks=content_blocks,
            artifact_refs=artifact_refs,
        )
        semantic = build_frozen_fact(
            ToolTerminalProjectionSemanticFact,
            schema_version="tool_terminal_projection_semantic.v1",
            projection_kind="tool_result",
            canonical_result_block_semantic=canonical_semantic,
            execution_semantics=ingress.execution_semantics,
            observation_timing=ingress.observation_timing,
            semantic_artifact_content_fingerprints=tuple(
                item.ref_fingerprint for item in artifact_refs
            ),
            terminal_process_observation_receipt=None,
            terminal_process_monitor_registration=None,
            terminal_process_monitor_cancellation=None,
        )
        payload = ToolTerminalProjectionPayloadFact(
            schema_version="tool_terminal_projection_payload.v2",
            projection_kind="tool_result",
            canonical_result_block=canonical_block,
        )
        source_event_identity = stable_event_identity(
            requirement,
            runtime_session_id=self._runtime_session.runtime_session_id,
        )
        source_fact = build_frozen_fact(
            ToolResultSemanticSourceFact,
            schema_version="tool_result_semantic_source.v1",
            source_kind="external_requirement",
            tool_call_id=block.id,
            source_event_identity=source_event_identity,
            source_delta_count=0,
            source_first_delta_index=None,
            source_last_delta_index=None,
            source_semantic_accumulator=context_fingerprint(
                "external-tool-result-source-accumulator:v1",
                {
                    "requirement_fingerprint": (
                        ingress.requirement_ref.requirement_fingerprint
                    ),
                    "ingress_fingerprint": ingress.ingress_fingerprint,
                },
            ),
            tool_result_semantic_domain_contract_fingerprint=(
                EXTERNAL_TOOL_RESULT_SEMANTIC_DOMAIN_CONTRACT_FINGERPRINT
            ),
            reducer_contract_fingerprint=(
                EXTERNAL_TOOL_RESULT_REDUCER_CONTRACT_FINGERPRINT
            ),
        )
        document = build_frozen_fact(
            TerminalProjectionDocumentFact,
            schema_version="terminal_projection_document.v2",
            document_contract_fingerprint=self._contracts.document.contract_fingerprint,
            semantic_identity=semantic,
            payload=payload,
            source_fact=source_fact,
            usage_status=None,
            usage=None,
            reported_model_id=None,
            tool_result_artifact_refs=artifact_refs,
        )
        canonical_bytes = canonical_json_bytes(document.model_dump(mode="json"))
        if len(canonical_bytes) > self._contracts.document.max_document_bytes:
            raise ValueError("external terminal projection document exceeds contract")
        artifact_id = (
            f"terminal-projection:tool:{result.run_id}:{block.id}:"
            f"{document.fact_fingerprint.removeprefix('sha256:')[:24]}"
        )
        join = ToolTerminalProjectionSemanticJoinFact(
            schema_version="tool_terminal_projection_semantic_join.v1",
            projection_kind="tool_result",
            tool_call_id=block.id,
            model_tool_name=block.name,
            result_state=ToolResultStateFact(block.state.value),
            terminal_process_monitor_registration_semantic_fingerprint=None,
            terminal_process_monitor_cancellation_semantic_fingerprint=None,
            semantic_fingerprint=semantic.semantic_fingerprint,
        )
        reference = build_frozen_fact(
            TerminalProjectionReferenceFact,
            schema_version="terminal_projection_reference.v2",
            projection_kind="tool_result",
            semantic_join=join,
            document_fact_fingerprint=document.fact_fingerprint,
            document_artifact_id=artifact_id,
            document_sha256=f"sha256:{sha256(canonical_bytes).hexdigest()}",
            document_byte_count=len(canonical_bytes),
            document_contract_fingerprint=self._contracts.document.contract_fingerprint,
        )
        committed = ToolResultTerminalProjectionCommittedEvent(
            id=f"tool_terminal_projection:{result.run_id}:{block.id}:committed",
            run_id=result.run_id,
            turn_id=result.turn_id,
            reply_id=result.reply_id,
            created_at=result.created_at,
            metadata=dict(result.metadata),
            tool_call_id=block.id,
            source_kind="external_requirement",
            source_event_identity=source_event_identity,
            projection_reference=reference,
        )
        end_reference = build_frozen_fact(
            ToolResultTerminalProjectionEndReferenceFact,
            schema_version="tool_result_terminal_projection_end_ref.v2",
            projection_committed_event_identity=stable_event_identity(
                committed,
                runtime_session_id=self._runtime_session.runtime_session_id,
            ),
            projection_reference=reference,
        )
        return PreparedToolTerminalProjection(
            document=document,
            canonical_document_bytes=canonical_bytes,
            projection_reference=reference,
            committed_event=committed,
            end_reference=end_reference,
            content_artifacts=content_artifacts,
        )

    def _external_content_blocks(
        self,
        *,
        run_id: str,
        block: ToolResultBlock,
    ) -> tuple[
        tuple[CanonicalToolResultContentBlockFact, ...],
        tuple[_ContentArtifactCandidate, ...],
    ]:
        output: list[CanonicalToolResultContentBlockFact] = []
        artifacts: list[_ContentArtifactCandidate] = []
        for index, item in enumerate(block.output):
            if isinstance(item, TextBlock):
                content, candidate = self._content(
                    item.text,
                    media_type=self._contracts.content_canonicalization.text_media_type,
                    owner_key=f"external-text:{run_id}:{block.id}:{index}",
                )
                semantic = build_frozen_fact(
                    CanonicalToolResultTextBlockSemanticFact,
                    schema_version="canonical_tool_result_text_block_semantic.v1",
                    content_kind="text",
                    block_id=item.id,
                    block_index=index,
                    content_semantic_identity=content.semantic_identity,
                )
            else:
                assert isinstance(item, DataBlock)
                text = (
                    item.source.data
                    if item.source.type == "base64"
                    else item.source.url
                )
                media_type = normalize_data_media_type(
                    item.source.media_type,
                    contract=self._contracts.media_type_normalization,
                )
                content, candidate = self._content(
                    text,
                    media_type=media_type,
                    owner_key=f"external-data:{run_id}:{block.id}:{index}",
                )
                semantic = build_frozen_fact(
                    CanonicalToolResultDataBlockSemanticFact,
                    schema_version="canonical_tool_result_data_block_semantic.v2",
                    content_kind="data",
                    block_id=item.id,
                    block_index=index,
                    name=item.name,
                    media_type=media_type,
                    source_kind=item.source.type,
                    content_semantic_identity=content.semantic_identity,
                    artifact_content_fingerprints=(),
                )
            if candidate is not None:
                artifacts.append(candidate)
            output.append(
                build_frozen_fact(
                    CanonicalToolResultContentBlockFact,
                    schema_version="canonical_tool_result_content_block.v1",
                    semantic_identity=semantic,
                    content=content,
                )
            )
        return tuple(output), tuple(artifacts)

    def _build_projection(
        self,
        *,
        source: _PendingToolResultSource,
        terminal: ToolResultEndEvent,
    ) -> PreparedToolTerminalProjection:
        if source.start.tool_call_id != terminal.tool_call_id:
            raise ValueError("tool terminal projection call identity mismatch")
        content_blocks, content_artifacts = self._content_blocks(source)
        artifact_refs = tuple(_artifact_fact(item) for item in terminal.artifacts)
        content_semantics = tuple(
            item.semantic_identity.semantic_fingerprint for item in content_blocks
        )
        artifact_fingerprints = tuple(item.ref_fingerprint for item in artifact_refs)
        canonical_semantic = build_frozen_fact(
            CanonicalToolResultBlockSemanticFact,
            schema_version="canonical_tool_result_block_semantic.v1",
            tool_call_id=terminal.tool_call_id,
            model_tool_name=source.start.tool_call_name,
            result_state=ToolResultStateFact(terminal.state.value),
            ordered_content_semantic_fingerprints=content_semantics,
            artifact_content_fingerprints=artifact_fingerprints,
        )
        canonical_block = build_frozen_fact(
            CanonicalToolResultBlockFact,
            schema_version="canonical_tool_result_block.v1",
            semantic_identity=canonical_semantic,
            content_blocks=content_blocks,
            artifact_refs=artifact_refs,
        )
        execution_semantics = ToolResultExecutionSemanticsFact(
            render_profile=terminal.render_profile,
            result_state=ToolResultStateFact(terminal.state.value),
            essential_capture_policy=terminal.essential_capture_policy,
            essential_result=terminal.essential_result,
            terminal_payload_timing=terminal.terminal_payload_timing,
            rollup_semantics=terminal.rollup_semantics,
        )
        semantic = build_frozen_fact(
            ToolTerminalProjectionSemanticFact,
            schema_version="tool_terminal_projection_semantic.v1",
            projection_kind="tool_result",
            canonical_result_block_semantic=canonical_semantic,
            execution_semantics=execution_semantics,
            observation_timing=terminal.observation_timing,
            semantic_artifact_content_fingerprints=artifact_fingerprints,
            terminal_process_observation_receipt=(
                terminal.terminal_process_observation_receipt
            ),
            terminal_process_monitor_registration=(
                terminal.terminal_process_monitor_registration
            ),
            terminal_process_monitor_cancellation=(
                terminal.terminal_process_monitor_cancellation
            ),
        )
        payload = ToolTerminalProjectionPayloadFact(
            schema_version="tool_terminal_projection_payload.v2",
            projection_kind="tool_result",
            canonical_result_block=canonical_block,
        )
        source_fact = build_frozen_fact(
            ToolResultSemanticSourceFact,
            schema_version="tool_result_semantic_source.v1",
            source_kind="tool_result_stream",
            tool_call_id=terminal.tool_call_id,
            source_event_identity=stable_event_identity(
                source.start,
                runtime_session_id=self._runtime_session.runtime_session_id,
            ),
            source_delta_count=len(source.deltas),
            source_first_delta_index=0 if source.deltas else None,
            source_last_delta_index=len(source.deltas) - 1 if source.deltas else None,
            source_semantic_accumulator=_tool_source_accumulator(source.deltas),
            tool_result_semantic_domain_contract_fingerprint=(
                TOOL_RESULT_SEMANTIC_DOMAIN_CONTRACT_FINGERPRINT
            ),
            reducer_contract_fingerprint=(
                TOOL_TERMINAL_PROJECTION_REDUCER_CONTRACT_FINGERPRINT
            ),
        )
        document = build_frozen_fact(
            TerminalProjectionDocumentFact,
            schema_version="terminal_projection_document.v2",
            document_contract_fingerprint=self._contracts.document.contract_fingerprint,
            semantic_identity=semantic,
            payload=payload,
            source_fact=source_fact,
            usage_status=None,
            usage=None,
            reported_model_id=None,
            tool_result_artifact_refs=artifact_refs,
        )
        canonical_bytes = canonical_json_bytes(document.model_dump(mode="json"))
        if len(canonical_bytes) > self._contracts.document.max_document_bytes:
            raise ValueError("tool terminal projection document exceeds contract")
        artifact_id = (
            f"terminal-projection:tool:{terminal.run_id}:{terminal.tool_call_id}:"
            f"{document.fact_fingerprint.removeprefix('sha256:')[:24]}"
        )
        join = ToolTerminalProjectionSemanticJoinFact(
            schema_version="tool_terminal_projection_semantic_join.v1",
            projection_kind="tool_result",
            tool_call_id=terminal.tool_call_id,
            model_tool_name=source.start.tool_call_name,
            result_state=ToolResultStateFact(terminal.state.value),
            terminal_process_monitor_registration_semantic_fingerprint=(
                None
                if terminal.terminal_process_monitor_registration is None
                else terminal.terminal_process_monitor_registration.registration_semantic_fingerprint
            ),
            terminal_process_monitor_cancellation_semantic_fingerprint=(
                None
                if terminal.terminal_process_monitor_cancellation is None
                else terminal.terminal_process_monitor_cancellation.cancellation_semantic_fingerprint
            ),
            semantic_fingerprint=semantic.semantic_fingerprint,
        )
        reference = build_frozen_fact(
            TerminalProjectionReferenceFact,
            schema_version="terminal_projection_reference.v2",
            projection_kind="tool_result",
            semantic_join=join,
            document_fact_fingerprint=document.fact_fingerprint,
            document_artifact_id=artifact_id,
            document_sha256=f"sha256:{sha256(canonical_bytes).hexdigest()}",
            document_byte_count=len(canonical_bytes),
            document_contract_fingerprint=self._contracts.document.contract_fingerprint,
        )
        committed = ToolResultTerminalProjectionCommittedEvent(
            id=f"tool_terminal_projection:{terminal.run_id}:{terminal.tool_call_id}:committed",
            run_id=terminal.run_id,
            turn_id=terminal.turn_id,
            reply_id=terminal.reply_id,
            created_at=terminal.created_at,
            metadata=dict(terminal.metadata),
            tool_call_id=terminal.tool_call_id,
            source_kind=source_fact.source_kind,
            source_event_identity=source_fact.source_event_identity,
            projection_reference=reference,
        )
        end_ref = build_frozen_fact(
            ToolResultTerminalProjectionEndReferenceFact,
            schema_version="tool_result_terminal_projection_end_ref.v2",
            projection_committed_event_identity=stable_event_identity(
                committed,
                runtime_session_id=self._runtime_session.runtime_session_id,
            ),
            projection_reference=reference,
        )
        return PreparedToolTerminalProjection(
            document=document,
            canonical_document_bytes=canonical_bytes,
            projection_reference=reference,
            committed_event=committed,
            end_reference=end_ref,
            content_artifacts=content_artifacts,
        )

    def _bind_projection(
        self,
        projection: PreparedToolTerminalProjection,
    ) -> PreparedToolTerminalProjection:
        event = self._runtime_session.prepare_event_for_write(
            projection.committed_event
        )
        end_ref = build_frozen_fact(
            ToolResultTerminalProjectionEndReferenceFact,
            schema_version="tool_result_terminal_projection_end_ref.v2",
            projection_committed_event_identity=stable_event_identity(
                event,
                runtime_session_id=self._runtime_session.runtime_session_id,
            ),
            projection_reference=projection.projection_reference,
        )
        return PreparedToolTerminalProjection(
            document=projection.document,
            canonical_document_bytes=projection.canonical_document_bytes,
            projection_reference=projection.projection_reference,
            committed_event=event,
            end_reference=end_ref,
            content_artifacts=projection.content_artifacts,
        )

    def _content_blocks(
        self,
        source: _PendingToolResultSource,
    ) -> tuple[
        tuple[CanonicalToolResultContentBlockFact, ...],
        tuple[_ContentArtifactCandidate, ...],
    ]:
        blocks: list[CanonicalToolResultContentBlockFact] = []
        artifacts: list[_ContentArtifactCandidate] = []
        text_parts: list[str] = []

        def flush_text() -> None:
            if not text_parts:
                return
            text = "".join(text_parts)
            text_parts.clear()
            index = len(blocks)
            content, candidate = self._content(
                text,
                media_type=self._contracts.content_canonicalization.text_media_type,
                owner_key=f"text:{source.start.run_id}:{source.start.tool_call_id}:{index}",
            )
            if candidate is not None:
                artifacts.append(candidate)
            semantic = build_frozen_fact(
                CanonicalToolResultTextBlockSemanticFact,
                schema_version="canonical_tool_result_text_block_semantic.v1",
                content_kind="text",
                block_id=f"tool-result-text:{source.start.tool_call_id}:{index}",
                block_index=index,
                content_semantic_identity=content.semantic_identity,
            )
            blocks.append(
                build_frozen_fact(
                    CanonicalToolResultContentBlockFact,
                    schema_version="canonical_tool_result_content_block.v1",
                    semantic_identity=semantic,
                    content=content,
                )
            )

        for delta in source.deltas:
            if isinstance(delta, ToolResultTextDeltaEvent):
                text_parts.append(delta.delta)
                continue
            flush_text()
            text = delta.data if delta.data is not None else delta.url
            assert text is not None
            media_type = normalize_data_media_type(
                delta.media_type,
                contract=self._contracts.media_type_normalization,
            )
            index = len(blocks)
            content, candidate = self._content(
                text,
                media_type=media_type,
                owner_key=f"data:{source.start.run_id}:{source.start.tool_call_id}:{index}",
            )
            if candidate is not None:
                artifacts.append(candidate)
            semantic = build_frozen_fact(
                CanonicalToolResultDataBlockSemanticFact,
                schema_version="canonical_tool_result_data_block_semantic.v2",
                content_kind="data",
                block_id=delta.block_id,
                block_index=index,
                name=None,
                media_type=media_type,
                source_kind="base64" if delta.data is not None else "url",
                content_semantic_identity=content.semantic_identity,
                artifact_content_fingerprints=(),
            )
            blocks.append(
                build_frozen_fact(
                    CanonicalToolResultContentBlockFact,
                    schema_version="canonical_tool_result_content_block.v1",
                    semantic_identity=semantic,
                    content=content,
                )
            )
        flush_text()
        return tuple(blocks), tuple(artifacts)

    def _content(
        self,
        text: str,
        *,
        media_type: str,
        owner_key: str,
    ) -> tuple[TerminalContentFact, _ContentArtifactCandidate | None]:
        encoded = text.encode("utf-8")
        if len(encoded) <= self._contracts.document.max_inline_content_bytes_per_block:
            return (
                build_terminal_inline_content(
                    text,
                    media_type=media_type,
                    contract=self._contracts.content_canonicalization,
                ),
                None,
            )
        digest = f"sha256:{sha256(encoded).hexdigest()}"
        semantic = build_frozen_fact(
            TerminalContentSemanticFact,
            schema_version="terminal_content_semantic.v2",
            canonical_content_sha256=digest,
            utf8_bytes=len(encoded),
            media_type=media_type,
            content_canonicalization_contract_fingerprint=(
                self._contracts.content_canonicalization.contract_fingerprint
            ),
        )
        artifact_id = (
            "terminal-projection-content:"
            f"{sha256(owner_key.encode()).hexdigest()[:12]}:"
            f"{digest.removeprefix('sha256:')[:24]}"
        )
        reference = build_frozen_fact(
            TerminalArtifactContentReferenceFact,
            schema_version="terminal_artifact_content_ref.v2",
            storage_kind="artifact",
            semantic_identity=semantic,
            artifact_id=artifact_id,
            artifact_sha256=digest,
            artifact_bytes=len(encoded),
            media_type=media_type,
            artifact_codec="identity_utf8",
            artifact_codec_contract_fingerprint=(
                self._contracts.artifact_codec.contract_fingerprint
            ),
        )
        return reference, _ContentArtifactCandidate(
            artifact_id=artifact_id,
            text=text,
            media_type=media_type,
            sha256=digest,
            byte_count=len(encoded),
        )

    async def _persist_projection(
        self,
        projection: PreparedToolTerminalProjection,
        *,
        run_id: str,
        deadline_monotonic: float,
    ) -> None:
        for candidate in projection.content_artifacts:
            confirmation = await self._runtime_session.context_input_io_service.execute(
                operation_name="tool-terminal-projection-content-write",
                operation=lambda candidate=candidate: (
                    self._runtime_session.archive.put_text_if_absent_or_confirm_identical(
                        candidate.artifact_id,
                        candidate.text,
                        session_id=self._runtime_session.runtime_session_id,
                        run_id=run_id,
                        media_type=candidate.media_type,
                        semantic_metadata={
                            "projection_kind": "tool_result_content",
                            "content_sha256": candidate.sha256,
                            "artifact_codec_contract_fingerprint": (
                                self._contracts.artifact_codec.contract_fingerprint
                            ),
                        },
                        deadline_monotonic=deadline_monotonic,
                    )
                ),
                deadline_monotonic=deadline_monotonic,
            )
            if (
                confirmation.result.id != candidate.artifact_id
                or confirmation.result.digest != candidate.sha256
                or confirmation.result.size_bytes != candidate.byte_count
            ):
                raise RuntimeError("tool projection content artifact drifted")
        reference = projection.projection_reference
        confirmation = await self._runtime_session.context_input_io_service.execute(
            operation_name="tool-terminal-projection-document-write",
            operation=lambda: self._runtime_session.archive.put_text_if_absent_or_confirm_identical(
                reference.document_artifact_id,
                projection.canonical_document_bytes.decode("utf-8"),
                session_id=self._runtime_session.runtime_session_id,
                run_id=run_id,
                media_type=TERMINAL_PROJECTION_MEDIA_TYPE,
                semantic_metadata={
                    "projection_kind": "tool_result",
                    "document_fact_fingerprint": reference.document_fact_fingerprint,
                    "document_contract_fingerprint": (
                        reference.document_contract_fingerprint
                    ),
                },
                deadline_monotonic=deadline_monotonic,
            ),
            deadline_monotonic=deadline_monotonic,
        )
        if (
            confirmation.result.id != reference.document_artifact_id
            or confirmation.result.digest != reference.document_sha256
            or confirmation.result.size_bytes != reference.document_byte_count
        ):
            raise RuntimeError("tool terminal projection artifact drifted")


def _tool_source_accumulator(
    deltas: Sequence[ToolResultTextDeltaEvent | ToolResultDataDeltaEvent],
) -> str:
    accumulator = context_fingerprint("tool-terminal-source-accumulator:v1", "empty")
    for index, event in enumerate(deltas):
        accumulator = context_fingerprint(
            "tool-terminal-source-accumulator:v1",
            {
                "previous": accumulator,
                "index": index,
                "event_type": str(event.type),
                "canonical_event": canonical_event_payload_bytes(
                    event.model_copy(update={"sequence": None})
                ).decode("utf-8"),
            },
        )
    return accumulator


def _artifact_fact(artifact: ToolResultArtifactRef) -> ContextToolResultArtifactRefFact:
    preview = None
    if artifact.preview is not None:
        read_more = freeze_json(artifact.preview.read_more)
        from pulsara_agent.primitives.context import FrozenJsonObjectFact

        if not isinstance(read_more, FrozenJsonObjectFact):
            raise TypeError("artifact read_more must be an object")
        preview = ContextToolResultPreviewFact(
            preview_policy=artifact.preview.preview_policy,
            preview_chars=artifact.preview.preview_chars,
            original_chars=artifact.preview.original_chars,
            original_bytes=artifact.preview.original_bytes,
            omitted_middle_chars=artifact.preview.omitted_middle_chars,
            visible_head_chars=artifact.preview.visible_head_chars,
            visible_tail_chars=artifact.preview.visible_tail_chars,
            read_more=read_more,
        )
    payload = {
        "artifact_id": artifact.artifact_id,
        "role": artifact.role,
        "media_type": artifact.media_type,
        "size_bytes": artifact.size_bytes,
        "stored_complete": artifact.stored_complete,
        "loss_reason": artifact.loss_reason,
        "preview": preview,
    }
    return ContextToolResultArtifactRefFact(
        **payload,
        ref_fingerprint=context_fingerprint(
            "context-tool-result-artifact-ref:v1", payload
        ),
    )


__all__ = [
    "ExternalExecutionResultCandidate",
    "PreparedToolTerminalProjection",
    "TOOL_RESULT_SEMANTIC_DOMAIN_CONTRACT_FINGERPRINT",
    "TOOL_TERMINAL_PROJECTION_REDUCER_CONTRACT_FINGERPRINT",
    "ToolTerminalProjectionService",
    "ToolTerminalProjectionStateStore",
    "ToolResultEndCandidate",
]
