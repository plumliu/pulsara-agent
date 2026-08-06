from __future__ import annotations

import asyncio
import hashlib
import json
import tracemalloc
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from time import monotonic

import pytest

from pulsara_agent.event import ContextCompiledEvent, EventContext
from pulsara_agent.memory.artifacts.archive import InMemoryArchiveStore
from pulsara_agent.memory.artifacts.postgres_archive import PostgresArtifactStore
from pulsara_agent.ports.mcp_secret import seal_mcp_json_object
from pulsara_agent.primitives._context_base import (
    ContextEventReferenceFact,
    canonical_json_bytes,
)
from pulsara_agent.primitives.context_input_audit_storage import (
    ContextInputAuditComponentKind,
    ContextInputAuditComponentOwnership,
    ContextInputAuditMaterializationPlanFact,
    ContextInputAuditPageFact,
    MAX_AUDIT_PAGE_CANONICAL_BYTES,
)
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.storage_frozen import build_frozen_storage_fact
from pulsara_agent.runtime.context_input.audit_gc import (
    ContextInputAuditGcEligibility,
    ResolvedContextInputAuditMaintenancePolicy,
    garbage_collect_incomplete_context_input_audits,
)
from pulsara_agent.runtime.context_input.audit_doctor import (
    inspect_context_input_audits,
)
from pulsara_agent.runtime.context_input.audit_materializer import (
    ContextInputAuditMaterializationDisposition,
    OversizedContextInputAuditSourceBasis,
    PreparedContextInputAuditCaptureComponent,
    PreparedContextInputAuditCaptureMaterialization,
    PreparedContextInputAuditComponent,
    PreparedContextInputAuditMaterialization,
    PreparedContextInputAuditSourceCapture,
    materialize_captured_context_input_audit,
    materialize_context_input_audit,
    prepare_context_input_audit_source_basis,
)
from pulsara_agent.runtime.context_input.replay import (
    AuditUnavailable,
    ContextInputReplayError,
    load_context_input_audit,
)
from pulsara_agent.runtime.context_input.audit_storage import (
    ContextInputAuditArtifactConflict,
    ContextInputAuditArtifactIntegrityError,
    ContextInputAuditArtifactRepository,
    expected_audit_artifact_reference,
    validate_context_input_audit_plan_reference,
)
from pulsara_agent.runtime.context_input.commit import (
    CONTEXT_INPUT_AUDIT_EXTRACTOR_CONTRACTS,
    MAX_CONTEXT_COMPILED_EVENT_CANONICAL_BYTES,
    build_context_compiled_event,
    context_input_audit_component_ownership,
)
from pulsara_agent.runtime.context_input.io_service import (
    AuditOfferDisposition,
    ContextInputIoService,
    best_effort_audit_process_usage,
)
from pulsara_agent.runtime.long_horizon.checkpoint_maintenance import (
    InMemoryCheckpointMaintenanceAuthority,
)
from tests.support.model_call import context_compiled_contract_fields
from tests.support.runtime_session import in_memory_runtime_session


def _compiled_event() -> ContextCompiledEvent:
    return build_context_compiled_event(
        **EventContext(
            run_id="run:test",
            turn_id="turn:test",
            reply_id="reply:test",
        ).event_fields(),
        **context_compiled_contract_fields(
            context_id="context:test",
            model_call_index=1,
        ),
        context_id="context:test",
        model_call_index=1,
    )


def _materialization(
    event: ContextCompiledEvent,
    *,
    value: object = ("small",),
) -> PreparedContextInputAuditMaterialization:
    assert event.semantic_commit is not None
    assert event.audit_expectation is not None
    basis = prepare_context_input_audit_source_basis(
        semantic_commit=event.semantic_commit,
        expectation=event.audit_expectation,
        components=(
            PreparedContextInputAuditComponent(
                ContextInputAuditComponentKind.COMPILED_SECTIONS,
                value,
            ),
        ),
        known_canonical_bytes=len(canonical_json_bytes(value)),
    )
    return PreparedContextInputAuditMaterialization(
        source_basis=basis,
        model_start_reference=ContextEventReferenceFact(
            runtime_session_id="runtime:test",
            event_id="model-start:test",
            sequence=3,
            event_type="MODEL_CALL_START",
            payload_fingerprint="sha256:" + "1" * 64,
        ),
        provider_input_append_reference=ContextEventReferenceFact(
            runtime_session_id="runtime:test",
            event_id="provider-append:test",
            sequence=2,
            event_type="PROVIDER_INPUT_APPEND_COMMITTED",
            payload_fingerprint="sha256:" + "2" * 64,
        ),
    )


def _capture_materialization(
    event: ContextCompiledEvent,
    component: PreparedContextInputAuditCaptureComponent,
) -> PreparedContextInputAuditCaptureMaterialization:
    assert event.semantic_commit is not None
    assert event.audit_expectation is not None
    return PreparedContextInputAuditCaptureMaterialization(
        source_capture=PreparedContextInputAuditSourceCapture(
            semantic_commit=event.semantic_commit,
            expectation=event.audit_expectation,
            components=(component,),
        ),
        model_start_reference=ContextEventReferenceFact(
            runtime_session_id="runtime:test",
            event_id="model-start:test",
            sequence=3,
            event_type="MODEL_CALL_START",
            payload_fingerprint="sha256:" + "1" * 64,
        ),
        provider_input_append_reference=ContextEventReferenceFact(
            runtime_session_id="runtime:test",
            event_id="provider-append:test",
            sequence=2,
            event_type="PROVIDER_INPUT_APPEND_COMMITTED",
            payload_fingerprint="sha256:" + "2" * 64,
        ),
    )


def test_compact_context_commit_and_expectation_have_physical_bounds() -> None:
    event = _compiled_event()
    assert event.semantic_commit is not None
    assert event.audit_expectation is not None
    assert len(canonical_json_bytes(event.semantic_commit)) <= 64 * 1024
    assert len(canonical_json_bytes(event.audit_expectation)) <= 8 * 1024
    assert (
        len(canonical_json_bytes(event)) <= MAX_CONTEXT_COMPILED_EVENT_CANONICAL_BYTES
    )
    payload = event.model_dump(mode="json")
    for removed in (
        "sections",
        "tool_specs",
        "diagnostics",
        "prepared_provider_input",
        "manifest_projection_reference",
        "manifest_write_outcome",
        "input_audit",
    ):
        assert removed not in payload


def test_provider_install_exactly_joins_canonical_provider_plan() -> None:
    event = _compiled_event()
    install = event.provider_input_preparation_install
    assert install is not None
    payload = {
        field_name: getattr(install, field_name)
        for field_name in type(install).model_fields
        if field_name not in {"schema_version", "install_fingerprint"}
    }
    payload["canonical_provider_input_plan_fingerprint"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="installation ownership/guard mismatch"):
        build_frozen_fact(
            type(install),
            schema_version="provider_input_preparation_install.v2",
            **payload,
        )


def test_audit_component_registry_is_closed_and_exhaustive() -> None:
    contracts = {
        item.component_kind.value: item
        for item in CONTEXT_INPUT_AUDIT_EXTRACTOR_CONTRACTS
    }
    assert set(contracts) == {item.value for item in ContextInputAuditComponentKind}
    assert len(contracts) == len(ContextInputAuditComponentKind)
    for kind in ContextInputAuditComponentKind:
        contract = contracts[kind.value]
        assert contract.ownership is context_input_audit_component_ownership(kind)
        assert contract.extractor_id.endswith(kind.value)
        assert contract.extractor_version == "1"
        assert contract.extractor_fingerprint.startswith("sha256:")


def test_audit_materialization_is_plan_first_and_root_last() -> None:
    event = _compiled_event()
    archive = InMemoryArchiveStore()
    materialization = _materialization(event, value=("x" * 300_000,))
    result = materialize_context_input_audit(
        materialization=materialization,
        repository=ContextInputAuditArtifactRepository(archive),
        deadline_monotonic=monotonic() + 5,
    )
    assert (
        result.disposition is ContextInputAuditMaterializationDisposition.MATERIALIZED
    )
    expectation = materialization.source_basis.expectation
    keys = tuple(archive.blobs)
    assert keys[0] == expectation.expected_plan_artifact_id
    assert keys[-1] == expectation.expected_root_artifact_id
    assert result.page_count > 0
    assert all(
        len(blob.text_content.encode("utf-8")) <= 256 * 1024
        for artifact_id, blob in archive.blobs.items()
        if artifact_id.startswith("context-input-audit-page:")
        and blob.text_content is not None
    )


def test_audit_page_same_identity_requires_identical_canonical_bytes() -> None:
    event = _compiled_event()
    archive = InMemoryArchiveStore()
    repository = ContextInputAuditArtifactRepository(archive)
    materialization = _materialization(event, value=("x" * 300_000,))
    result = materialize_context_input_audit(
        materialization=materialization,
        repository=repository,
        deadline_monotonic=monotonic() + 5,
    )
    assert (
        result.disposition is ContextInputAuditMaterializationDisposition.MATERIALIZED
    )
    assert event.audit_expectation is not None
    plan = repository.get_expected_plan(
        artifact_id=event.audit_expectation.expected_plan_artifact_id,
        source_runtime_session_id="runtime:test",
        source_run_id="run:test",
        deadline_monotonic=monotonic() + 5,
    ).fact
    page_reference = plan.page_references[0]
    page = repository.get_exact(
        reference=page_reference,
        source_runtime_session_id="runtime:test",
        source_run_id="run:test",
        fact_type=ContextInputAuditPageFact,
        deadline_monotonic=monotonic() + 5,
    )
    # An identical retry is a compatible success.
    assert (
        repository.put_exact(
            artifact_id=page_reference.artifact_id,
            fact=page,
            deadline_monotonic=monotonic() + 5,
        )
        == page_reference
    )

    changed_fragment = page.canonical_json_fragment + " "
    changed_payload = page.model_dump(
        mode="python", exclude={"page_storage_fingerprint"}
    )
    changed_payload.update(
        canonical_json_fragment=changed_fragment,
        canonical_payload_sha256=(
            "sha256:" + hashlib.sha256(changed_fragment.encode("utf-8")).hexdigest()
        ),
        canonical_payload_bytes=len(changed_fragment.encode("utf-8")),
    )
    changed = build_frozen_storage_fact(
        ContextInputAuditPageFact,
        **changed_payload,
    )
    with pytest.raises(ContextInputAuditArtifactConflict):
        repository.put_exact(
            artifact_id=page_reference.artifact_id,
            fact=changed,
            deadline_monotonic=monotonic() + 5,
        )


def test_root_plan_reference_must_use_deterministic_expected_artifact_id() -> None:
    event = _compiled_event()
    assert event.audit_expectation is not None
    archive = InMemoryArchiveStore()
    repository = ContextInputAuditArtifactRepository(archive)
    materialization = _materialization(event)
    result = materialize_context_input_audit(
        materialization=materialization,
        repository=repository,
        deadline_monotonic=monotonic() + 5,
    )
    assert result.root_reference is not None
    root, _ = repository.get_expected_root(
        artifact_id=event.audit_expectation.expected_root_artifact_id,
        source_runtime_session_id="runtime:test",
        source_run_id="run:test",
        deadline_monotonic=monotonic() + 5,
    )
    plan = repository.get_exact(
        reference=root.plan_artifact_reference,
        source_runtime_session_id="runtime:test",
        source_run_id="run:test",
        fact_type=ContextInputAuditMaterializationPlanFact,
        deadline_monotonic=monotonic() + 5,
    )
    forged_reference = expected_audit_artifact_reference(
        artifact_id="context-input-audit-plan:forged",
        fact=plan,
    )
    forged_root = root.model_copy(update={"plan_artifact_reference": forged_reference})
    with pytest.raises(ContextInputAuditArtifactIntegrityError):
        validate_context_input_audit_plan_reference(
            root=forged_root,
            plan=plan,
            expected_plan_artifact_id=(
                event.audit_expectation.expected_plan_artifact_id
            ),
        )


def test_existing_authority_references_are_inline_and_never_page_owned() -> None:
    event = _compiled_event()
    assert event.semantic_commit is not None
    assert event.audit_expectation is not None
    source = prepare_context_input_audit_source_basis(
        semantic_commit=event.semantic_commit,
        expectation=event.audit_expectation,
        components=(
            PreparedContextInputAuditComponent(
                ContextInputAuditComponentKind.SNAPSHOT,
                ("authority-reference",),
                ContextInputAuditComponentOwnership.EXISTING_AUTHORITY_REFERENCE,
            ),
            PreparedContextInputAuditComponent(
                ContextInputAuditComponentKind.COMPILED_DIAGNOSTICS,
                ("x" * 300_000,),
                ContextInputAuditComponentOwnership.PAGE_OWNED_DETAIL,
            ),
        ),
        known_canonical_bytes=300_000,
    )
    assert not isinstance(source, OversizedContextInputAuditSourceBasis)
    materialization = PreparedContextInputAuditMaterialization(
        source_basis=source,
        model_start_reference=ContextEventReferenceFact(
            runtime_session_id="runtime:test",
            event_id="model-start:test",
            sequence=3,
            event_type="MODEL_CALL_START",
            payload_fingerprint="sha256:" + "1" * 64,
        ),
        provider_input_append_reference=ContextEventReferenceFact(
            runtime_session_id="runtime:test",
            event_id="provider-append:test",
            sequence=2,
            event_type="PROVIDER_INPUT_APPEND_COMMITTED",
            payload_fingerprint="sha256:" + "2" * 64,
        ),
    )
    archive = InMemoryArchiveStore()
    repository = ContextInputAuditArtifactRepository(archive)
    result = materialize_context_input_audit(
        materialization=materialization,
        repository=repository,
        deadline_monotonic=monotonic() + 5,
    )
    assert (
        result.disposition is ContextInputAuditMaterializationDisposition.MATERIALIZED
    )
    plan = repository.get_expected_plan(
        artifact_id=event.audit_expectation.expected_plan_artifact_id,
        source_runtime_session_id="runtime:test",
        source_run_id="run:test",
        deadline_monotonic=monotonic() + 5,
    ).fact
    authority = plan.components[0]
    detail = plan.components[1]
    assert authority.component_ownership is (
        ContextInputAuditComponentOwnership.EXISTING_AUTHORITY_REFERENCE
    )
    assert authority.storage_kind == "inline"
    assert not authority.page_ordinals
    assert detail.component_ownership is (
        ContextInputAuditComponentOwnership.PAGE_OWNED_DETAIL
    )
    assert detail.storage_kind == "paged"


def test_oversized_existing_authority_reference_fails_before_plan_write() -> None:
    event = _compiled_event()
    assert event.semantic_commit is not None
    assert event.audit_expectation is not None
    source = prepare_context_input_audit_source_basis(
        semantic_commit=event.semantic_commit,
        expectation=event.audit_expectation,
        components=(
            PreparedContextInputAuditComponent(
                ContextInputAuditComponentKind.SNAPSHOT,
                ("x" * (8 * 1024 + 1),),
                ContextInputAuditComponentOwnership.EXISTING_AUTHORITY_REFERENCE,
            ),
        ),
        known_canonical_bytes=8 * 1024 + 1,
    )
    assert not isinstance(source, OversizedContextInputAuditSourceBasis)
    materialization = PreparedContextInputAuditMaterialization(
        source_basis=source,
        model_start_reference=ContextEventReferenceFact(
            runtime_session_id="runtime:test",
            event_id="model-start:test",
            sequence=3,
            event_type="MODEL_CALL_START",
            payload_fingerprint="sha256:" + "1" * 64,
        ),
        provider_input_append_reference=ContextEventReferenceFact(
            runtime_session_id="runtime:test",
            event_id="provider-append:test",
            sequence=2,
            event_type="PROVIDER_INPUT_APPEND_COMMITTED",
            payload_fingerprint="sha256:" + "2" * 64,
        ),
    )
    archive = InMemoryArchiveStore()
    result = materialize_context_input_audit(
        materialization=materialization,
        repository=ContextInputAuditArtifactRepository(archive),
        deadline_monotonic=monotonic() + 5,
    )
    assert result.disposition is (
        ContextInputAuditMaterializationDisposition.SKIPPED_PHYSICAL_BOUND
    )
    assert not archive.blobs


def test_sanitized_incident_fixture_has_no_second_context_window() -> None:
    fixture = json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "context_input_manifest_reference_paging_incident.json"
        ).read_text(encoding="utf-8")
    )
    event = build_context_compiled_event(
        **EventContext(
            run_id="run:test",
            turn_id="turn:test",
            reply_id="reply:test",
        ).event_fields(),
        **context_compiled_contract_fields(
            context_id="context:test",
            model_call_index=1,
            estimated_tokens=fixture["provider_estimated_tokens"],
        ),
        context_id="context:test",
        model_call_index=1,
    )
    assert event.semantic_commit is not None
    assert event.semantic_commit.final_payload_estimated_tokens == 56_000
    assert (
        len(canonical_json_bytes(event)) <= MAX_CONTEXT_COMPILED_EVENT_CANONICAL_BYTES
    )
    materialization = _materialization(
        event,
        value=("x" * fixture["legacy_flat_manifest_canonical_bytes"],),
    )
    archive = InMemoryArchiveStore()
    result = materialize_context_input_audit(
        materialization=materialization,
        repository=ContextInputAuditArtifactRepository(archive),
        deadline_monotonic=monotonic() + 10,
    )
    assert (
        result.disposition is ContextInputAuditMaterializationDisposition.MATERIALIZED
    )
    assert result.total_page_canonical_bytes > 1_000_000


def test_known_oversized_source_drops_components_and_is_typed_skip() -> None:
    event = _compiled_event()
    assert event.semantic_commit is not None
    assert event.audit_expectation is not None
    source = prepare_context_input_audit_source_basis(
        semantic_commit=event.semantic_commit,
        expectation=event.audit_expectation,
        components=(
            PreparedContextInputAuditComponent(
                ContextInputAuditComponentKind.COMPILED_SECTIONS,
                ("must-not-be-retained",),
            ),
        ),
        known_canonical_bytes=16 * 1024 * 1024 + 1,
    )
    assert isinstance(source, OversizedContextInputAuditSourceBasis)
    service = ContextInputIoService()
    offered = service.offer_best_effort_nowait(
        operation_name="oversized-audit",
        operation=lambda: pytest.fail("oversized audit worker must not start"),
        deadline_monotonic=monotonic() + 1,
        resident_charge_bytes=32 * 1024 * 1024 + 1,
    )
    assert offered.disposition is AuditOfferDisposition.SKIPPED_PHYSICAL_BOUND
    assert service.pending_count() == 0
    service.close_if_idle()


def test_streaming_audit_capture_stays_within_resident_permit() -> None:
    async def scenario() -> None:
        event = _compiled_event()
        assert event.semantic_commit is not None
        assert event.audit_expectation is not None
        observed: list[str] = []
        source = ("x" * (12 * 1024 * 1024),)
        capture = PreparedContextInputAuditSourceCapture(
            semantic_commit=event.semantic_commit,
            expectation=event.audit_expectation,
            components=(
                PreparedContextInputAuditCaptureComponent(
                    kind=ContextInputAuditComponentKind.TOOL_RESULT_BUDGET_REPORT,
                    source={"payload": source[0]},
                    ownership=(ContextInputAuditComponentOwnership.PAGE_OWNED_DETAIL),
                ),
            ),
        )
        bound = PreparedContextInputAuditCaptureMaterialization(
            source_capture=capture,
            model_start_reference=ContextEventReferenceFact(
                runtime_session_id="runtime:test",
                event_id="model-start:test",
                sequence=3,
                event_type="MODEL_CALL_START",
                payload_fingerprint="sha256:" + "1" * 64,
            ),
            provider_input_append_reference=ContextEventReferenceFact(
                runtime_session_id="runtime:test",
                event_id="provider-append:test",
                sequence=2,
                event_type="PROVIDER_INPUT_APPEND_COMMITTED",
                payload_fingerprint="sha256:" + "2" * 64,
            ),
        )
        service = ContextInputIoService()
        archive = InMemoryArchiveStore()
        deadline = monotonic() + 10
        tracemalloc.start()
        tracemalloc.reset_peak()
        offered = service.offer_best_effort_nowait(
            operation_name="lazy-audit-capture",
            operation=lambda: materialize_captured_context_input_audit(
                capture_materialization=bound,
                repository=ContextInputAuditArtifactRepository(archive),
                deadline_monotonic=deadline,
            ),
            deadline_monotonic=deadline,
            resident_charge_bytes=32 * 1024 * 1024,
            completion_observer=lambda code, _pages, _bytes: observed.append(code),
        )
        assert offered.disposition is AuditOfferDisposition.ACCEPTED
        await service.drain_pending(deadline_monotonic=deadline)
        await asyncio.sleep(0)
        _, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        service.close_if_idle()
        assert observed == [
            str(ContextInputAuditMaterializationDisposition.MATERIALIZED)
        ]
        assert peak_bytes <= 32 * 1024 * 1024
        assert len(archive.blobs) > 2
        assert best_effort_audit_process_usage() == (0, 0)

    asyncio.run(scenario())


def test_streaming_audit_capture_rejects_oversize_without_resident_overrun() -> None:
    event = _compiled_event()
    source = ("x" * (20 * 1024 * 1024),)
    capture = _capture_materialization(
        event,
        PreparedContextInputAuditCaptureComponent(
            kind=ContextInputAuditComponentKind.TOOL_RESULT_BUDGET_REPORT,
            source={"payload": source[0]},
            ownership=ContextInputAuditComponentOwnership.PAGE_OWNED_DETAIL,
        ),
    )
    tracemalloc.start()
    tracemalloc.reset_peak()
    result = materialize_captured_context_input_audit(
        capture_materialization=capture,
        repository=ContextInputAuditArtifactRepository(InMemoryArchiveStore()),
        deadline_monotonic=monotonic() + 10,
    )
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert result.disposition is (
        ContextInputAuditMaterializationDisposition.SKIPPED_PHYSICAL_BOUND
    )
    assert peak_bytes <= 32 * 1024 * 1024


def test_streaming_audit_pages_bound_final_escaped_storage_carrier() -> None:
    event = _compiled_event()
    capture = _capture_materialization(
        event,
        PreparedContextInputAuditCaptureComponent(
            kind=ContextInputAuditComponentKind.TOOL_RESULT_BUDGET_REPORT,
            source={"payload": "\\" * 160_000},
            ownership=ContextInputAuditComponentOwnership.PAGE_OWNED_DETAIL,
        ),
    )
    archive = InMemoryArchiveStore()
    result = materialize_captured_context_input_audit(
        capture_materialization=capture,
        repository=ContextInputAuditArtifactRepository(archive),
        deadline_monotonic=monotonic() + 10,
    )

    assert result.disposition is (
        ContextInputAuditMaterializationDisposition.MATERIALIZED
    )
    pages = tuple(
        blob
        for artifact_id, blob in archive.blobs.items()
        if artifact_id.startswith("context-input-audit-page:")
    )
    assert len(pages) >= 2
    assert all(blob.size_bytes <= MAX_AUDIT_PAGE_CANONICAL_BYTES for blob in pages)


def test_streaming_audit_capture_rejects_mapping_fanout_before_sorting() -> None:
    event = _compiled_event()
    source = {f"key:{ordinal:05d}": ordinal for ordinal in range(65_537)}
    capture = _capture_materialization(
        event,
        PreparedContextInputAuditCaptureComponent(
            kind=ContextInputAuditComponentKind.TOOL_RESULT_BUDGET_REPORT,
            source=source,
            ownership=ContextInputAuditComponentOwnership.PAGE_OWNED_DETAIL,
        ),
    )
    tracemalloc.start()
    tracemalloc.reset_peak()
    result = materialize_captured_context_input_audit(
        capture_materialization=capture,
        repository=ContextInputAuditArtifactRepository(InMemoryArchiveStore()),
        deadline_monotonic=monotonic() + 10,
    )
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert result.disposition is (
        ContextInputAuditMaterializationDisposition.SKIPPED_PHYSICAL_BOUND
    )
    assert peak_bytes < 1024 * 1024


def test_audit_capture_exception_is_typed_process_local_skip() -> None:
    event = _compiled_event()
    assert event.semantic_commit is not None
    assert event.audit_expectation is not None

    result = materialize_captured_context_input_audit(
        capture_materialization=PreparedContextInputAuditCaptureMaterialization(
            source_capture=PreparedContextInputAuditSourceCapture(
                semantic_commit=event.semantic_commit,
                expectation=event.audit_expectation,
                components=(
                    PreparedContextInputAuditCaptureComponent(
                        kind=(
                            ContextInputAuditComponentKind.COMPILED_LIFECYCLE_DECISIONS
                        ),
                        source=(object(),),
                        ownership=(
                            ContextInputAuditComponentOwnership.PAGE_OWNED_DETAIL
                        ),
                    ),
                ),
            ),
            model_start_reference=ContextEventReferenceFact(
                runtime_session_id="runtime:test",
                event_id="model-start:test",
                sequence=3,
                event_type="MODEL_CALL_START",
                payload_fingerprint="sha256:" + "1" * 64,
            ),
            provider_input_append_reference=ContextEventReferenceFact(
                runtime_session_id="runtime:test",
                event_id="provider-append:test",
                sequence=2,
                event_type="PROVIDER_INPUT_APPEND_COMMITTED",
                payload_fingerprint="sha256:" + "2" * 64,
            ),
        ),
        repository=ContextInputAuditArtifactRepository(InMemoryArchiveStore()),
        deadline_monotonic=monotonic() + 1,
    )
    assert result.disposition is (
        ContextInputAuditMaterializationDisposition.SKIPPED_SOURCE_CAPTURE
    )
    assert result.diagnostic_code == "audit_source_capture_skipped"


def test_streaming_audit_capture_rejects_nested_sealed_secret() -> None:
    event = _compiled_event()
    result = materialize_captured_context_input_audit(
        capture_materialization=_capture_materialization(
            event,
            PreparedContextInputAuditCaptureComponent(
                kind=ContextInputAuditComponentKind.TOOL_RESULT_BUDGET_REPORT,
                source={"private": seal_mcp_json_object({"token": "secret"})},
                ownership=(ContextInputAuditComponentOwnership.PAGE_OWNED_DETAIL),
            ),
        ),
        repository=ContextInputAuditArtifactRepository(InMemoryArchiveStore()),
        deadline_monotonic=monotonic() + 1,
    )

    assert result.disposition is (
        ContextInputAuditMaterializationDisposition.SKIPPED_SOURCE_CAPTURE
    )


def test_audit_source_rejects_sealed_mcp_secret_carriers() -> None:
    with pytest.raises(TypeError, match="MCP continuation secret"):
        PreparedContextInputAuditComponent(
            ContextInputAuditComponentKind.COMPILED_DIAGNOSTICS,
            seal_mcp_json_object({"token": "secret"}),
        )


def test_orphan_audit_root_cannot_override_missing_model_start(tmp_path) -> None:
    event = _compiled_event()
    runtime = in_memory_runtime_session(
        tmp_path,
        runtime_session_id="runtime:test",
    )
    runtime.event_log.append(event)
    materialization = _materialization(event, value=("exact",))
    result = materialize_context_input_audit(
        materialization=materialization,
        repository=ContextInputAuditArtifactRepository(runtime.archive),
        deadline_monotonic=monotonic() + 5,
    )
    assert (
        result.disposition is ContextInputAuditMaterializationDisposition.MATERIALIZED
    )
    orphan = load_context_input_audit(
        event=event,
        event_log=runtime.event_log,
        provider_input_store=None,
        artifact_store=runtime.archive,
    )
    assert isinstance(orphan, AuditUnavailable)
    assert orphan.reason == "model_start_not_committed"

    expectation = materialization.source_basis.expectation
    root = runtime.archive.blobs.pop(expectation.expected_root_artifact_id)
    missing = load_context_input_audit(
        event=event,
        event_log=runtime.event_log,
        provider_input_store=None,
        artifact_store=runtime.archive,
    )
    assert isinstance(missing, AuditUnavailable)
    assert missing.reason == "model_start_not_committed"
    runtime.archive.blobs[expectation.expected_root_artifact_id] = root


def test_audit_doctor_rejects_orphan_root_in_explicit_exact_mode(tmp_path) -> None:
    event = _compiled_event()
    runtime = in_memory_runtime_session(
        tmp_path,
        runtime_session_id="runtime:test",
    )
    runtime.event_log.append(event)
    materialization = _materialization(event, value=("doctor",))
    result = materialize_context_input_audit(
        materialization=materialization,
        repository=ContextInputAuditArtifactRepository(runtime.archive),
        deadline_monotonic=monotonic() + 5,
    )
    assert (
        result.disposition is ContextInputAuditMaterializationDisposition.MATERIALIZED
    )
    report = inspect_context_input_audits(
        runtime_session_id="runtime:test",
        event_log=runtime.event_log,
        artifact_store=runtime.archive,
    )
    assert report.unavailable_count == 1
    assert report.entries[0].status == "audit_unavailable"
    assert report.entries[0].reason_code == "model_start_not_committed"

    assert event.audit_expectation is not None
    with pytest.raises(ContextInputReplayError):
        inspect_context_input_audits(
            runtime_session_id="runtime:test",
            event_log=runtime.event_log,
            artifact_store=runtime.archive,
            require_exact_audit=True,
        )


@dataclass(slots=True)
class _FailingArchive:
    inner: InMemoryArchiveStore
    fail_artifact_id: str

    def put_text_if_absent_or_confirm_identical(self, blob_id, *args, **kwargs):
        if blob_id == self.fail_artifact_id:
            raise RuntimeError("synthetic optional audit write failure")
        return self.inner.put_text_if_absent_or_confirm_identical(
            blob_id, *args, **kwargs
        )

    def get_info(self, *args, **kwargs):
        return self.inner.get_info(*args, **kwargs)

    def get_text(self, *args, **kwargs):
        return self.inner.get_text(*args, **kwargs)


class _DeadlineRecordingArchive(InMemoryArchiveStore):
    def __init__(self) -> None:
        super().__init__()
        self.delete_deadlines: list[float | None] = []

    def delete_if_identity(self, blob_id, **kwargs):
        self.delete_deadlines.append(kwargs.get("deadline_monotonic"))
        return super().delete_if_identity(blob_id, **kwargs)


class _DeadlineRecordingPostgresCursor:
    def __init__(self) -> None:
        self.statements: list[tuple[str, object]] = []
        self.rowcount = 0
        self.connection: _DeadlineRecordingPostgresConnection | None = None

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def execute(self, query, parameters=None) -> None:
        statement = " ".join(str(query).split())
        self.statements.append((statement, parameters))
        if statement.startswith("delete from artifacts"):
            self.rowcount = 1

    def fetchone(self):
        return {
            "id": "artifact:test",
            "session_id": "runtime:test",
            "media_type": "application/json",
            "digest": "sha256:digest",
            "metadata": {"semantic_metadata_fingerprint": "sha256:metadata"},
        }


class _DeadlineRecordingPostgresConnection:
    def __init__(self, cursor: _DeadlineRecordingPostgresCursor) -> None:
        self._cursor = cursor
        self.closed = False
        self.cancelled = Event()
        self.commit_count = 0
        cursor.connection = self

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def cursor(self) -> _DeadlineRecordingPostgresCursor:
        return self._cursor

    def commit(self) -> None:
        self.commit_count += 1

    def cancel_safe(self, *, timeout: float) -> None:
        assert timeout > 0
        self.cancelled.set()

    def close(self) -> None:
        self.closed = True
        self.cancelled.set()


class _DeadlineRecordingPostgresProvider:
    def __init__(self) -> None:
        self.cursor = _DeadlineRecordingPostgresCursor()
        self.connection_owner = _DeadlineRecordingPostgresConnection(self.cursor)
        self.deadlines: list[float] = []

    def connection(self, *, deadline_monotonic, **_kwargs):
        self.deadlines.append(deadline_monotonic)
        return self.connection_owner


def test_postgres_audit_delete_installs_deadline_before_advisory_lock() -> None:
    provider = _DeadlineRecordingPostgresProvider()
    deadline = monotonic() + 5

    deleted = PostgresArtifactStore(provider).delete_if_identity(  # type: ignore[arg-type]
        "artifact:test",
        session_id="runtime:test",
        digest="sha256:digest",
        media_type="application/json",
        semantic_metadata_fingerprint="sha256:metadata",
        deadline_monotonic=deadline,
    )

    assert deleted is True
    assert provider.deadlines == [deadline]
    statements = tuple(item[0] for item in provider.cursor.statements)
    assert statements[0].startswith("select set_config('statement_timeout'")
    assert statements[1].startswith("select pg_advisory_xact_lock")
    assert statements[2].startswith("select set_config('statement_timeout'")
    assert statements[3].startswith("select id, session_id, media_type, digest")
    assert statements[4].startswith("select set_config('statement_timeout'")
    assert statements[5] == "delete from artifacts where id = %s"
    assert statements[6].startswith("select set_config('statement_timeout'")
    assert provider.connection_owner.commit_count == 1


def test_postgres_audit_delete_deadline_cancels_the_whole_transaction() -> None:
    class SlowCursor(_DeadlineRecordingPostgresCursor):
        def execute(self, query, parameters=None) -> None:
            statement = " ".join(str(query).split())
            if not statement.startswith("select set_config('statement_timeout'"):
                assert self.connection is not None
                if self.connection.cancelled.wait(1.0):
                    raise RuntimeError("synthetic physical cancellation")
            super().execute(query, parameters)

    provider = _DeadlineRecordingPostgresProvider()
    provider.cursor = SlowCursor()
    provider.connection_owner = _DeadlineRecordingPostgresConnection(provider.cursor)
    started = monotonic()

    with pytest.raises(TimeoutError, match="absolute deadline"):
        PostgresArtifactStore(provider).delete_if_identity(  # type: ignore[arg-type]
            "artifact:test",
            session_id="runtime:test",
            digest="sha256:digest",
            media_type="application/json",
            semantic_metadata_fingerprint="sha256:metadata",
            deadline_monotonic=started + 0.1,
        )

    assert monotonic() - started < 0.25
    assert provider.connection_owner.cancelled.is_set()
    assert provider.connection_owner.commit_count == 0


def test_postgres_audit_delete_normalizes_preexpired_operation_control() -> None:
    provider = _DeadlineRecordingPostgresProvider()

    with pytest.raises(TimeoutError, match="absolute deadline"):
        PostgresArtifactStore(provider).delete_if_identity(  # type: ignore[arg-type]
            "artifact:test",
            session_id="runtime:test",
            digest="sha256:digest",
            media_type="application/json",
            semantic_metadata_fingerprint="sha256:metadata",
            deadline_monotonic=monotonic() - 1,
        )

    assert provider.deadlines == []
    assert provider.connection_owner.commit_count == 0


def test_plan_failure_writes_no_pages_and_root_failure_leaves_incomplete_plan() -> None:
    event = _compiled_event()
    materialization = _materialization(event, value=("x" * 300_000,))
    expectation = materialization.source_basis.expectation

    plan_inner = InMemoryArchiveStore()
    plan_result = materialize_context_input_audit(
        materialization=materialization,
        repository=ContextInputAuditArtifactRepository(
            _FailingArchive(plan_inner, expectation.expected_plan_artifact_id)
        ),
        deadline_monotonic=monotonic() + 5,
    )
    assert (
        plan_result.disposition
        is ContextInputAuditMaterializationDisposition.FAILED_OPERATIONALLY
    )
    assert not plan_inner.blobs

    root_inner = InMemoryArchiveStore()
    root_result = materialize_context_input_audit(
        materialization=materialization,
        repository=ContextInputAuditArtifactRepository(
            _FailingArchive(root_inner, expectation.expected_root_artifact_id)
        ),
        deadline_monotonic=monotonic() + 5,
    )
    assert (
        root_result.disposition
        is ContextInputAuditMaterializationDisposition.FAILED_OPERATIONALLY
    )
    assert expectation.expected_plan_artifact_id in root_inner.blobs
    assert expectation.expected_root_artifact_id not in root_inner.blobs
    assert any(
        item.startswith("context-input-audit-page:") for item in root_inner.blobs
    )


@pytest.mark.parametrize("page_selector", ["first", "middle", "last"])
def test_any_page_failure_leaves_plan_without_completion_root(page_selector) -> None:
    event = _compiled_event()
    materialization = _materialization(event, value=("x" * 900_000,))
    expectation = materialization.source_basis.expectation
    baseline = InMemoryArchiveStore()
    baseline_result = materialize_context_input_audit(
        materialization=materialization,
        repository=ContextInputAuditArtifactRepository(baseline),
        deadline_monotonic=monotonic() + 10,
    )
    assert baseline_result.disposition is (
        ContextInputAuditMaterializationDisposition.MATERIALIZED
    )
    page_ids = tuple(
        item for item in baseline.blobs if item.startswith("context-input-audit-page:")
    )
    assert len(page_ids) >= 3
    selected = {
        "first": page_ids[0],
        "middle": page_ids[len(page_ids) // 2],
        "last": page_ids[-1],
    }[page_selector]
    archive = InMemoryArchiveStore()
    result = materialize_context_input_audit(
        materialization=materialization,
        repository=ContextInputAuditArtifactRepository(
            _FailingArchive(archive, selected)
        ),
        deadline_monotonic=monotonic() + 10,
    )
    assert result.disposition is (
        ContextInputAuditMaterializationDisposition.FAILED_OPERATIONALLY
    )
    assert expectation.expected_plan_artifact_id in archive.blobs
    assert expectation.expected_root_artifact_id not in archive.blobs


def test_best_effort_audit_lane_is_bounded_and_does_not_block_critical_io() -> None:
    async def scenario() -> None:
        entered = Event()
        release = Event()
        services = [ContextInputIoService() for _ in range(9)]

        def blocked() -> str:
            entered.set()
            release.wait(5)
            return "done"

        offers = [
            service.offer_best_effort_nowait(
                operation_name="audit-test",
                operation=blocked,
                deadline_monotonic=monotonic() + 5,
                resident_charge_bytes=1024 * 1024,
            )
            for service in services
        ]
        assert [item.disposition for item in offers[:8]] == [
            AuditOfferDisposition.ACCEPTED
        ] * 8
        assert offers[8].disposition is AuditOfferDisposition.SKIPPED_PROCESS_CAPACITY
        assert best_effort_audit_process_usage() == (8, 8 * 1024 * 1024)
        assert (
            await services[0].execute(
                operation_name="required-context-read",
                operation=lambda: "required",
                deadline_monotonic=monotonic() + 2,
            )
            == "required"
        )
        release.set()
        for service in services:
            await service.drain_pending(deadline_monotonic=monotonic() + 5)
            service.close_if_idle()
        assert best_effort_audit_process_usage() == (0, 0)

    asyncio.run(scenario())


def test_best_effort_audit_resident_limit_and_observer_failure_release_permits() -> (
    None
):
    async def scenario() -> None:
        release = Event()
        services = [ContextInputIoService() for _ in range(3)]

        def blocked() -> str:
            release.wait(5)
            return "done"

        offers = tuple(
            service.offer_best_effort_nowait(
                operation_name="resident-bound-audit",
                operation=blocked,
                deadline_monotonic=monotonic() + 5,
                resident_charge_bytes=32 * 1024 * 1024,
                completion_observer=lambda *_: (_ for _ in ()).throw(
                    RuntimeError("observer failure")
                ),
            )
            for service in services
        )
        assert tuple(item.disposition for item in offers) == (
            AuditOfferDisposition.ACCEPTED,
            AuditOfferDisposition.ACCEPTED,
            AuditOfferDisposition.SKIPPED_PROCESS_RESIDENT_BOUND,
        )
        release.set()
        for service in services:
            await service.drain_pending(deadline_monotonic=monotonic() + 5)
            service.close_if_idle()
        assert best_effort_audit_process_usage() == (0, 0)

    asyncio.run(scenario())


def test_best_effort_audit_offer_after_close_is_typed_skip() -> None:
    service = ContextInputIoService()
    service.close_if_idle()
    offered = service.offer_best_effort_nowait(
        operation_name="closed-audit",
        operation=lambda: "unused",
        deadline_monotonic=monotonic() + 1,
        resident_charge_bytes=1,
    )
    assert offered.disposition is AuditOfferDisposition.SKIPPED_SERVICE_CLOSED


def test_incomplete_audit_gc_discovers_plan_pages_and_never_deletes_components(
    tmp_path,
) -> None:
    event = _compiled_event()
    runtime = in_memory_runtime_session(
        tmp_path,
        runtime_session_id="runtime:test",
    )
    runtime.event_log.append(event)
    materialization = _materialization(event, value=("x" * 300_000,))
    expectation = materialization.source_basis.expectation
    archive = _DeadlineRecordingArchive()
    runtime.archive = archive
    result = materialize_context_input_audit(
        materialization=materialization,
        repository=ContextInputAuditArtifactRepository(
            _FailingArchive(archive, expectation.expected_root_artifact_id)
        ),
        deadline_monotonic=monotonic() + 5,
    )
    assert (
        result.disposition
        is ContextInputAuditMaterializationDisposition.FAILED_OPERATIONALLY
    )
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    for blob in archive.blobs.values():
        blob.created_at = old
    archive.put_text(
        "canonical-component:must-survive",
        "canonical",
        session_id="runtime:test",
        run_id="run:test",
    )
    authority = InMemoryCheckpointMaintenanceAuthority(is_quiescent=lambda _: True)
    eligibility = ContextInputAuditGcEligibility(
        runtime_session_id="runtime:test",
        session_close_confirmed=True,
        run_owners_drained=True,
        context_input_io_drained=True,
    )
    dry_run = garbage_collect_incomplete_context_input_audits(
        runtime_session_id="runtime:test",
        event_log=runtime.event_log,
        archive=archive,
        maintenance_authority=authority,
        eligibility=eligibility,
        policy=ResolvedContextInputAuditMaintenancePolicy(),
        dry_run=True,
    )
    assert expectation.expected_plan_artifact_id in (
        dry_run.deletion_candidate_artifact_ids
    )
    assert "canonical-component:must-survive" not in (
        dry_run.deletion_candidate_artifact_ids
    )
    deleted = garbage_collect_incomplete_context_input_audits(
        runtime_session_id="runtime:test",
        event_log=runtime.event_log,
        archive=archive,
        maintenance_authority=authority,
        eligibility=eligibility,
        dry_run=False,
    )
    assert expectation.expected_plan_artifact_id in deleted.deleted_artifact_ids
    assert "canonical-component:must-survive" in archive.blobs
    assert expectation.expected_plan_artifact_id not in archive.blobs
    assert archive.delete_deadlines
    assert all(item is not None for item in archive.delete_deadlines)


@pytest.mark.parametrize("status", ["pressure", "failed"])
def test_every_noncompiled_context_branch_uses_the_same_physical_bound(status) -> None:
    event = build_context_compiled_event(
        **EventContext(
            run_id="run:test",
            turn_id="turn:test",
            reply_id="reply:test",
        ).event_fields(),
        **context_compiled_contract_fields(
            status=status,
            context_id="context:test",
            model_call_index=1,
        ),
        context_id="context:test",
        model_call_index=1,
    )
    assert (
        len(canonical_json_bytes(event)) <= MAX_CONTEXT_COMPILED_EVENT_CANONICAL_BYTES
    )
