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


def test_agent_uses_typed_candidate_sources_not_component_string_facade() -> None:
    text = (ROOT / "src/pulsara_agent/runtime/agent.py").read_text(encoding="utf-8")
    assert "ContextCandidateCollectionInput(" in text
    assert "ContextCandidateSourceText" not in text
    assert "candidate_texts=" not in text
    assert "shadow_snapshot" not in text
    assert "render_runtime_context_prompt" not in text
    agent_tree = ast.parse(text)
    candidate_input_calls = [
        node
        for node in ast.walk(agent_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ContextCandidateCollectionInput"
    ]
    assert candidate_input_calls
    for call in candidate_input_calls:
        keyword_names = {keyword.arg for keyword in call.keywords}
        assert "runtime_context" not in keyword_names
        assert "plan_revision" not in keyword_names

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
