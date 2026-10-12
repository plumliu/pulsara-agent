"""Closed model-management actions; UI run-once has its own request owner."""

from __future__ import annotations
from pulsara_agent.scheduling.contracts import ScheduledTaskError

_FIELDS = {
    "list": (set(), {"cursor", "status", "session_id"}),
    "get": ({"task_id"}, set()),
    "create": ({"values"}, {"session_id", "status"}),
    "update": ({"task_id", "expected_revision", "values"}, set()),
    "pause": ({"task_id", "expected_revision"}, set()),
    "resume": ({"task_id", "expected_revision"}, set()),
    "delete": ({"task_id", "expected_revision"}, set()),
    "switch": ({"task_id", "expected_revision", "target"}, set()),
}


def creation_status(value=None):
    if value is None:
        return "PAUSED"
    if not isinstance(value, str) or value not in ("ACTIVE", "PAUSED"):
        raise ScheduledTaskError("INVALID_REQUEST", "创建状态须为 ACTIVE、PAUSED 或 null。")
    return value


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
    for key in keys & {"task_id", "session_id", "cursor"}:
        if not isinstance(value[key], str) or not value[key]:
            raise ScheduledTaskError("INVALID_REQUEST", "任务身份字段无效。")
    if "expected_revision" in value and (
        type(value["expected_revision"]) is not int or value["expected_revision"] < 1
    ):
        raise ScheduledTaskError("INVALID_REQUEST", "任务版本无效。")
    if value["action"] == "create":
        creation_status(value.get("status"))
    if value["action"] == "switch":
        target = value["target"]
        if not isinstance(target, dict) or target.get("kind") not in (
            "existing",
            "create",
        ):
            raise ScheduledTaskError("INVALID_REQUEST", "切换目标无效。")
        if target["kind"] == "existing":
            if set(target) != {"kind", "task_id", "expected_revision"}:
                raise ScheduledTaskError("INVALID_REQUEST", "切换目标字段无效。")
            validate_action(
                {
                    "action": "resume",
                    "task_id": target["task_id"],
                    "expected_revision": target["expected_revision"],
                }
            )
            if target["task_id"] == value["task_id"]:
                raise ScheduledTaskError("INVALID_REQUEST", "请选择另一个计划。")
        elif set(target) != {"kind", "values"}:
            raise ScheduledTaskError("INVALID_REQUEST", "切换目标字段无效。")
    return value


def action_schema():
    properties = {
        "task_id": {"type": "string"},
        "session_id": {
            "type": ["string", "null"],
            "minLength": 1,
            "description": "Omit or use null for this session; otherwise use the exact target session ID.",
        },
        "expected_revision": {"type": "integer", "minimum": 1},
        "cursor": {
            "type": "string",
            "description": "Copy next_cursor and repeat the same status/session_id filters.",
        },
        "status": {
            "type": ["string", "null"],
            "enum": ["ACTIVE", "PAUSED", None],
            "description": "Initial status: omitted/null means PAUSED; explicitly ACTIVE starts the automatic plan.",
        },
        "values": {
            "type": "object",
            "additionalProperties": False,
            "required": ["name", "prompt", "schedule", "timezone", "permission_mode"],
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Short task name; choose one if unspecified.",
                },
                "prompt": {
                    "type": "string",
                    "description": "Self-contained instructions and useful output expectations for future runs. Keep timing and session/model/permission configuration in their fields.",
                },
                "timezone": {
                    "type": "string",
                    "description": "IANA timezone; default new tasks to Asia/Shanghai unless the user specifies another zone.",
                },
                "permission_mode": {
                    "type": "string",
                    "description": "For create, use the current effective preset or a user-requested narrower one. Never exceed current permissions or silently narrow saved permissions to bypass a rejection.",
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
    properties["target"] = {
        "oneOf": [
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "task_id", "expected_revision"],
                "properties": {
                    "kind": {"const": "existing"},
                    "task_id": properties["task_id"],
                    "expected_revision": properties["expected_revision"],
                },
            },
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "values"],
                "properties": {
                    "kind": {"const": "create"},
                    "values": properties["values"],
                },
            },
        ]
    }
    return {
        "type": "object",
        "description": (
            "Send only the fields listed for the selected action. "
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
                    key: (
                        {"const": action}
                        if key == "action"
                        else {
                            "type": "string",
                            "enum": ["ACTIVE", "PAUSED"],
                            "description": "Filter by automatic plan status.",
                        }
                        if key == "status" and action == "list"
                        else properties[key]
                    )
                    for key in ["action", *sorted(required | optional)]
                },
            }
            for action, (required, optional) in _FIELDS.items()
        ],
    }
