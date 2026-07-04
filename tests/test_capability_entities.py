from pulsara_agent.entities.capability import Plugin, Policy, Skill, Tool
from pulsara_agent.jsonld import NodeRef
from pulsara_agent.ontology import capability as cap


def test_skill_serializes_tool_edges() -> None:
    skill = Skill(
        id="skill:search",
        version="1.0.0",
        provides_tool=(NodeRef("tool:rg"),),
        requires=(NodeRef("tool:fd"),),
        allowed_in_scope="ctx:workspace/test_workspace",
        source_data_uri="https://pulsara.dev/skill/search.md",
    )

    doc = skill.to_jsonld()
    assert doc["@type"] == [cap.SKILL.name]
    assert doc[cap.VERSION.name] == "1.0.0"
    assert doc[cap.PROVIDES_TOOL.name] == [{"@id": "tool:rg"}]
    assert doc[cap.REQUIRES.name] == [{"@id": "tool:fd"}]


def test_tool_serializes_schemas() -> None:
    tool = Tool(
        id="tool:rg",
        version="14.1.0",
        has_input_schema="https://pulsara.dev/tool/rg/input.json",
        has_output_schema="https://pulsara.dev/tool/rg/output.json",
        allowed_in_scope="ctx:workspace/test_workspace",
    )

    doc = tool.to_jsonld()
    assert doc["@type"] == [cap.TOOL.name]
    assert doc[cap.HAS_INPUT_SCHEMA.name] == "https://pulsara.dev/tool/rg/input.json"
    assert doc[cap.ALLOWED_IN_SCOPE.name] == "ctx:workspace/test_workspace"


def test_plugin_serializes_provides_edges() -> None:
    plugin = Plugin(
        id="plugin:devkit",
        version="2.3.1",
        provides_tool=(NodeRef("tool:rg"), NodeRef("tool:fd")),
        provides_skill=(NodeRef("skill:search"),),
    )

    doc = plugin.to_jsonld()
    assert doc["@type"] == [cap.PLUGIN.name]
    assert doc[cap.PROVIDES_TOOL.name] == [{"@id": "tool:rg"}, {"@id": "tool:fd"}]
    assert doc[cap.PROVIDES_SKILL.name] == [{"@id": "skill:search"}]


def test_policy_serializes_scope_constraints() -> None:
    policy = Policy(
        id="policy:no-prod-writes",
        allowed_in_scope="ctx:dev",
        blocked_in_scope="ctx:prod",
    )

    doc = policy.to_jsonld()
    assert doc["@type"] == [cap.POLICY.name]
    assert doc[cap.ALLOWED_IN_SCOPE.name] == "ctx:dev"
    assert doc[cap.BLOCKED_IN_SCOPE.name] == "ctx:prod"
