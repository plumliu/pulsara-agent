"""Round 5B long-horizon compaction contracts and process-local ownership."""

from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
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
    FrozenRetainedHistoricalRequest,
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
    DestinationDialogueEntry,
    DestinationDialogueProjectionPlan,
    RecentDialogueUnit,
    crosses_compaction_resource_headroom,
    enumerate_destination_backbone_projections,
    enumerate_complete_tool_groups,
    enumerate_safe_summary_prefixes,
    freeze_compaction_continuation,
    freeze_destination_dialogue_projection_plan,
    rebase_compaction_dispatch_read_through_sequence,
    retain_destination_tool_evidence,
    resolved_compaction_headroom_bounds,
    should_trigger_compaction,
    validate_compaction_reclaim,
)
import pulsara_agent.conversation_kernel.compaction.coordinator as compaction_coordinator
import pulsara_agent.conversation_kernel.compaction.model_call as compaction_model_call
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
from pulsara_agent.conversation_kernel.memory.contracts import MemoryUsePolicy
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
from pulsara_agent.llm.input import LLMMessage
from pulsara_agent.llm.request import (
    MAXIMUM_PROVIDER_WIRE_INPUT_BYTES,
    FrozenProviderWireInputQuote,
)
from pulsara_agent.model_input.contracts import (
    CanonicalInputOriginKind,
    CanonicalModelInputIdentity,
    CanonicalModelInputSnapshot,
    ContextBindingBaseKind,
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


def _wire_quote(
    *,
    semantic_tokens: int,
    final_tokens: int,
    budget_tokens: int = 1_000,
    wire_api: str = "openai_chat_completions",
    estimator_fingerprint: str | None = None,
    wire_bytes: int | None = None,
) -> FrozenProviderWireInputQuote:
    return FrozenProviderWireInputQuote(
        wire_api=wire_api,
        estimator_fingerprint=(
            estimator_fingerprint
            or PulsaraHeuristicTokenEstimatorV1().fact.estimator_fingerprint
        ),
        effective_input_budget_tokens=budget_tokens,
        semantic_estimated_input_tokens=semantic_tokens,
        generic_wire_estimated_input_tokens=final_tokens,
        replaced_generic_wire_estimated_tokens=0,
        replay_wire_estimated_tokens=0,
        final_wire_estimated_input_tokens=final_tokens,
        final_wire_utf8_bytes=wire_bytes or max(1, final_tokens * 2),
    )


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


def test_model_switch_destination_projection_enumerates_every_safe_suffix() -> None:
    units = (
        RecentDialogueUnit(
            "turn:old",
            (1, 2),
            (
                DestinationDialogueEntry("user", "old question"),
                DestinationDialogueEntry("assistant", "old answer"),
            ),
        ),
        RecentDialogueUnit(
            "turn:new",
            (3, 4),
            (
                DestinationDialogueEntry("user", "new question"),
                DestinationDialogueEntry("assistant", "new answer"),
            ),
        ),
    )
    plan = DestinationDialogueProjectionPlan(
        units,
        (
            "prior handover",
            ("quoted request",),
            (
                FrozenRetainedHistoricalRequest(
                    FrozenProviderInputItemKind.USER,
                    CanonicalInputOriginKind.HUMAN_MESSAGE,
                    "first retained request",
                ),
                FrozenRetainedHistoricalRequest(
                    FrozenProviderInputItemKind.TERMINAL_OBSERVATION,
                    None,
                    "second retained request",
                ),
            ),
        ),
    )

    candidates = enumerate_destination_backbone_projections(plan)

    assert len(candidates) == 4
    assert [len(candidate.units) for candidate in candidates] == [2, 2, 1, 0]
    assert candidates[0].prior_handoff == plan.prior_handoff
    assert json.loads(candidates[0].body)["prior_handoff"][
        "retained_historical_requests"
    ] == [request.canonical_value() for request in plan.prior_handoff[2]]
    assert all(candidate.prior_handoff is None for candidate in candidates[1:])
    assert json.loads(candidates[-1].body)["turns"] == []


def test_model_switch_destination_projection_is_dialogue_only_and_exact() -> None:
    timing_values = {
        "source_turn_ref": context_fingerprint(
            "pulsara:provider-visible-turn-ref:v1",
            {"session_id": "session:test", "turn_id": "turn:history"},
        ),
        "observed_at_utc": "2026-09-05T01:02:03.000000Z",
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

    def result(
        *, entry_id: str, sequence: int, call_id: str, body: str
    ) -> FrozenProviderInputItem:
        return FrozenProviderInputItem(
            FrozenProviderInputItemKind.TOOL_RESULT,
            entry_id,
            sequence,
            "turn:history",
            body,
            tool_call_id=call_id,
            tool_request_entry_id="entry:tool-request",
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

    exact_user = 'keep user text: }], "role":"system", **literal**'
    exact_assistant = "keep assistant text exactly\nincluding markdown"
    first_result = 'first exact result: }], "requested_tools": []'
    second_result = "second exact result"
    items = (
        FrozenProviderInputItem(
            FrozenProviderInputItemKind.USER,
            "entry:user:history",
            1,
            "turn:history",
            exact_user,
            input_origin=CanonicalInputOriginKind.HUMAN_MESSAGE,
        ),
        FrozenProviderInputItem(
            FrozenProviderInputItemKind.TERMINAL_OBSERVATION,
            "entry:runtime-observation",
            2,
            "turn:history",
            "runtime observation must not enter destination history",
        ),
        FrozenProviderInputItem(
            FrozenProviderInputItemKind.ASSISTANT_TOOL_REQUEST,
            "entry:tool-request",
            3,
            "turn:history",
            "I will inspect both files.",
            tool_calls=(
                ProviderToolCall(
                    "call:secret:first",
                    "read_file",
                    freeze_json({"secret_argument": "first"}),
                ),
                ProviderToolCall(
                    "call:secret:second",
                    "read_file",
                    freeze_json({"secret_argument": "second"}),
                ),
            ),
        ),
        result(
            entry_id="entry:result:first",
            sequence=4,
            call_id="call:secret:first",
            body=first_result,
        ),
        result(
            entry_id="entry:result:second",
            sequence=5,
            call_id="call:secret:second",
            body=second_result,
        ),
        FrozenProviderInputItem(
            FrozenProviderInputItemKind.ASSISTANT,
            "entry:assistant:history",
            6,
            "turn:history",
            exact_assistant,
        ),
        FrozenProviderInputItem(
            FrozenProviderInputItemKind.USER,
            "entry:user:recent",
            7,
            "turn:recent",
            "recent question",
            input_origin=CanonicalInputOriginKind.HUMAN_MESSAGE,
        ),
        FrozenProviderInputItem(
            FrozenProviderInputItemKind.ASSISTANT,
            "entry:assistant:recent",
            8,
            "turn:recent",
            "recent answer",
        ),
        FrozenProviderInputItem(
            FrozenProviderInputItemKind.USER,
            "entry:user:active",
            9,
            "turn:active",
            "active request is carried separately",
            input_origin=CanonicalInputOriginKind.HUMAN_MESSAGE,
        ),
    )
    canonical = SimpleNamespace(
        identity=SimpleNamespace(initial_entry_id="entry:user:active"),
        items=items,
    )
    canonical_read = SimpleNamespace(
        dispatch_read=SimpleNamespace(
            compile_snapshot=SimpleNamespace(canonical_input=canonical)
        ),
        safe_head_range=SimpleNamespace(ordered_items=items, closures=()),
    )
    active = FrozenCompactionActiveRequest(
        entry_id="entry:user:active",
        entry_sequence=9,
        location=CompactionActiveRequestLocation.SNAPSHOT_EXACT,
        text="active request is carried separately",
    )

    plan = freeze_destination_dialogue_projection_plan(
        canonical_read=canonical_read,  # type: ignore[arg-type]
        active_request=active,
    )
    projection = enumerate_destination_backbone_projections(plan)[0]
    decoded = json.loads(projection.body)

    assert len(plan.units) == 2
    assert [
        tool["name"] for tool in decoded["turns"][0]["entries"][1]["requested_tools"]
    ] == [
        "read_file",
        "read_file",
    ]
    assert decoded["turns"][0]["entries"][0]["text"] == exact_user
    assert decoded["turns"][0]["entries"][2]["text"] == exact_assistant
    rendered = projection.body.decode("utf-8")
    assert "runtime observation must not enter destination history" not in rendered
    assert "active request is carried separately" not in rendered
    assert "call:secret" not in rendered
    assert "secret_argument" not in rendered
    assert first_result not in rendered
    assert second_result not in rendered

    retained = retain_destination_tool_evidence(
        projection,
        projection.eligible_evidence[0],
    )
    retained_decoded = json.loads(retained.body)
    retained_tools = retained_decoded["turns"][0]["entries"][1]["requested_tools"]
    assert retained_tools[0]["retained_result"] == first_result
    assert retained_tools[1]["result_omitted"] is True
    assert retained.eligible_evidence == (projection.eligible_evidence[1],)


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


@pytest.mark.parametrize(
    "invalid",
    [
        None,
        {},
        "request",
        [None],
        [
            {
                "item_kind": "USER",
                "input_origin": "TERMINAL_OBSERVATION",
                "text": "wrong origin",
            }
        ],
        [{"item_kind": "USER", "input_origin": "HUMAN_MESSAGE", "text": " "}],
    ],
)
def test_retained_historical_requests_reject_non_list_or_invalid_items(invalid) -> None:
    from pulsara_agent.primitives.context import canonical_json_bytes

    carrier = build_compaction_snapshot_carrier(
        summary=freeze_compaction_summary_output("summary", maximum_utf8_bytes=65_536),
        recent_user_messages=(),
        continuation_mode=CompactionContinuationMode.AWAIT_NEXT_USER,
        active_request=None,
    )
    raw = json.loads(carrier.body)
    raw["retained_historical_requests"] = invalid
    with pytest.raises(ValueError):
        parse_compaction_snapshot_carrier(canonical_json_bytes(raw))


def test_retained_historical_requests_roundtrip_is_ordered_and_has_no_legacy_parser() -> (
    None
):
    from pulsara_agent.primitives.context import canonical_json_bytes

    requests = (
        FrozenRetainedHistoricalRequest(
            FrozenProviderInputItemKind.USER,
            CanonicalInputOriginKind.HUMAN_MESSAGE,
            "identical text",
        ),
        FrozenRetainedHistoricalRequest(
            FrozenProviderInputItemKind.TERMINAL_OBSERVATION, None, "identical text"
        ),
    )
    carrier = build_compaction_snapshot_carrier(
        summary=freeze_compaction_summary_output("summary", maximum_utf8_bytes=65_536),
        recent_user_messages=(),
        continuation_mode=CompactionContinuationMode.AWAIT_NEXT_USER,
        active_request=None,
        retained_historical_requests=requests,
    )
    assert (
        parse_compaction_snapshot_carrier(carrier.body).retained_historical_requests
        == requests
    )
    raw = json.loads(carrier.body)
    del raw["retained_historical_requests"]
    with pytest.raises(ValueError, match="fields"):
        parse_compaction_snapshot_carrier(canonical_json_bytes(raw))
    raw["retained_historical_request"] = requests[0].canonical_value()
    with pytest.raises(ValueError, match="fields"):
        parse_compaction_snapshot_carrier(canonical_json_bytes(raw))


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
        "retained_historical_requests",
    }
    assert decoded["retained_historical_requests"] == []
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


def test_round5b_has_no_presummary_successor_lower_bound_gate() -> None:
    root = Path(__file__).resolve().parents[1]
    planner = (
        root / "src/pulsara_agent/conversation_kernel/compaction/planner.py"
    ).read_text()
    coordinator = (
        root / "src/pulsara_agent/conversation_kernel/compaction/coordinator.py"
    ).read_text()
    assert "estimate_unavoidable_compaction_successor_tokens" not in planner
    assert "estimate_unavoidable_compaction_successor_tokens" not in coordinator


def test_final_wire_successor_deadline_starts_after_snapshot_publication() -> None:
    coordinator = (
        ROOT / "src/pulsara_agent/conversation_kernel/compaction/coordinator.py"
    ).read_text(encoding="utf-8")

    summary_terminal = coordinator.index("raw = await prepared_summary.open_once()")
    publication = coordinator.index("content = await self._content(", summary_terminal)
    successor_deadline = coordinator.index(
        "successor_deadline = monotonic()", publication
    )

    assert summary_terminal < publication < successor_deadline


def test_round5b_trigger_uses_exact_prepared_target_budget_without_262k_cap() -> None:
    source_view = SimpleNamespace(
        normal_compile_binding=SimpleNamespace(
            effective_input_budget_tokens=400_000,
        ),
        provider_wire_quote=SimpleNamespace(
            final_wire_estimated_input_tokens=250_000,
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


@pytest.mark.parametrize(
    ("semantic_tokens", "final_tokens", "expected"),
    (
        (100, 850, True),
        (100, 849, False),
        (999, 849, False),
    ),
)
def test_round5b_automatic_trigger_uses_final_wire_not_semantic_estimate(
    semantic_tokens: int,
    final_tokens: int,
    expected: bool,
) -> None:
    source_view = SimpleNamespace(
        normal_compile_binding=SimpleNamespace(
            effective_input_budget_tokens=1_000,
        ),
        provider_wire_quote=_wire_quote(
            semantic_tokens=semantic_tokens,
            final_tokens=final_tokens,
        ),
        physical_working_set=SimpleNamespace(
            post_base_item_count=1,
            post_base_canonical_utf8_bytes=1,
            continuity_epoch_logical_utf8_bytes=1,
        ),
    )

    assert (
        should_trigger_compaction(
            source_view=source_view,
            policy=ResolvedCompactionPolicy(auto_trigger_ratio=0.85),
            force=False,
        )
        is expected
    )


def test_round5b_manual_reclaim_uses_wire_delta_and_only_rejects_actual_shortfall() -> (
    None
):
    policy = ResolvedCompactionPolicy(minimum_reclaim_tokens=20_000)
    # The semantic compactable prefix can be smaller than the minimum; native
    # replay replacement arithmetic makes the exact final-wire reclaim larger.
    assert (
        validate_compaction_reclaim(
            source_tokens=100_000,
            successor_tokens=70_000,
            hard_input_budget_tokens=100_000,
            policy=policy,
            force=True,
            enforce_soft_target=False,
        )
        == 30_000
    )
    with pytest.raises(CompactionReclaimUnavailable, match="enough"):
        validate_compaction_reclaim(
            source_tokens=100_000,
            successor_tokens=90_001,
            hard_input_budget_tokens=100_000,
            policy=policy,
            force=True,
            enforce_soft_target=False,
        )


def test_round5b_summary_prefix_enumerates_every_finite_safe_boundary() -> None:
    count = 257
    messages = tuple(LLMMessage.user(f"message {index}") for index in range(count))
    placements = tuple(
        SimpleNamespace(
            message_ordinal=index,
            origin_entry_id=f"entry:{index + 1}",
            within_origin_ordinal=0,
        )
        for index in range(count)
    )
    items = tuple(
        SimpleNamespace(
            source_entry_id=f"entry:{index + 1}",
            source_entry_sequence=index + 1,
        )
        for index in range(count)
    )
    binding_fingerprint = context_fingerprint("test:binding", "safe-prefix")
    binding = SimpleNamespace(
        binding_fingerprint=binding_fingerprint,
        tool_surface=SimpleNamespace(tool_specs=()),
    )
    projection = SimpleNamespace(
        system_prompt="ROOT SYSTEM",
        messages=messages,
        message_placements=placements,
        tools=(),
        compile_binding_fingerprint=binding_fingerprint,
    )
    source_view = SimpleNamespace(
        source_view_fingerprint=context_fingerprint("test:source-view", "safe-prefix"),
        materialized_messages=lambda: messages,
        materialized_system_prompt=lambda: "ROOT SYSTEM",
        predecessor_epoch_view=None,
        normal_compile_binding=binding,
        canonical_dispatch_read=SimpleNamespace(
            compile_snapshot=SimpleNamespace(
                canonical_input=SimpleNamespace(items=items)
            )
        ),
        exact_safe_canonical_head=count,
    )

    candidates = enumerate_safe_summary_prefixes(
        source_view=source_view,
        complete_tool_groups=(),
        retained_group_count=0,
        source_projection=projection,
    )

    assert len(candidates) == count
    assert tuple(item[1].summary_prefix_message_count for item in candidates) == tuple(
        range(count, 0, -1)
    )

    installed_count = 193
    source_view.predecessor_epoch_view = SimpleNamespace(
        messages=messages[:installed_count]
    )
    compatible_candidates = enumerate_safe_summary_prefixes(
        source_view=source_view,
        complete_tool_groups=(),
        retained_group_count=0,
        source_projection=projection,
    )
    assert tuple(
        item[1].summary_prefix_message_count for item in compatible_candidates
    ) == tuple(range(count, installed_count - 1, -1))


def test_compaction_summary_wire_proof_rejects_installed_prefix_truncation() -> None:
    root = freeze_json({"role": "system", "content": "root"})
    installed = tuple(freeze_json({"ordinal": index}) for index in range(3))
    synthetic_request = freeze_json({"role": "user", "content": "summarize"})
    predecessor = SimpleNamespace(
        wire_input_plan=SimpleNamespace(
            materialization=SimpleNamespace(
                root_policy_value=root,
                tool_items=(),
                ordered_input_items=installed,
            )
        )
    )
    source_proof = compaction_model_call.InstalledPrefixSummarySourceProof(
        source_view=SimpleNamespace(predecessor_epoch_view=predecessor),
        source_projection=SimpleNamespace(),
        prefix_proof=SimpleNamespace(),
        _seal=compaction_model_call._COMPACTION_SUMMARY_SOURCE_SEAL,
    )
    semantic = SimpleNamespace(source_proof=source_proof)
    truncated = SimpleNamespace(
        materialization=SimpleNamespace(
            root_policy_value=root,
            tool_items=(),
            ordered_input_items=(*installed[:2], synthetic_request),
        )
    )

    with pytest.raises(ValueError, match="rewrote the installed prefix"):
        compaction_model_call._require_summary_wire_prefix(semantic, truncated)

    extended = SimpleNamespace(
        materialization=SimpleNamespace(
            root_policy_value=root,
            tool_items=(),
            ordered_input_items=(
                *installed,
                freeze_json({"ordinal": 3}),
                synthetic_request,
            ),
        )
    )
    compaction_model_call._require_summary_wire_prefix(semantic, extended)


def test_model_switch_wire_transition_uses_destination_trigger_and_exact_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    estimator = PulsaraHeuristicTokenEstimatorV1()
    target_fact = SimpleNamespace(target_fingerprint="sha256:" + "1" * 64)
    profile = SimpleNamespace(
        route_wire_profile=SimpleNamespace(wire_api="openai_responses")
    )
    target = SimpleNamespace(
        fact=target_fact,
        model_profile=profile,
        token_estimator=estimator,
    )
    destination_binding = SimpleNamespace(connection_id="destination")
    identity = SimpleNamespace(
        session_id="session:test",
        turn_id="turn:test",
        conversation_scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
    )
    canonical_read = SimpleNamespace(
        compile_snapshot=SimpleNamespace(
            canonical_input=SimpleNamespace(identity=identity)
        )
    )
    source_quote = _wire_quote(
        semantic_tokens=900,
        final_tokens=900,
        budget_tokens=1_000,
        estimator_fingerprint=estimator.fact.estimator_fingerprint,
    )
    source_candidate = SimpleNamespace(
        canonical_read=canonical_read,
        semantic_input=SimpleNamespace(canonical_input_identity=identity),
    )
    successor_binding = SimpleNamespace(
        target_fact=target_fact,
        estimator=estimator,
        estimator_fingerprint=estimator.fact.estimator_fingerprint,
        effective_input_budget_tokens=1_000,
    )
    successor_candidate = SimpleNamespace(
        canonical_read=canonical_read,
        semantic_input=SimpleNamespace(canonical_input_identity=identity),
        prepared_call=SimpleNamespace(
            call=SimpleNamespace(target=target, binding=destination_binding),
            compile_binding=successor_binding,
        ),
    )
    destination = SimpleNamespace(
        target=target,
        call=SimpleNamespace(binding=destination_binding),
    )
    source_view = SimpleNamespace(
        canonical_dispatch_read=canonical_read,
        provider_wire_quote=source_quote,
    )
    monkeypatch.setattr(
        compaction_coordinator,
        "_compaction_cut_lineage_exactly_joins",
        lambda **_: True,
    )

    below = SimpleNamespace(
        candidate=successor_candidate,
        quote=_wire_quote(
            semantic_tokens=849,
            final_tokens=849,
            budget_tokens=1_000,
            wire_api="openai_responses",
            estimator_fingerprint=estimator.fact.estimator_fingerprint,
        ),
        wire_input_plan=object(),
    )
    transition = compaction_coordinator.validate_model_switch_wire_transition(
        source_view=source_view,
        source_candidate=source_candidate,
        successor_wire=below,
        destination_target=destination,
        policy=ResolvedCompactionPolicy(auto_trigger_ratio=0.85),
        phase="PRE_FULL",
    )
    assert transition.successor_quote.final_wire_estimated_input_tokens == 849
    assert transition.reclaim_tokens == 0

    at_trigger = SimpleNamespace(
        candidate=successor_candidate,
        quote=_wire_quote(
            semantic_tokens=850,
            final_tokens=850,
            budget_tokens=1_000,
            wire_api="openai_responses",
            estimator_fingerprint=estimator.fact.estimator_fingerprint,
        ),
        wire_input_plan=object(),
    )
    with pytest.raises(CompactionReclaimUnavailable, match="below B trigger"):
        compaction_coordinator.validate_model_switch_wire_transition(
            source_view=source_view,
            source_candidate=source_candidate,
            successor_wire=at_trigger,
            destination_target=destination,
            policy=ResolvedCompactionPolicy(auto_trigger_ratio=0.85),
            phase="PRE_FULL",
        )

    destination.call = SimpleNamespace(
        binding=SimpleNamespace(connection_id="different")
    )
    with pytest.raises(
        compaction_coordinator.CompactionWireTransitionDrift,
        match="exact-join",
    ):
        compaction_coordinator.validate_model_switch_wire_transition(
            source_view=source_view,
            source_candidate=source_candidate,
            successor_wire=below,
            destination_target=destination,
            policy=ResolvedCompactionPolicy(auto_trigger_ratio=0.85),
            phase="PRE_FULL",
        )


@pytest.mark.parametrize(
    "drift",
    (
        "target",
        "profile",
        "wire_api",
        "estimator",
        "estimator_impl",
        "budget",
        "cut",
        "native",
        "hard_tokens",
        "hard_bytes",
    ),
)
def test_compaction_wire_transition_joins_authority_before_numeric_reclaim(
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    estimator = PulsaraHeuristicTokenEstimatorV1()
    estimator_fingerprint = estimator.fact.estimator_fingerprint
    source_profile = SimpleNamespace(
        route_wire_profile=SimpleNamespace(wire_api="openai_chat_completions")
    )
    successor_profile = (
        SimpleNamespace(route_wire_profile=SimpleNamespace(wire_api="openai_responses"))
        if drift == "profile"
        else source_profile
    )
    source_target_fact = SimpleNamespace(name="target")
    successor_target_fact = (
        SimpleNamespace(name="other-target")
        if drift == "target"
        else source_target_fact
    )
    successor_estimator = (
        SimpleNamespace(
            fact=SimpleNamespace(
                estimator_id="other",
                estimator_version="v1",
                estimator_fingerprint="sha256:" + ("1" * 64),
            )
        )
        if drift == "estimator"
        else SimpleNamespace(fact=estimator.fact)
        if drift == "estimator_impl"
        else estimator
    )
    successor_estimator_fingerprint = successor_estimator.fact.estimator_fingerprint
    successor_budget = 900 if drift == "budget" else 1_000
    source_identity = SimpleNamespace(
        session_id="session:test",
        turn_id="turn:test",
        conversation_scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
    )
    successor_identity = (
        SimpleNamespace(
            session_id="session:other",
            turn_id="turn:test",
            conversation_scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        )
        if drift == "cut"
        else source_identity
    )

    def canonical_read(identity):
        return SimpleNamespace(
            compile_snapshot=SimpleNamespace(
                canonical_input=SimpleNamespace(identity=identity)
            )
        )

    source_read = canonical_read(source_identity)
    successor_read = canonical_read(successor_identity)
    source_native = ("native",)
    successor_native = ("other-native",) if drift == "native" else source_native
    source_binding = SimpleNamespace(
        target_fact=source_target_fact,
        estimator=estimator,
        estimator_fingerprint=estimator_fingerprint,
        effective_input_budget_tokens=1_000,
    )
    successor_binding = SimpleNamespace(
        target_fact=successor_target_fact,
        estimator=successor_estimator,
        estimator_fingerprint=successor_estimator_fingerprint,
        effective_input_budget_tokens=successor_budget,
    )
    source_quote = _wire_quote(
        semantic_tokens=800,
        final_tokens=900,
        estimator_fingerprint=estimator_fingerprint,
        wire_bytes=(
            MAXIMUM_PROVIDER_WIRE_INPUT_BYTES + 2 if drift == "hard_bytes" else 1_800
        ),
    )
    successor_wire_api = (
        "openai_responses"
        if drift in {"wire_api", "profile"}
        else source_quote.wire_api
    )
    successor_quote = _wire_quote(
        semantic_tokens=400,
        final_tokens=1_001 if drift == "hard_tokens" else 500,
        budget_tokens=successor_budget,
        wire_api=successor_wire_api,
        estimator_fingerprint=successor_estimator_fingerprint,
        wire_bytes=(
            MAXIMUM_PROVIDER_WIRE_INPUT_BYTES + 1 if drift == "hard_bytes" else 1_000
        ),
    )
    source_candidate = SimpleNamespace(
        canonical_read=source_read,
        semantic_input=SimpleNamespace(
            canonical_input_identity=source_identity,
            final_estimate=SimpleNamespace(total_input_tokens=800),
        ),
        prepared_call=SimpleNamespace(
            call=SimpleNamespace(
                target=SimpleNamespace(
                    fact=source_target_fact,
                    model_profile=source_profile,
                )
            ),
            compile_binding=source_binding,
        ),
        native_projection_set=source_native,
    )
    successor_candidate = SimpleNamespace(
        canonical_read=successor_read,
        semantic_input=SimpleNamespace(
            canonical_input_identity=successor_identity,
            final_estimate=SimpleNamespace(total_input_tokens=400),
        ),
        prepared_call=SimpleNamespace(
            call=SimpleNamespace(
                target=SimpleNamespace(
                    fact=successor_target_fact,
                    model_profile=successor_profile,
                )
            ),
            compile_binding=successor_binding,
        ),
        native_projection_set=successor_native,
    )
    source_view = SimpleNamespace(
        canonical_dispatch_read=source_read,
        provider_wire_quote=source_quote,
    )
    successor_wire = SimpleNamespace(
        candidate=successor_candidate,
        quote=successor_quote,
        wire_input_plan=(None if drift in {"hard_tokens", "hard_bytes"} else object()),
    )
    reclaim_calls = 0

    def observed_reclaim(**kwargs):
        del kwargs
        nonlocal reclaim_calls
        reclaim_calls += 1
        if drift != "native":
            raise AssertionError("numeric reclaim ran before the structural join")
        return 400

    monkeypatch.setattr(
        compaction_coordinator,
        "validate_compaction_reclaim",
        observed_reclaim,
    )
    if drift == "native":
        monkeypatch.setattr(
            compaction_coordinator,
            "_compaction_cut_lineage_exactly_joins",
            lambda **_: True,
        )
        transition = compaction_coordinator.validate_compaction_wire_transition(
            source_view=source_view,
            source_candidate=source_candidate,
            successor_wire=successor_wire,
            policy=ResolvedCompactionPolicy(minimum_reclaim_tokens=1),
            force=True,
            enforce_soft_target=False,
            phase="PRE_FULL",
        )
        assert transition.reclaim_tokens == 400
        assert reclaim_calls == 1
        return
    if drift in {"hard_tokens", "hard_bytes"}:
        monkeypatch.setattr(
            compaction_coordinator,
            "_compaction_cut_lineage_exactly_joins",
            lambda **_: True,
        )
        expected = "hard model budget" if drift == "hard_tokens" else "hard physical"
        with pytest.raises(CompactionPlanningError, match=expected):
            compaction_coordinator.validate_compaction_wire_transition(
                source_view=source_view,
                source_candidate=source_candidate,
                successor_wire=successor_wire,
                policy=ResolvedCompactionPolicy(minimum_reclaim_tokens=1),
                force=True,
                enforce_soft_target=False,
                phase="PRE_FULL",
            )
        assert reclaim_calls == 0
        return
    with pytest.raises(CompactionPlanningError, match="does not exact-join"):
        compaction_coordinator.validate_compaction_wire_transition(
            source_view=source_view,
            source_candidate=source_candidate,
            successor_wire=successor_wire,
            policy=ResolvedCompactionPolicy(minimum_reclaim_tokens=1),
            force=True,
            enforce_soft_target=False,
            phase="PRE_FULL",
        )
    assert reclaim_calls == 0


def test_compaction_fenced_restarts_are_stack_free_and_keep_one_attempt_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = object.__new__(compaction_coordinator.CompactionCoordinator)
    restart_count = sys.getrecursionlimit() + 17
    calls: list[dict[str, object]] = []

    async def execute_once(_self, **kwargs):
        calls.append(kwargs)
        if len(calls) <= restart_count:
            return compaction_coordinator._CompactionFencedRestart(
                maximum_retained_tool_groups=1,
                pre_compact_dispatched=True,
            )
        return compaction_coordinator.CompactionExecutionResult(
            CompactionOutcome(
                CompactionDisposition.NOT_NEEDED,
                "turn:stack-free",
                None,
                None,
                "BELOW_TRIGGER",
            )
        )

    monkeypatch.setattr(
        compaction_coordinator.CompactionCoordinator,
        "_execute_compaction_fenced_once",
        execute_once,
    )
    scope = CompactionScope(
        session_id="session:stack-free",
        workspace_id="workspace:stack-free",
        turn_id="turn:stack-free",
        scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
    )

    result = asyncio.run(
        coordinator._execute_compaction_fenced(
            turn_id=scope.turn_id,
            model_call_index=1,
            inherited_memory_use_policy=MemoryUsePolicy.ENABLED,
            trigger=CompactionTrigger.MANUAL,
            force=True,
            expected_scope=scope,
            post_adoption_branch=CompactionTargetBranch.ACTIVE_INSTALLATION,
            stable_command_id="command:stack-free",
            maximum_retained_tool_groups=1,
        )
    )

    assert result.outcome.public_code == "BELOW_TRIGGER"
    assert len(calls) == restart_count + 1
    attempt_token = calls[0]["attempt_token"]
    assert all(item["attempt_token"] is attempt_token for item in calls)
    assert calls[0]["pre_compact_dispatched"] is False
    assert all(item["pre_compact_dispatched"] is True for item in calls[1:])
    assert all(item["maximum_retained_tool_groups"] == 1 for item in calls)


def test_compaction_cut_lineage_uses_full_history_materialization_floor() -> None:
    source_identity = SimpleNamespace(
        session_id="session:test",
        turn_id="turn:test",
        initial_entry_id="entry:current-user",
        context_binding_revision_id="binding:source",
        provider_input_through_sequence=49,
        conversation_scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
    )
    successor_identity = SimpleNamespace(
        session_id="session:test",
        turn_id="turn:test",
        initial_entry_id="entry:current-user",
        context_binding_revision_id="binding:successor",
        provider_input_through_sequence=49,
        conversation_scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
    )
    source_binding = SimpleNamespace(
        binding_revision_id="binding:source",
        revision_ordinal=0,
        base_kind=ContextBindingBaseKind.FULL_HISTORY,
        context_snapshot_id=None,
        source_through_sequence=48,
    )
    successor_binding = SimpleNamespace(
        binding_revision_id="binding:successor",
        revision_ordinal=1,
        base_kind=ContextBindingBaseKind.SNAPSHOT,
        context_snapshot_id="snapshot:test",
        source_through_sequence=11,
    )
    source_prefix = SimpleNamespace(
        item_kind=FrozenProviderInputItemKind.USER,
        source_entry_id="entry:prefix",
        source_entry_sequence=11,
        text="old prefix",
    )
    retained_suffix = SimpleNamespace(
        item_kind=FrozenProviderInputItemKind.USER,
        source_entry_id="entry:current-user",
        source_entry_sequence=49,
        text="current request",
    )
    snapshot = SimpleNamespace(
        item_kind=FrozenProviderInputItemKind.CONTEXT_SNAPSHOT,
        source_entry_id=None,
        source_entry_sequence=11,
        text="summary",
    )
    shared_fact = object()

    def candidate(*, identity, binding, items):
        canonical_input = SimpleNamespace(
            identity=identity,
            items=items,
            canonical_utf8_bytes=sum(len(item.text.encode("utf-8")) for item in items),
            closures=(),
            late_outcomes=(),
        )
        return SimpleNamespace(
            canonical_read=SimpleNamespace(
                compile_snapshot=SimpleNamespace(
                    canonical_input=canonical_input,
                    context_binding_fact=binding,
                    run_permission_snapshot=shared_fact,
                    plan_workflow_fact=shared_fact,
                    plan_handoff_fact=shared_fact,
                    previous_turn_outcome_fact=shared_fact,
                    tool_observation_freshness_fact=shared_fact,
                ),
                replay_manifest_cut=SimpleNamespace(manifests=()),
            )
        )

    source = candidate(
        identity=source_identity,
        binding=source_binding,
        items=(source_prefix, retained_suffix),
    )
    successor = candidate(
        identity=successor_identity,
        binding=successor_binding,
        items=(snapshot, retained_suffix),
    )

    assert compaction_coordinator._compaction_cut_lineage_exactly_joins(
        source_candidate=source,
        successor_candidate=successor,
        phase="PRE_FULL",
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
    assert len(CONVERSATION_KERNEL_RELATIONS) == 28
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


def test_final_wire_quote_does_not_expand_fingerprint_or_registry_topology() -> None:
    contracts = ROOT / "src/pulsara_agent/conversation_kernel/compaction/contracts.py"
    tree = ast.parse(contracts.read_text(encoding="utf-8"), filename=str(contracts))
    source_view = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "FrozenCompactionSourceView"
    )
    field_names = {
        node.target.id
        for node in source_view.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }
    assert "provider_wire_quote" in field_names
    post_init = next(
        node
        for node in source_view.body
        if isinstance(node, ast.FunctionDef) and node.name == "__post_init__"
    )
    expected = next(
        node.value
        for node in ast.walk(post_init)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "expected"
            for target in node.targets
        )
    )
    fingerprint_constants = {
        node.value
        for node in ast.walk(expected)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert "provider_wire_quote" not in fingerprint_constants
    assert not any("quote" in value for value in fingerprint_constants)

    production = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "src/pulsara_agent").rglob("*.py")
    )
    assert "quote_fingerprint" not in production
    assert "measurement_registry" not in production
    assert "MAX_PREFIX_TRIALS" not in production
    assert "dispatch_crosses_threshold" not in production
    estimator = (ROOT / "src/pulsara_agent/llm/estimator.py").read_text(
        encoding="utf-8"
    )
    assert "openai_chat" not in estimator
    assert "openai_responses" not in estimator


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
