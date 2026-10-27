"""Small read-only contract for recovering saved ROOT conversation content."""

from dataclasses import dataclass


SESSION_QUERY_TOOL_NAMES = frozenset(
    {"search_sessions", "search_session_content", "read_session_content"}
)
SESSION_QUERY_MAX_ITEMS = 50  # Existing cold-search page budget, never corpus size.


def session_target_schema():
    return {
        "type": "object",
        "properties": {
            "kind": {"const": "SESSION"},
            "session_id": {"type": "string", "minLength": 1, "description": "Exact session ID."},
        },
        "required": ["kind", "session_id"],
        "additionalProperties": False,
    }


def session_entry_target_schema():
    return {
        "type": "object",
        "properties": {
            "kind": {"const": "SESSION_ENTRY"},
            "session_id": {
                "type": "string", "minLength": 1,
                "description": "Exact session ID; omit for this conversation.",
            },
            "entry_id": {
                "type": "string", "minLength": 1,
                "description": "Exact entry ID in the selected session.",
            },
        },
        "required": ["kind", "entry_id"],
        "additionalProperties": False,
    }


def session_entry_target(session_id: str, entry_id: str):
    return {"kind": "SESSION_ENTRY", "session_id": session_id, "entry_id": entry_id}


@dataclass(frozen=True, slots=True)
class SessionContentRange:
    """Actual call's installed raw suffix floor, with its active-request exclusion."""

    through_sequence: int
    excluded_entry_id: str | None = None

    def __post_init__(self):
        if type(self.through_sequence) is not int or self.through_sequence < 0:
            raise ValueError("invalid session content range")
        if self.excluded_entry_id is not None and (
            not isinstance(self.excluded_entry_id, str) or not self.excluded_entry_id
        ):
            raise ValueError("invalid active request exclusion")


def session_query_input_schema(name: str, *, default_chars: int, maximum_chars: int):
    properties = {
        "cursor": {
            "type": "string",
            "minLength": 1,
            "description": "Exact next_cursor returned by this tool.",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": SESSION_QUERY_MAX_ITEMS,
            "default": 20,
        },
    }
    if name in {"search_sessions", "search_session_content"}:
        properties["query"] = {
            "type": "string",
            "description": (
                "Nonblank natural-language or technical-identifier query; Chinese and English are supported."
                if name == "search_session_content"
                else "Title or message keywords; omission or blank text lists by activity."
            ),
        }
        if name == "search_session_content":
            properties["query"].update(minLength=1, pattern=r"\S")
    if name == "search_sessions":
        properties["lifecycle"] = {
            "type": "string",
            "enum": ["ALL", "OPEN", "ARCHIVED"],
            "description": "Omit for ALL.",
        }
    else:
        properties.update(
            {
                "target": session_target_schema(),
                "include_tools": {
                    "type": "boolean",
                    "description": (
                        "Also search saved tool names, arguments and results, excluding "
                        "the three session-query tools' own calls/results. Omit for false."
                        if name == "search_session_content"
                        else "Include historical tool names, arguments, public results and "
                        "retained output. Results and retained output are labeled separately. "
                        "Omit for false."
                    ),
                },
            }
        )
    if name == "read_session_content":
        properties.update(
            {
                "target": {"oneOf": [session_target_schema(), session_entry_target_schema()]},
                "direction": {
                    "type": "string",
                    "enum": ["older", "newer"],
                    "description": (
                        "With SESSION_ENTRY, default newer; otherwise default older. "
                        "The anchor is read first; older then reads preceding messages, newer following messages. "
                        "Without an anchor, older starts at latest and newer at earliest."
                    ),
                },
                "max_chars": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": maximum_chars,
                    "default": default_chars,
                    "description": "Total text characters across this page's entries.",
                },
            }
        )
    budgets = {"limit", "max_chars"} if name == "read_session_content" else {"limit"}
    first = {key: value for key, value in properties.items() if key != "cursor"}
    continuation = {
        key: value for key, value in properties.items() if key in budgets | {"cursor"}
    }
    return {
        "type": "object",
        "oneOf": [
            {
                "type": "object",
                "properties": first,
                "required": ["query"] if name == "search_session_content" else [],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": continuation,
                "required": ["cursor"],
                "additionalProperties": False,
            },
        ],
    }


def session_content_range(facts):
    """Use the actual installed compile cut; never read latest compaction here."""
    from pulsara_agent.model_input.contracts import ContextBindingBaseKind, CompactionSnapshotCarrier
    binding=facts.context_binding_fact
    if binding.base_kind is ContextBindingBaseKind.FULL_HISTORY:
        return SessionContentRange(0)
    carriers=[item.content for item in facts.canonical_input.items if isinstance(item.content,CompactionSnapshotCarrier)]
    if len(carriers)!=1:
        raise RuntimeError('session query lost the installed snapshot carrier')
    active=carriers[0].active_request
    excluded=active.entry_id if active is not None and active.location.value=='SNAPSHOT_EXACT' else None
    return SessionContentRange(binding.source_through_sequence,excluded)
