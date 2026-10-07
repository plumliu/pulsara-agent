"""Small read-only contract for recovering saved ROOT conversation content."""

from dataclasses import dataclass


SESSION_QUERY_TOOL_NAMES = frozenset(
    {"search_sessions", "search_session_content", "read_session_content"}
)
SESSION_QUERY_MAX_ITEMS = 50  # Existing cold-search page budget, never corpus size.


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
            "description": "Continue using only this returned cursor and optional page budgets.",
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
            "description": "Literal keywords; all must match in one entry. Required on the first content search.",
        }
    if name == "search_sessions":
        properties["lifecycle"] = {
            "type": "string",
            "enum": ["ALL", "OPEN", "ARCHIVED"],
        }
    else:
        properties.update(
            {
                "session_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Omit for this session's earlier saved content.",
                },
                "include_tools": {
                    "type": "boolean",
                    "description": "Include saved tool calls (names and arguments), public results and retained output; default false.",
                },
            }
        )
    if name == "read_session_content":
        properties.update(
            {
                "entry_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Start with this exact message, default direction newer.",
                },
                "direction": {
                    "type": "string",
                    "enum": ["older", "newer"],
                    "description": "Message direction; without an anchor default older, from latest.",
                },
                "max_chars": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": maximum_chars,
                    "default": default_chars,
                    "description": "Total text characters for this page, across all entries.",
                },
            }
        )
    return {
        "type": "object",
        "properties": properties,
        "required": [],
        "additionalProperties": False,
    }
