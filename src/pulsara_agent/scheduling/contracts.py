"""Closed task values; configuration rows own plans, the ordinary queue owns runs."""

from __future__ import annotations
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from dateutil import rrule, tz
from pulsara_agent.primitives.permission import PermissionMode

UTC = timezone.utc


class ScheduledTaskError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def utc_instant(value: object) -> datetime:
    if not isinstance(value, str):
        raise ScheduledTaskError("INVALID_SCHEDULE", "UTC 时间必须为字符串。")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ScheduledTaskError("INVALID_SCHEDULE", "时间格式无效。") from exc
    if (
        result.tzinfo is None
        or result.utcoffset() != timedelta(0)
        or result.microsecond
    ):
        raise ScheduledTaskError("INVALID_SCHEDULE", "时间必须为整秒 UTC instant。")
    return result.astimezone(UTC)


def validate_rule(rule: object, timezone_name: str) -> dict:
    try:
        ZoneInfo(timezone_name)
    except (ValueError, TypeError, ZoneInfoNotFoundError) as exc:
        raise ScheduledTaskError("INVALID_TIMEZONE", "时区名称无效。") from exc
    if not isinstance(rule, dict) or rule.get("contract") != "scheduled-rule:v1":
        raise ScheduledTaskError("INVALID_SCHEDULE", "时间规则版本无效。")
    fields = {
        "once": {"run_at_utc"},
        "interval": {"anchor_at_utc", "seconds"},
        "daily": {"start_date", "time"},
        "weekly": {"start_date", "time", "weekdays"},
        "monthly": {"start_date", "time", "day"},
    }
    kind = rule.get("kind")
    if (
        not isinstance(kind, str)
        or kind not in fields
        or set(rule) != fields[kind] | {"contract", "kind"}
    ):
        raise ScheduledTaskError("INVALID_SCHEDULE", "时间规则字段无效。")
    value = dict(rule)
    if kind == "once":
        value["run_at_utc"] = utc_instant(value["run_at_utc"]).isoformat()
    elif kind == "interval":
        value["anchor_at_utc"] = utc_instant(value["anchor_at_utc"]).isoformat()
        if type(value["seconds"]) is not int or value["seconds"] <= 0:
            raise ScheduledTaskError("INVALID_SCHEDULE", "间隔必须为正整数秒。")
    else:
        try:
            if (
                not isinstance(value["start_date"], str)
                or date.fromisoformat(value["start_date"]).isoformat()
                != value["start_date"]
            ):
                raise ValueError()
            clock = value["time"]
            if (
                not isinstance(clock, str)
                or len(clock) != 5
                or time.fromisoformat(clock).strftime("%H:%M") != clock
            ):
                raise ValueError()
        except (TypeError, ValueError) as exc:
            raise ScheduledTaskError(
                "INVALID_SCHEDULE", "日期或 HH:mm 时间无效。"
            ) from exc
        if kind == "weekly":
            days = value["weekdays"]
            if (
                not isinstance(days, list)
                or not days
                or any(type(d) is not int or not 1 <= d <= 7 for d in days)
                or len(set(days)) != len(days)
            ):
                raise ScheduledTaskError("INVALID_SCHEDULE", "星期必须为不重复的 1–7。")
            value["weekdays"] = sorted(days)
        if kind == "monthly" and (
            type(value["day"]) is not int or not 1 <= value["day"] <= 31
        ):
            raise ScheduledTaskError("INVALID_SCHEDULE", "每月日期必须为 1–31。")
    return value


def next_occurrence(rule: dict, timezone_name: str, after: datetime) -> datetime | None:
    """dateutil enumerates only the current calendar window, never past years.

    Our closed interval=1 rules specify every phase field; rebasing the temporary
    dtstart to this period therefore preserves the sequence. DST is checked
    explicitly because dateutil can enumerate nonexistent wall times.
    """
    rule = validate_rule(rule, timezone_name)
    if after.tzinfo is None:
        raise ValueError("query instant must be timezone-aware")
    after = after.astimezone(UTC)
    kind = rule["kind"]
    try:
        if kind == "once":
            result = utc_instant(rule["run_at_utc"])
            return result if result > after else None
        if kind == "interval":
            anchor = utc_instant(rule["anchor_at_utc"])
            seconds = rule["seconds"]
            delta = after - anchor
            elapsed = delta.days * 86400 + delta.seconds
            index = max(0, elapsed // seconds + 1)
            return anchor + timedelta(seconds=index * seconds)
        zone = ZoneInfo(timezone_name)
        local = after.astimezone(zone)
        window = local.date()
        if kind == "weekly":
            window -= timedelta(days=window.weekday())
        elif kind == "monthly":
            window = window.replace(day=1)
        start = max(window, date.fromisoformat(rule["start_date"]))
        hour, minute = map(int, rule["time"].split(":"))
        kwargs = dict(
            dtstart=datetime.combine(start, time(), zone),
            interval=1,
            byhour=hour,
            byminute=minute,
            bysecond=0,
            cache=False,
            wkst=rrule.MO,
        )
        if kind == "weekly":
            kwargs["byweekday"] = tuple(d - 1 for d in rule["weekdays"])
        elif kind == "monthly":
            kwargs["bymonthday"] = rule["day"]
        freq = {"daily": rrule.DAILY, "weekly": rrule.WEEKLY, "monthly": rrule.MONTHLY}[
            kind
        ]
        for candidate in rrule.rrule(freq, **kwargs):
            if not tz.datetime_exists(candidate):
                continue
            instant = candidate.replace(fold=0).astimezone(UTC)
            if instant > after:
                return instant
        return None
    except (ValueError, OverflowError):
        raise ScheduledTaskError(
            "TIME_RANGE_EXHAUSTED", "时间规则超出可表示范围。"
        ) from None


@dataclass(frozen=True, slots=True)
class ScheduledAdmission:
    task_id: str
    session_id: str
    revision: int
    due_at: datetime
    prompt: str
    permission_mode: PermissionMode
    manual: bool = False
    client_command_id: str | None = None

    def __post_init__(self):
        if (
            not self.task_id
            or not self.session_id
            or type(self.revision) is not int
            or self.revision < 1
            or not self.prompt.strip()
        ):
            raise ValueError("scheduled admission identity is invalid")
        if (
            self.due_at.tzinfo is None
            or self.due_at.microsecond
            or self.due_at.utcoffset() != timedelta(0)
        ):
            raise ValueError("scheduled due must be an integral UTC instant")
        if self.manual != (self.client_command_id is not None):
            raise ValueError("manual command identity is incomplete")

    def provenance(self) -> dict:
        return {
            "task_id": self.task_id,
            "task_revision": self.revision,
            "due_at_utc": self.due_at.isoformat(),
        }

    def action_value(self) -> dict:
        return {
            "client_command_id": self.client_command_id,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "expected_revision": self.revision,
            "request_at_utc": self.due_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class ScheduledProvenance:
    task_id: str
    task_revision: int
    due_at_utc: str

    def __post_init__(self):
        if (
            not isinstance(self.task_id, str)
            or not self.task_id
            or type(self.task_revision) is not int
            or self.task_revision < 1
        ):
            raise ValueError("invalid scheduled provenance")
        utc_instant(self.due_at_utc)

    def to_dict(self):
        return {
            "task_id": self.task_id,
            "task_revision": self.task_revision,
            "due_at_utc": self.due_at_utc,
        }

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != {
            "task_id",
            "task_revision",
            "due_at_utc",
        }:
            raise ValueError("scheduled provenance is not closed")
        return cls(**value)
