"""Complete read-only conversation tool inputs and installed-history scope."""

from dataclasses import dataclass
from pulsara_agent.identity import ID_PAYLOAD_PATTERN

SESSION_QUERY_TOOL_NAMES = frozenset(
    {"search_sessions", "search_session_content", "read_session_content"}
)
SESSION_QUERY_MAX_ITEMS = (
    50  # User-interface database page size, not model search scope.
)


def _target_schema(kind, field, id_kind):
    return {
        "type": "object",
        "properties": {
            "kind": {"const": kind},
            field: {"type": "string", "pattern": rf"^{id_kind}:{ID_PAYLOAD_PATTERN}$"},
        },
        "required": ["kind", field],
        "additionalProperties": False,
    }


def session_target_schema():
    return _target_schema("SESSION", "session_id", "session")


def session_entry_target_schema():
    return _target_schema("SESSION_ENTRY", "entry_id", "entry")


def session_entry_target(entry_id):
    return {"kind": "SESSION_ENTRY", "entry_id": entry_id}


@dataclass(frozen=True, slots=True)
class SessionContentRange:
    through_sequence: int
    excluded_entry_id: str | None = None

    def __post_init__(self):
        if type(self.through_sequence) is not int or self.through_sequence < 0:
            raise ValueError("invalid session content range")


def session_query_input_schema(name):
    if name == "search_sessions":
        properties = {
            "query": {"type": "string"},
            "lifecycle": {"type": "string", "enum": ["ALL", "OPEN", "ARCHIVED"]},
        }
        required = []
    elif name == "search_session_content":
        properties = {
            "target": session_target_schema(),
            "query": {"type": "string", "minLength": 1, "pattern": r"\S"},
        }
        required = ["target", "query"]
    elif name == "read_session_content":
        return {
            "oneOf": [
                {
                    "type": "object",
                    "properties": {"target": target, "limit": limit},
                    "required": ["target", "limit"],
                    "additionalProperties": False,
                }
                for target, limit in (
                    (session_target_schema(), {"type": "integer", "maximum": -1}),
                    (session_entry_target_schema(), {"type": "integer", "not": {"const": 0}}),
                )
            ]
        }
    else:
        raise ValueError("Unknown history tool")
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def session_query_result_schema(name):
    """The complete history projection returned to JS, including err.result."""
    from pulsara_agent.ports.tool_execution import tool_business_value_schema

    if name == "search_sessions":
        properties = {
            "target": session_target_schema(),
            "title": {"type": "string"},
            "workspace_path": {"type": "string"},
            "updated_at": {"type": "string", "format": "date-time"},
            "matched_in": {"enum": ["ENTRY", "TITLE"]},
        }
        required = ["target", "title", "workspace_path", "updated_at"]
    elif name == "search_session_content":
        properties = {"target": session_entry_target_schema(), "text": {"type": "string"}}
        required = list(properties)
    elif name == "read_session_content":
        properties = {
            "target": session_entry_target_schema(),
            "role": {"enum": ["user message", "user steer", "assistant commentary", "assistant final answer"]},
            "at": {"type": "string", "format": "date-time"},
            "text": {"type": "string"},
        }
        required = list(properties)
    else:
        raise ValueError("Unknown history tool")
    record = {"type": "object", "properties": properties, "required": required,
        "additionalProperties": False}
    content = {"type": "array", "items": {"oneOf": [
        {"type": "object", "properties": {"type": {"const": "text"}, "value": {"type": "string"}},
            "required": ["type", "value"], "additionalProperties": False},
        {"type": "object", "properties": {"type": {"const": "object"}, "value": record},
            "required": ["type", "value"], "additionalProperties": False},
    ]}}
    return tool_business_value_schema(content_schema=content, statuses=("SUCCESS", "ERROR", "CANCELLED"))


def session_content_range(facts):
    """Use the actual installed compile cut; never read latest compaction here."""
    from pulsara_agent.model_input.contracts import (
        ContextBindingBaseKind,
        CompactionSnapshotCarrier,
    )

    binding = facts.context_binding_fact
    if binding.base_kind is ContextBindingBaseKind.FULL_HISTORY:
        return SessionContentRange(0)
    carriers = [
        item.content
        for item in facts.canonical_input.items
        if isinstance(item.content, CompactionSnapshotCarrier)
    ]
    if len(carriers) != 1:
        raise RuntimeError("session query lost the installed snapshot carrier")
    carrier = carriers[0]
    active = carrier.active_request_entry_id
    excluded = active if active in carrier.retained_entry_ids else None
    return SessionContentRange(binding.source_through_sequence, excluded)
