from pulsara_agent.ontology import capability as cap
from pulsara_agent.ontology import context as ctx
from pulsara_agent.ontology import memory
from pulsara_agent.ontology import runtime as rt
from pulsara_agent.ontology.registry import CORE_CONTEXT, GRAPH_BASE


def test_core_context_contains_all_ontology_families() -> None:
    assert CORE_CONTEXT["mem"] == memory.MEMORY.base
    assert CORE_CONTEXT["ctx"] == ctx.CONTEXT_NS.base
    assert CORE_CONTEXT["rt"] == rt.RUNTIME.base
    assert CORE_CONTEXT["cap"] == cap.CAPABILITY.base
    assert CORE_CONTEXT["graph"] == GRAPH_BASE


def test_ontology_type_iris_are_split_by_family() -> None:
    assert memory.CLAIM.value == "https://pulsara.dev/memory#Claim"
    assert memory.DECISION.value == "https://pulsara.dev/memory#Decision"
    assert ctx.SCOPE.value == "https://pulsara.dev/context#Scope"
    assert rt.RUN_TIMELINE.value == "https://pulsara.dev/runtime#RunTimeline"
    assert rt.TOOL_RESULT.value == "https://pulsara.dev/runtime#ToolResult"
    assert cap.SKILL.value == "https://pulsara.dev/capability#Skill"


def test_memory_ontology_no_longer_exports_runtime_evidence_terms() -> None:
    for old_name in [
        "RUN_TIMELINE",
        "TURN",
        "TOOL_RESULT",
        "ARTIFACT",
        "EVIDENCE",
        "EVENT_SPAN",
        "SOURCE_SESSION",
        "SOURCE_RUN",
        "SOURCE_TURN",
        "SOURCE_REPLY",
        "SOURCE_EVENT",
        "START_SEQUENCE",
        "END_SEQUENCE",
        "PRODUCED",
        "STORED_AS",
        "ToolExecutionStatus",
        "EvidenceSourceType",
    ]:
        assert not hasattr(memory, old_name)
