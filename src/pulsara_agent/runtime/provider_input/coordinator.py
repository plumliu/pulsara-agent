"""RuntimeSession owner for provider-input preparation and artifact operations."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from threading import RLock
from time import monotonic

from pulsara_agent.event import (
    EventContext,
    EventType,
    ProviderInputAppendCommittedEvent,
    utc_now,
)
from pulsara_agent.event_log.serialization import DEFAULT_EVENT_SCHEMA_REGISTRY
from pulsara_agent.llm.terminal_projection import stable_event_identity
from pulsara_agent.llm.terminal_projection import (
    TERMINAL_PROJECTION_MEDIA_TYPE,
    hydrate_terminal_projection_text,
)
from pulsara_agent.llm.resolution import ResolvedModelCall
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.provider_input import (
    CommittedProviderInputReferenceFact,
    ContextInputManifestProjectionReferenceFact,
    OneShotGenerationScopeFact,
)
from pulsara_agent.runtime.context_engine.types import CompiledContext
from pulsara_agent.runtime.context_input.live import PreparedLiveContextSnapshot
from pulsara_agent.runtime.provider_input.materialization import hydrate_carrier
from pulsara_agent.runtime.provider_input.continuation import (
    PreparedProviderInputContinuationMaterialization,
    prepare_provider_input_continuation,
    required_continuation_content_artifacts,
)
from pulsara_agent.runtime.provider_input.planner import (
    PreparedProviderInputPlanningBundle,
    PreparedProviderInputStartBundle,
    ProviderInputResidentRestoreRequired,
    ProviderInputRolloverPlanningRequired,
    build_session_provider_input_continuity_scope,
    build_session_generation_close_event,
    plan_one_shot_provider_input,
    plan_provider_input_append,
)
from pulsara_agent.runtime.provider_input.store import (
    ProviderInputGenerationStore,
    ProviderInputResidentGeneration,
)
from pulsara_agent.runtime.provider_input.vector import (
    load_provider_input_vector_state,
    persist_provider_input_artifacts,
)


class ProviderInputGenerationCoordinator:
    """Owns async preparation; committed state remains reducer-owned."""

    def __init__(self, *, runtime_session, store: ProviderInputGenerationStore) -> None:
        self._runtime_session = runtime_session
        self._store = store
        self._prepare_lock = asyncio.Lock()
        self._attempt_lock = RLock()
        self._attempts: dict[str, _OwnedPreparationAttempt] = {}

    def committed_source_heads_for_compiled_call(
        self,
        *,
        prepared_context_input: PreparedLiveContextSnapshot,
    ):
        """Freeze historical replacement heads before compiler allocation."""

        scope = build_session_provider_input_continuity_scope(
            runtime_session_id=self._runtime_session.runtime_session_id,
            prepared_context_input=prepared_context_input,
        )
        core = self._store.snapshot(scope.scope_fingerprint).core_state
        return core.committed_source_heads if core is not None else ()

    async def prepare_compiled_call(
        self,
        *,
        call: ResolvedModelCall,
        compiled_context: CompiledContext,
        prepared_context_input: PreparedLiveContextSnapshot,
        event_context: EventContext,
        deadline_monotonic: float | None = None,
    ) -> PreparedProviderInputPlanningBundle:
        deadline = deadline_monotonic or monotonic() + 30.0
        scope_fact = build_session_provider_input_continuity_scope(
            runtime_session_id=self._runtime_session.runtime_session_id,
            prepared_context_input=prepared_context_input,
        )
        async with self._prepare_lock:
            if self._store.has_staged_preparation_for_scope(
                scope_fact.scope_fingerprint
            ):
                raise ProviderInputPreparationStale(
                    "provider generation scope already has a staged preparation"
                )
            snapshot = self._store.snapshot(scope_fact.scope_fingerprint)
            rollover_from = None
            if snapshot.core_state is not None and snapshot.resident is None:
                await self._restore_resident(
                    generation_id=snapshot.core_state.generation.generation_id,
                    root=snapshot.core_state.unit_vector_root,
                    deadline_monotonic=deadline,
                )
                snapshot = self._store.snapshot(scope_fact.scope_fingerprint)
            rollover_intent = None
            continuation_materializations: dict[
                str, PreparedProviderInputContinuationMaterialization
            ] = {}
            for _attempt in range(3):
                try:
                    pending_continuation = _planning_pending_continuation(
                        snapshot=snapshot,
                        rollover_from=rollover_from,
                    )
                    pending_materialization = None
                    if pending_continuation is not None:
                        pending_materialization = continuation_materializations.get(
                            pending_continuation.continuation_fingerprint
                        )
                        if pending_materialization is None:
                            pending_materialization = (
                                await self._prepare_pending_continuation(
                                    pending_continuation,
                                    deadline_monotonic=deadline,
                                    stable_entries=(
                                        prepared_context_input.transcript_projection_evidence.stable_entries
                                    ),
                                )
                            )
                            continuation_materializations[
                                pending_continuation.continuation_fingerprint
                            ] = pending_materialization
                    prepared = plan_provider_input_append(
                        call=call,
                        compiled_context=compiled_context,
                        prepared_context_input=prepared_context_input,
                        generation_snapshot=snapshot,
                        event_context=event_context,
                        runtime_session_id=self._runtime_session.runtime_session_id,
                        rollover_from=rollover_from,
                        rollover_intent=rollover_intent,
                        pending_continuation_materialization=(pending_materialization),
                    )
                    if not isinstance(prepared, PreparedProviderInputPlanningBundle):
                        raise ProviderInputPreparationStale(
                            "pre-manifest provider planner published a start candidate"
                        )
                    break
                except ProviderInputResidentRestoreRequired as exc:
                    core = snapshot.core_state
                    if (
                        core is None
                        or core.generation.generation_id != exc.generation_id
                    ):
                        raise
                    await self._restore_resident(
                        generation_id=exc.generation_id,
                        root=core.unit_vector_root,
                        deadline_monotonic=deadline,
                    )
                    snapshot = self._store.snapshot(scope_fact.scope_fingerprint)
                except ProviderInputRolloverPlanningRequired as exc:
                    if snapshot.core_state is None or rollover_from is not None:
                        raise
                    rollover_from = snapshot
                    rollover_intent = exc.intent
            else:
                raise ProviderInputPreparationStale(
                    "provider input planning did not converge"
                )
            await persist_provider_input_artifacts(
                runtime_session=self._runtime_session,
                run_id=event_context.run_id,
                artifacts=prepared.artifacts,
                deadline_monotonic=deadline,
            )
            current = self._store.snapshot(scope_fact.scope_fingerprint)
            if (
                current.scope_binding != snapshot.scope_binding
                or current.core_state != snapshot.core_state
                or current.preparation_attribution != snapshot.preparation_attribution
            ):
                raise ProviderInputPreparationStale(
                    "provider generation changed during artifact confirmation"
                )
            if rollover_from is not None:
                old_core = rollover_from.core_state
                assert old_core is not None
                current_old = self._store.snapshot(
                    old_core.generation.scope.scope_fingerprint
                )
                if (
                    current_old.core_state != old_core
                    or current_old.scope_binding != rollover_from.scope_binding
                    or current_old.preparation_attribution
                    != rollover_from.preparation_attribution
                ):
                    raise ProviderInputPreparationStale(
                        "provider rollover predecessor changed during preparation"
                    )
            return prepared

    async def finalize_compiled_call(
        self,
        *,
        call: ResolvedModelCall,
        compiled_context: CompiledContext,
        prepared_context_input: PreparedLiveContextSnapshot,
        event_context: EventContext,
        planning_bundle: PreparedProviderInputPlanningBundle,
        manifest_projection_reference: ContextInputManifestProjectionReferenceFact,
        deadline_monotonic: float | None = None,
    ) -> PreparedProviderInputStartBundle:
        """Create the sole preparation owner after the manifest is confirmed FULL."""

        deadline = deadline_monotonic or monotonic() + 30.0
        scope = build_session_provider_input_continuity_scope(
            runtime_session_id=self._runtime_session.runtime_session_id,
            prepared_context_input=prepared_context_input,
        )
        async with self._prepare_lock:
            if self._store.has_staged_preparation_for_scope(scope.scope_fingerprint):
                raise ProviderInputPreparationStale(
                    "provider generation scope already has a staged preparation"
                )
            snapshot = self._store.snapshot(scope.scope_fingerprint)
            if snapshot.core_state is not None and snapshot.resident is None:
                await self._restore_resident(
                    generation_id=snapshot.core_state.generation.generation_id,
                    root=snapshot.core_state.unit_vector_root,
                    deadline_monotonic=deadline,
                )
                snapshot = self._store.snapshot(scope.scope_fingerprint)
            rollover_intent = planning_bundle.prepared_plan.rollover_intent
            rollover_from = snapshot if rollover_intent is not None else None
            pending = _planning_pending_continuation(
                snapshot=snapshot,
                rollover_from=rollover_from,
            )
            pending_materialization = None
            if pending is not None:
                pending_materialization = await self._prepare_pending_continuation(
                    pending,
                    deadline_monotonic=deadline,
                    stable_entries=(
                        prepared_context_input.transcript_projection_evidence.stable_entries
                    ),
                )
            finalized = plan_provider_input_append(
                call=call,
                compiled_context=compiled_context,
                prepared_context_input=prepared_context_input,
                generation_snapshot=snapshot,
                event_context=event_context,
                runtime_session_id=self._runtime_session.runtime_session_id,
                rollover_from=rollover_from,
                rollover_intent=rollover_intent,
                pending_continuation_materialization=pending_materialization,
                manifest_projection_reference=manifest_projection_reference,
            )
            if not isinstance(finalized, PreparedProviderInputStartBundle):
                raise ProviderInputPreparationStale(
                    "post-manifest provider planner did not produce a start candidate"
                )
            if (
                finalized.prepared_plan != planning_bundle.prepared_plan
                or finalized.carrier != planning_bundle.carrier
                or finalized.resident != planning_bundle.resident
                or finalized.artifacts != planning_bundle.artifacts
            ):
                raise ProviderInputPreparationStale(
                    "provider generation changed between manifest and finalization"
                )
            finalized = _bind_bundle_to_write_boundary(
                runtime_session=self._runtime_session,
                bundle=finalized,
            )
            await persist_provider_input_artifacts(
                runtime_session=self._runtime_session,
                run_id=event_context.run_id,
                artifacts=finalized.artifacts,
                deadline_monotonic=deadline,
            )
            current = self._store.snapshot(scope.scope_fingerprint)
            if (
                current.scope_binding != snapshot.scope_binding
                or current.core_state != snapshot.core_state
                or current.preparation_attribution != snapshot.preparation_attribution
            ):
                raise ProviderInputPreparationStale(
                    "provider generation changed during post-manifest confirmation"
                )
            self._register_attempt(finalized, run_id=event_context.run_id)
            return finalized

    async def prepare_one_shot_call(
        self,
        *,
        call: ResolvedModelCall,
        context: LLMContext,
        event_context: EventContext,
        operation_kind: str,
        operation_id: str,
        attempt_index: int = 0,
        clock_observed_at_utc: str | None = None,
        deadline_monotonic: float | None = None,
    ) -> PreparedProviderInputStartBundle:
        """Prepare one direct invocation under its own exact generation."""

        deadline = deadline_monotonic or monotonic() + 30.0
        scope = build_frozen_fact(
            OneShotGenerationScopeFact,
            schema_version="one_shot_generation_scope.v1",
            operation_kind=operation_kind,
            operation_id=operation_id,
            attempt_index=attempt_index,
        )
        async with self._prepare_lock:
            if self._store.has_staged_preparation_for_scope(scope.scope_fingerprint):
                raise ProviderInputPreparationStale(
                    "one-shot provider scope already has a staged preparation"
                )
            snapshot = self._store.snapshot(scope.scope_fingerprint)
            prepared = plan_one_shot_provider_input(
                call=call,
                context=context,
                generation_snapshot=snapshot,
                event_context=event_context,
                runtime_session_id=self._runtime_session.runtime_session_id,
                operation_kind=operation_kind,
                operation_id=operation_id,
                attempt_index=attempt_index,
                clock_observed_at_utc=clock_observed_at_utc or utc_now(),
            )
            prepared = _bind_bundle_to_write_boundary(
                runtime_session=self._runtime_session,
                bundle=prepared,
            )
            await persist_provider_input_artifacts(
                runtime_session=self._runtime_session,
                run_id=event_context.run_id,
                artifacts=prepared.artifacts,
                deadline_monotonic=deadline,
            )
            current = self._store.snapshot(scope.scope_fingerprint)
            if (
                current.scope_binding != snapshot.scope_binding
                or current.core_state is not None
                or current.preparation_attribution is not None
            ):
                raise ProviderInputPreparationStale(
                    "one-shot provider scope changed during artifact confirmation"
                )
            self._register_attempt(
                prepared,
                run_id=event_context.run_id,
            )
            return prepared

    def activate_preparation(
        self,
        bundle: PreparedProviderInputStartBundle,
    ) -> None:
        """Transfer a prepared resident into the exact ModelStart commit owner."""

        owner = bundle.prepared_candidate.preparation_ownership
        with self._attempt_lock:
            attempt = self._attempts.get(owner.preparation_id)
            if attempt is None or attempt.bundle != bundle:
                raise ProviderInputPreparationStale(
                    "provider preparation attempt is not owned by this session"
                )
            if attempt.activated:
                return
            if bundle.is_one_shot:
                self._store.stage_ephemeral_preparation(owner, bundle.resident)
            else:
                if self._store.preparation_snapshot(owner.preparation_id) is None:
                    raise ProviderInputPreparationStale(
                        "compiled provider preparation is not durably attributed"
                    )
                self._store.stage_prepared_resident(owner, bundle.resident)
            attempt.activated = True

    @property
    def owned_preparation_count(self) -> int:
        with self._attempt_lock:
            return len(self._attempts)

    def owned_preparation_ids_for_run(self, run_id: str) -> tuple[str, ...]:
        with self._attempt_lock:
            return tuple(
                sorted(
                    preparation_id
                    for preparation_id, attempt in self._attempts.items()
                    if attempt.run_id == run_id
                )
            )

    async def settle_run_preparations(
        self,
        run_id: str,
        *,
        reason: str = "run_terminated_before_start",
    ) -> None:
        for preparation_id in self.owned_preparation_ids_for_run(run_id):
            await self.abandon_uncommitted_preparation(
                preparation_id,
                reason=reason,
            )

    async def abandon_uncommitted_preparation(
        self,
        preparation_id: str,
        *,
        reason: str = "caller_cancelled_before_start",
    ) -> None:
        if self._store.preparation_snapshot(preparation_id) is None:
            self._store.discard_staged_resident(preparation_id)
            self._retire_attempt(preparation_id)
            return
        await self._runtime_session.provider_input_preparation_recovery_service.abandon_preparation(
            preparation_id,
            reason=reason,
        )
        if self._store.preparation_snapshot(preparation_id) is None:
            self._store.discard_staged_resident(preparation_id)
            self._retire_attempt(preparation_id)

    def reject_before_worker_start(
        self,
        bundle: PreparedProviderInputStartBundle,
    ) -> None:
        """Release an ephemeral owner when the registry rejects installation."""

        preparation_id = bundle.prepared_candidate.preparation_ownership.preparation_id
        if self._store.preparation_snapshot(preparation_id) is not None:
            # ContextCompiled is durable; its recovery service remains owner.
            return
        self._store.discard_staged_resident(preparation_id)
        self._retire_attempt(preparation_id)

    def close_owned_attempts_after_recovery(self) -> None:
        """Drop process-local owners only after durable recovery has completed."""

        with self._attempt_lock:
            preparation_ids = tuple(self._attempts)
        unresolved = tuple(
            preparation_id
            for preparation_id in preparation_ids
            if self._store.preparation_snapshot(preparation_id) is not None
        )
        if unresolved:
            raise RuntimeError(
                "cannot close provider input coordinator with unresolved preparations"
            )
        for preparation_id in preparation_ids:
            self._store.discard_staged_resident(preparation_id)
            self._retire_attempt(preparation_id)

    def close_open_session_generations_sync(self) -> None:
        """Durably terminalize every open session/window generation at teardown."""

        for snapshot in self._store.open_session_continuity_snapshots():
            core = snapshot.core_state
            attribution = snapshot.attribution_state
            if core is None or attribution is None:
                raise RuntimeError("open provider generation lacks attribution state")
            start_ref = attribution.latest_model_start_event_ref
            if start_ref is None:
                raise RuntimeError(
                    "open provider generation lacks ModelStart authority"
                )
            rows = self._runtime_session.event_log.read_raw_events_by_id(
                (start_ref.event_id,),
                deadline_monotonic=monotonic() + 30.0,
            )
            if len(rows) != 1:
                raise RuntimeError("provider generation ModelStart is unavailable")
            start = rows[0].decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
            if start.type is not EventType.MODEL_CALL_START:
                raise RuntimeError("provider generation attribution is not ModelStart")
            close_event = build_session_generation_close_event(
                core=core,
                event_context=EventContext(
                    run_id=start.run_id,
                    turn_id=start.turn_id,
                    reply_id=start.reply_id,
                ),
            )
            result = self._runtime_session.write_events_from_thread((close_event,))
            if tuple(item.id for item in result.committed_events) != (close_event.id,):
                raise RuntimeError(
                    "provider generation session close was not committed"
                )

    def _register_attempt(
        self,
        bundle: PreparedProviderInputStartBundle,
        *,
        run_id: str,
    ) -> None:
        owner = bundle.prepared_candidate.preparation_ownership
        attempt = _OwnedPreparationAttempt(
            run_id=run_id,
            bundle=bundle,
        )
        with self._attempt_lock:
            existing = self._attempts.get(owner.preparation_id)
            if existing is not None and existing.bundle != bundle:
                raise ProviderInputPreparationStale(
                    "provider preparation attempt identity conflict"
                )
            self._attempts[owner.preparation_id] = existing or attempt

    def _retire_attempt(self, preparation_id: str) -> None:
        with self._attempt_lock:
            self._attempts.pop(preparation_id, None)

    async def _restore_resident(
        self,
        *,
        generation_id: str,
        root,
        deadline_monotonic: float,
    ) -> None:
        result = await self._runtime_session.context_input_io_service.execute(
            operation_name=f"provider-input-vector-restore:{generation_id}",
            operation=lambda: load_provider_input_vector_state(
                archive=self._runtime_session.archive,
                runtime_session_id=self._runtime_session.runtime_session_id,
                root=root,
                deadline_monotonic=deadline_monotonic,
            ),
            deadline_monotonic=deadline_monotonic,
        )
        vector_state, reachable = result
        authority_horizons = _resident_authority_horizons(vector_state.units)
        replay_bindings = _resident_replay_bindings(vector_state.units)
        self._store.install_restored_resident(
            generation_id,
            ProviderInputResidentGeneration(
                units=vector_state.units,
                vector_state=vector_state,
                carrier=hydrate_carrier(vector_state.units),
                authority_horizons=authority_horizons,
                replay_bindings=replay_bindings,
                reachable_artifact_ids=reachable,
            ),
        )

    async def _prepare_pending_continuation(
        self,
        pending,
        *,
        deadline_monotonic: float,
        stable_entries,
    ) -> PreparedProviderInputContinuationMaterialization:
        reference = pending.terminal_projection_reference
        try:
            document = (
                self._runtime_session.transcript_projection_document_registry.resolve(
                    reference
                )
            )
        except ValueError:

            def read_document_text() -> str:
                info = self._runtime_session.archive.get_info(
                    reference.document_artifact_id,
                    session_id=self._runtime_session.runtime_session_id,
                    deadline_monotonic=deadline_monotonic,
                )
                if (
                    info.digest != reference.document_sha256
                    or info.size_bytes != reference.document_byte_count
                    or info.media_type != TERMINAL_PROJECTION_MEDIA_TYPE
                ):
                    raise ValueError(
                        "pending continuation terminal document identity drifted"
                    )
                return self._runtime_session.archive.get_text(
                    reference.document_artifact_id,
                    session_id=self._runtime_session.runtime_session_id,
                    deadline_monotonic=deadline_monotonic,
                )

            text = await self._runtime_session.context_input_io_service.execute(
                operation_name="provider-input-continuation-document-read",
                operation=read_document_text,
                deadline_monotonic=deadline_monotonic,
            )
            document = hydrate_terminal_projection_text(reference, text)
            self._runtime_session.transcript_projection_document_registry.register(
                reference,
                document,
            )

        content_references = required_continuation_content_artifacts(document)

        def read_content_texts() -> dict[str, str]:
            hydrated: dict[str, str] = {}
            for content in content_references:
                info = self._runtime_session.archive.get_info(
                    content.artifact_id,
                    session_id=self._runtime_session.runtime_session_id,
                    deadline_monotonic=deadline_monotonic,
                )
                if (
                    info.digest != content.artifact_sha256
                    or info.size_bytes != content.artifact_bytes
                    or info.media_type != content.media_type
                ):
                    raise ValueError(
                        "pending continuation content artifact identity drifted"
                    )
                hydrated[content.artifact_id] = self._runtime_session.archive.get_text(
                    content.artifact_id,
                    session_id=self._runtime_session.runtime_session_id,
                    deadline_monotonic=deadline_monotonic,
                )
            return hydrated

        content_texts = await self._runtime_session.context_input_io_service.execute(
            operation_name="provider-input-continuation-content-read",
            operation=read_content_texts,
            deadline_monotonic=deadline_monotonic,
        )
        return prepare_provider_input_continuation(
            pending=pending,
            document=document,
            terminal_content_texts=content_texts,
            stable_entries=stable_entries,
        )


class ProviderInputPreparationStale(RuntimeError):
    pass


@dataclass(slots=True)
class _OwnedPreparationAttempt:
    run_id: str
    bundle: PreparedProviderInputStartBundle
    activated: bool = False


def _planning_pending_continuation(*, snapshot, rollover_from):
    source = rollover_from if rollover_from is not None else snapshot
    core = source.core_state
    return core.accepted_but_not_appended_continuation if core is not None else None


def _bind_bundle_to_write_boundary(
    *,
    runtime_session,
    bundle: PreparedProviderInputStartBundle,
) -> PreparedProviderInputStartBundle:
    """Freeze session metadata before deriving same-batch event identities."""

    companions = tuple(
        runtime_session.prepare_event_for_write(event)
        for event in bundle.companion_events
    )
    append = next(
        (
            event
            for event in companions
            if isinstance(event, ProviderInputAppendCommittedEvent)
        ),
        None,
    )
    if append is None:
        raise ProviderInputPreparationStale(
            "provider input start bundle lacks its append event"
        )
    source = bundle.committed_reference
    reference = build_frozen_fact(
        CommittedProviderInputReferenceFact,
        schema_version="committed_provider_input_reference.v2",
        reference_kind=source.reference_kind,
        generation_id=source.generation_id,
        committed_generation_revision=source.committed_generation_revision,
        resulting_generation_core_state_fingerprint=(
            source.resulting_generation_core_state_fingerprint
        ),
        append_committed_event_identity=stable_event_identity(
            append,
            runtime_session_id=runtime_session.runtime_session_id,
        ),
        resulting_prefix_fingerprint=source.resulting_prefix_fingerprint,
        resulting_unit_vector_root=source.resulting_unit_vector_root,
        authority_horizon_set=source.authority_horizon_set,
        replay_binding_set=source.replay_binding_set,
        provider_input_plan_fingerprint=source.provider_input_plan_fingerprint,
        manifest_projection_reference_fingerprint=(
            source.manifest_projection_reference_fingerprint
        ),
        causal_validation_fingerprint=source.causal_validation_fingerprint,
        transcript_frontier_fingerprint=source.transcript_frontier_fingerprint,
    )
    return PreparedProviderInputStartBundle(
        prepared_candidate=bundle.prepared_candidate,
        companion_events=companions,
        committed_reference=reference,
        carrier=bundle.carrier,
        resident=bundle.resident,
        artifacts=bundle.artifacts,
        prepared_plan=bundle.prepared_plan,
    )


def _resident_authority_horizons(units):
    by_owner = {}
    for unit in units:
        for horizon in unit.attribution.authority_horizons:
            current = by_owner.get(horizon.runtime_session_id)
            if current is None or horizon.through_sequence > current.through_sequence:
                by_owner[horizon.runtime_session_id] = horizon
            elif (
                horizon.through_sequence == current.through_sequence
                and horizon != current
            ):
                raise ValueError("restored provider horizon identity conflicts")
    return tuple(by_owner[key] for key in sorted(by_owner))


def _resident_replay_bindings(units):
    by_fingerprint = {}
    for unit in units:
        for binding in unit.attribution.required_replay_bindings:
            existing = by_fingerprint.get(binding.identity_fingerprint)
            if existing is not None and existing != binding:
                raise ValueError("restored provider replay binding conflicts")
            by_fingerprint[binding.identity_fingerprint] = binding
    return tuple(by_fingerprint[key] for key in sorted(by_fingerprint))


__all__ = [
    "ProviderInputGenerationCoordinator",
    "ProviderInputPreparationStale",
]
