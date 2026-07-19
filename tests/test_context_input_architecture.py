from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRIMITIVE_FILES = (
    ROOT / "src/pulsara_agent/primitives/_context_base.py",
    ROOT / "src/pulsara_agent/primitives/context.py",
    ROOT / "src/pulsara_agent/primitives/tool_observation.py",
    ROOT / "src/pulsara_agent/primitives/tool_result.py",
)


def test_context_primitives_do_not_depend_on_runtime_event_message_or_mcp() -> None:
    forbidden = (
        "pulsara_agent.runtime",
        "pulsara_agent.event",
        "pulsara_agent.message",
        "pulsara_agent.runtime.mcp",
    )
    for path in PRIMITIVE_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports = [
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        ]
        assert not any(
            module.startswith(prefix) for module in imports for prefix in forbidden
        ), f"{path.name} has a forbidden high-layer import"


def test_process_local_build_fingerprint_is_not_in_event_safe_primitives() -> None:
    for path in PRIMITIVE_FILES:
        assert "implementation_build_fingerprint" not in path.read_text(
            encoding="utf-8"
        )


def test_c5_deleted_context_facades_cannot_return() -> None:
    deleted = (
        ROOT / "src/pulsara_agent/runtime/context.py",
        ROOT / "src/pulsara_agent/runtime/context_engine/compiler.py",
        ROOT / "src/pulsara_agent/runtime/context_engine/lifecycle.py",
        ROOT / "src/pulsara_agent/runtime/context_engine/tool_results.py",
        ROOT / "src/pulsara_agent/runtime/context_input/legacy.py",
    )
    assert not any(path.exists() for path in deleted)
    forbidden_symbols = (
        "ContextCompile" + "Inputs",
        "ContextCompile" + "Request",
        "build_llm_" + "context(",
        "msg_to_llm_" + "messages(",
        "render_segmented_llm_" + "messages(",
        "build_unattributed_generic_" + "result_semantics",
        "to_legacy_renderer_" + "payload",
    )
    for root in (ROOT / "src", ROOT / "tests"):
        for path in root.rglob("*.py"):
            if path == Path(__file__).resolve():
                continue
            text = path.read_text(encoding="utf-8")
            assert not any(symbol in text for symbol in forbidden_symbols), (
                f"{path} reintroduced a deleted context facade"
            )


def test_execution_semantics_never_parses_serialized_tool_output() -> None:
    path = ROOT / "src/pulsara_agent/capability/result_semantics.py"
    text = path.read_text(encoding="utf-8")
    assert "json.loads" not in text
    assert "_json_object" not in text
    assert "result.output" not in text

    for root in (
        ROOT / "src/pulsara_agent/capability",
        ROOT / "src/pulsara_agent/runtime",
        ROOT / "src/pulsara_agent/tools",
    ):
        for source in root.rglob("*.py"):
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                function = node.func
                if not (
                    isinstance(function, ast.Attribute)
                    and function.attr == "loads"
                    and isinstance(function.value, ast.Name)
                    and function.value.id == "json"
                ):
                    continue
                argument = ast.unparse(node.args[0])
                assert not argument.endswith(".output"), (
                    f"{source} parses serialized tool output as JSON"
                )


def test_immutable_renderer_has_no_msg_or_loop_state_input() -> None:
    path = ROOT / "src/pulsara_agent/runtime/context_input/render.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert "Msg" not in names
    assert "LoopState" not in names
    assert "LLMMessage" not in names
    assert "LLMToolCall" not in names
    assert "segmented_messages" not in path.read_text(encoding="utf-8")

    compiler_text = (
        ROOT / "src/pulsara_agent/runtime/context_input/compiler.py"
    ).read_text(encoding="utf-8")
    assert "def lower_transcript_for_context(" in compiler_text
    assert "TranscriptToolResultRefFact" in compiler_text


def test_agent_uses_context_source_registry_not_legacy_candidate_facade() -> None:
    text = (ROOT / "src/pulsara_agent/runtime/agent.py").read_text(encoding="utf-8")
    assert "ContextCandidateCollectionInput" not in text
    assert "candidate_sources=" not in text
    assert "ContextCandidateSourceText" not in text
    assert "candidate_texts=" not in text
    assert "shadow_snapshot" not in text
    assert "render_runtime_context_prompt" not in text
    candidate_tree = ast.parse(
        (ROOT / "src/pulsara_agent/runtime/context_input/candidate.py").read_text(
            encoding="utf-8"
        )
    )
    collector = next(
        node
        for node in ast.walk(candidate_tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "collect_context_candidates"
    )
    assert "sources" not in {argument.arg for argument in collector.args.kwonlyargs}

    source_root = ROOT / "src/pulsara_agent/runtime/context_input/sources"
    for path in source_root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported_names = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        assert "ContextFactSnapshotFact" not in imported_names
        assert "LLMMessage" not in imported_names
    candidate_text = (
        ROOT / "src/pulsara_agent/runtime/context_input/candidate.py"
    ).read_text(encoding="utf-8")
    assert "memory_projection: str | None" not in candidate_text
    assert "subagent_results: str | None" not in candidate_text

    types_tree = ast.parse(
        (ROOT / "src/pulsara_agent/runtime/context_engine/types.py").read_text(
            encoding="utf-8"
        )
    )
    class_names = {
        node.name for node in ast.walk(types_tree) if isinstance(node, ast.ClassDef)
    }
    assert "ContextSection" not in class_names
    assert "ContextSectionSourceTiming" not in class_names
    assert "ContextSectionRenderTiming" not in class_names


def test_all_production_model_lifecycle_callers_supply_provider_input() -> None:
    callers = (
        ROOT / "src/pulsara_agent/runtime/agent.py",
        ROOT / "src/pulsara_agent/runtime/compaction/service.py",
        ROOT / "src/pulsara_agent/runtime/long_horizon/window_compaction_service.py",
        ROOT / "src/pulsara_agent/memory/reflection/engine.py",
        ROOT / "src/pulsara_agent/memory/governance/engine.py",
    )
    for path in callers:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        calls = tuple(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and (
                isinstance(node.func, ast.Name)
                and node.func.id == "prepare_model_lifecycle_start_bundle"
                or isinstance(node.func, ast.Attribute)
                and node.func.attr == "prepare_model_lifecycle_start_bundle"
            )
        )
        assert calls, f"{path} has no model lifecycle preparation"
        for call in calls:
            keywords = {item.arg for item in call.keywords}
            assert "provider_input_start_bundle" in keywords, (
                f"{path} bypasses canonical provider-input preparation"
            )

    coordinator = (
        ROOT / "src/pulsara_agent/runtime/provider_input/coordinator.py"
    ).read_text(encoding="utf-8")
    assert "ModelCallStartEvent" not in coordinator
    assert ".write_events(" not in coordinator


def test_new_context_source_and_provider_input_facts_declare_schema_version() -> None:
    for path in (
        ROOT / "src/pulsara_agent/primitives/context_source.py",
        ROOT / "src/pulsara_agent/primitives/provider_input.py",
    ):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if not any(
                isinstance(base, ast.Name) and base.id == "FrozenFactBase"
                for base in node.bases
            ):
                continue
            annotated = {
                item.target.id
                for item in node.body
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
            }
            assert "schema_version" in annotated, (
                f"{path}:{node.name} lacks a frozen schema_version"
            )


def test_provider_input_generation_hard_cut_has_no_remote_or_clock_ingress() -> None:
    source_root = ROOT / "src/pulsara_agent"
    forbidden_remote_ingress = (
        "previous_response_id",
        "context_management",
    )
    for path in source_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not any(item in text for item in forbidden_remote_ingress), (
            f"{path} reintroduced provider-owned continuation state"
        )

    pure_modules = (
        ROOT / "src/pulsara_agent/runtime/provider_input/materialization.py",
        ROOT / "src/pulsara_agent/runtime/provider_input/planner.py",
        ROOT / "src/pulsara_agent/runtime/provider_input/vector.py",
    )
    for path in pure_modules:
        text = path.read_text(encoding="utf-8")
        assert "utc_now(" not in text
        assert "datetime.now(" not in text
        assert "time.time(" not in text
        assert "ContextSourceRegistry" not in text
        assert "prepare_transcript_provider_projection" not in text
        assert "materialize_transcript_provider_projection" not in text


def test_provider_input_event_safe_state_keeps_bounded_references_only() -> None:
    primitives = ast.parse(
        (ROOT / "src/pulsara_agent/primitives/provider_input.py").read_text(
            encoding="utf-8"
        )
    )
    classes = {
        node.name: node
        for node in ast.walk(primitives)
        if isinstance(node, ast.ClassDef)
    }

    core_fields = {
        item.target.id
        for item in classes["CommittedProviderInputGenerationCoreStateFact"].body
        if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
    }
    assert "latest_model_start_event_ref" not in core_fields
    assert "preparation_ownership" not in core_fields
    assert "authority_horizons" not in core_fields

    for class_name in (
        "CanonicalProviderInputPlanFact",
        "ProviderTranscriptFrontierFact",
        "ProviderInputPendingContinuationFact",
        "ProviderInputAwaitingControlDispositionFact",
    ):
        fields = {
            item.target.id
            for item in classes[class_name].body
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
        }
        assert "authority_horizons" not in fields, (
            f"{class_name} embeds an unbounded cross-ledger horizon tuple"
        )

    event_text = (ROOT / "src/pulsara_agent/event/events.py").read_text(
        encoding="utf-8"
    )
    provider_event_region = event_text[
        event_text.index(
            "class ProviderInputGenerationStartedEvent"
        ) : event_text.index("class ModelCallTerminalProjectionCommittedEvent")
    ]
    assert "LLMContext" not in provider_event_region
    assert "ordered_messages" not in provider_event_region
    assert "materialized_input" not in provider_event_region


def test_provider_input_causal_order_hard_cut_cannot_regress() -> None:
    primitive_path = ROOT / "src/pulsara_agent/primitives/provider_input.py"
    primitive_tree = ast.parse(
        primitive_path.read_text(encoding="utf-8"), filename=str(primitive_path)
    )
    classes = {
        node.name: node
        for node in ast.walk(primitive_tree)
        if isinstance(node, ast.ClassDef)
    }

    def fields(class_name: str) -> set[str]:
        return {
            item.target.id
            for item in classes[class_name].body
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
        }

    assert not any(
        "successor" in field for field in fields("ProviderProjectionPositionFact")
    )
    unit_semantic_fields = fields("ProviderInputUnitSemanticFact")
    assert "provider_lane" not in unit_semantic_fields
    assert "invocation_classification" not in unit_semantic_fields

    planner_path = ROOT / "src/pulsara_agent/runtime/provider_input/planner.py"
    planner_text = planner_path.read_text(encoding="utf-8")
    for forbidden in (
        "current_trigger_units",
        "transcript_before_trigger",
        "current-trigger-unit",
        "ProviderInputTranscriptFrontierFact",
        "SessionWindowGenerationScopeFact",
    ):
        assert forbidden not in planner_text
    assert 'provider_lane != "current_user"' not in planner_text
    assert 'provider_lane == "current_user"' not in planner_text

    event_text = (ROOT / "src/pulsara_agent/event/events.py").read_text(
        encoding="utf-8"
    )
    assert "ProviderInputUnitAttributionSupplementedEvent" not in event_text

    provider_runtime_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "src/pulsara_agent/runtime/provider_input").glob("*.py")
    )
    assert '"provider-ordered-transcript-projection"' not in provider_runtime_text

    planner_tree = ast.parse(planner_text, filename=str(planner_path))
    rollover = next(
        node
        for node in ast.walk(planner_tree)
        if isinstance(node, ast.ClassDef) and node.name == "ProviderInputRolloverRequired"
    )
    initializer = next(
        node
        for node in rollover.body
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    assert [item.arg for item in initializer.args.args] == ["self", "request"]


def test_no_context_test_helper_infers_semantics_from_serialized_result_json() -> None:
    forbidden = (
        "build_execution_semantics_from_output",
        "infer_tool_result_semantics",
        "build_unattributed_generic_result_semantics",
        "to_legacy_renderer_payload",
    )
    for root in (ROOT / "tests/support", ROOT / "tests"):
        for path in root.rglob("*.py"):
            if path == Path(__file__).resolve():
                continue
            text = path.read_text(encoding="utf-8")
            assert not any(symbol in text for symbol in forbidden), (
                f"{path} reintroduced serialized-output semantics inference"
            )
