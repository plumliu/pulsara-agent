"""Public Agent/Work parameters and process-local request preparation.

IDs here are the real session/request IDs. Preparation owns no rows or execution.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Mapping

from jsonschema import Draft202012Validator

from pulsara_agent.identity import ID_PAYLOAD_PATTERN, random_id, require_id

AGENT_TOOLS = frozenset(
    {
        "run_agent",
        "query_agents",
        "send_agent_message",
        "wait_agent",
        "stop_agent",
    }
)


def closed(
    value: object, allowed: set[str], required: set[str] = frozenset()
) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(k, str) for k in value):
        raise ValueError("parameters must be an object")
    if set(value) - allowed or required - set(value):
        raise ValueError("unexpected or missing parameter")
    return value


def target_id(value: object, kind: str, *, allow_root: bool = False) -> str:
    key = "agent_id" if kind == "AGENT" else "work_id"
    target = closed(value, {"kind", key}, {"kind", key})
    if target["kind"] != kind:
        raise ValueError(f"target must have kind {kind}")
    identity = target[key]
    if allow_root and identity == "ROOT":
        return "ROOT"
    return require_id(identity, "session" if kind == "AGENT" else "queue")


def agent_target(identity: str) -> dict[str, str]:
    return {"kind": "AGENT", "agent_id": identity}


def work_target(identity: str) -> dict[str, str]:
    return {"kind": "WORK", "work_id": identity}


def target_schema(kind: str, *, allow_root: bool = False) -> dict:
    key = "agent_id" if kind == "AGENT" else "work_id"
    prefix = "session" if kind == "AGENT" else "queue"
    pattern = f"^{prefix}:{ID_PAYLOAD_PATTERN}$"
    identity = {"type": "string", "pattern": pattern}
    if allow_root:
        identity = {"anyOf": [identity, {"const": "ROOT"}]}
    return {
        "type": "object",
        "properties": {"kind": {"const": kind}, key: identity},
        "required": ["kind", key],
        "additionalProperties": False,
    }


def query_agents_input_schema() -> dict:
    page = {
        "cursor": {"type": "string", "minLength": 1, "maxLength": 512},
        # The existing collaboration per-page boundary, not a membership cap.
        "max_items": {"type": "integer", "minimum": 1, "maximum": 50, "default": 50},
    }
    return {
        "oneOf": [
            {
                "type": "object",
                "properties": fields,
                "required": required,
                "additionalProperties": False,
            }
            for fields, required in (
                (page, []),
                ({"target": target_schema("AGENT"), **page}, ["target"]),
                ({"target": target_schema("WORK")}, ["target"]),
            )
        ]
    }


def _targets(
    value: object,
    kind: str,
    *,
    maximum: int | None = None,
    nonempty: bool = False,
    allow_root: bool = False,
) -> tuple[str, ...]:
    if (
        not isinstance(value, (list, tuple))
        or (nonempty and not value)
        or (maximum is not None and len(value) > maximum)
    ):
        raise ValueError("target collection has invalid shape or size")
    identities = tuple(target_id(item, kind, allow_root=allow_root) for item in value)
    if len(set(identities)) != len(identities):
        raise ValueError("duplicate targets are not allowed")
    return identities


def _text(value: object, name: str, maximum_utf8_bytes: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty text")
    if (
        maximum_utf8_bytes is not None
        and len(value.encode("utf-8")) > maximum_utf8_bytes
    ):
        raise ValueError(f"{name} exceeds its UTF-8 byte boundary")
    return value


def validate_run_parameters(arguments: Mapping[str, object]) -> None:
    a = closed(
        arguments,
        {
            "instruction",
            "target",
            "name",
            "context",
            "materials",
            "depends_on",
        },
        {"instruction"},
    )
    # Preserve the existing per-request text boundaries; these are not history caps.
    _text(a["instruction"], "instruction", 65_536)
    if "target" in a:
        target_id(a["target"], "AGENT")
        if "name" in a or "context" in a:
            raise ValueError(
                "name and context are available only when creating an Agent"
            )
    if "name" in a:
        _text(a["name"], "name", 256)
    if "context" in a:
        context = closed(a["context"], {"mode", "turns"}, {"mode"})
        if context["mode"] == "none":
            closed(context, {"mode"}, {"mode"})
        elif context["mode"] == "last_n":
            if type(context.get("turns")) is not int or not 1 <= context["turns"] <= 3:
                raise ValueError("last_n context requires turns in 1..3")
        else:
            raise ValueError("unknown context mode")
    for field in ("materials", "depends_on"):
        if field in a:
            _targets(a[field], "WORK", maximum=16)


def validate_collaboration_parameters(
    name: str, arguments: Mapping[str, object]
) -> None:
    if name == "run_agent":
        validate_run_parameters(arguments)
        return
    if name == "run_agent.batch":
        parse_batch_members(arguments)
        return
    if name == "query_agents":
        a = closed(arguments, {"target", "cursor", "max_items"})
        if not Draft202012Validator(query_agents_input_schema()).is_valid(dict(a)):
            raise ValueError("use one query_agents branch; WORK accepts only target")
        if "target" in a:
            target_id(a["target"], a["target"]["kind"])
    elif name == "stop_agent":
        a = closed(arguments, {"target"}, {"target"})
        kind = a["target"].get("kind") if isinstance(a["target"], Mapping) else None
        if kind not in {"AGENT", "WORK"}:
            raise ValueError("expected AGENT or WORK target")
        target_id(a["target"], kind)
    elif name == "send_agent_message":
        a = closed(arguments, {"target", "message"}, {"target", "message"})
        target_id(a["target"], "AGENT", allow_root=True)
        _text(a["message"], "message", 16_384)
    elif name in {"wait_agent", "wait_agent.works"}:
        works = name.endswith(".works")
        a = closed(
            arguments,
            {"targets", "settle"}
            if works
            else {"targets", "settle", "timeout_seconds"},
            {"targets"},
        )
        _targets(
            a["targets"],
            "WORK" if works else "AGENT",
            nonempty=True,
            allow_root=not works,
        )
        if a.get("settle", "all") not in {"all", "first"}:
            raise ValueError("settle must be all or first")
        if not works:
            timeout = a.get("timeout_seconds", 30)
            if (
                isinstance(timeout, bool)
                or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout)
                or not 0 <= timeout <= 300
            ):
                raise ValueError("timeout_seconds must be in 0..300")
    else:
        raise ValueError("unknown collaboration operation")
    if "cursor" in a:
        _text(a["cursor"], "cursor", 512)
    if "max_items" in a and (
        type(a["max_items"]) is not int or not 1 <= a["max_items"] <= 50
    ):
        raise ValueError("max_items must be in 1..50")


@dataclass(frozen=True, slots=True)
class PreparedAgentRequest:
    agent_id: str
    work_id: str
    instruction: str
    create_agent: bool
    name: str | None = None
    context_mode: str = "none"
    context_turns: int | None = None
    materials: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    initial_context: tuple[str, ...] = ()

    def __post_init__(self):
        if type(self.create_agent) is not bool:
            raise ValueError("create_agent must be a frozen boolean")
        if any(
            not isinstance(value, tuple)
            for value in (self.materials, self.depends_on, self.initial_context)
        ):
            raise ValueError("prepared request collections must be immutable tuples")
        if any(not isinstance(value, str) for value in self.initial_context):
            raise ValueError("initial context must contain actual text")
        require_id(self.agent_id, "session")
        require_id(self.work_id, "queue")
        arguments = {
            "instruction": self.instruction,
            "materials": [work_target(x) for x in self.materials],
            "depends_on": [work_target(x) for x in self.depends_on],
        }
        if self.create_agent:
            if self.name is not None:
                arguments["name"] = self.name
            arguments["context"] = {
                "mode": self.context_mode,
                **(
                    {"turns": self.context_turns}
                    if self.context_turns is not None
                    else {}
                ),
            }
        else:
            arguments["target"] = agent_target(self.agent_id)
            if (
                self.name is not None
                or self.context_mode != "none"
                or self.context_turns is not None
            ):
                raise ValueError("reused Agent cannot carry creation options")
        validate_run_parameters(arguments)


def prepare_batch_refs(specs: Mapping[str, Mapping[str, object]]) -> dict[str, dict]:
    if not isinstance(specs, Mapping) or not 1 <= len(specs) <= 16:
        raise ValueError("one batch accepts 1..16 members")
    peers = {}
    for key, spec in specs.items():
        if not isinstance(key, str) or not key or not isinstance(spec, Mapping):
            raise ValueError(
                "batch member must have a nonempty key and object specification"
            )
        identity = (
            target_id(spec["target"], "AGENT")
            if "target" in spec
            else random_id("session")
        )
        peers[key] = {
            "agent": agent_target(identity),
            "work": work_target(random_id("queue")),
        }
    return peers


def prepared_request(
    parameters: Mapping[str, object],
    *,
    agent_id: str | None = None,
    work_id: str | None = None,
) -> PreparedAgentRequest:
    validate_run_parameters(parameters)
    create = "target" not in parameters
    actual_agent = (
        target_id(parameters["target"], "AGENT")
        if not create
        else agent_id or random_id("session")
    )
    if agent_id is not None and actual_agent != agent_id:
        raise ValueError("batch target changed after identities were prepared")
    context = parameters.get("context", {"mode": "none"})
    return PreparedAgentRequest(
        agent_id=actual_agent,
        work_id=work_id or random_id("queue"),
        instruction=parameters["instruction"],
        create_agent=create,
        name=parameters.get("name"),
        context_mode=context["mode"],
        context_turns=context.get("turns"),
        materials=_targets(parameters.get("materials", []), "WORK", maximum=16),
        depends_on=_targets(parameters.get("depends_on", []), "WORK", maximum=16),
    )


def parse_batch_members(
    arguments: Mapping[str, object],
) -> tuple[tuple[str, PreparedAgentRequest], ...]:
    a = closed(arguments, {"members"}, {"members"})
    members = a["members"]
    if not isinstance(members, (list, tuple)) or not 1 <= len(members) <= 16:
        raise ValueError("one batch accepts 1..16 members")
    result = []
    for member in members:
        m = closed(
            member,
            {"key", "agent_id", "work_id", "parameters"},
            {"key", "agent_id", "work_id", "parameters"},
        )
        key = _text(m["key"], "member key")
        result.append(
            (
                key,
                prepared_request(
                    m["parameters"], agent_id=m["agent_id"], work_id=m["work_id"]
                ),
            )
        )
    if len({k for k, _ in result}) != len(result) or len(
        {r.work_id for _, r in result}
    ) != len(result):
        raise ValueError("batch member identities must be unique")
    if sum(len(r.depends_on) for _, r in result) > 64:
        raise ValueError("one batch accepts at most 64 explicit dependencies")
    validate_agent_request_graph(tuple(r for _, r in result))
    batch_ids = {r.work_id for _, r in result}
    if any(batch_ids.intersection(r.materials) for _, r in result):
        raise ValueError("unfinished batch work must use depends_on, not materials")
    return tuple(result)


def validate_batch_identities(
    arguments: Mapping[str, object], peers: Mapping[str, Mapping[str, object]]
) -> None:
    members = parse_batch_members(arguments)
    if list(peers) != [k for k, _ in members]:
        raise ValueError("batch member order or keys changed")
    for key, request in members:
        if peers[key] != {
            "agent": agent_target(request.agent_id),
            "work": work_target(request.work_id),
        }:
            raise ValueError("batch identities changed after preparation")


def validate_agent_request_graph(
    requests: tuple[PreparedAgentRequest, ...],
    existing: tuple[tuple[str, str, tuple[str, ...]], ...] = (),
) -> None:
    """Reject cycles combining successful prerequisites and Agent FIFO positions."""
    ordered = (*existing, *((r.work_id, r.agent_id, r.depends_on) for r in requests))
    graph = {work: set(deps) for work, _, deps in ordered}
    if len(graph) != len(ordered):
        raise ValueError("duplicate request identity")
    previous = {}
    for work, agent, _ in ordered:
        if agent in previous:
            graph[work].add(previous[agent])
        previous[agent] = work
    # Iterative traversal supports long-lived queues without a Python recursion cap.
    done = set()
    for start in graph:
        visiting = set()
        stack = [(start, False)]
        while stack:
            node, leaving = stack.pop()
            if leaving:
                visiting.remove(node)
                done.add(node)
            elif node in visiting:
                raise ValueError(
                    "successful dependencies and Agent FIFO form a waiting cycle"
                )
            elif node not in done and node in graph:
                visiting.add(node)
                stack.append((node, True))
                stack.extend((dep, False) for dep in graph[node] if dep in graph)
