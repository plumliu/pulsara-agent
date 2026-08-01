"""Atomic artifact preparation and queue-content retention storage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from time import monotonic
from typing import Any, Protocol

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pulsara_agent.memory.artifacts.archive import (
    InMemoryArchiveStore,
    canonical_artifact_semantic_metadata,
)
from pulsara_agent.memory.artifacts.postgres_archive import PostgresArtifactStore
from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.prompt_queue import (
    PROMPT_QUEUE_ARTIFACT_MAX_UTF8_BYTES,
    PROMPT_QUEUE_ARTIFACT_STORAGE_CONTRACT_FINGERPRINT,
    ConfirmedArtifactQueueContentFact,
    PreparedPromptQueueContentFact,
    PromptQueueArtifactPreparationHoldFact,
    PromptQueueArtifactWriteReceiptIdentityFact,
)
from pulsara_agent.primitives.storage_frozen import build_frozen_storage_fact
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane


_MEDIA_TYPE = "text/plain; charset=utf-8"
_CODEC = "utf-8"
_SEMANTIC_REFERENCE = "pulsara.canonical-utf8-text-artifact:v1"
_HOLD_LIFETIME = timedelta(hours=24)


class PromptQueueArtifactStoragePort(Protocol):
    """Storage owner used both before and inside the queue transaction."""

    def prepare(
        self,
        *,
        runtime_session_id: str,
        owner_client_submission_identity: str,
        text: str,
        deadline_monotonic: float,
    ) -> ConfirmedArtifactQueueContentFact: ...

    def apply_accept_in_memory(
        self,
        *,
        runtime_session_id: str,
        queue_item_id: str,
        content: PreparedPromptQueueContentFact,
    ) -> None: ...

    def apply_retire_in_memory(
        self,
        *,
        runtime_session_id: str,
        queue_item_id: str,
        content: PreparedPromptQueueContentFact,
    ) -> None: ...

    def apply_accept_postgres(
        self,
        cursor: Any,
        *,
        runtime_session_id: str,
        queue_item_id: str,
        content: PreparedPromptQueueContentFact,
    ) -> None: ...

    def apply_retire_postgres(
        self,
        cursor: Any,
        *,
        runtime_session_id: str,
        queue_item_id: str,
        content: PreparedPromptQueueContentFact,
    ) -> None: ...

    def release_expired_prepared(
        self,
        *,
        runtime_session_id: str,
        expired_before_utc: str,
        maximum_holds: int,
        deadline_monotonic: float,
    ) -> tuple[str, ...]: ...


@dataclass(slots=True)
class InMemoryPromptQueueArtifactStorage:
    archive: InMemoryArchiveStore

    def prepare(
        self,
        *,
        runtime_session_id: str,
        owner_client_submission_identity: str,
        text: str,
        deadline_monotonic: float,
    ) -> ConfirmedArtifactQueueContentFact:
        if monotonic() >= deadline_monotonic:
            raise TimeoutError("queue artifact preparation deadline expired")
        prepared = _prepared_identity(
            runtime_session_id=runtime_session_id,
            owner_client_submission_identity=owner_client_submission_identity,
            text=text,
        )
        with self.archive._lock:
            confirmation = self.archive.put_text_if_absent_or_confirm_identical(
                prepared.artifact_id,
                text,
                session_id=runtime_session_id,
                run_id=None,
                media_type=_MEDIA_TYPE,
                semantic_metadata=prepared.semantic_metadata,
                deadline_monotonic=deadline_monotonic,
            )
            record = self.archive.get_info(
                prepared.artifact_id,
                session_id=runtime_session_id,
                deadline_monotonic=deadline_monotonic,
            )
            return _install_or_confirm_hold(
                holds=self.archive.prompt_queue_artifact_holds,
                prepared=prepared,
                confirmation_status=confirmation.status,
                stored_location=record.stored_at,
            )

    def apply_accept_in_memory(
        self,
        *,
        runtime_session_id: str,
        queue_item_id: str,
        content: PreparedPromptQueueContentFact,
    ) -> None:
        with self.archive._lock:
            key = (runtime_session_id, queue_item_id)
            payload = _content_reference_payload(
                runtime_session_id=runtime_session_id,
                queue_item_id=queue_item_id,
                content=content,
            )
            existing = self.archive.prompt_queue_content_references.get(key)
            if existing is not None and existing != payload:
                raise ValueError("prompt queue content reference conflicts")
            resulting_hold = None
            if isinstance(content, ConfirmedArtifactQueueContentFact):
                hold = self._require_hold(content.preparation_id)
                _validate_prepared_hold(
                    hold,
                    runtime_session_id=runtime_session_id,
                    queue_item_id=queue_item_id,
                    content=content,
                    require_state="PREPARED",
                )
                resulting_hold = _transition_hold(
                    hold, state="CONSUMED", consuming_queue_item_id=queue_item_id
                )
            self.archive.prompt_queue_content_references[key] = payload
            if resulting_hold is not None:
                self.archive.prompt_queue_artifact_holds[
                    resulting_hold.preparation_id
                ] = resulting_hold.model_dump(mode="json")

    def apply_retire_in_memory(
        self,
        *,
        runtime_session_id: str,
        queue_item_id: str,
        content: PreparedPromptQueueContentFact,
    ) -> None:
        with self.archive._lock:
            key = (runtime_session_id, queue_item_id)
            expected = _content_reference_payload(
                runtime_session_id=runtime_session_id,
                queue_item_id=queue_item_id,
                content=content,
            )
            if self.archive.prompt_queue_content_references.get(key) != expected:
                raise ValueError("prompt queue retirement reference mismatch")
            resulting_hold = None
            if isinstance(content, ConfirmedArtifactQueueContentFact):
                hold = self._require_hold(content.preparation_id)
                _validate_prepared_hold(
                    hold,
                    runtime_session_id=runtime_session_id,
                    queue_item_id=queue_item_id,
                    content=content,
                    require_state="CONSUMED",
                )
                resulting_hold = _transition_hold(
                    hold, state="RELEASED", consuming_queue_item_id=queue_item_id
                )
            self.archive.prompt_queue_content_references.pop(key)
            if resulting_hold is not None:
                self.archive.prompt_queue_artifact_holds[
                    resulting_hold.preparation_id
                ] = resulting_hold.model_dump(mode="json")

    def apply_accept_postgres(self, cursor: Any, **_: Any) -> None:
        del cursor
        raise TypeError("in-memory queue artifact storage cannot mutate PostgreSQL")

    def apply_retire_postgres(self, cursor: Any, **_: Any) -> None:
        del cursor
        raise TypeError("in-memory queue artifact storage cannot mutate PostgreSQL")

    def release_expired_prepared(
        self,
        *,
        runtime_session_id: str,
        expired_before_utc: str,
        maximum_holds: int,
        deadline_monotonic: float,
    ) -> tuple[str, ...]:
        _validate_maintenance_request(
            runtime_session_id=runtime_session_id,
            expired_before_utc=expired_before_utc,
            maximum_holds=maximum_holds,
            deadline_monotonic=deadline_monotonic,
        )
        cutoff = _parse_utc(expired_before_utc)
        released: list[str] = []
        with self.archive._lock:
            for preparation_id, payload in sorted(
                self.archive.prompt_queue_artifact_holds.items()
            ):
                if len(released) >= maximum_holds:
                    break
                if monotonic() >= deadline_monotonic:
                    raise TimeoutError(
                        "prompt queue artifact-hold maintenance deadline expired"
                    )
                hold = PromptQueueArtifactPreparationHoldFact.model_validate(payload)
                if (
                    hold.runtime_session_id != runtime_session_id
                    or hold.state != "PREPARED"
                    or _parse_utc(hold.expires_at_utc) > cutoff
                ):
                    continue
                referencing = tuple(
                    value
                    for value in self.archive.prompt_queue_content_references.values()
                    if value.get("preparation_id") == preparation_id
                )
                if referencing:
                    raise ValueError(
                        "PREPARED queue artifact hold already has a content reference"
                    )
                next_hold = _transition_hold(
                    hold,
                    state="RELEASED",
                    consuming_queue_item_id=None,
                )
                self.archive.prompt_queue_artifact_holds[preparation_id] = (
                    next_hold.model_dump(mode="json")
                )
                released.append(preparation_id)
        return tuple(released)

    def _require_hold(
        self, preparation_id: str
    ) -> PromptQueueArtifactPreparationHoldFact:
        payload = self.archive.prompt_queue_artifact_holds.get(preparation_id)
        if payload is None:
            raise ValueError("prompt queue artifact preparation hold is missing")
        return PromptQueueArtifactPreparationHoldFact.model_validate(payload)


@dataclass(slots=True)
class PostgresPromptQueueArtifactStorage:
    archive: PostgresArtifactStore

    def prepare(
        self,
        *,
        runtime_session_id: str,
        owner_client_submission_identity: str,
        text: str,
        deadline_monotonic: float,
    ) -> ConfirmedArtifactQueueContentFact:
        prepared = _prepared_identity(
            runtime_session_id=runtime_session_id,
            owner_client_submission_identity=owner_client_submission_identity,
            text=text,
        )
        with self.archive.connection_provider.connection(
            lane=PostgresConnectionLane.ARTIFACT,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                self.archive._apply_statement_deadline(cursor, deadline_monotonic)
                self.archive._lock_artifact(cursor, prepared.artifact_id)
                self.archive._validate_owner(
                    cursor, session_id=runtime_session_id, run_id=None
                )
                cursor.execute(
                    """
                    INSERT INTO artifacts (
                        id, session_id, run_id, media_type, text_body, digest,
                        size_bytes, stored_at, metadata
                    ) VALUES (%s, %s, NULL, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (
                        prepared.artifact_id,
                        runtime_session_id,
                        _MEDIA_TYPE,
                        text,
                        prepared.digest,
                        prepared.byte_count,
                        prepared.stored_location,
                        Jsonb(prepared.semantic_metadata),
                    ),
                )
                confirmation_status = (
                    "inserted" if cursor.rowcount == 1 else "confirmed_identical"
                )
                artifact_row = cursor.execute(
                    """
                    SELECT id, session_id, run_id, media_type, text_body,
                           binary_body, digest, size_bytes, stored_at,
                           created_at, metadata
                    FROM artifacts WHERE id = %s FOR UPDATE
                    """,
                    (prepared.artifact_id,),
                ).fetchone()
                if artifact_row is None:
                    raise ValueError("prepared artifact row disappeared")
                self.archive._validate_deterministic_text_row(
                    artifact_row,
                    blob_id=prepared.artifact_id,
                    content=text,
                    digest=prepared.digest,
                    size_bytes=prepared.byte_count,
                    media_type=_MEDIA_TYPE,
                    session_id=runtime_session_id,
                    run_id=None,
                    semantic_metadata=prepared.semantic_metadata,
                )
                existing = cursor.execute(
                    """
                    SELECT hold_payload
                    FROM prompt_queue_artifact_preparation_holds
                    WHERE preparation_id = %s
                    FOR UPDATE
                    """,
                    (prepared.preparation_id,),
                ).fetchone()
                if existing is None:
                    content, hold = _build_prepared_hold(
                        prepared=prepared,
                        confirmation_status=confirmation_status,
                        stored_location=str(artifact_row["stored_at"]),
                    )
                    cursor.execute(
                        """
                        INSERT INTO prompt_queue_artifact_preparation_holds (
                            preparation_id, session_id,
                            owner_client_submission_identity, artifact_id,
                            artifact_identity_fingerprint, content_fingerprint,
                            state, consuming_queue_item_id, hold_revision,
                            created_at_utc, expires_at_utc, hold_payload,
                            preparation_fingerprint, hold_row_fingerprint
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, NULL, %s,
                            %s, %s, %s, %s, %s
                        )
                        """,
                        (
                            hold.preparation_id,
                            hold.runtime_session_id,
                            hold.owner_client_submission_identity,
                            hold.artifact_id,
                            hold.artifact_identity_fingerprint,
                            hold.content_fingerprint,
                            hold.state,
                            hold.hold_revision,
                            hold.created_at_utc,
                            hold.expires_at_utc,
                            Jsonb(hold.model_dump(mode="json")),
                            hold.preparation_fingerprint,
                            hold.hold_row_fingerprint,
                        ),
                    )
                    return content
                hold = PromptQueueArtifactPreparationHoldFact.model_validate(
                    dict(existing["hold_payload"])
                )
                return _content_from_existing_hold(prepared=prepared, hold=hold)

    def apply_accept_in_memory(self, **_: Any) -> None:
        raise TypeError("PostgreSQL queue artifact storage cannot mutate in memory")

    def apply_retire_in_memory(self, **_: Any) -> None:
        raise TypeError("PostgreSQL queue artifact storage cannot mutate in memory")

    def apply_accept_postgres(
        self,
        cursor: Any,
        *,
        runtime_session_id: str,
        queue_item_id: str,
        content: PreparedPromptQueueContentFact,
    ) -> None:
        payload = _content_reference_payload(
            runtime_session_id=runtime_session_id,
            queue_item_id=queue_item_id,
            content=content,
        )
        if isinstance(content, ConfirmedArtifactQueueContentFact):
            artifact_row = cursor.execute(
                """
                SELECT id, session_id, media_type, digest, size_bytes,
                       stored_at, metadata
                FROM artifacts WHERE id = %s FOR UPDATE
                """,
                (content.stable_content_addressed_artifact_id,),
            ).fetchone()
            _validate_artifact_row(
                artifact_row,
                runtime_session_id=runtime_session_id,
                content=content,
            )
            hold_row = cursor.execute(
                """
                SELECT hold_payload
                FROM prompt_queue_artifact_preparation_holds
                WHERE preparation_id = %s
                FOR UPDATE
                """,
                (content.preparation_id,),
            ).fetchone()
            if hold_row is None:
                raise ValueError("prompt queue artifact hold is missing")
            hold = PromptQueueArtifactPreparationHoldFact.model_validate(
                dict(hold_row["hold_payload"])
            )
            _validate_prepared_hold(
                hold,
                runtime_session_id=runtime_session_id,
                queue_item_id=queue_item_id,
                content=content,
                require_state="PREPARED",
            )
            if _parse_utc(hold.expires_at_utc) <= datetime.now(UTC):
                raise ValueError("prompt queue artifact preparation expired")
            consumed = _transition_hold(
                hold, state="CONSUMED", consuming_queue_item_id=queue_item_id
            )
            _update_hold(cursor, before=hold, after=consumed)
        cursor.execute(
            """
            INSERT INTO prompt_queue_content_references (
                session_id, queue_item_id, content_kind,
                content_semantic_fingerprint, content_attribution_fingerprint,
                content_fact_fingerprint, artifact_id, preparation_id,
                hold_revision, reference_payload, reference_fingerprint
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (session_id, queue_item_id) DO NOTHING
            """,
            (
                runtime_session_id,
                queue_item_id,
                content.content_kind,
                content.content_semantic_fingerprint,
                content.content_attribution_fingerprint,
                content.content_fact_fingerprint,
                (
                    content.stable_content_addressed_artifact_id
                    if isinstance(content, ConfirmedArtifactQueueContentFact)
                    else None
                ),
                (
                    content.preparation_id
                    if isinstance(content, ConfirmedArtifactQueueContentFact)
                    else None
                ),
                (
                    content.preparation_hold_revision
                    if isinstance(content, ConfirmedArtifactQueueContentFact)
                    else None
                ),
                Jsonb(payload),
                payload["reference_fingerprint"],
            ),
        )
        if cursor.rowcount != 1:
            row = cursor.execute(
                """
                SELECT reference_payload
                FROM prompt_queue_content_references
                WHERE session_id = %s AND queue_item_id = %s
                """,
                (runtime_session_id, queue_item_id),
            ).fetchone()
            if row is None or dict(row["reference_payload"]) != payload:
                raise ValueError("prompt queue content reference conflicts")

    def apply_retire_postgres(
        self,
        cursor: Any,
        *,
        runtime_session_id: str,
        queue_item_id: str,
        content: PreparedPromptQueueContentFact,
    ) -> None:
        expected = _content_reference_payload(
            runtime_session_id=runtime_session_id,
            queue_item_id=queue_item_id,
            content=content,
        )
        row = cursor.execute(
            """
            SELECT reference_payload
            FROM prompt_queue_content_references
            WHERE session_id = %s AND queue_item_id = %s
            FOR UPDATE
            """,
            (runtime_session_id, queue_item_id),
        ).fetchone()
        if row is None or dict(row["reference_payload"]) != expected:
            raise ValueError("prompt queue retirement reference mismatch")
        if isinstance(content, ConfirmedArtifactQueueContentFact):
            hold_row = cursor.execute(
                """
                SELECT hold_payload
                FROM prompt_queue_artifact_preparation_holds
                WHERE preparation_id = %s
                FOR UPDATE
                """,
                (content.preparation_id,),
            ).fetchone()
            if hold_row is None:
                raise ValueError("prompt queue consumed hold is missing")
            hold = PromptQueueArtifactPreparationHoldFact.model_validate(
                dict(hold_row["hold_payload"])
            )
            _validate_prepared_hold(
                hold,
                runtime_session_id=runtime_session_id,
                queue_item_id=queue_item_id,
                content=content,
                require_state="CONSUMED",
            )
            released = _transition_hold(
                hold, state="RELEASED", consuming_queue_item_id=queue_item_id
            )
            _update_hold(cursor, before=hold, after=released)
        cursor.execute(
            """
            DELETE FROM prompt_queue_content_references
            WHERE session_id = %s AND queue_item_id = %s
            """,
            (runtime_session_id, queue_item_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("prompt queue content reference retirement CAS failed")

    def release_expired_prepared(
        self,
        *,
        runtime_session_id: str,
        expired_before_utc: str,
        maximum_holds: int,
        deadline_monotonic: float,
    ) -> tuple[str, ...]:
        _validate_maintenance_request(
            runtime_session_id=runtime_session_id,
            expired_before_utc=expired_before_utc,
            maximum_holds=maximum_holds,
            deadline_monotonic=deadline_monotonic,
        )
        cutoff = _parse_utc(expired_before_utc)
        released: list[str] = []
        with self.archive.connection_provider.connection(
            lane=PostgresConnectionLane.ARTIFACT,
            row_factory=dict_row,
            deadline_monotonic=deadline_monotonic,
        ) as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                self.archive._apply_statement_deadline(cursor, deadline_monotonic)
                rows = cursor.execute(
                    """
                    SELECT preparation_id, session_id, state, hold_revision,
                           expires_at_utc, hold_payload, hold_row_fingerprint
                    FROM prompt_queue_artifact_preparation_holds
                    WHERE session_id = %s
                      AND state = 'PREPARED'
                      AND expires_at_utc <= %s
                    ORDER BY expires_at_utc, preparation_id
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                    """,
                    (runtime_session_id, cutoff, maximum_holds),
                ).fetchall()
                for row in rows:
                    if monotonic() >= deadline_monotonic:
                        raise TimeoutError(
                            "prompt queue artifact-hold maintenance deadline expired"
                        )
                    hold = PromptQueueArtifactPreparationHoldFact.model_validate(
                        dict(row["hold_payload"])
                    )
                    if (
                        str(row["preparation_id"]) != hold.preparation_id
                        or str(row["session_id"]) != hold.runtime_session_id
                        or str(row["state"]) != hold.state
                        or int(row["hold_revision"]) != hold.hold_revision
                        or str(row["hold_row_fingerprint"]) != hold.hold_row_fingerprint
                        or row["expires_at_utc"] != _parse_utc(hold.expires_at_utc)
                        or hold.runtime_session_id != runtime_session_id
                        or hold.state != "PREPARED"
                        or _parse_utc(hold.expires_at_utc) > cutoff
                    ):
                        raise ValueError(
                            "prompt queue artifact-hold storage payload drifted"
                        )
                    reference = cursor.execute(
                        """
                        SELECT queue_item_id
                        FROM prompt_queue_content_references
                        WHERE preparation_id = %s
                        LIMIT 1
                        """,
                        (hold.preparation_id,),
                    ).fetchone()
                    if reference is not None:
                        raise ValueError(
                            "PREPARED queue artifact hold already has a content reference"
                        )
                    next_hold = _transition_hold(
                        hold,
                        state="RELEASED",
                        consuming_queue_item_id=None,
                    )
                    _update_hold(cursor, before=hold, after=next_hold)
                    released.append(hold.preparation_id)
        return tuple(released)


@dataclass(frozen=True, slots=True)
class _PreparedArtifactIdentity:
    runtime_session_id: str
    owner_client_submission_identity: str
    text: str
    byte_count: int
    digest: str
    artifact_id: str
    stored_location: str
    semantic_metadata: dict[str, object]
    semantic_metadata_fingerprint: str
    artifact_identity_fingerprint: str
    content_semantic_fingerprint: str
    preparation_id: str
    preparation_fingerprint: str


def build_prompt_queue_artifact_storage(
    archive: ArtifactStore,
) -> PromptQueueArtifactStoragePort:
    if isinstance(archive, InMemoryArchiveStore):
        return InMemoryPromptQueueArtifactStorage(archive)
    if isinstance(archive, PostgresArtifactStore):
        return PostgresPromptQueueArtifactStorage(archive)
    raise TypeError(
        "RuntimeSession requires a typed prompt-queue artifact storage adapter"
    )


def prompt_queue_content_semantic_fingerprint(text: str) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) > PROMPT_QUEUE_ARTIFACT_MAX_UTF8_BYTES:
        raise ValueError("prompt queue content exceeds its frozen artifact byte cap")
    return context_fingerprint(
        "prompt-queue-content-semantic:v1",
        {
            "canonical_payload_sha256": f"sha256:{sha256(encoded).hexdigest()}",
            "canonical_byte_count": len(encoded),
            "normalized_media_type": _MEDIA_TYPE,
            "codec": _CODEC,
            "content_semantic_reference": _SEMANTIC_REFERENCE,
        },
    )


def _prepared_identity(
    *,
    runtime_session_id: str,
    owner_client_submission_identity: str,
    text: str,
) -> _PreparedArtifactIdentity:
    encoded = text.encode("utf-8")
    if not encoded:
        raise ValueError("artifact-backed queue content cannot be empty")
    if len(encoded) > PROMPT_QUEUE_ARTIFACT_MAX_UTF8_BYTES:
        raise ValueError("prompt queue content exceeds its frozen artifact byte cap")
    digest = f"sha256:{sha256(encoded).hexdigest()}"
    content_semantic_fingerprint = prompt_queue_content_semantic_fingerprint(text)
    artifact_id = context_fingerprint(
        "prompt-queue-artifact-id:v1", (runtime_session_id, digest)
    ).replace("sha256:", "artifact:prompt-queue:")
    stored_location = f"postgres://artifacts/{artifact_id}"
    metadata_base = {
        "artifact_kind": "prompt_queue_content",
        "content_semantic_fingerprint": content_semantic_fingerprint,
        "semantic_reference": _SEMANTIC_REFERENCE,
    }
    metadata_fingerprint = context_fingerprint(
        "prompt-queue-artifact-semantic-metadata:v1", metadata_base
    )
    metadata = canonical_artifact_semantic_metadata(
        {**metadata_base, "semantic_metadata_fingerprint": metadata_fingerprint}
    )
    artifact_identity = context_fingerprint(
        "prompt-queue-artifact-identity:v1",
        {
            "artifact_id": artifact_id,
            "runtime_session_id": runtime_session_id,
            "digest": digest,
            "byte_count": len(encoded),
            "media_type": _MEDIA_TYPE,
            "semantic_metadata_fingerprint": metadata_fingerprint,
        },
    )
    preparation_id = context_fingerprint(
        "prompt-queue-artifact-preparation-id:v1",
        {
            "runtime_session_id": runtime_session_id,
            "owner_client_submission_identity": owner_client_submission_identity,
            "artifact_identity_fingerprint": artifact_identity,
            "content_semantic_fingerprint": content_semantic_fingerprint,
        },
    ).replace("sha256:", "prompt-queue-preparation:")
    preparation_fingerprint = context_fingerprint(
        "prompt-queue-artifact-preparation:v1",
        {
            "preparation_id": preparation_id,
            "runtime_session_id": runtime_session_id,
            "owner_client_submission_identity": owner_client_submission_identity,
            "artifact_id": artifact_id,
            "artifact_identity_fingerprint": artifact_identity,
            "content_semantic_fingerprint": content_semantic_fingerprint,
        },
    )
    return _PreparedArtifactIdentity(
        runtime_session_id=runtime_session_id,
        owner_client_submission_identity=owner_client_submission_identity,
        text=text,
        byte_count=len(encoded),
        digest=digest,
        artifact_id=artifact_id,
        stored_location=stored_location,
        semantic_metadata=metadata,
        semantic_metadata_fingerprint=metadata_fingerprint,
        artifact_identity_fingerprint=artifact_identity,
        content_semantic_fingerprint=content_semantic_fingerprint,
        preparation_id=preparation_id,
        preparation_fingerprint=preparation_fingerprint,
    )


def _build_prepared_hold(
    *,
    prepared: _PreparedArtifactIdentity,
    confirmation_status: str,
    stored_location: str,
) -> tuple[ConfirmedArtifactQueueContentFact, PromptQueueArtifactPreparationHoldFact]:
    receipt = build_frozen_fact(
        PromptQueueArtifactWriteReceiptIdentityFact,
        schema_version="prompt_queue_artifact_write_receipt_identity.v1",
        artifact_storage_contract_fingerprint=(
            PROMPT_QUEUE_ARTIFACT_STORAGE_CONTRACT_FINGERPRINT
        ),
        confirmation_status=confirmation_status,
        artifact_id=prepared.artifact_id,
        artifact_digest=prepared.digest,
        artifact_size_bytes=prepared.byte_count,
        media_type=_MEDIA_TYPE,
        semantic_metadata_fingerprint=prepared.semantic_metadata_fingerprint,
        stored_location_identity=stored_location,
    )
    created = datetime.now(UTC)
    hold = build_frozen_storage_fact(
        PromptQueueArtifactPreparationHoldFact,
        schema_version="prompt_queue_artifact_preparation_hold.v1",
        preparation_id=prepared.preparation_id,
        runtime_session_id=prepared.runtime_session_id,
        owner_client_submission_identity=prepared.owner_client_submission_identity,
        artifact_id=prepared.artifact_id,
        artifact_identity_fingerprint=prepared.artifact_identity_fingerprint,
        content_fingerprint=prepared.content_semantic_fingerprint,
        state="PREPARED",
        consuming_queue_item_id=None,
        hold_revision=0,
        created_at_utc=_format_utc(created),
        expires_at_utc=_format_utc(created + _HOLD_LIFETIME),
        confirmed_write_receipt_identity=receipt,
        confirmed_write_receipt_fingerprint=receipt.receipt_identity_fingerprint,
        preparation_fingerprint=prepared.preparation_fingerprint,
    )
    return _content_from_hold(prepared=prepared, hold=hold), hold


def _install_or_confirm_hold(
    *,
    holds: dict[str, dict[str, Any]],
    prepared: _PreparedArtifactIdentity,
    confirmation_status: str,
    stored_location: str,
) -> ConfirmedArtifactQueueContentFact:
    existing = holds.get(prepared.preparation_id)
    if existing is not None:
        hold = PromptQueueArtifactPreparationHoldFact.model_validate(existing)
        return _content_from_existing_hold(prepared=prepared, hold=hold)
    content, hold = _build_prepared_hold(
        prepared=prepared,
        confirmation_status=confirmation_status,
        stored_location=stored_location,
    )
    holds[hold.preparation_id] = hold.model_dump(mode="json")
    return content


def _content_from_existing_hold(
    *,
    prepared: _PreparedArtifactIdentity,
    hold: PromptQueueArtifactPreparationHoldFact,
) -> ConfirmedArtifactQueueContentFact:
    if (
        hold.runtime_session_id != prepared.runtime_session_id
        or hold.owner_client_submission_identity
        != prepared.owner_client_submission_identity
        or hold.artifact_id != prepared.artifact_id
        or hold.artifact_identity_fingerprint != prepared.artifact_identity_fingerprint
        or hold.content_fingerprint != prepared.content_semantic_fingerprint
        or hold.preparation_fingerprint != prepared.preparation_fingerprint
        or hold.state == "RELEASED"
    ):
        raise ValueError("prompt queue preparation identity conflicts")
    return _content_from_hold(prepared=prepared, hold=hold)


def _content_from_hold(
    *,
    prepared: _PreparedArtifactIdentity,
    hold: PromptQueueArtifactPreparationHoldFact,
) -> ConfirmedArtifactQueueContentFact:
    receipt = hold.confirmed_write_receipt_identity
    attribution = context_fingerprint(
        "prompt-queue-content-attribution:v1",
        {
            "content_kind": "confirmed_artifact",
            "preparation_id": hold.preparation_id,
            "preparation_fingerprint": hold.preparation_fingerprint,
            "hold_revision": hold.hold_revision,
            "stable_artifact_id": hold.artifact_id,
            "artifact_identity_fingerprint": hold.artifact_identity_fingerprint,
            "confirmed_write_receipt_identity": receipt,
            "confirmed_write_receipt_fingerprint": (
                receipt.receipt_identity_fingerprint
            ),
            "storage_location_attribution": receipt.stored_location_identity,
        },
    )
    return build_frozen_fact(
        ConfirmedArtifactQueueContentFact,
        schema_version="prompt_queue_confirmed_artifact_content.v1",
        content_kind="confirmed_artifact",
        preparation_id=hold.preparation_id,
        preparation_fingerprint=hold.preparation_fingerprint,
        preparation_hold_revision=hold.hold_revision,
        stable_content_addressed_artifact_id=hold.artifact_id,
        artifact_identity_fingerprint=hold.artifact_identity_fingerprint,
        canonical_payload_sha256=prepared.digest,
        canonical_byte_count=prepared.byte_count,
        media_type=_MEDIA_TYPE,
        codec=_CODEC,
        artifact_semantic_reference=_SEMANTIC_REFERENCE,
        confirmed_write_receipt_identity=receipt,
        confirmed_write_receipt_fingerprint=receipt.receipt_identity_fingerprint,
        content_semantic_fingerprint=prepared.content_semantic_fingerprint,
        content_attribution_fingerprint=attribution,
    )


def _transition_hold(
    hold: PromptQueueArtifactPreparationHoldFact,
    *,
    state: str,
    consuming_queue_item_id: str | None,
) -> PromptQueueArtifactPreparationHoldFact:
    values = hold.model_dump(mode="python")
    values["confirmed_write_receipt_identity"] = hold.confirmed_write_receipt_identity
    values.pop("hold_row_fingerprint")
    values.update(
        state=state,
        consuming_queue_item_id=consuming_queue_item_id,
        hold_revision=hold.hold_revision + 1,
    )
    return build_frozen_storage_fact(PromptQueueArtifactPreparationHoldFact, **values)


def _validate_prepared_hold(
    hold: PromptQueueArtifactPreparationHoldFact,
    *,
    runtime_session_id: str,
    queue_item_id: str,
    content: ConfirmedArtifactQueueContentFact,
    require_state: str,
) -> None:
    expected_consumer = queue_item_id if require_state == "CONSUMED" else None
    if (
        hold.runtime_session_id != runtime_session_id
        or hold.state != require_state
        or hold.consuming_queue_item_id != expected_consumer
        or hold.preparation_id != content.preparation_id
        or hold.preparation_fingerprint != content.preparation_fingerprint
        or hold.artifact_id != content.stable_content_addressed_artifact_id
        or hold.artifact_identity_fingerprint != content.artifact_identity_fingerprint
        or hold.content_fingerprint != content.content_semantic_fingerprint
        or hold.confirmed_write_receipt_fingerprint
        != content.confirmed_write_receipt_fingerprint
    ):
        raise ValueError("prompt queue artifact hold join mismatch")


def _content_reference_payload(
    *,
    runtime_session_id: str,
    queue_item_id: str,
    content: PreparedPromptQueueContentFact,
) -> dict[str, Any]:
    base = {
        "runtime_session_id": runtime_session_id,
        "queue_item_id": queue_item_id,
        "content_kind": content.content_kind,
        "content_semantic_fingerprint": content.content_semantic_fingerprint,
        "content_attribution_fingerprint": content.content_attribution_fingerprint,
        "content_fact_fingerprint": content.content_fact_fingerprint,
        "artifact_id": (
            content.stable_content_addressed_artifact_id
            if isinstance(content, ConfirmedArtifactQueueContentFact)
            else None
        ),
        "preparation_id": (
            content.preparation_id
            if isinstance(content, ConfirmedArtifactQueueContentFact)
            else None
        ),
        "hold_revision": (
            content.preparation_hold_revision
            if isinstance(content, ConfirmedArtifactQueueContentFact)
            else None
        ),
    }
    return {
        **base,
        "reference_fingerprint": context_fingerprint(
            "prompt-queue-content-reference:v1", base
        ),
    }


def _validate_artifact_row(
    row: Any,
    *,
    runtime_session_id: str,
    content: ConfirmedArtifactQueueContentFact,
) -> None:
    if row is None:
        raise ValueError("prompt queue artifact row is missing")
    metadata = dict(row["metadata"] or {})
    receipt = content.confirmed_write_receipt_identity
    observed_identity = context_fingerprint(
        "prompt-queue-artifact-identity:v1",
        {
            "artifact_id": str(row["id"]),
            "runtime_session_id": runtime_session_id,
            "digest": str(row["digest"]),
            "byte_count": int(row["size_bytes"]),
            "media_type": str(row["media_type"]),
            "semantic_metadata_fingerprint": metadata.get(
                "semantic_metadata_fingerprint"
            ),
        },
    )
    if (
        row["session_id"] != runtime_session_id
        or row["id"] != content.stable_content_addressed_artifact_id
        or row["digest"] != content.canonical_payload_sha256
        or int(row["size_bytes"]) != content.canonical_byte_count
        or row["media_type"] != content.media_type
        or row["stored_at"] != receipt.stored_location_identity
        or metadata.get("semantic_metadata_fingerprint")
        != receipt.semantic_metadata_fingerprint
        or observed_identity != content.artifact_identity_fingerprint
    ):
        raise ValueError("prompt queue artifact row identity mismatch")


def _update_hold(
    cursor: Any,
    *,
    before: PromptQueueArtifactPreparationHoldFact,
    after: PromptQueueArtifactPreparationHoldFact,
) -> None:
    cursor.execute(
        """
        UPDATE prompt_queue_artifact_preparation_holds
        SET state = %s,
            consuming_queue_item_id = %s,
            hold_revision = %s,
            hold_payload = %s,
            hold_row_fingerprint = %s,
            updated_at = now()
        WHERE preparation_id = %s
          AND hold_revision = %s
          AND hold_row_fingerprint = %s
        """,
        (
            after.state,
            after.consuming_queue_item_id,
            after.hold_revision,
            Jsonb(after.model_dump(mode="json")),
            after.hold_row_fingerprint,
            before.preparation_id,
            before.hold_revision,
            before.hold_row_fingerprint,
        ),
    )
    if cursor.rowcount != 1:
        raise ValueError("prompt queue artifact hold CAS failed")


def _format_utc(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _validate_maintenance_request(
    *,
    runtime_session_id: str,
    expired_before_utc: str,
    maximum_holds: int,
    deadline_monotonic: float,
) -> None:
    if not runtime_session_id or not 1 <= maximum_holds <= 256:
        raise ValueError("prompt queue artifact-hold maintenance request is malformed")
    cutoff = _parse_utc(expired_before_utc)
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("prompt queue artifact-hold cutoff must be timezone-aware")
    if monotonic() >= deadline_monotonic:
        raise TimeoutError("prompt queue artifact-hold maintenance deadline expired")


__all__ = [
    "InMemoryPromptQueueArtifactStorage",
    "PostgresPromptQueueArtifactStorage",
    "PromptQueueArtifactStoragePort",
    "build_prompt_queue_artifact_storage",
    "prompt_queue_content_semantic_fingerprint",
]
