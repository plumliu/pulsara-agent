"""Static gates for the Round 5A.1 provider-terminal hard boundary."""

from __future__ import annotations

import ast
from pathlib import Path

from pulsara_agent.conversation_kernel.jobs import JOB_HANDLER_CATALOG
from pulsara_agent.conversation_kernel.vocabulary import (
    APPEND_GUARDS,
    COMMITTED_EVENT_DESCRIPTORS,
    LIVE_EVENT_TYPES,
    SUBJECT_SLOTS,
)
from pulsara_agent.storage.migrations.manifest import (
    CONVERSATION_KERNEL_RELATIONS,
)
from pulsara_agent.llm.adapters.openai.responses import (
    RESPONSES_REPLAYABLE_OUTPUT_ITEM_TYPES,
)


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION = ROOT / "src/pulsara_agent"
KERNEL = PRODUCTION / "conversation_kernel"


def _python_sources() -> tuple[Path, ...]:
    return tuple(sorted(PRODUCTION.rglob("*.py")))


def _call_sites(attribute_name: str) -> tuple[Path, ...]:
    result: list[Path] = []
    for path in _python_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(
            isinstance(node, ast.Attribute) and node.attr == attribute_name
            for node in ast.walk(tree)
        ):
            result.append(path)
    return tuple(result)


def test_round5a1_assistant_mutation_and_replay_have_single_process_owners() -> None:
    assert _call_sites("commit_assistant_message") == (
        KERNEL / "assistant_settlement.py",
    )
    assert _call_sites("reserve_assistant_replay_fragment") == (
        KERNEL / "assistant_settlement.py",
    )
    assert _call_sites("promote_assistant_replay_fragment") == (
        KERNEL / "assistant_settlement.py",
    )
    replay_constructors: list[Path] = []
    for path in _python_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "ProviderAssistantReplayFragment"
            for node in ast.walk(tree)
        ):
            replay_constructors.append(path)
    assert replay_constructors == [KERNEL / "direct_model.py"]


def test_round5a1_replay_is_process_local_and_remote_history_is_not_authority() -> None:
    forbidden = {
        "previous_response_id": [],
        "ProviderAssistantReplayFragment": [],
    }
    repository_paths = (
        KERNEL / "repository.py",
        *sorted((KERNEL / "_repository").glob("*.py")),
    )
    for path in _python_sources():
        source = path.read_text(encoding="utf-8")
        if "previous_response_id" in source:
            forbidden["previous_response_id"].append(
                str(path.relative_to(ROOT))
            )
    for path in repository_paths:
        if "ProviderAssistantReplayFragment" in path.read_text(encoding="utf-8"):
            forbidden["ProviderAssistantReplayFragment"].append(
                str(path.relative_to(ROOT))
            )
    assert forbidden == {
        "previous_response_id": [],
        "ProviderAssistantReplayFragment": [],
    }


def test_round5a1_responses_allowlist_and_oracles_remain_closed() -> None:
    assert RESPONSES_REPLAYABLE_OUTPUT_ITEM_TYPES == {
        "reasoning",
        "message",
        "function_call",
    }
    assert len(COMMITTED_EVENT_DESCRIPTORS) == 31
    assert len(LIVE_EVENT_TYPES) == 24
    assert len(SUBJECT_SLOTS) == 13
    assert len(APPEND_GUARDS) == 2
    assert len(CONVERSATION_KERNEL_RELATIONS) == 25
    assert len(JOB_HANDLER_CATALOG) == 1


def test_round5a1_terminal_path_does_not_import_compaction_or_recovery() -> None:
    paths = (
        KERNEL / "assistant_settlement.py",
        KERNEL / "direct_model.py",
        PRODUCTION / "llm/normalized_transport.py",
        PRODUCTION / "ports/provider_stream.py",
        PRODUCTION / "llm/adapters/openai/chat_completions.py",
        PRODUCTION / "llm/adapters/openai/responses.py",
    )
    forbidden = (
        "checkpoint",
        "receipt",
        "repair",
        "previous_response_id",
        "BACKGROUND_COMPACTION",
    )
    for path in paths:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        identifiers = {
            node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Name)
        } | {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert not any(
            token.lower() in value.lower()
            for token in forbidden
            for value in (*imports, *identifiers)
        ), path
