"""Typed ArtifactStore adapter for optional context-input audit facts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol, TypeVar, cast

from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.ports.artifact import ArtifactContentConflict
from pulsara_agent.ports.mcp_secret import assert_not_mcp_secret
from pulsara_agent.primitives._context_base import (
    canonical_json_bytes,
    context_fingerprint,
)
from pulsara_agent.primitives.context_input_audit_storage import (
    CONTEXT_INPUT_AUDIT_PAGE_MEDIA_TYPE,
    CONTEXT_INPUT_AUDIT_PLAN_MEDIA_TYPE,
    CONTEXT_INPUT_AUDIT_ROOT_MEDIA_TYPE,
    ContextInputAuditMaterializationPlanFact,
    ContextInputAuditPageFact,
    ContextInputAuditRootFact,
    ContextInputAuditStorageFact,
    ContextInputAuditStoredArtifactReferenceFact,
)
from pulsara_agent.primitives.storage_frozen import (
    DURABLE_STORAGE_FACT_FINGERPRINT_REGISTRY,
    FrozenStorageFactBase,
    build_frozen_storage_fact,
)


class ContextInputAuditArtifactError(RuntimeError):
    pass


class ContextInputAuditArtifactMissing(ContextInputAuditArtifactError):
    pass


class ContextInputAuditArtifactConflict(ContextInputAuditArtifactError):
    pass


class ContextInputAuditArtifactIntegrityError(ContextInputAuditArtifactError):
    pass


class ContextInputAuditMaintenanceStore(ArtifactStore, Protocol):
    def delete_if_identity(
        self,
        blob_id: str,
        *,
        session_id: str,
        digest: str,
        media_type: str,
        semantic_metadata_fingerprint: str,
        deadline_monotonic: float,
    ) -> bool: ...


_StorageT = TypeVar("_StorageT", bound=FrozenStorageFactBase)


@dataclass(frozen=True, slots=True)
class StoredContextInputAuditFact:
    fact: ContextInputAuditStorageFact
    reference: ContextInputAuditStoredArtifactReferenceFact
    created_at_utc: str


def _fact_fingerprint(fact: FrozenStorageFactBase) -> str:
    spec = DURABLE_STORAGE_FACT_FINGERPRINT_REGISTRY.resolve(fact.schema_version)
    value = getattr(fact, spec.own_fingerprint_field)
    if not isinstance(value, str) or not value:
        raise ContextInputAuditArtifactIntegrityError(
            "audit storage fact fingerprint is absent"
        )
    return value


def _assert_audit_storage_fact_secret_safe(fact: FrozenStorageFactBase) -> None:
    """Allow this storage vocabulary while rejecting nested secret carriers."""

    for field_name in type(fact).model_fields:
        value = getattr(fact, field_name)
        if isinstance(value, FrozenStorageFactBase):
            _assert_audit_storage_fact_secret_safe(value)
            continue
        if isinstance(value, tuple) and all(
            isinstance(item, FrozenStorageFactBase) for item in value
        ):
            for item in value:
                _assert_audit_storage_fact_secret_safe(item)
            continue
        assert_not_mcp_secret(value, sink="ContextInputAuditArtifactRepository")


def _media_type(fact: FrozenStorageFactBase) -> str:
    if isinstance(fact, ContextInputAuditMaterializationPlanFact):
        return CONTEXT_INPUT_AUDIT_PLAN_MEDIA_TYPE
    if isinstance(fact, ContextInputAuditPageFact):
        return CONTEXT_INPUT_AUDIT_PAGE_MEDIA_TYPE
    if isinstance(fact, ContextInputAuditRootFact):
        return CONTEXT_INPUT_AUDIT_ROOT_MEDIA_TYPE
    raise TypeError("unsupported context input audit storage fact")


def _metadata(
    *,
    fact: ContextInputAuditStorageFact,
    media_type: str,
) -> tuple[dict[str, object], str]:
    base: dict[str, object] = {
        "artifact_kind": "context_input_audit",
        "schema_version": fact.schema_version,
        "source_runtime_session_id": fact.source_runtime_session_id,
        "source_run_id": fact.source_run_id,
        "materialization_key": fact.materialization_key,
        "storage_fact_fingerprint": _fact_fingerprint(fact),
        "media_type": media_type,
    }
    fingerprint = context_fingerprint("context-input-audit-semantic-metadata:v1", base)
    return {**base, "semantic_metadata_fingerprint": fingerprint}, fingerprint


def expected_audit_artifact_reference(
    *,
    artifact_id: str,
    fact: ContextInputAuditStorageFact,
) -> ContextInputAuditStoredArtifactReferenceFact:
    content = canonical_json_bytes(fact.model_dump(mode="json"))
    media_type = _media_type(fact)
    _, metadata_fingerprint = _metadata(fact=fact, media_type=media_type)
    return build_frozen_storage_fact(
        ContextInputAuditStoredArtifactReferenceFact,
        schema_version="context_input_audit_stored_artifact_reference.v1",
        artifact_id=artifact_id,
        content_sha256="sha256:" + hashlib.sha256(content).hexdigest(),
        content_bytes=len(content),
        media_type=media_type,
        storage_fact_schema_version=fact.schema_version,
        storage_fact_fingerprint=_fact_fingerprint(fact),
        semantic_metadata_fingerprint=metadata_fingerprint,
    )


def validate_context_input_audit_plan_reference(
    *,
    root: ContextInputAuditRootFact,
    plan: ContextInputAuditMaterializationPlanFact,
    expected_plan_artifact_id: str,
) -> ContextInputAuditStoredArtifactReferenceFact:
    """Prove that a root points at the deterministic plan carrier."""

    expected = expected_audit_artifact_reference(
        artifact_id=expected_plan_artifact_id,
        fact=plan,
    )
    if root.plan_artifact_reference != expected:
        raise ContextInputAuditArtifactIntegrityError(
            "context input audit root points at a non-canonical plan reference"
        )
    return expected


class ContextInputAuditArtifactRepository:
    """Narrow codec and identity owner over the generic artifact store."""

    def __init__(self, archive: ArtifactStore) -> None:
        self._archive = archive

    def put_exact(
        self,
        *,
        artifact_id: str,
        fact: ContextInputAuditStorageFact,
        deadline_monotonic: float,
    ) -> ContextInputAuditStoredArtifactReferenceFact:
        _assert_audit_storage_fact_secret_safe(fact)
        content_bytes = canonical_json_bytes(fact.model_dump(mode="json"))
        content = content_bytes.decode("utf-8")
        media_type = _media_type(fact)
        metadata, _ = _metadata(fact=fact, media_type=media_type)
        expected = expected_audit_artifact_reference(
            artifact_id=artifact_id,
            fact=fact,
        )
        try:
            confirmation = self._archive.put_text_if_absent_or_confirm_identical(
                artifact_id,
                content,
                session_id=fact.source_runtime_session_id,
                run_id=fact.source_run_id,
                media_type=media_type,
                semantic_metadata=metadata,
                deadline_monotonic=deadline_monotonic,
            )
        except ArtifactContentConflict as exc:
            raise ContextInputAuditArtifactConflict(
                "context input audit artifact identity conflicts"
            ) from exc
        if (
            confirmation.result.id != artifact_id
            or confirmation.result.digest != expected.content_sha256
            or confirmation.result.size_bytes != expected.content_bytes
        ):
            raise ContextInputAuditArtifactIntegrityError(
                "context input audit artifact confirmation drifted"
            )
        return expected

    def get_exact(
        self,
        *,
        reference: ContextInputAuditStoredArtifactReferenceFact,
        source_runtime_session_id: str,
        source_run_id: str,
        fact_type: type[_StorageT],
        deadline_monotonic: float,
    ) -> _StorageT:
        try:
            info = self._archive.get_info(
                reference.artifact_id,
                session_id=source_runtime_session_id,
                deadline_monotonic=deadline_monotonic,
            )
            content = self._archive.get_text(
                reference.artifact_id,
                session_id=source_runtime_session_id,
                deadline_monotonic=deadline_monotonic,
            )
        except (KeyError, FileNotFoundError) as exc:
            raise ContextInputAuditArtifactMissing(
                "context input audit artifact is missing"
            ) from exc
        metadata = info.metadata or {}
        if (
            info.id != reference.artifact_id
            or info.media_type != reference.media_type
            or info.digest != reference.content_sha256
            or info.size_bytes != reference.content_bytes
            or metadata.get("semantic_metadata_fingerprint")
            != reference.semantic_metadata_fingerprint
        ):
            raise ContextInputAuditArtifactIntegrityError(
                "context input audit artifact physical identity mismatch"
            )
        encoded = content.encode("utf-8")
        if (
            "sha256:" + hashlib.sha256(encoded).hexdigest() != reference.content_sha256
            or len(encoded) != reference.content_bytes
        ):
            raise ContextInputAuditArtifactIntegrityError(
                "context input audit artifact content identity mismatch"
            )
        try:
            payload = json.loads(content)
            fact = fact_type.model_validate(payload)
        except Exception as exc:
            raise ContextInputAuditArtifactIntegrityError(
                "context input audit artifact cannot be decoded"
            ) from exc
        if (
            fact.source_runtime_session_id != source_runtime_session_id
            or fact.source_run_id != source_run_id
            or fact.schema_version != reference.storage_fact_schema_version
            or _fact_fingerprint(fact) != reference.storage_fact_fingerprint
        ):
            raise ContextInputAuditArtifactIntegrityError(
                "context input audit fact/reference join mismatch"
            )
        rebuilt = expected_audit_artifact_reference(
            artifact_id=reference.artifact_id,
            fact=cast(ContextInputAuditStorageFact, fact),
        )
        if rebuilt != reference:
            raise ContextInputAuditArtifactIntegrityError(
                "context input audit reference is not canonical"
            )
        return fact

    def get_expected_root(
        self,
        *,
        artifact_id: str,
        source_runtime_session_id: str,
        source_run_id: str,
        deadline_monotonic: float,
    ) -> tuple[
        ContextInputAuditRootFact,
        ContextInputAuditStoredArtifactReferenceFact,
    ]:
        """Read a deterministic root ID before its physical reference is known."""

        stored = self._get_expected(
            artifact_id=artifact_id,
            source_runtime_session_id=source_runtime_session_id,
            source_run_id=source_run_id,
            fact_type=ContextInputAuditRootFact,
            deadline_monotonic=deadline_monotonic,
        )
        return cast(ContextInputAuditRootFact, stored.fact), stored.reference

    def get_expected_plan(
        self,
        *,
        artifact_id: str,
        source_runtime_session_id: str,
        source_run_id: str,
        deadline_monotonic: float,
    ) -> StoredContextInputAuditFact:
        """Read a deterministic incomplete-plan ID with its maintenance age."""

        return self._get_expected(
            artifact_id=artifact_id,
            source_runtime_session_id=source_runtime_session_id,
            source_run_id=source_run_id,
            fact_type=ContextInputAuditMaterializationPlanFact,
            deadline_monotonic=deadline_monotonic,
        )

    def _get_expected(
        self,
        *,
        artifact_id: str,
        source_runtime_session_id: str,
        source_run_id: str,
        fact_type: type[_StorageT],
        deadline_monotonic: float,
    ) -> StoredContextInputAuditFact:
        try:
            info = self._archive.get_info(
                artifact_id,
                session_id=source_runtime_session_id,
                deadline_monotonic=deadline_monotonic,
            )
            content = self._archive.get_text(
                artifact_id,
                session_id=source_runtime_session_id,
                deadline_monotonic=deadline_monotonic,
            )
        except (KeyError, FileNotFoundError) as exc:
            raise ContextInputAuditArtifactMissing(
                "context input audit expected artifact is missing"
            ) from exc
        try:
            fact = fact_type.model_validate_json(content)
        except Exception as exc:
            raise ContextInputAuditArtifactIntegrityError(
                "context input audit expected artifact cannot be decoded"
            ) from exc
        if (
            fact.source_runtime_session_id != source_runtime_session_id
            or fact.source_run_id != source_run_id
        ):
            raise ContextInputAuditArtifactIntegrityError(
                "context input audit expected artifact owner mismatch"
            )
        typed_fact = cast(ContextInputAuditStorageFact, fact)
        reference = expected_audit_artifact_reference(
            artifact_id=artifact_id,
            fact=typed_fact,
        )
        metadata = info.metadata or {}
        if (
            info.id != artifact_id
            or info.media_type != reference.media_type
            or info.digest != reference.content_sha256
            or info.size_bytes != reference.content_bytes
            or metadata.get("semantic_metadata_fingerprint")
            != reference.semantic_metadata_fingerprint
        ):
            raise ContextInputAuditArtifactIntegrityError(
                "context input audit expected artifact physical identity mismatch"
            )
        return StoredContextInputAuditFact(
            fact=typed_fact,
            reference=reference,
            created_at_utc=info.created_at,
        )


class ContextInputAuditMaintenanceRepository(ContextInputAuditArtifactRepository):
    def __init__(self, archive: ContextInputAuditMaintenanceStore) -> None:
        super().__init__(archive)
        self._maintenance_archive = archive

    def delete_exact(
        self,
        *,
        reference: ContextInputAuditStoredArtifactReferenceFact,
        source_runtime_session_id: str,
        deadline_monotonic: float,
    ) -> bool:
        return self._maintenance_archive.delete_if_identity(
            reference.artifact_id,
            session_id=source_runtime_session_id,
            digest=reference.content_sha256,
            media_type=reference.media_type,
            semantic_metadata_fingerprint=(reference.semantic_metadata_fingerprint),
            deadline_monotonic=deadline_monotonic,
        )


__all__ = [
    "ContextInputAuditArtifactConflict",
    "ContextInputAuditArtifactError",
    "ContextInputAuditArtifactIntegrityError",
    "ContextInputAuditArtifactMissing",
    "ContextInputAuditArtifactRepository",
    "ContextInputAuditMaintenanceRepository",
    "ContextInputAuditMaintenanceStore",
    "StoredContextInputAuditFact",
    "expected_audit_artifact_reference",
    "validate_context_input_audit_plan_reference",
]
