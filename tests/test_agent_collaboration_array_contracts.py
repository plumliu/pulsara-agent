"""Ordered dispatch members and branch-specific wait contracts."""

import pytest
from jsonschema import Draft202012Validator

from pulsara_agent.identity import random_id
from pulsara_agent.conversation_kernel.subagents.collaboration_contracts import (
    agent_target,
    work_target,
    prepare_batch_refs,
    prepared_request,
    parse_batch_members,
    validate_batch_identities,
    validate_collaboration_parameters,
    assignment_parameters_schema,
    run_agent_parameters_schema,
    wait_agent_parameters_schema,
    wait_message_parameters_schema,
)


def envelope(parameters):
    refs = prepare_batch_refs(
        [{"target": p["target"]} if "target" in p else {} for p in parameters]
    )
    return refs, {
        "members": [
            dict(
                agent_id=refs["agents"][i]["agent_id"],
                work_id=refs["works"][i]["work_id"],
                parameters=p,
            )
            for i, p in enumerate(parameters)
        ]
    }


def test_single_and_multiple_dispatch_use_same_ordered_envelope():
    agent = agent_target(random_id("session"))
    for parameters in (
        [{"instruction": "one"}],
        [
            {"target": agent, "instruction": "one", "work_title": "same"},
            {"target": agent, "instruction": "two", "work_title": "same"},
        ],
    ):
        refs, arguments = envelope(parameters)
        validate_collaboration_parameters("run_agent", arguments)
        validate_batch_identities(arguments, refs)
        requests = parse_batch_members(arguments)
        assert [r.work_id for r in requests] == [t["work_id"] for t in refs["works"]]
        assert [r.agent_id for r in requests] == [t["agent_id"] for t in refs["agents"]]
        assert Draft202012Validator(run_agent_parameters_schema()).is_valid(arguments)
    assert (
        refs["agents"][0] == refs["agents"][1] and refs["works"][0] != refs["works"][1]
    )
    arguments["members"].reverse()
    with pytest.raises(ValueError, match="identities"):
        validate_batch_identities(arguments, refs)


@pytest.mark.parametrize("reuse", [False, True])
def test_work_title_on_create_and_reuse_has_one_utf8_contract(reuse):
    base = {
        "instruction": "x",
        **({"target": agent_target(random_id("session"))} if reuse else {}),
    }
    assert prepared_request(base).work_title is None
    assert (
        prepared_request({**base, "work_title": "汉" * 85 + "x"}).work_title
        == "汉" * 85 + "x"
    )
    for bad in [None, "", " \t", 1, "汉" * 86]:
        with pytest.raises(ValueError):
            prepared_request({**base, "work_title": bad})
    with pytest.raises(ValueError):
        prepared_request({**base, "name": "old title"})
    if reuse:
        with pytest.raises(ValueError):
            prepared_request({**base, "context": {"mode": "none"}})


def test_wait_schemas_are_distinct_closed_contracts():
    agents = {"agents": [agent_target(random_id("session"))]}
    works = {"works": [work_target(random_id("queue"))]}
    definitions = {
        "wait_message": (wait_message_parameters_schema(), [{}, {"timeout_seconds": 0}, {"timeout_seconds": 300}]),
        "wait_agent": (wait_agent_parameters_schema(), [agents, *[dict(agents, until=until, settle=settle, timeout_seconds=timeout) for until in ("idle", "work_end") for settle in ("all", "first") for timeout in (0, 30, 300)]]),
    }
    for name, (schema, valid) in definitions.items():
        validator = Draft202012Validator(schema)
        for args in valid:
            validate_collaboration_parameters(name, args)
            assert validator.is_valid(args)
        for args in [{"target": agent_target("ROOT")}, {"targets": []}, {"unknown": 1}]:
            with pytest.raises(ValueError):
                validate_collaboration_parameters(name, args)
            assert not validator.is_valid(args)
    for name, args in [
        ("wait_message", agents), ("wait_message", works), ("wait_message", {"settle": "all"}),
        ("wait_agent", {}), ("wait_agent", works), ("wait_agent", dict(agents, **works)),
        ("wait_agent", {"agents": []}),
        ("wait_agent", {"agents": agents["agents"] * 2}),
        ("wait_agent", dict(agents, settle="any")),
        ("wait_agent", dict(agents, until="next")), ("wait_agent", dict(agents, until=None)),
        ("wait_agent", {"agents": [agent_target("ROOT")]}),
    ]:
        with pytest.raises(ValueError):
            validate_collaboration_parameters(name, args)
        assert not Draft202012Validator(definitions[name][0]).is_valid(args)
    for name, args in [
        ("run_agent", {"instruction": "old single"}),
        ("run_agent.batch", {"members": []}),
        ("wait_agent.works", works),
        ("wait_work", works),
    ]:
        with pytest.raises(ValueError):
            validate_collaboration_parameters(name, args)


@pytest.mark.parametrize("name", ["wait_message", "wait_agent"])
@pytest.mark.parametrize("timeout", [True, False, None, "30", [], {}, -1, 301, float("nan"), float("inf"), -float("inf")])
def test_wait_timeout_rejects_invalid_values_at_actual_boundary(name, timeout):
    args = {"timeout_seconds": timeout}
    if name == "wait_agent":
        args["agents"] = [agent_target(random_id("session"))]
    with pytest.raises(ValueError):
        validate_collaboration_parameters(name, args)


def test_assignment_schema_is_the_internal_member_definition():
    assert (
        run_agent_parameters_schema()["properties"]["members"]["items"]["properties"][
            "parameters"
        ]
        == assignment_parameters_schema()
    )


def test_agent_wait_schema_defaults_match_observation_contract():
    properties = wait_agent_parameters_schema()["properties"]
    assert properties["until"]["default"] == "idle"
    assert properties["settle"]["default"] == "all"
    assert properties["timeout_seconds"]["default"] == 30


@pytest.mark.parametrize("until", [None, True, False, 0, "", "next", [], {}])
def test_agent_wait_rejects_invalid_until_types_and_values(until):
    args = {"agents": [agent_target(random_id("session"))], "until": until}
    with pytest.raises(ValueError):
        validate_collaboration_parameters("wait_agent", args)
    assert not Draft202012Validator(wait_agent_parameters_schema()).is_valid(args)
