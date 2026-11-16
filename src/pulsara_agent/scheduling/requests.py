"""Native schedule inputs. User-page concurrency stays with its HTTP owner."""
from __future__ import annotations

from jsonschema import Draft202012Validator

from pulsara_agent.identity import is_valid_id
from pulsara_agent.scheduling.contracts import ScheduledTaskError


def schedule_target(task_id):
    return {"kind": "SCHEDULE", "task_id": task_id}


def schedule_target_schema():
    return {"type": "object", "properties": {"kind": {"const": "SCHEDULE"}, "task_id": {"type": "string"}},
            "required": ["kind", "task_id"], "additionalProperties": False}


def creation_status(value=None):
    if value is None:
        return "PAUSED"
    if value not in ("ACTIVE", "PAUSED"):
        raise ScheduledTaskError("INVALID_REQUEST", "创建状态须为 ACTIVE 或 PAUSED。")
    return value


def schedule_rule_schema(*, creating: bool):
    properties = {
        "once": {"run_at": {"type": "string", "description": "指定 timezone 下的当地日期时间 YYYY-MM-DDTHH:mm:ss，不含 UTC offset；必须是可用的未来时点。"}},
        "interval": {"seconds": {"type": "integer", "minimum": 900, "description": "固定间隔秒数，至少 900 秒。"}, "anchor_at_utc": {"type": "string", "description": "带 UTC offset 的绝对锚点；创建时省略则固定为本次保存时刻。" if creating else "带 UTC offset 的绝对锚点；整体替换规则时必须填写。"}},
        "daily": {"time": {"type": "string", "description": "timezone 下的当地时间 HH:mm。"}, "start_date": {"type": "string", "description": "当地开始日期 YYYY-MM-DD；创建时省略则固定为 timezone 下的今天。" if creating else "当地开始日期 YYYY-MM-DD；整体替换规则时必须填写。"}},
        "weekly": {"time": {"type": "string", "description": "timezone 下的当地时间 HH:mm。"}, "weekdays": {"type": "array", "minItems": 1, "uniqueItems": True, "description": "周内日期，1 为星期一，7 为星期日。",
                    "items": {"type": "integer", "minimum": 1, "maximum": 7}}, "start_date": {"type": "string", "description": "当地开始日期 YYYY-MM-DD；创建时省略则固定为 timezone 下的今天。" if creating else "当地开始日期 YYYY-MM-DD；整体替换规则时必须填写。"}},
        "monthly": {"time": {"type": "string", "description": "timezone 下的当地时间 HH:mm。"}, "day": {"type": "integer", "minimum": 1, "maximum": 31, "description": "每月日期 1—31；当月没有该日期时跳过。"}, "start_date": {"type": "string", "description": "当地开始日期 YYYY-MM-DD；创建时省略则固定为 timezone 下的今天。" if creating else "当地开始日期 YYYY-MM-DD；整体替换规则时必须填写。"}},
    }
    return {"oneOf": [
        {"type": "object", "additionalProperties": False,
         "properties": {"kind": {"const": kind}, **fields},
         "required": ["kind", *(key for key in fields if not (creating and key in {"anchor_at_utc", "start_date"}))]}
        for kind, fields in properties.items()
    ]}


def schedule_values_schema(*, creating: bool):
    return {"type": "object", "additionalProperties": False, "minProperties": 1,
            "required": ["name", "prompt", "schedule"] if creating else [],
            "properties": {"name": {"type": "string", "minLength": 1}, "prompt": {"type": "string", "minLength": 1},
                           "timezone": {"type": "string", "description": "IANA 时区；创建时省略使用 Asia/Shanghai，修改时省略保留原值。"}, "schedule": schedule_rule_schema(creating=creating)}}


def manage_schedule_schema():
    target = schedule_target_schema()
    create_values = schedule_values_schema(creating=True)
    branches = [
        {"type": "object", "additionalProperties": False, "required": ["action", "values"],
         "properties": {"action": {"const": "create"}, "values": create_values,
                        "status": {"type": "string", "enum": ["ACTIVE", "PAUSED"]}}},
        {"type": "object", "additionalProperties": False, "required": ["target", "action", "replacement"],
         "properties": {"target": target, "action": {"const": "switch"}, "replacement": {"oneOf": [
             {"type": "object", "additionalProperties": False, "required": ["kind", "target"],
              "properties": {"kind": {"const": "existing"}, "target": target}},
             {"type": "object", "additionalProperties": False, "required": ["kind", "values"],
              "properties": {"kind": {"const": "create"}, "values": create_values}},
         ]}}},
    ]
    for action in ("update", "pause", "resume", "delete"):
        branches.append({"type": "object", "additionalProperties": False,
            "required": ["target", "action", *(["values"] if action == "update" else [])],
            "properties": {"target": target, "action": {"const": action},
                           **({"values": schedule_values_schema(creating=False)} if action == "update" else {})}})
    return {"oneOf": branches}


def query_schedules_schema():
    return {"oneOf": [
        {"type": "object", "additionalProperties": False, "properties": {
            "status": {"type": "string", "enum": ["ALL", "ACTIVE", "PAUSED"]}}},
        {"type": "object", "additionalProperties": False, "required": ["target"],
         "properties": {"target": schedule_target_schema()}},
    ]}


def validate_schedule_target(value):
    if (not isinstance(value, dict) or set(value) != {"kind", "task_id"}
        or value["kind"] != "SCHEDULE" or not is_valid_id(value["task_id"], "schedule")):
        raise ScheduledTaskError("INVALID_ID", "定时任务目标无效。")
    return value


def validate_action(value):
    if not isinstance(value, dict) or not Draft202012Validator(manage_schedule_schema()).is_valid(value):
        raise ScheduledTaskError("INVALID_REQUEST", "定时任务操作参数无效。")
    if "target" in value:
        validate_schedule_target(value["target"])
    if value["action"] == "switch" and value["replacement"]["kind"] == "existing":
        validate_schedule_target(value["replacement"]["target"])
        if value["replacement"]["target"] == value["target"]:
            raise ScheduledTaskError("INVALID_REQUEST", "请选择另一个计划。")
    return value


def validate_query(value):
    if not isinstance(value, dict) or not Draft202012Validator(query_schedules_schema()).is_valid(value):
        raise ScheduledTaskError("INVALID_REQUEST", "定时任务查询参数无效。")
    if "target" in value:
        validate_schedule_target(value["target"])
    return value
