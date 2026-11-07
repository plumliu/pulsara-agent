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


def test_wait_schemas_share_exact_branches_and_reject_old_forms():
    agents = {"agents": [agent_target("ROOT")]}
    works = {"works": [work_target(random_id("queue"))]}
    for args in [agents, works]:
        validate_collaboration_parameters("wait_agent", args)
        assert Draft202012Validator(wait_agent_parameters_schema()).is_valid(args)
    native = Draft202012Validator(wait_agent_parameters_schema(include_works=False))
    assert native.is_valid(agents) and not native.is_valid(works)
    for args in [
        dict(agents, **works),
        dict(works, timeout_seconds=1),
        {"targets": agents["agents"]},
        {"agents": []},
        {"works": works["works"] * 2},
        {"agents": agents["agents"] * 2},
    ]:
        with pytest.raises(ValueError):
            validate_collaboration_parameters("wait_agent", args)
    for name, args in [
        ("run_agent", {"instruction": "old single"}),
        ("run_agent.batch", {"members": []}),
        ("wait_agent.works", works),
    ]:
        with pytest.raises(ValueError):
            validate_collaboration_parameters(name, args)


def test_assignment_schema_is_the_internal_member_definition():
    assert (
        run_agent_parameters_schema()["properties"]["members"]["items"]["properties"][
            "parameters"
        ]
        == assignment_parameters_schema()
    )
