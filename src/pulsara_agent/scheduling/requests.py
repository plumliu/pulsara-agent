"""The closed management action schema shared by tool and HTTP owners."""

from __future__ import annotations
from pulsara_agent.scheduling.contracts import ScheduledTaskError

_FIELDS = {
    "list": (set(), {"cursor", "status", "session_id"}),
    "get": ({"task_id"}, set()),
    "create": ({"values"}, {"session_id"}),
    "update": ({"task_id", "expected_revision", "values"}, set()),
    "pause": ({"task_id", "expected_revision"}, set()),
    "resume": ({"task_id", "expected_revision"}, set()),
    "delete": ({"task_id", "expected_revision"}, set()),
    "run_now": (
        {"task_id", "expected_revision", "client_command_id", "request_at_utc"},
        {"session_id"},
    ),
}


def validate_action(value):
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("action"), str)
        or value["action"] not in _FIELDS
    ):
        raise ScheduledTaskError("INVALID_ACTION", "任务操作无效。")
    required, optional = _FIELDS[value["action"]]
    keys = set(value) - {"action"}
    if not required <= keys or keys - required - optional:
        raise ScheduledTaskError("INVALID_REQUEST", "任务操作字段无效。")
    for key in keys & {"task_id", "session_id", "client_command_id", "cursor"}:
        if not isinstance(value[key], str) or not value[key]:
            raise ScheduledTaskError("INVALID_REQUEST", "任务身份字段无效。")
    if "expected_revision" in value and (
        type(value["expected_revision"]) is not int or value["expected_revision"] < 1
    ):
        raise ScheduledTaskError("INVALID_REQUEST", "任务版本无效。")
    return value


def action_schema():
    properties = {
        "task_id": {"type": "string"},
        "session_id": {
            "type": ["string", "null"],
            "minLength": 1,
            "description": "Omit or use null for this session; otherwise copy the exact target session ID.",
        },
        "expected_revision": {"type": "integer", "minimum": 1},
        "client_command_id": {
            "type": "string",
            "description": "Stable command identity; preserve it and request_at_utc when retrying the same action.",
        },
        "request_at_utc": {
            "type": "string",
            "description": "Integral UTC ISO instant, for this manual action; preserve on retries.",
        },
        "cursor": {
            "type": "string",
            "description": "Copy next_cursor and repeat the same status/session_id filters; this cursor stores only a position.",
        },
        "status": {
            "type": "string",
            "enum": ["ACTIVE", "PAUSED", "COMPLETED", "ENABLED"],
            "description": "List filter: ENABLED groups ACTIVE and already dispatched one-time COMPLETED tasks. Other values match the canonical status exactly.",
        },
        "values": {
            "type": "object",
            "additionalProperties": False,
            "required": ["name", "prompt", "schedule", "timezone", "permission_mode"],
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Short task name. Choose one if the user did not supply it; preserve on unrelated edits.",
                },
                "prompt": {
                    "type": "string",
                    "description": "Self-contained instructions for future runs, including useful output expectations. Keep schedule and session/model/permission metadata in their fields. Preserve on unrelated edits.",
                },
                "timezone": {
                    "type": "string",
                    "description": "IANA timezone. New task default: Asia/Shanghai unless the user specifies another zone. Preserve the saved zone on unrelated edits.",
                },
                "permission_mode": {
                    "type": "string",
                    "description": "For create use the current effective preset, or a narrower one requested by the user. Preserve on unrelated edits; never exceed current permissions or silently narrow a saved task to bypass a rejection.",
                    "enum": [
                        "read-only",
                        "ask-permissions",
                        "accept-edits",
                        "bypass-permissions",
                    ],
                },
                "schedule": {
                    "oneOf": [
                        {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["contract", "kind", *fields],
                            "properties": {
                                "contract": {"const": "scheduled-rule:v1"},
                                "kind": {"const": kind},
                                **fields,
                            },
                        }
                        for kind, fields in {
                            "once": {"run_at_utc": {"type": "string"}},
                            "interval": {
                                "anchor_at_utc": {"type": "string"},
                                "seconds": {"type": "integer", "minimum": 1},
                            },
                            "daily": {
                                "start_date": {"type": "string"},
                                "time": {"type": "string"},
                            },
                            "weekly": {
                                "start_date": {"type": "string"},
                                "time": {"type": "string"},
                                "weekdays": {
                                    "type": "array",
                                    "minItems": 1,
                                    "uniqueItems": True,
                                    "items": {
                                        "type": "integer",
                                        "minimum": 1,
                                        "maximum": 7,
                                    },
                                },
                            },
                            "monthly": {
                                "start_date": {"type": "string"},
                                "time": {"type": "string"},
                                "day": {"type": "integer", "minimum": 1, "maximum": 31},
                            },
                        }.items()
                    ]
                },
            },
        },
    }
    return {
        "type": "object",
        "description": (
            "Besides action, send only the fields in its signature below; omit all others, even null. "
            "Omit optional fields for defaults. "
            + " ".join(
                f"{action}: required {', '.join(sorted(required)) or 'none'}; "
                f"optional {', '.join(sorted(optional)) or 'none'}."
                for action, (required, optional) in _FIELDS.items()
            )
        ),
        "oneOf": [
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["action", *sorted(required)],
                "properties": {
                    key: {"const": action} if key == "action" else properties[key]
                    for key in ["action", *sorted(required | optional)]
                },
            }
            for action, (required, optional) in _FIELDS.items()
        ],
    }
