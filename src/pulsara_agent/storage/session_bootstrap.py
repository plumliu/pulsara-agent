"""Unique PostgreSQL owner for immutable RuntimeSession creation."""

from __future__ import annotations

from time import monotonic
from typing import cast

from psycopg import Connection
from psycopg.types.json import Jsonb

from pulsara_agent.primitives.transcript_accumulators import (
    EMPTY_LEDGER_CONTINUITY_ACCUMULATOR,
    EMPTY_TRANSCRIPT_SEMANTIC_ACCUMULATOR,
)
from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.primitives.compaction import (
    BackgroundDerivedWorkBudgetAccountFact,
    DEFAULT_BACKGROUND_DERIVED_WORK_BUDGET_POLICY,
    build_background_budget_genesis,
)
from pulsara_agent.primitives.runtime_event_vocabulary import (
    build_bounded_runtime_failure_diagnostic,
)
from pulsara_agent.projection_jobs.contracts import (
    DurableProjectionCommitConfirmation,
    DurableProjectionKindActivationFact,
    DurableProjectionSessionCutoverFact,
    PreActivationProjectionHookContractFact,
    PreActivationProjectionSessionCutoverFact,
    RuntimeSessionBootstrapCommitOutcomeFact,
    RuntimeSessionBootstrapStateFact,
    RuntimeSessionOwnerBootstrapCandidateFact,
    RuntimeSessionOwnerSemanticFact,
    build_projection_fact,
)
from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane,
    VerifiedPostgresConnectionProviderProtocol,
)
from pulsara_agent.storage.runtime_write_admission import read_runtime_write_epoch


def build_runtime_session_owner_semantic(
    *,
    runtime_session_id: str,
    workspace_root: str | None,
) -> RuntimeSessionOwnerSemanticFact:
    normalized_workspace = (
        workspace_root.strip() if workspace_root is not None else None
    )
    if not runtime_session_id:
        raise ValueError("runtime_session_id must be non-empty")
    if normalized_workspace == "":
        normalized_workspace = None
    return cast(
        RuntimeSessionOwnerSemanticFact,
        build_projection_fact(
            RuntimeSessionOwnerSemanticFact,
            schema_version="runtime_session_owner_semantic.v1",
            runtime_session_id=runtime_session_id,
            workspace_root=normalized_workspace,
        ),
    )


def build_runtime_session_bootstrap_candidate(
    *,
    runtime_session_id: str,
    workspace_root: str | None,
    expected_admission_epoch_fingerprint: str,
) -> RuntimeSessionOwnerBootstrapCandidateFact:
    owner = build_runtime_session_owner_semantic(
        runtime_session_id=runtime_session_id,
        workspace_root=workspace_root,
    )
    return cast(
        RuntimeSessionOwnerBootstrapCandidateFact,
        build_projection_fact(
            RuntimeSessionOwnerBootstrapCandidateFact,
            schema_version="runtime_session_owner_bootstrap_candidate.v1",
            session_owner=owner,
            expected_admission_epoch_fingerprint=(expected_admission_epoch_fingerprint),
            background_budget_policy=DEFAULT_BACKGROUND_DERIVED_WORK_BUDGET_POLICY,
            background_budget_account=build_background_budget_genesis(
                runtime_session_id=runtime_session_id,
                policy=DEFAULT_BACKGROUND_DERIVED_WORK_BUDGET_POLICY,
            ),
        ),
    )


class PostgresRuntimeSessionOwnerBootstrapPort:
    """Commit and exact-confirm a session plus its complete cutover bundle."""

    def __init__(
        self,
        connection_provider: VerifiedPostgresConnectionProviderProtocol,
    ) -> None:
        self._connection_provider = connection_provider

    def candidate(
        self,
        *,
        runtime_session_id: str,
        workspace_root: str | None,
    ) -> RuntimeSessionOwnerBootstrapCandidateFact:
        return build_runtime_session_bootstrap_candidate(
            runtime_session_id=runtime_session_id,
            workspace_root=workspace_root,
            expected_admission_epoch_fingerprint=(
                self._connection_provider.schema_binding.runtime_write_admission_epoch_fingerprint
            ),
        )

    def bootstrap(
        self,
        *,
        candidate: RuntimeSessionOwnerBootstrapCandidateFact,
        deadline_monotonic: float,
    ) -> RuntimeSessionBootstrapCommitOutcomeFact:
        inserted = False
        try:
            self._require_remaining(deadline_monotonic)
            with self._connection_provider.connection(
                lane=PostgresConnectionLane.HOST_CONTROL,
                deadline_monotonic=deadline_monotonic,
            ) as connection:
                connection.execute(
                    "SELECT pg_catalog.pg_advisory_xact_lock("
                    "pg_catalog.hashtextextended(%s, 0))",
                    (
                        "pulsara:runtime-session-bootstrap:"
                        + candidate.session_owner.runtime_session_id,
                    ),
                )
                epoch = read_runtime_write_epoch(connection)
                if (
                    epoch.epoch_fingerprint
                    != candidate.expected_admission_epoch_fingerprint
                ):
                    return self._outcome(
                        confirmation=DurableProjectionCommitConfirmation.CONFLICT,
                        candidate=candidate,
                        resulting_state=None,
                        physical_disposition=None,
                    )
                row = connection.execute(
                    """
                    SELECT id, workspace_root
                    FROM public.sessions
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (candidate.session_owner.runtime_session_id,),
                ).fetchone()
                if row is None:
                    created = connection.execute(
                        """
                        INSERT INTO public.sessions (id, workspace_root)
                        VALUES (%s, %s)
                        RETURNING id
                        """,
                        (
                            candidate.session_owner.runtime_session_id,
                            candidate.session_owner.workspace_root,
                        ),
                    ).fetchone()
                    inserted = created is not None
                elif (
                    str(row[0]) != candidate.session_owner.runtime_session_id
                    or row[1] != candidate.session_owner.workspace_root
                ):
                    return self._outcome(
                        confirmation=DurableProjectionCommitConfirmation.CONFLICT,
                        candidate=candidate,
                        resulting_state=None,
                        physical_disposition=None,
                    )
                self._install_required_cutovers(
                    connection,
                    candidate=candidate,
                )
                state = self._read_state(
                    connection,
                    candidate=candidate,
                )
                if state is None:
                    return self._outcome(
                        confirmation=DurableProjectionCommitConfirmation.CONFLICT,
                        candidate=candidate,
                        resulting_state=None,
                        physical_disposition=None,
                    )
            return self._outcome(
                confirmation=DurableProjectionCommitConfirmation.FULL,
                candidate=candidate,
                resulting_state=state,
                physical_disposition=("inserted" if inserted else "exact_confirmed"),
            )
        except BaseException as error:
            return self._confirm_after_uncertain_write(
                candidate=candidate,
                deadline_monotonic=deadline_monotonic,
                error=error,
            )

    def _confirm_after_uncertain_write(
        self,
        *,
        candidate: RuntimeSessionOwnerBootstrapCandidateFact,
        deadline_monotonic: float,
        error: BaseException,
    ) -> RuntimeSessionBootstrapCommitOutcomeFact:
        try:
            self._require_remaining(deadline_monotonic)
            with self._connection_provider.connection(
                lane=PostgresConnectionLane.HOST_CONTROL,
                deadline_monotonic=deadline_monotonic,
            ) as connection:
                epoch = read_runtime_write_epoch(connection)
                row = connection.execute(
                    """
                    SELECT id, workspace_root
                    FROM public.sessions
                    WHERE id = %s
                    """,
                    (candidate.session_owner.runtime_session_id,),
                ).fetchone()
                active_count = int(
                    connection.execute(
                        """
                        SELECT count(*)
                        FROM public.durable_projection_session_cutovers
                        WHERE runtime_session_id = %s
                        """,
                        (candidate.session_owner.runtime_session_id,),
                    ).fetchone()[0]
                )
                pre_count = int(
                    connection.execute(
                        """
                        SELECT count(*)
                        FROM public.durable_projection_pre_activation_session_cutovers
                        WHERE runtime_session_id = %s
                        """,
                        (candidate.session_owner.runtime_session_id,),
                    ).fetchone()[0]
                )
                budget_count = int(
                    connection.execute(
                        """
                        SELECT count(*)
                        FROM public.background_derived_work_budget_accounts
                        WHERE runtime_session_id = %s
                        """,
                        (candidate.session_owner.runtime_session_id,),
                    ).fetchone()[0]
                )
                if (
                    row is None
                    and active_count == 0
                    and pre_count == 0
                    and budget_count == 0
                    and epoch.epoch_fingerprint
                    == candidate.expected_admission_epoch_fingerprint
                ):
                    return self._outcome(
                        confirmation=DurableProjectionCommitConfirmation.NONE,
                        candidate=candidate,
                        resulting_state=None,
                        physical_disposition=None,
                        failure=error,
                    )
                state = self._read_state(connection, candidate=candidate)
                if (
                    state is not None
                    and epoch.epoch_fingerprint
                    == candidate.expected_admission_epoch_fingerprint
                ):
                    return self._outcome(
                        confirmation=DurableProjectionCommitConfirmation.FULL,
                        candidate=candidate,
                        resulting_state=state,
                        physical_disposition="exact_confirmed",
                    )
                return self._outcome(
                    confirmation=DurableProjectionCommitConfirmation.CONFLICT,
                    candidate=candidate,
                    resulting_state=None,
                    physical_disposition=None,
                    failure=error,
                )
        except BaseException as confirmation_error:
            return self._outcome(
                confirmation=DurableProjectionCommitConfirmation.UNRESOLVED,
                candidate=candidate,
                resulting_state=None,
                physical_disposition=None,
                failure=confirmation_error,
            )

    @staticmethod
    def _install_required_cutovers(
        connection: Connection,
        *,
        candidate: RuntimeSessionOwnerBootstrapCandidateFact,
    ) -> None:
        active, pre_activation = (
            PostgresRuntimeSessionOwnerBootstrapPort._expected_cutovers(
                connection,
                candidate=candidate,
            )
        )
        for cutover in active:
            connection.execute(
                """
                INSERT INTO public.durable_projection_session_cutovers (
                    runtime_session_id,
                    projection_kind,
                    cutover_through_sequence,
                    cutover_payload,
                    cutover_fingerprint
                ) VALUES (%s, %s, 0, %s, %s)
                ON CONFLICT (runtime_session_id, projection_kind) DO NOTHING
                """,
                (
                    cutover.runtime_session_id,
                    cutover.projection_kind.value,
                    Jsonb(cutover.model_dump(mode="json")),
                    cutover.cutover_fingerprint,
                ),
            )
        for cutover in pre_activation:
            connection.execute(
                """
                INSERT INTO public.durable_projection_pre_activation_session_cutovers (
                    runtime_session_id,
                    projection_kind,
                    cutover_payload,
                    cutover_fingerprint
                ) VALUES (%s, %s, %s, %s)
                ON CONFLICT (runtime_session_id, projection_kind) DO NOTHING
                """,
                (
                    cutover.runtime_session_id,
                    cutover.projection_kind.value,
                    Jsonb(cutover.model_dump(mode="json")),
                    cutover.cutover_fingerprint,
                ),
            )
        account = candidate.background_budget_account
        policy = candidate.background_budget_policy
        connection.execute(
            """
            INSERT INTO public.background_derived_work_budget_accounts (
                runtime_session_id,
                policy_payload,
                policy_fingerprint,
                account_revision,
                account_payload,
                account_fingerprint
            ) VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (runtime_session_id) DO NOTHING
            """,
            (
                account.runtime_session_id,
                Jsonb(policy.model_dump(mode="json")),
                policy.policy_fingerprint,
                account.account_revision,
                Jsonb(account.model_dump(mode="json")),
                account.account_fingerprint,
            ),
        )
        from pulsara_agent.storage.prompt_queue_bootstrap import (
            install_prompt_queue_genesis,
        )

        install_prompt_queue_genesis(
            connection,
            runtime_session_id=candidate.session_owner.runtime_session_id,
            require_empty_queue_chain=True,
        )

    @staticmethod
    def _expected_cutovers(
        connection: Connection,
        *,
        candidate: RuntimeSessionOwnerBootstrapCandidateFact,
    ) -> tuple[
        tuple[DurableProjectionSessionCutoverFact, ...],
        tuple[PreActivationProjectionSessionCutoverFact, ...],
    ]:
        active_rows = tuple(
            connection.execute(
                """
                SELECT activation_payload
                FROM public.durable_projection_kind_activations
                ORDER BY projection_kind
                """
            ).fetchall()
        )
        pre_rows = tuple(
            connection.execute(
                """
                SELECT contract_payload
                FROM public.durable_projection_pre_activation_contracts
                ORDER BY projection_kind
                """
            ).fetchall()
        )
        active = tuple(
            DurableProjectionKindActivationFact.model_validate(row[0])
            for row in active_rows
        )
        pre_activation = tuple(
            PreActivationProjectionHookContractFact.model_validate(row[0])
            for row in pre_rows
        )
        active_kinds = {item.activation_semantic.projection_kind for item in active}
        pre_kinds = {item.contract_semantic.projection_kind for item in pre_activation}
        if active_kinds & pre_kinds:
            raise ValueError("projection kind has active and pre-activation authority")
        expected_active: list[DurableProjectionSessionCutoverFact] = []
        for activation in active:
            cutover = cast(
                DurableProjectionSessionCutoverFact,
                build_projection_fact(
                    DurableProjectionSessionCutoverFact,
                    schema_version="durable_projection_session_cutover.v1",
                    runtime_session_id=candidate.session_owner.runtime_session_id,
                    projection_kind=(activation.activation_semantic.projection_kind),
                    cutover_through_sequence=0,
                    cutover_ledger_continuity_accumulator=(
                        EMPTY_LEDGER_CONTINUITY_ACCUMULATOR
                    ),
                    cutover_ledger_payload_prefix_bytes=0,
                    cutover_transcript_semantic_prefix_count=0,
                    cutover_transcript_semantic_prefix_accumulator=(
                        EMPTY_TRANSCRIPT_SEMANTIC_ACCUMULATOR
                    ),
                    migration_version=activation.activation_migration_version,
                    migration_registry_prefix_fingerprint=(
                        activation.resulting_migration_registry_prefix_fingerprint
                    ),
                    activation_fingerprint=activation.activation_fingerprint,
                    seed_contract_fingerprint=(
                        activation.activation_semantic.seed_contract.seed_contract_fingerprint
                    ),
                    cutover_policy_id="post_cutover_events_only",
                ),
            )
            expected_active.append(cutover)
        expected_pre_activation: list[PreActivationProjectionSessionCutoverFact] = []
        for contract in pre_activation:
            semantic = contract.contract_semantic
            cutover = cast(
                PreActivationProjectionSessionCutoverFact,
                build_projection_fact(
                    PreActivationProjectionSessionCutoverFact,
                    schema_version=("pre_activation_projection_session_cutover.v1"),
                    runtime_session_id=candidate.session_owner.runtime_session_id,
                    projection_kind=semantic.projection_kind,
                    pre_activation_contract_fingerprint=(contract.contract_fingerprint),
                    cutover_through_sequence=0,
                    cutover_ledger_continuity_accumulator=(
                        EMPTY_LEDGER_CONTINUITY_ACCUMULATOR
                    ),
                    cutover_ledger_payload_prefix_bytes=0,
                    cutover_transcript_semantic_prefix_count=0,
                    cutover_transcript_semantic_prefix_accumulator=(
                        EMPTY_TRANSCRIPT_SEMANTIC_ACCUMULATOR
                    ),
                    migration_version=contract.installation_migration_version,
                    migration_registry_prefix_fingerprint=(
                        contract.resulting_migration_registry_prefix_fingerprint
                    ),
                ),
            )
            expected_pre_activation.append(cutover)
        return tuple(expected_active), tuple(expected_pre_activation)

    @staticmethod
    def _read_state(
        connection: Connection,
        *,
        candidate: RuntimeSessionOwnerBootstrapCandidateFact,
    ) -> RuntimeSessionBootstrapStateFact | None:
        session_row = connection.execute(
            """
            SELECT id, workspace_root
            FROM public.sessions
            WHERE id = %s
            """,
            (candidate.session_owner.runtime_session_id,),
        ).fetchone()
        if session_row is None or (
            str(session_row[0]) != candidate.session_owner.runtime_session_id
            or session_row[1] != candidate.session_owner.workspace_root
        ):
            return None
        expected_active, expected_pre = (
            PostgresRuntimeSessionOwnerBootstrapPort._expected_cutovers(
                connection,
                candidate=candidate,
            )
        )
        active_rows = tuple(
            connection.execute(
                """
                SELECT cutover_payload, cutover_fingerprint
                FROM public.durable_projection_session_cutovers
                WHERE runtime_session_id = %s
                ORDER BY projection_kind
                """,
                (candidate.session_owner.runtime_session_id,),
            ).fetchall()
        )
        active_cutovers = tuple(
            DurableProjectionSessionCutoverFact.model_validate(row[0])
            for row in active_rows
        )
        pre_rows = tuple(
            connection.execute(
                """
                SELECT cutover_payload, cutover_fingerprint
                FROM public.durable_projection_pre_activation_session_cutovers
                WHERE runtime_session_id = %s
                ORDER BY projection_kind
                """,
                (candidate.session_owner.runtime_session_id,),
            ).fetchall()
        )
        pre_cutovers = tuple(
            PreActivationProjectionSessionCutoverFact.model_validate(row[0])
            for row in pre_rows
        )
        if (
            len(active_rows) != len(expected_active)
            or len(pre_rows) != len(expected_pre)
            or any(
                item.cutover_fingerprint != str(row[1])
                for item, row in zip(active_cutovers, active_rows, strict=True)
            )
            or any(
                item.cutover_fingerprint != str(row[1])
                for item, row in zip(pre_cutovers, pre_rows, strict=True)
            )
            or active_cutovers != expected_active
            or pre_cutovers != expected_pre
        ):
            return None
        budget_row = connection.execute(
            """
            SELECT policy_payload, policy_fingerprint, account_revision,
                   account_payload, account_fingerprint
            FROM public.background_derived_work_budget_accounts
            WHERE runtime_session_id = %s
            """,
            (candidate.session_owner.runtime_session_id,),
        ).fetchone()
        if budget_row is None or (
            budget_row[0] != candidate.background_budget_policy.model_dump(mode="json")
            or str(budget_row[1])
            != candidate.background_budget_policy.policy_fingerprint
        ):
            return None
        from pulsara_agent.storage.prompt_queue_bootstrap import (
            read_prompt_queue_genesis,
        )

        queue_account, queue_checkpoint = read_prompt_queue_genesis(
            connection,
            runtime_session_id=candidate.session_owner.runtime_session_id,
        )
        if (
            queue_account is None
            or queue_checkpoint is None
            or queue_account.checkpoint_generation
            != queue_checkpoint.checkpoint_generation
            or queue_account.checkpoint_through_sequence
            != queue_checkpoint.through_sequence
            or queue_account.checkpoint_fingerprint
            != queue_checkpoint.checkpoint_fingerprint
        ):
            return None
        try:
            current_budget_account = (
                BackgroundDerivedWorkBudgetAccountFact.model_validate(budget_row[3])
            )
        except (TypeError, ValueError):
            return None
        if (
            current_budget_account.runtime_session_id
            != candidate.session_owner.runtime_session_id
            or current_budget_account.policy_fingerprint
            != candidate.background_budget_policy.policy_fingerprint
            or int(budget_row[2]) != current_budget_account.account_revision
            or str(budget_row[4]) != current_budget_account.account_fingerprint
        ):
            return None
        active_fingerprints = tuple(
            item.cutover_fingerprint for item in active_cutovers
        )
        pre_fingerprints = tuple(item.cutover_fingerprint for item in pre_cutovers)
        return cast(
            RuntimeSessionBootstrapStateFact,
            build_projection_fact(
                RuntimeSessionBootstrapStateFact,
                schema_version="runtime_session_bootstrap_state.v1",
                session_owner=candidate.session_owner,
                ordered_active_cutover_fingerprints=active_fingerprints,
                ordered_pre_activation_cutover_fingerprints=pre_fingerprints,
                cutover_set_accumulator=context_fingerprint(
                    "runtime-session-bootstrap-cutover-set:v1",
                    {
                        "active": active_fingerprints,
                        "pre_activation": pre_fingerprints,
                    },
                ),
                background_budget_account_fingerprint=(
                    current_budget_account.account_fingerprint
                ),
                admission_epoch_fingerprint=(
                    candidate.expected_admission_epoch_fingerprint
                ),
            ),
        )

    @staticmethod
    def _outcome(
        *,
        confirmation: DurableProjectionCommitConfirmation,
        candidate: RuntimeSessionOwnerBootstrapCandidateFact,
        resulting_state: RuntimeSessionBootstrapStateFact | None,
        physical_disposition: str | None,
        failure: BaseException | None = None,
    ) -> RuntimeSessionBootstrapCommitOutcomeFact:
        return cast(
            RuntimeSessionBootstrapCommitOutcomeFact,
            build_projection_fact(
                RuntimeSessionBootstrapCommitOutcomeFact,
                schema_version="runtime_session_bootstrap_commit_outcome.v1",
                confirmation=confirmation,
                attempted_candidate_fingerprint=candidate.candidate_fingerprint,
                resulting_state=resulting_state,
                physical_disposition=physical_disposition,
                failure=(
                    build_bounded_runtime_failure_diagnostic(
                        error=failure,
                        redaction_profile_id="runtime_session_bootstrap_error.v1",
                    )
                    if failure is not None
                    else None
                ),
            ),
        )

    @staticmethod
    def _require_remaining(deadline_monotonic: float) -> None:
        if monotonic() >= deadline_monotonic:
            raise TimeoutError("runtime session bootstrap deadline exceeded")


__all__ = [
    "PostgresRuntimeSessionOwnerBootstrapPort",
    "build_runtime_session_bootstrap_candidate",
    "build_runtime_session_owner_semantic",
]
