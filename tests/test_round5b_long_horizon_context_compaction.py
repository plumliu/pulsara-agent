"""Round 5B long-horizon compaction contracts and process-local ownership."""

from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pulsara_agent.conversation_kernel.compaction.contracts import (
    CompactionActiveRequestLocation,
    CompactionCanonicalAdoptionFactoryInput,
    CompactionConfirmationKind,
    CompactionContinuationMode,
    CompactionDisposition,
    CompactionOutcome,
    CompactionScope,
    CompactionTargetBranch,
    CompactionTrigger,
    ExpectedCompactionPredecessorRevision,
    FrozenCompactionActiveRequest,
    ResolvedCompactionPolicy,
    build_prepared_compaction_canonical_adoption,
    build_prepared_manual_compaction_command,
)
from pulsara_agent.conversation_kernel.contracts import InlineContent
from pulsara_agent.conversation_kernel.assembler import (
    MAXIMUM_COMPLETED_ASSISTANT_MESSAGE_UTF8_BYTES,
)
from pulsara_agent.conversation_kernel.compaction.planner import (
    CompactionPlanningError,
    CompactionReclaimUnavailable,
    crosses_compaction_resource_headroom,
    estimate_unavoidable_compaction_successor_tokens,
    enumerate_complete_tool_groups,
    freeze_compaction_continuation,
    rebase_compaction_dispatch_read_through_sequence,
    resolved_compaction_headroom_bounds,
    should_trigger_compaction,
    validate_compaction_reclaim,
)
from pulsara_agent.conversation_kernel.compaction.prompt import (
    build_compaction_snapshot_carrier,
    compaction_summary_request,
    freeze_compaction_summary_output,
    parse_compaction_snapshot_carrier,
)
from pulsara_agent.conversation_kernel.compaction.runtime import (
    HostCompactionRuntimeOwner,
)
from pulsara_agent.conversation_kernel.host import KernelHostSession
from pulsara_agent.conversation_kernel.limits import STAGE2_LIMITS
from pulsara_agent.conversation_kernel.compaction.runtime_handoff import (
    CompactionRuntimeHandoffBoundError,
    FrozenTerminalMonitorHandoffFact,
    FrozenTerminalProcessHandoffFact,
    freeze_compaction_runtime_handoff,
    freeze_subagent_task_board_fact,
)
from pulsara_agent.conversation_kernel.compaction.retained_skill import (
    FrozenRetainedSkillContextItem,
    FrozenRetainedSkillContextSelection,
    _exact_historical_read,
    _historical_catalog_row,
    _installed_observation_fingerprint,
    _installed_retained_items,
    _was_installed_full,
    remove_full_tail_duplicates,
)
from pulsara_agent.conversation_kernel.vocabulary import (
    APPEND_GUARDS,
    COMMITTED_EVENT_DESCRIPTORS,
    LIVE_EVENT_TYPES,
    SUBJECT_SLOTS,
)
from pulsara_agent.llm.estimator import PulsaraHeuristicTokenEstimatorV1
from pulsara_agent.model_input.contracts import (
    CanonicalInputOriginKind,
    CanonicalModelInputIdentity,
    CanonicalModelInputSnapshot,
    FrozenProviderInputItem,
    FrozenProviderInputItemKind,
    ContextSourceKind,
    ContextTrustClass,
    ModelInputScopeKind,
    ProviderToolCall,
    ProviderToolResultContextMetadata,
    STRUCTURED_MODEL_INPUT_LIMITS,
    ToolResultProviderRenderMode,
    canonical_model_input_identity_fingerprint,
    canonical_model_input_snapshot_fingerprint,
    provider_input_item_fingerprint,
)
from pulsara_agent.model_input.continuity import (
    ProviderInputContinuityScope,
    SourceObservationLifecycle,
    SourceObservationPresence,
    encode_runtime_observation,
)
from pulsara_agent.model_input.lowering import lower_canonical_item
from pulsara_agent.model_input.provider_replay import (
    FrozenCanonicalProviderDispatchRead,
    freeze_provider_replay_manifest_cut,
)
from pulsara_agent.ports.artifact import (
    ToolOutputArtifactDisposition,
    ToolOutputSourceCoverage,
    ToolResultDisplayKind,
)
from pulsara_agent.primitives.context import (
    canonical_json_bytes,
    context_fingerprint,
    freeze_json,
)
from pulsara_agent.primitives.tool_observation import (
    FrozenToolObservationTimingFact,
    ToolObservationDurationDisposition,
    ToolObservationOrigin,
    tool_observation_timing_fingerprint,
)
from pulsara_agent.storage.migrations.manifest import (
    CONVERSATION_KERNEL_RELATIONS,
)
from tests.support.round3 import static_canonical_compile_facts


ROOT = Path(__file__).resolve().parents[1]


def test_round5b_manual_command_settlement_keys_the_exact_semantic_digest() -> None:
    host = object.__new__(KernelHostSession)
    host._manual_compaction_command_attempts = {}
    host._compaction = HostCompactionRuntimeOwner()
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def settle(candidate):
        calls.append(candidate.semantic_digest)
        started.set()
        await release.wait()
        return CompactionConfirmationKind.FULL

    host._settle_manual_compaction_command_worker = settle
    exact = build_prepared_manual_compaction_command(
        session_id="session:test",
        command_id="command:compact",
        scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
        target_turn_id="turn:test",
        expected_active_turn_id="turn:test",
        force=True,
    )
    conflict = build_prepared_manual_compaction_command(
        session_id="session:test",
        command_id="command:compact",
        scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
        target_turn_id="turn:test",
        expected_active_turn_id="turn:test",
        force=False,
    )

    async def exercise() -> tuple[CompactionConfirmationKind, ...]:
        first = asyncio.create_task(host._settle_manual_compaction_command(exact))
        await started.wait()
        duplicate = asyncio.create_task(host._settle_manual_compaction_command(exact))
        conflicting = await host._settle_manual_compaction_command(conflict)
        release.set()
        results = (await first, await duplicate, conflicting)
        await host._compaction.aclose()
        return results

    assert asyncio.run(exercise()) == (
        CompactionConfirmationKind.FULL,
        CompactionConfirmationKind.FULL,
        CompactionConfirmationKind.CONFLICT,
    )
    assert calls == [exact.semantic_digest]


def test_round5b_compaction_cut_rebase_only_advances_global_sequence() -> None:
    item = FrozenProviderInputItem(
        item_kind=FrozenProviderInputItemKind.USER,
        source_entry_id="entry:root:1",
        source_entry_sequence=1,
        source_turn_id="turn:root",
        text="root exact-scope history",
        input_origin=CanonicalInputOriginKind.HUMAN_MESSAGE,
    )
    identity_values = {
        "session_id": "session:test",
        "turn_id": "turn:root",
        "initial_entry_id": "entry:root:1",
        "context_binding_revision_id": "binding:root:1",
        "provider_input_through_sequence": 1,
        "conversation_scope_kind": ModelInputScopeKind.ROOT,
        "scope_subagent_task_id": None,
    }
    identity = CanonicalModelInputIdentity(
        **identity_values,
        identity_fingerprint=canonical_model_input_identity_fingerprint(
            **identity_values
        ),
    )
    canonical_bytes = len(item.text.encode("utf-8"))
    snapshot = CanonicalModelInputSnapshot(
        identity=identity,
        items=(item,),
        canonical_utf8_bytes=canonical_bytes,
        snapshot_fingerprint=canonical_model_input_snapshot_fingerprint(
            identity=identity,
            items=(item,),
            canonical_utf8_bytes=canonical_bytes,
            closures=(),
            late_outcomes=(),
        ),
    )
    compile_snapshot = static_canonical_compile_facts(snapshot)
    manifest_cut = freeze_provider_replay_manifest_cut(
        session_id=identity.session_id,
        scope=ProviderInputContinuityScope(
            session_id=identity.session_id,
            scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        ),
        context_binding_revision_id=identity.context_binding_revision_id,
        provider_input_through_sequence=1,
        manifests=(),
    )
    dispatch = FrozenCanonicalProviderDispatchRead(
        compile_snapshot=compile_snapshot,
        replay_manifest_cut=manifest_cut,
        composite_fingerprint=context_fingerprint(
            "pulsara.canonical-provider-dispatch-read:v1",
            {
                "compile": compile_snapshot.canonical_read_cut_fingerprint,
                "replay_manifest_cut": manifest_cut.cut_fingerprint,
            },
        ),
    )

    assert (
        rebase_compaction_dispatch_read_through_sequence(
            dispatch, provider_input_through_sequence=1
        )
        == dispatch
    )
    rebased = rebase_compaction_dispatch_read_through_sequence(
        dispatch, provider_input_through_sequence=9
    )
    assert rebased != dispatch
    assert rebased.compile_snapshot.canonical_input.items == snapshot.items
    assert rebased.compile_snapshot.canonical_input.closures == snapshot.closures
    assert (
        rebased.compile_snapshot.canonical_input.late_outcomes == snapshot.late_outcomes
    )
    assert rebased.replay_manifest_cut.manifests == manifest_cut.manifests
    assert (
        rebased.compile_snapshot.context_binding_fact
        == compile_snapshot.context_binding_fact
    )
    assert (
        rebased.compile_snapshot.canonical_input.identity.provider_input_through_sequence
        == 9
    )
    assert rebased.replay_manifest_cut.provider_input_through_sequence == 9
    assert (
        rebased.compile_snapshot.canonical_read_cut_fingerprint
        != compile_snapshot.canonical_read_cut_fingerprint
    )
    assert rebased.composite_fingerprint != dispatch.composite_fingerprint
    with pytest.raises(ValueError, match="cannot move backwards"):
        rebase_compaction_dispatch_read_through_sequence(
            dispatch, provider_input_through_sequence=0
        )


def test_round5b_first_full_history_adoption_uses_zero_effective_floor() -> None:
    """The revision-zero marker is an exact row identity, not a range floor."""

    scope = CompactionScope(
        session_id="session:test",
        workspace_id="workspace:test",
        turn_id="turn:late",
        scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
    )

    def prepare(base_kind: str, context_snapshot_id: str | None) -> object:
        return build_prepared_compaction_canonical_adoption(
            CompactionCanonicalAdoptionFactoryInput(
                scope=scope,
                target_branch=CompactionTargetBranch.ACTIVE_INSTALLATION,
                expected_turn_status="RUNNING",
                predecessor=ExpectedCompactionPredecessorRevision(
                    binding_revision_id="binding:predecessor",
                    revision_ordinal=0,
                    base_kind=base_kind,
                    context_snapshot_id=context_snapshot_id,
                    # A late-created turn can have a revision-zero marker above
                    # the earliest protected same-scope history boundary.
                    source_through_sequence=100,
                ),
                snapshot_id="snapshot:new",
                binding_revision_id="binding:new",
                event_id="event:new",
                source_through_sequence=40,
                source_digest="sha256:" + "a" * 64,
                snapshot_content=InlineContent.from_bytes(b"summary"),
                compiler_contract="compiler:test",
                prompt_contract="prompt:test",
                model_contract="model:test",
                occurred_at=datetime.now(timezone.utc),
                actor_id="runtime:test",
            )
        )

    candidate = prepare("FULL_HISTORY", None)
    assert candidate.predecessor.source_through_sequence == 100
    assert candidate.predecessor.effective_materialization_lineage_floor == 0
    assert candidate.snapshot.source_through_sequence == 40

    with pytest.raises(ValueError, match="factory input"):
        prepare("SNAPSHOT", "snapshot:existing")


def test_round5b_summary_normalizer_and_snapshot_carrier_are_bounded() -> None:
    raw = (
        "\ufeff<analysis>private scratch</analysis>\r\n"
        "```xml\r\n<summary>紧凑交接 x </summary> y</summary>\r\n```"
    )
    summary = freeze_compaction_summary_output(raw, maximum_utf8_bytes=65_536)
    assert "<\\/summary>" in summary.body
    assert "analysis" not in summary.body

    carrier = build_compaction_snapshot_carrier(
        summary=summary,
        recent_user_messages=("第一条", "second"),
        continuation_mode=CompactionContinuationMode.RESUME_ACTIVE_TURN,
        active_request=FrozenCompactionActiveRequest(
            entry_id="entry:active",
            entry_sequence=7,
            location=CompactionActiveRequestLocation.SNAPSHOT_EXACT,
            text="current exact request",
        ),
    )
    decoded = json.loads(carrier.body)
    assert set(decoded) == {
        "continuation",
        "earlier_context_summary",
        "recent_user_messages",
    }
    assert decoded["continuation"]["mode"] == "RESUME_ACTIVE_TURN"
    assert decoded["continuation"]["instruction"].startswith(
        "HANDOFF COMPLETE / RESUME NOW"
    )
    assert "newer canonical turn activation" in decoded["continuation"]["instruction"]
    assert decoded["continuation"]["active_request"] == {
        "entry_id": "entry:active",
        "entry_sequence": 7,
        "location": "SNAPSHOT_EXACT",
        "text": "current exact request",
    }
    assert decoded["recent_user_messages"] == ["第一条", "second"]
    assert "body_digest" not in decoded
    assert parse_compaction_snapshot_carrier(carrier.body) == carrier

    idle = build_compaction_snapshot_carrier(
        summary=summary,
        recent_user_messages=("第一条",),
        continuation_mode=CompactionContinuationMode.AWAIT_NEXT_USER,
        active_request=None,
    )
    idle_decoded = json.loads(idle.body)
    assert idle_decoded["continuation"]["mode"] == "AWAIT_NEXT_USER"
    assert idle_decoded["continuation"]["active_request"] is None
    assert "survives a process restart" in idle_decoded["continuation"]["instruction"]
    assert parse_compaction_snapshot_carrier(idle.body) == idle
    with pytest.raises(ValueError, match="fields"):
        parse_compaction_snapshot_carrier(
            b'{"earlier_context_summary":"legacy","recent_user_messages":[]}'
        )


def test_round5b_summary_prompt_keeps_lifecycle_out_of_compaction() -> None:
    request = compaction_summary_request()

    assert "restrictions above" in request
    assert "end when this summary response" in request
    assert "Never say that a user request is queued, deferred, blocked" in request
    assert "Runtime separately and mechanically owns" in request
    assert "Do not infer either lifecycle" in request
    assert "ACTIVE-TURN HANDOFF" not in request
    assert "IDLE HANDOFF" not in request


def test_round5b_repeated_compaction_carries_runtime_owned_active_request() -> None:
    summary = freeze_compaction_summary_output("old handoff", maximum_utf8_bytes=100)
    carrier = build_compaction_snapshot_carrier(
        summary=summary,
        recent_user_messages=(),
        continuation_mode=CompactionContinuationMode.RESUME_ACTIVE_TURN,
        active_request=FrozenCompactionActiveRequest(
            entry_id="entry:active",
            entry_sequence=4,
            location=CompactionActiveRequestLocation.SNAPSHOT_EXACT,
            text="exact active request",
        ),
    )
    snapshot_item = FrozenProviderInputItem(
        item_kind=FrozenProviderInputItemKind.CONTEXT_SNAPSHOT,
        source_entry_id=None,
        source_entry_sequence=8,
        source_turn_id=None,
        text=carrier.body.decode("utf-8"),
    )
    source_view = SimpleNamespace(
        canonical_dispatch_read=SimpleNamespace(
            compile_snapshot=SimpleNamespace(
                canonical_input=SimpleNamespace(
                    identity=SimpleNamespace(
                        initial_entry_id="entry:active",
                        provider_input_through_sequence=12,
                    ),
                    items=(snapshot_item,),
                )
            )
        )
    )

    mode, active = freeze_compaction_continuation(
        source_view=source_view,
        target_branch=CompactionTargetBranch.ACTIVE_INSTALLATION,
        source_through_sequence=12,
    )

    assert mode is CompactionContinuationMode.RESUME_ACTIVE_TURN
    assert active is not None
    assert active.entry_id == "entry:active"
    assert active.location is CompactionActiveRequestLocation.SNAPSHOT_EXACT
    assert active.text == "exact active request"


def test_round5b_non_human_initial_activation_is_mechanically_resumable() -> None:
    plan_item = FrozenProviderInputItem(
        item_kind=FrozenProviderInputItemKind.PLAN_CONTINUATION,
        source_entry_id="entry:plan",
        source_entry_sequence=9,
        source_turn_id="turn:plan",
        text='{"plan_continuation":"resume approved plan"}',
        input_origin=CanonicalInputOriginKind.PLAN_CONTINUATION,
    )
    source_view = SimpleNamespace(
        canonical_dispatch_read=SimpleNamespace(
            compile_snapshot=SimpleNamespace(
                canonical_input=SimpleNamespace(
                    identity=SimpleNamespace(
                        initial_entry_id="entry:plan",
                        provider_input_through_sequence=9,
                    ),
                    items=(plan_item,),
                )
            )
        )
    )

    mode, active = freeze_compaction_continuation(
        source_view=source_view,
        target_branch=CompactionTargetBranch.ACTIVE_INSTALLATION,
        source_through_sequence=9,
    )

    assert mode is CompactionContinuationMode.RESUME_ACTIVE_TURN
    assert active is not None
    assert active.location is CompactionActiveRequestLocation.SNAPSHOT_EXACT
    assert active.text == plan_item.text


@pytest.mark.parametrize(
    ("raw", "expected"),
    (
        ("plain text", "plain text"),
        ("<summary>free-form paragraph</summary>", "free-form paragraph"),
        ("```markdown\n- progress\n- next step\n```", "- progress\n- next step"),
        (
            "<summary>没有固定标题，也可以自然成段。</summary>",
            "没有固定标题，也可以自然成段。",
        ),
    ),
)
def test_round5b_summary_normalizer_accepts_guided_freeform_text(
    raw: str,
    expected: str,
) -> None:
    summary = freeze_compaction_summary_output(raw, maximum_utf8_bytes=65_536)
    assert summary.body == expected


def test_round5b_summary_has_no_compaction_specific_64k_cap() -> None:
    raw = "交接" * 25_000
    assert len(raw.encode("utf-8")) > 65_536
    summary = freeze_compaction_summary_output(
        raw,
        maximum_utf8_bytes=MAXIMUM_COMPLETED_ASSISTANT_MESSAGE_UTF8_BYTES,
    )
    assert summary.body == raw


@pytest.mark.parametrize(
    "raw",
    (
        "",
        " \r\n\t",
        "x" * (MAXIMUM_COMPLETED_ASSISTANT_MESSAGE_UTF8_BYTES + 1),
    ),
)
def test_round5b_summary_normalizer_rejects_only_empty_or_overbound_text(
    raw: str,
) -> None:
    with pytest.raises(ValueError, match="empty|exceeds"):
        freeze_compaction_summary_output(
            raw,
            maximum_utf8_bytes=MAXIMUM_COMPLETED_ASSISTANT_MESSAGE_UTF8_BYTES,
        )


def test_round5b_reclaim_force_only_bypasses_soft_target() -> None:
    policy = ResolvedCompactionPolicy(minimum_reclaim_tokens=100)
    with pytest.raises(CompactionReclaimUnavailable, match="enough"):
        validate_compaction_reclaim(
            source_tokens=1_000,
            successor_tokens=950,
            hard_input_budget_tokens=2_000,
            policy=policy,
            force=True,
            enforce_soft_target=True,
        )
    with pytest.raises(CompactionReclaimUnavailable, match="does not reclaim context"):
        validate_compaction_reclaim(
            source_tokens=1_000,
            successor_tokens=1_000,
            hard_input_budget_tokens=2_000,
            policy=policy,
            force=True,
            enforce_soft_target=True,
        )
    with pytest.raises(CompactionPlanningError, match="post target"):
        validate_compaction_reclaim(
            source_tokens=1_900,
            successor_tokens=1_200,
            hard_input_budget_tokens=2_000,
            policy=policy,
            force=False,
            enforce_soft_target=True,
        )
    assert (
        validate_compaction_reclaim(
            source_tokens=1_900,
            successor_tokens=1_200,
            hard_input_budget_tokens=2_000,
            policy=policy,
            force=True,
            enforce_soft_target=True,
        )
        == 700
    )
    with pytest.raises(CompactionPlanningError, match="hard model budget"):
        validate_compaction_reclaim(
            source_tokens=3_000,
            successor_tokens=2_001,
            hard_input_budget_tokens=2_000,
            policy=policy,
            force=True,
            enforce_soft_target=True,
        )


def test_round5b_longest_suffix_skips_impossible_old_tool_tail_before_open() -> None:
    """Old retained groups must not protect a later near-budget transcript."""

    fingerprint = "sha256:" + "a" * 64
    large_users = tuple(
        FrozenProviderInputItem(
            item_kind=FrozenProviderInputItemKind.USER,
            source_entry_id=f"entry:{index}",
            source_entry_sequence=index,
            source_turn_id=f"turn:{index}",
            text="长" * 48_000,
            input_origin=CanonicalInputOriginKind.HUMAN_MESSAGE,
        )
        for index in range(1, 16)
    )
    source_view = SimpleNamespace(
        source_view_fingerprint=fingerprint,
        canonical_dispatch_read=SimpleNamespace(
            compile_snapshot=SimpleNamespace(
                canonical_input=SimpleNamespace(items=large_users)
            )
        ),
        normal_compile_binding=SimpleNamespace(
            estimator=PulsaraHeuristicTokenEstimatorV1(),
        ),
    )
    old_group_tail = SimpleNamespace(
        source_view_fingerprint=fingerprint,
        source_through_sequence=0,
    )
    no_group_tail = SimpleNamespace(
        source_view_fingerprint=fingerprint,
        source_through_sequence=15,
    )
    deadline = 1e30
    protected_quote = estimate_unavoidable_compaction_successor_tokens(
        source_view=source_view,
        tail=old_group_tail,
        recent_user_messages=(),
        continuation_mode=CompactionContinuationMode.AWAIT_NEXT_USER,
        active_request=None,
        deadline_monotonic=deadline,
    )
    compacted_quote = estimate_unavoidable_compaction_successor_tokens(
        source_view=source_view,
        tail=no_group_tail,
        recent_user_messages=(),
        continuation_mode=CompactionContinuationMode.AWAIT_NEXT_USER,
        active_request=None,
        deadline_monotonic=deadline,
    )
    policy = ResolvedCompactionPolicy()

    with pytest.raises(CompactionPlanningError, match="post target"):
        validate_compaction_reclaim(
            source_tokens=207_600,
            successor_tokens=protected_quote,
            hard_input_budget_tokens=239_616,
            policy=policy,
            force=False,
            enforce_soft_target=True,
        )
    assert (
        validate_compaction_reclaim(
            source_tokens=207_600,
            successor_tokens=compacted_quote,
            hard_input_budget_tokens=239_616,
            policy=policy,
            force=False,
            enforce_soft_target=True,
        )
        > 0
    )


def test_round5b_trigger_uses_exact_prepared_target_budget_without_262k_cap() -> None:
    source_view = SimpleNamespace(
        normal_compile_binding=SimpleNamespace(
            effective_input_budget_tokens=400_000,
        ),
        provider_projection=SimpleNamespace(
            final_estimate=SimpleNamespace(total_input_tokens=250_000),
        ),
        physical_working_set=SimpleNamespace(
            post_base_item_count=1,
            post_base_canonical_utf8_bytes=1,
            continuity_epoch_logical_utf8_bytes=1,
        ),
    )

    assert not should_trigger_compaction(
        source_view=source_view,
        policy=ResolvedCompactionPolicy(auto_trigger_ratio=0.85),
        force=False,
    )


def test_round5b_resource_headroom_exact_boundaries() -> None:
    bounds = resolved_compaction_headroom_bounds()
    maximum_admission = max(
        STAGE2_LIMITS.prompt_hard_bytes,
        MAXIMUM_COMPLETED_ASSISTANT_MESSAGE_UTF8_BYTES,
        STAGE2_LIMITS.tool_result_hard_bytes,
    )
    assert bounds.reserved_canonical_utf8_bytes == maximum_admission
    assert bounds.reserved_epoch_logical_utf8_bytes == maximum_admission
    assert (
        bounds.soft_canonical_utf8_byte_limit
        + MAXIMUM_COMPLETED_ASSISTANT_MESSAGE_UTF8_BYTES
        <= bounds.maximum_canonical_utf8_bytes
    )
    assert not crosses_compaction_resource_headroom(
        post_base_item_count=bounds.soft_canonical_item_limit - 1,
        post_base_canonical_utf8_bytes=bounds.soft_canonical_utf8_byte_limit - 1,
        continuity_epoch_logical_utf8_bytes=(
            bounds.soft_epoch_logical_utf8_byte_limit - 1
        ),
    )
    assert crosses_compaction_resource_headroom(
        post_base_item_count=bounds.soft_canonical_item_limit,
        post_base_canonical_utf8_bytes=0,
        continuity_epoch_logical_utf8_bytes=0,
    )
    assert crosses_compaction_resource_headroom(
        post_base_item_count=0,
        post_base_canonical_utf8_bytes=14 << 20,
        continuity_epoch_logical_utf8_bytes=0,
    )


def test_round5b_terminal_provider_race_handoffs_manual_to_idle_owner() -> None:
    owner = HostCompactionRuntimeOwner(
        policy=ResolvedCompactionPolicy(automatic_enabled=False)
    )
    host = object.__new__(KernelHostSession)
    host.session_id = "session:test"
    host._lock = asyncio.Lock()
    host._closing = False
    host._pending_root_successor = None
    host._active_turn_id = "turn:terminal"
    host._active_cancellation_intent = None
    host._active_command_id = "command:turn"
    host._active_root_phase = object()
    host._queue_wake = asyncio.Event()
    host._monitor_wake = asyncio.Event()
    host._compaction = owner
    idle_calls: list[tuple[str, str, bool]] = []

    class _Compaction:
        async def compact_idle_turn(
            self, *, turn_id: str, command_id: str, force: bool
        ) -> CompactionOutcome:
            idle_calls.append((turn_id, command_id, force))
            return CompactionOutcome(
                CompactionDisposition.COMPACTED,
                turn_id,
                "snapshot:test",
                1,
                "COMPACTED",
            )

    class _Runner:
        async def compact_idle_turn(
            self, *, turn_id: str, command_id: str, force: bool
        ) -> CompactionOutcome:
            return await _Compaction().compact_idle_turn(
                turn_id=turn_id,
                command_id=command_id,
                force=force,
            )

    idle_marks: list[str] = []
    host._runner = _Runner()
    host._tools = SimpleNamespace(
        todo_owner=SimpleNamespace(
            mark_root_idle=lambda *, exact_turn_id: idle_marks.append(exact_turn_id)
        )
    )

    async def no_command(_command_id: str):
        return None

    async def full_confirmation(_candidate):
        from pulsara_agent.conversation_kernel.compaction.contracts import (
            CompactionConfirmationKind,
        )

        return CompactionConfirmationKind.FULL

    host._query_command_row = no_command
    host._settle_manual_compaction_command = full_confirmation

    async def exercise() -> CompactionOutcome:
        provider_started = asyncio.Event()
        provider_release = asyncio.Event()

        async def active_owner() -> None:
            provider_started.set()
            await provider_release.wait()
            current = asyncio.current_task()
            assert current is not None
            await host._settle_active_root_task(current)

        active_task = asyncio.create_task(active_owner())
        host._active_task = active_task
        await provider_started.wait()
        compact = asyncio.create_task(
            host.compact_context(command_id="command:compact", force=True)
        )
        while (
            await owner.find_manual(
                command_id="command:compact",
                scope_kind=ModelInputScopeKind.ROOT,
                scope_subagent_task_id=None,
            )
            is None
        ):
            await asyncio.sleep(0)
        provider_release.set()
        await active_task
        outcome = await asyncio.wait_for(compact, timeout=1)
        await owner.aclose()
        return outcome

    outcome = asyncio.run(exercise())

    assert outcome.disposition is CompactionDisposition.COMPACTED
    assert idle_calls == [("turn:terminal", "command:compact", True)]
    assert idle_marks == ["turn:terminal"]
    assert host._active_task is None


def test_round5b_runtime_handoff_is_ordered_bounded_and_body_free() -> None:
    handoff = freeze_compaction_runtime_handoff(
        terminal_processes=(
            FrozenTerminalProcessHandoffFact(
                process_id="process:b",
                terminal_session_id="terminal:1",
                status="running",
                command_preview="build",
                cwd="/workspace",
            ),
            FrozenTerminalProcessHandoffFact(
                process_id="process:a",
                terminal_session_id="terminal:2",
                status="running",
                command_preview="test",
                cwd="/workspace",
            ),
        ),
        terminal_monitors=(
            FrozenTerminalMonitorHandoffFact(
                monitor_id="monitor:1",
                process_id="process:a",
                state="active",
                pending_observation=True,
            ),
        ),
        todo=None,
        subagent_tasks=(
            freeze_subagent_task_board_fact(
                task_id="task:1",
                task_key="inspect",
                label="Inspect",
                status="ACTIVE",
                objective_preview="inspect",
                dependency_total=0,
                dependency_remaining=0,
                pending_message_count=0,
                accepted_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ),
        ),
    )
    assert handoff is not None
    payload = json.loads(handoff.full_text)
    assert [item["process_id"] for item in payload["terminal_processes"]] == [
        "process:a",
        "process:b",
    ]
    assert "output" not in handoff.full_text
    assert "result" not in handoff.full_text


def test_round5b_runtime_handoff_never_drops_actionable_identity() -> None:
    with pytest.raises(CompactionRuntimeHandoffBoundError):
        freeze_compaction_runtime_handoff(
            terminal_processes=(
                FrozenTerminalProcessHandoffFact(
                    process_id="process:1",
                    terminal_session_id="terminal:1",
                    status="running",
                    command_preview="x" * 500,
                    cwd="/" + "x" * 500,
                ),
            ),
            terminal_monitors=(),
            todo=None,
            subagent_tasks=(),
            maximum_utf8_bytes=32,
        )


def test_round5b_global_lane_installs_only_one_scope_fence() -> None:
    async def exercise() -> None:
        owner = HostCompactionRuntimeOwner()
        entered = asyncio.Event()
        release = asyncio.Event()
        second_entered = asyncio.Event()
        first = CompactionScope(
            session_id="session:1",
            workspace_id="workspace:1",
            turn_id="turn:1",
            scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        )
        second = CompactionScope(
            session_id="session:1",
            workspace_id="workspace:1",
            turn_id="turn:2",
            scope_kind=ModelInputScopeKind.SUBAGENT_TASK,
            scope_subagent_task_id="task:2",
        )

        async def first_operation() -> str:
            entered.set()
            await release.wait()
            return "first"

        async def second_operation() -> str:
            second_entered.set()
            return "second"

        first_task = asyncio.create_task(
            owner.run_fenced(
                scope=first,
                trigger=CompactionTrigger.MANUAL,
                operation=first_operation,
            )
        )
        await entered.wait()
        second_task = asyncio.create_task(
            owner.run_fenced(
                scope=second,
                trigger=CompactionTrigger.AUTO_ACTIVE_CONTEXT,
                operation=second_operation,
            )
        )
        await asyncio.sleep(0)
        assert not second_entered.is_set()
        assert not owner.is_fenced(
            scope_kind=ModelInputScopeKind.SUBAGENT_TASK,
            scope_subagent_task_id="task:2",
        )
        release.set()
        assert await first_task == "first"
        assert await second_task == "second"
        await owner.aclose()

    asyncio.run(exercise())


def test_round5b_close_drains_admitted_settlement_without_cancelling_it() -> None:
    async def exercise() -> None:
        owner = HostCompactionRuntimeOwner()
        started = asyncio.Event()
        release = asyncio.Event()
        cancelled = False

        async def settlement() -> str:
            nonlocal cancelled
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled = True
                raise
            return "FULL"

        task = owner.start_settlement(settlement(), name="test-settlement")
        await started.wait()
        close = asyncio.create_task(owner.aclose())
        await asyncio.sleep(0)
        assert not close.done()
        assert not task.cancelled()
        release.set()
        await close
        assert await task == "FULL"
        assert not cancelled

    asyncio.run(exercise())


def test_round5b_manual_candidate_is_idempotent_and_conflict_closed() -> None:
    async def exercise() -> None:
        owner = HostCompactionRuntimeOwner()
        request, future = await owner.request_manual(
            command_id="command:1",
            scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
            expected_turn_id="turn:1",
            force=False,
        )
        duplicate, same_future = await owner.request_manual(
            command_id="command:1",
            scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
            expected_turn_id="turn:1",
            force=False,
        )
        assert duplicate == request
        assert same_future is future
        with pytest.raises(RuntimeError, match="pending"):
            await owner.request_manual(
                command_id="command:2",
                scope_kind=ModelInputScopeKind.ROOT,
                scope_subagent_task_id=None,
                expected_turn_id="turn:1",
                force=True,
            )
        outcome = CompactionOutcome(
            CompactionDisposition.NOT_NEEDED,
            "turn:1",
            None,
            None,
            "NO_COMPACTABLE_PREFIX",
        )
        await owner.settle_manual(request, outcome)
        assert await future == outcome
        await owner.aclose()

    asyncio.run(exercise())


def test_round5b_retained_skill_drops_body_already_full_in_successor_tail() -> None:
    evidence = "sha256:" + "a" * 64
    item_values = {
        "name": "inspect",
        "catalog_location": "/workspace/.agents/skills/inspect/SKILL.md",
        "body": "Use the ordinary read path.",
        "delivery_sequence": 42,
        "evidence_source_entry_fingerprint": evidence,
    }
    item = FrozenRetainedSkillContextItem(
        **item_values,
    )
    body = canonical_json_bytes(
        {
            "skills": (
                {
                    "name": item.name,
                    "catalog_location": item.catalog_location,
                    "body": item.body,
                },
            )
        }
    ).decode("utf-8")
    estimator = PulsaraHeuristicTokenEstimatorV1()
    tokens = estimator.estimate_text(body)
    selection = FrozenRetainedSkillContextSelection(
        ordered_items=(item,),
        rendered_body=body,
        estimated_tokens=tokens,
        selection_fingerprint=context_fingerprint(
            "pulsara.retained-skill-context-selection.v1",
            {
                "items": (
                    (
                        item.name,
                        item.catalog_location,
                        item.body,
                        item.delivery_sequence,
                        item.evidence_source_entry_fingerprint,
                    ),
                ),
                "body": body,
                "tokens": tokens,
            },
        ),
    )

    reduced = remove_full_tail_duplicates(
        selection,
        full_source_entry_fingerprints=frozenset({evidence}),
        estimator=estimator,
    )

    assert reduced.ordered_items == ()
    assert reduced.rendered_body == '{"skills":[]}'
    assert reduced.estimated_tokens == 0


def test_round5b_retained_skill_proves_older_full_from_installed_message() -> None:
    timing_values = {
        "source_turn_ref": context_fingerprint(
            "pulsara:provider-visible-turn-ref:v1",
            {"session_id": "session:test", "turn_id": "turn:test"},
        ),
        "observed_at_utc": "2026-08-21T01:02:03.000000Z",
        "observation_duration_microseconds": 1_000,
        "duration_disposition": ToolObservationDurationDisposition.MEASURED,
        "tool_reported_duration_microseconds": None,
        "observation_origin": ToolObservationOrigin.BUILTIN,
    }
    provisional = FrozenToolObservationTimingFact.__new__(
        FrozenToolObservationTimingFact
    )
    for name, value in timing_values.items():
        object.__setattr__(provisional, name, value)
    object.__setattr__(provisional, "fact_fingerprint", "")
    timing = FrozenToolObservationTimingFact(
        **timing_values,
        fact_fingerprint=tool_observation_timing_fingerprint(provisional),
    )
    item = FrozenProviderInputItem(
        FrozenProviderInputItemKind.TOOL_RESULT,
        "entry:read-result",
        4,
        "turn:test",
        '{"status":"ok","content":"1|body"}',
        tool_call_id="call:read",
        tool_request_entry_id="entry:read-request",
        tool_result_context=ProviderToolResultContextMetadata(
            result_id="result:read",
            result_state="SUCCESS",
            display_kind=ToolResultDisplayKind.COMPLETE,
            artifact_disposition=ToolOutputArtifactDisposition.NOT_REQUIRED,
            artifact_id=None,
            source_coverage=ToolOutputSourceCoverage.COMPLETE,
            source_coverage_reason=None,
            artifact_unavailability_reason=None,
            model_visible_memory_fact_ids=(),
            timing=timing,
        ),
        tool_result_body_text='{"status":"ok","content":"1|body"}',
    )
    lowered = lower_canonical_item(
        item,
        artifact_read_available=True,
        limits=STRUCTURED_MODEL_INPUT_LIMITS,
    )
    full = next(
        variant.message
        for variant in lowered.tool_result_variants
        if variant.mode is ToolResultProviderRenderMode.FULL
    )
    key = (item.source_entry_id, provider_input_item_fingerprint(item))

    assert _was_installed_full(
        item,
        decisions={},
        installed_messages={key: (full,)},
    )
    assert not _was_installed_full(
        item,
        decisions={},
        installed_messages={key: ()},
    )

    catalog_body = canonical_json_bytes(
        {
            "skills": (
                {
                    "name": "inspect",
                    "description": "Inspect safely.",
                    "location": "/package/bundled_skills/inspect/SKILL.md",
                },
            )
        }
    ).decode("utf-8")

    def catalog_message(
        *,
        lifecycle: SourceObservationLifecycle,
        presence: SourceObservationPresence,
        body: str,
    ) -> object:
        return encode_runtime_observation(
            source_kind=ContextSourceKind.SKILL_CATALOG,
            trust_class=ContextTrustClass.UNTRUSTED_OBSERVATION,
            lifecycle=lifecycle,
            presence=presence,
            contract_version="pulsara.skill-catalog.v2",
            body=body,
        )

    full_catalog = catalog_message(
        lifecycle=SourceObservationLifecycle.SNAPSHOT,
        presence=SourceObservationPresence.VALUE,
        body=catalog_body,
    )
    unavailable_catalog = catalog_message(
        lifecycle=SourceObservationLifecycle.UNAVAILABLE,
        presence=SourceObservationPresence.UNAVAILABLE,
        body="",
    )
    predecessor = SimpleNamespace(
        messages=(full_catalog, unavailable_catalog),
        message_placements=(
            SimpleNamespace(message_ordinal=1),
            SimpleNamespace(message_ordinal=2),
        ),
    )
    location = "/package/bundled_skills/inspect/SKILL.md"
    assert (
        _historical_catalog_row(
            predecessor,
            before_message_ordinal=3,
            location=location,
        )
        is None
    )
    row = _historical_catalog_row(
        predecessor,
        before_message_ordinal=2,
        location=location,
    )
    assert row == {
        "name": "inspect",
        "description": "Inspect safely.",
        "location": location,
    }

    document = "---\nname: inspect\ndescription: Inspect safely.\n---\nRetained body"
    numbered = "\n".join(
        f"{ordinal}|{line}"
        for ordinal, line in enumerate(document.splitlines(), start=1)
    )
    exact_body = json.dumps(
        {
            "status": "ok",
            "path": location,
            "access_scope": "external_absolute",
            "workspace_relative": False,
            "offset": 1,
            "limit": 200,
            "total_lines": len(document.splitlines()),
            "file_size": len(document.encode("utf-8")),
            "truncated": False,
            "content": numbered,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    exact_item = FrozenProviderInputItem(
        FrozenProviderInputItemKind.TOOL_RESULT,
        "entry:exact-read-result",
        5,
        "turn:test",
        exact_body,
        tool_call_id="call:exact-read",
        tool_request_entry_id="entry:exact-read-request",
        tool_result_context=item.tool_result_context,
        tool_result_body_text=exact_body,
    )
    assert (
        _exact_historical_read(
            exact_item,
            catalog_row=row,
            root_policy=SimpleNamespace(roots=()),
        )
        == "Retained body"
    )


def test_round5b_installed_retained_skill_is_inherited_only_within_same_turn() -> None:
    body = canonical_json_bytes(
        {
            "skills": (
                {
                    "name": "inspect",
                    "catalog_location": ("/package/bundled_skills/inspect/SKILL.md"),
                    "body": "Retained body",
                },
            )
        }
    ).decode("utf-8")
    message = encode_runtime_observation(
        source_kind=ContextSourceKind.RETAINED_SKILL_CONTEXT,
        trust_class=ContextTrustClass.UNTRUSTED_OBSERVATION,
        lifecycle=SourceObservationLifecycle.SNAPSHOT,
        presence=SourceObservationPresence.VALUE,
        contract_version="pulsara.retained-skill-context.v1",
        body=body,
    )
    head = SimpleNamespace(
        source_kind=ContextSourceKind.RETAINED_SKILL_CONTEXT,
        presence=SourceObservationPresence.VALUE,
        installed_observation_fingerprint=(_installed_observation_fingerprint(message)),
        last_emitted_turn_id="turn:skill",
    )
    predecessor = SimpleNamespace(
        source_heads=(head,),
        messages=(message,),
    )

    inherited = _installed_retained_items(
        predecessor,
        target_turn_id="turn:skill",
    )

    assert tuple((item.name, item.body) for item in inherited) == (
        ("inspect", "Retained body"),
    )
    assert (
        _installed_retained_items(
            predecessor,
            target_turn_id="turn:next-root",
        )
        == ()
    )


def test_round5b_tool_groups_pair_reused_call_ids_with_exact_request() -> None:
    timing_values = {
        "source_turn_ref": context_fingerprint(
            "pulsara:provider-visible-turn-ref:v1",
            {"session_id": "session:test", "turn_id": "turn:test"},
        ),
        "observed_at_utc": "2026-08-21T01:02:03.000000Z",
        "observation_duration_microseconds": 1_000,
        "duration_disposition": ToolObservationDurationDisposition.MEASURED,
        "tool_reported_duration_microseconds": None,
        "observation_origin": ToolObservationOrigin.BUILTIN,
    }
    provisional = FrozenToolObservationTimingFact.__new__(
        FrozenToolObservationTimingFact
    )
    for name, value in timing_values.items():
        object.__setattr__(provisional, name, value)
    object.__setattr__(provisional, "fact_fingerprint", "")
    timing = FrozenToolObservationTimingFact(
        **timing_values,
        fact_fingerprint=tool_observation_timing_fingerprint(provisional),
    )

    def request(entry_id: str, sequence: int) -> FrozenProviderInputItem:
        arguments = freeze_json({"path": entry_id})
        return FrozenProviderInputItem(
            FrozenProviderInputItemKind.ASSISTANT_TOOL_REQUEST,
            entry_id,
            sequence,
            "turn:test",
            "",
            tool_calls=(ProviderToolCall("call:reused", "read_file", arguments),),
        )

    def result(
        entry_id: str, request_entry_id: str, sequence: int
    ) -> FrozenProviderInputItem:
        body = '{"status":"ok"}'
        return FrozenProviderInputItem(
            FrozenProviderInputItemKind.TOOL_RESULT,
            entry_id,
            sequence,
            "turn:test",
            body,
            tool_call_id="call:reused",
            tool_request_entry_id=request_entry_id,
            tool_result_context=ProviderToolResultContextMetadata(
                result_id=f"result:{entry_id}",
                result_state="SUCCESS",
                display_kind=ToolResultDisplayKind.COMPLETE,
                artifact_disposition=ToolOutputArtifactDisposition.NOT_REQUIRED,
                artifact_id=None,
                source_coverage=ToolOutputSourceCoverage.COMPLETE,
                source_coverage_reason=None,
                artifact_unavailability_reason=None,
                model_visible_memory_fact_ids=(),
                timing=timing,
            ),
            tool_result_body_text=body,
        )

    items = (
        request("entry:request:old", 1),
        result("entry:result:old", "entry:request:old", 2),
        request("entry:request:new", 3),
        result("entry:result:new", "entry:request:new", 4),
    )
    canonical_read = SimpleNamespace(
        dispatch_read=SimpleNamespace(
            compile_snapshot=SimpleNamespace(
                canonical_input=SimpleNamespace(items=items, closures=())
            )
        )
    )

    groups = enumerate_complete_tool_groups(canonical_read)

    assert tuple(group.assistant_entry_id for group in groups) == (
        "entry:request:old",
        "entry:request:new",
    )


def test_round5b_architecture_and_oracle_are_exact() -> None:
    assert len(COMMITTED_EVENT_DESCRIPTORS) == 29
    assert len(LIVE_EVENT_TYPES) == 24
    assert len(SUBJECT_SLOTS) == 11
    assert APPEND_GUARDS == ("HostWriterGuard",)
    assert len(CONVERSATION_KERNEL_RELATIONS) == 25
    assert not {
        "durable_jobs",
        "durable_job_attempts",
    } & set(CONVERSATION_KERNEL_RELATIONS)

    cold = ROOT / "src/pulsara_agent/conversation_kernel/cold_epoch.py"
    tree = ast.parse(cold.read_text(encoding="utf-8"), filename=str(cold))
    imported = {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    assert not any("repository" in item for item in imported)
    assert not any("compaction" in item for item in imported)
    assert not any("terminal" in item for item in imported)
    assert not (ROOT / "src/pulsara_agent/conversation_kernel/jobs.py").exists()
    assert not (ROOT / "src/pulsara_agent/conversation_kernel/job_model.py").exists()


def test_round9_2_compaction_hard_cut_has_one_post_adoption_install_path() -> None:
    coordinator = (
        ROOT / "src/pulsara_agent/conversation_kernel/compaction/coordinator.py"
    )
    source = coordinator.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(coordinator))
    settlement = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "CompactionCoordinator"
        for node in node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_complete_compaction_settlement"
    )
    calls = tuple(node for node in ast.walk(settlement) if isinstance(node, ast.Call))

    def attribute_calls(name: str) -> tuple[ast.Call, ...]:
        return tuple(
            call
            for call in calls
            if isinstance(call.func, ast.Attribute) and call.func.attr == name
        )

    def attribute_references(name: str) -> tuple[ast.Attribute, ...]:
        return tuple(
            node
            for node in ast.walk(settlement)
            if isinstance(node, ast.Attribute) and node.attr == name
        )

    installs = attribute_calls("install_provider_open")
    hook_siblings = attribute_calls("prepare_hook_context_sibling")
    assert len(installs) == 1
    assert len(hook_siblings) == 1
    assert len(attribute_calls("_settle_compaction_adoption")) == 1
    assert len(attribute_calls("_dispatch_post_compact")) == 1
    rotations = attribute_references("rotate_provider_input")
    assert len(rotations) == 1
    assert (
        attribute_calls("_settle_compaction_adoption")[0].lineno
        < attribute_calls("_dispatch_post_compact")[0].lineno
        < rotations[0].lineno
        < hook_siblings[0].lineno
        < installs[0].lineno
    )

    no_hook_prepares = tuple(
        call
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "prepare"
        and any(
            keyword.arg == "include_hook_context"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is False
            for keyword in call.keywords
        )
    )
    assert len(no_hook_prepares) == 2
    assert "SessionStartCompactPort" in source
    assert "child compaction received a ROOT start port" in source
    for forbidden in (
        "installed_base_augmentation",
        "revoke_successor",
        "supersede_successor",
        "runner_back_reference",
    ):
        assert forbidden not in source
