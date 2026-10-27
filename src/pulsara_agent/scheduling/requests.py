"""Closed model-management actions; UI run-once has its own request owner."""

from __future__ import annotations
from pulsara_agent.identity import is_valid_id
from pulsara_agent.scheduling.contracts import ScheduledTaskError
from pulsara_agent.ports.session_content import session_target_schema

_FIELDS = {
    "list": (set(), {"cursor", "status", "session"}),
    "get": ({"target"}, set()),
    "create": ({"values"}, {"session", "status"}),
    "update": ({"target", "expected_revision", "values"}, set()),
    "pause": ({"target", "expected_revision"}, set()),
    "resume": ({"target", "expected_revision"}, set()),
    "delete": ({"target", "expected_revision"}, set()),
    "switch": ({"target", "expected_revision", "replacement"}, set()),
}


def schedule_target(task_id):
    return {"kind": "SCHEDULE", "task_id": task_id}


def schedule_target_schema():
    return {
        "type": "object",
        "properties": {"kind": {"const": "SCHEDULE"}, "task_id": {"type": "string"}},
        "required": ["kind", "task_id"],
        "additionalProperties": False,
    }


def _validate_target(value, kind, field, identity_kind):
    if not isinstance(value, dict) or set(value) != {"kind", field} or value["kind"] != kind:
        raise ScheduledTaskError("INVALID_REQUEST", "任务目标字段无效。")
    if not is_valid_id(value[field], identity_kind):
        raise ScheduledTaskError("INVALID_ID", "任务身份字段无效。")


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
    if "target" in keys:
        _validate_target(value["target"], "SCHEDULE", "task_id", "schedule")
    if "session" in keys:
        _validate_target(value["session"], "SESSION", "session_id", "session")
    if "cursor" in keys and not is_valid_id(value["cursor"], "schedule"):
        raise ScheduledTaskError("INVALID_ID", "任务身份字段无效。")
    if "expected_revision" in value and (
        type(value["expected_revision"]) is not int or value["expected_revision"] < 1
    ):
        raise ScheduledTaskError("INVALID_REQUEST", "任务版本无效。")
    if value["action"] == "create":
        creation_status(value.get("status"))
    if value["action"] == "switch":
        replacement = value["replacement"]
        if not isinstance(replacement, dict) or replacement.get("kind") not in (
            "existing",
            "create",
        ):
            raise ScheduledTaskError("INVALID_REQUEST", "切换目标无效。")
        if replacement["kind"] == "existing":
            if set(replacement) != {"kind", "target", "expected_revision"}:
                raise ScheduledTaskError("INVALID_REQUEST", "切换目标字段无效。")
            validate_action(
                {
                    "action": "resume",
                    "target": replacement["target"],
                    "expected_revision": replacement["expected_revision"],
                }
            )
            if replacement["target"] == value["target"]:
                raise ScheduledTaskError("INVALID_REQUEST", "请选择另一个计划。")
        elif set(replacement) != {"kind", "values"}:
            raise ScheduledTaskError("INVALID_REQUEST", "切换目标字段无效。")
    return value


def action_schema():
    properties = {
        "target": schedule_target_schema(),
        "session": {
            "oneOf": [session_target_schema(), {"type": "null"}],
            "description": "Omit or use null for this session; otherwise copy a SESSION target. This is the list filter or creation parent.",
        },
        "expected_revision": {"type": "integer", "minimum": 1},
        "cursor": {
            "type": "string",
            "description": "Copy next_cursor and repeat the same status/session filters.",
        },
        "status": {
            "type": ["string", "null"],
            "enum": ["ACTIVE", "PAUSED", None],
        },
        "values": {
            "type": "object",
            "additionalProperties": False,
            "required": ["name", "prompt", "schedule", "timezone", "permission_mode"],
            "properties": {
                "name": {
                    "type": "string",
                },
                "prompt": {
                    "type": "string",
                },
                "timezone": {
                    "type": "string",
                },
                "permission_mode": {
                    "type": "string",
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
    properties["replacement"] = {
        "oneOf": [
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "target", "expected_revision"],
                "properties": {
                    "kind": {"const": "existing"},
                    "target": properties["target"],
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
