"""Closed schedule query/management inputs; UI run-once has its own owner."""

from __future__ import annotations
from jsonschema import Draft202012Validator

from pulsara_agent.identity import is_valid_id
from pulsara_agent.scheduling.contracts import ScheduledTaskError
from pulsara_agent.ports.session_content import session_target_schema

_FIELDS = {
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
    if "target" in value:
        _validate_target(value["target"], "SCHEDULE", "task_id", "schedule")
    if "session" in value and value["session"] is not None:
        _validate_target(value["session"], "SESSION", "session_id", "session")
    if "expected_revision" in value and (
        type(value["expected_revision"]) is not int or value["expected_revision"] < 1
    ):
        raise ScheduledTaskError("INVALID_REQUEST", "任务版本无效。")
    if value["action"] == "create":
        creation_status(value.get("status"))
    if value["action"] == "switch":
        replacement = value["replacement"]
        if not isinstance(replacement, dict) or replacement.get("kind") not in (
            "existing", "create"
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
    # Preserve domain error classification before enforcing the shared complete
    # structure. The schema remains authoritative for every executable branch.
    if not Draft202012Validator(manage_schedule_schema()).is_valid(value):
        raise ScheduledTaskError("INVALID_REQUEST", "任务操作字段无效。")
    return value


def _properties():
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
    return properties


def manage_schedule_schema():
    properties = _properties()
    return {
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


def query_schedules_schema():
    properties = _properties()
    return {
        "oneOf": [
            {
                "type": "object",
                "additionalProperties": False,
                "required": [],
                "properties": {
                    "cursor": properties["cursor"],
                    "status": {"type": "string", "enum": ["ACTIVE", "PAUSED"]},
                    "session": properties["session"],
                },
            },
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["target"],
                "properties": {"target": properties["target"]},
            },
        ]
    }


def validate_query(value):
    if not isinstance(value, dict):
        raise ScheduledTaskError("INVALID_REQUEST", "任务查询字段无效。")
    schema = query_schedules_schema()
    branch = schema["oneOf"][1 if "target" in value else 0]
    if set(value) - branch["properties"].keys():
        raise ScheduledTaskError("INVALID_REQUEST", "任务查询字段无效。")
    if "target" in value:
        _validate_target(value["target"], "SCHEDULE", "task_id", "schedule")
    if value.get("session") is not None:
        _validate_target(value["session"], "SESSION", "session_id", "session")
    if "cursor" in value and not is_valid_id(value["cursor"], "schedule"):
        raise ScheduledTaskError("INVALID_ID", "任务身份字段无效。")
    # HTTP list previously received this classification from the repository.
    # Null remains a structural query error, distinct from create's null default.
    if (
        "target" not in value
        and value.get("status") is not None
        and not Draft202012Validator(
            schema["oneOf"][0]["properties"]["status"]
        ).is_valid(value["status"])
    ):
        raise ScheduledTaskError("INVALID_STATUS", "任务状态无效。")
    if not Draft202012Validator(schema).is_valid(value):
        raise ScheduledTaskError("INVALID_REQUEST", "任务查询字段无效。")
    return value
