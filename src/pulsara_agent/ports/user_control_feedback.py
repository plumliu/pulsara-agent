"""Closed human process-control and workspace-recreation facts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json


USER_CONTROL_FEEDBACK_MEDIA_TYPE = (
    "application/vnd.pulsara.user-control-feedback+json"
)


@dataclass(frozen=True, slots=True)
class UserControlProcessFact:
    disposition: str
    status: str
    exit_code: int | None
    physical_state: str
    group_alive: bool | None


@dataclass(frozen=True, slots=True)
class UserControlMonitorFact:
    monitor_id: str
    outcome: str
    in_flight_observation_ids: tuple[str, ...] = ()
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ProcessControlFeedbackContent:
    command_id: str
    session_id: str
    host_session_id: str
    process_id: str
    command: str
    cwd: str
    origin_turn_id: str
    origin_subagent_task_id: str | None
    target_root_turn_id: str
    process: UserControlProcessFact
    monitor: UserControlMonitorFact | None
    public_code: str
    public_detail: str
    kind: str = "process_control"
    schema_version: str = "user_control_feedback.v2"
    source: str = "USER_CONTROL"
    new_control_attempt: bool = True
    other_work_stopped: bool = False

    def __post_init__(self) -> None:
        if (
            self.kind != "process_control"
            or self.schema_version != "user_control_feedback.v2"
            or self.source != "USER_CONTROL"
            or self.new_control_attempt is not True
            or self.other_work_stopped is not False
        ):
            raise ValueError("user control feedback contract identity is invalid")
        if not all(
            (
                self.command_id,
                self.session_id,
                self.host_session_id,
                self.process_id,
                self.command,
                self.cwd,
                self.origin_turn_id,
                self.target_root_turn_id,
                self.public_code,
                self.public_detail,
            )
        ):
            raise ValueError("user control feedback is incomplete")
        if self.origin_subagent_task_id is not None and not self.origin_subagent_task_id:
            raise ValueError("user control feedback subagent origin is invalid")
        _validate_process_mapping(
            {
                "disposition": self.process.disposition,
                "status": self.process.status,
                "exit_code": self.process.exit_code,
                "physical_state": self.process.physical_state,
                "group_alive": self.process.group_alive,
            }
        )
        if self.monitor is not None:
            _validate_monitor_mapping(
                {
                    "monitor_id": self.monitor.monitor_id,
                    "outcome": self.monitor.outcome,
                    "in_flight_observation_ids": list(
                        self.monitor.in_flight_observation_ids
                    ),
                    "detail": self.monitor.detail,
                }
            )

    def canonical_mapping(self) -> dict[str, object]:
        monitor = None
        if self.monitor is not None:
            monitor = {
                "monitor_id": self.monitor.monitor_id,
                "outcome": self.monitor.outcome,
                "in_flight_observation_ids": list(
                    self.monitor.in_flight_observation_ids
                ),
                "detail": self.monitor.detail,
            }
        return {
            "kind": self.kind,
            "schema_version": self.schema_version,
            "source": self.source,
            "command_id": self.command_id,
            "session_id": self.session_id,
            "host_session_id": self.host_session_id,
            "process_id": self.process_id,
            "command": self.command,
            "cwd": self.cwd,
            "origin_turn_id": self.origin_turn_id,
            "origin_subagent_task_id": self.origin_subagent_task_id,
            "target_root_turn_id": self.target_root_turn_id,
            "process": {
                "disposition": self.process.disposition,
                "status": self.process.status,
                "exit_code": self.process.exit_code,
                "physical_state": self.process.physical_state,
                "group_alive": self.process.group_alive,
            },
            "monitor": monitor,
            "public_code": self.public_code,
            "public_detail": self.public_detail,
            "new_control_attempt": self.new_control_attempt,
            "other_work_stopped": self.other_work_stopped,
        }

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.canonical_mapping(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class WorkspaceRecreatedFeedbackContent:
    session_id: str
    workspace_root: str
    created_at: datetime
    target_root_turn_id: str
    kind: str = "workspace_recreated"
    schema_version: str = "user_control_feedback.v2"
    source: str = "USER_WORKSPACE_RECOVERY"
    action: str = "CREATE_EMPTY_DIRECTORY_AND_CONTINUE"

    def __post_init__(self) -> None:
        from pathlib import Path
        if (not isinstance(self.session_id, str) or not self.session_id
            or not isinstance(self.target_root_turn_id, str) or not self.target_root_turn_id
            or not isinstance(self.workspace_root, str)
            or not isinstance(self.created_at, datetime)
            or not Path(self.workspace_root).is_absolute()
            or self.created_at.utcoffset() is None
            or self.kind != "workspace_recreated"
            or self.schema_version != "user_control_feedback.v2"
            or self.source != "USER_WORKSPACE_RECOVERY"
            or self.action != "CREATE_EMPTY_DIRECTORY_AND_CONTINUE"):
            raise ValueError("workspace recreation feedback identity is invalid")

    def canonical_mapping(self) -> dict[str, object]:
        return {"schema_version": self.schema_version, "kind": self.kind,
                "source": self.source, "action": self.action,
                "session_id": self.session_id, "workspace_root": self.workspace_root,
                "created_at": self.created_at.isoformat(),
                "target_root_turn_id": self.target_root_turn_id}

    def canonical_bytes(self) -> bytes:
        return json.dumps(self.canonical_mapping(), ensure_ascii=False,
                          sort_keys=True, separators=(",", ":")).encode("utf-8")


UserControlFeedbackContent = ProcessControlFeedbackContent | WorkspaceRecreatedFeedbackContent

@dataclass(frozen=True, slots=True)
class UserControlFeedbackInstallationAttempt:
    session_id: str
    workspace_id: str
    writer_generation: int
    target_root_turn_id: str
    entry_id: str
    content: UserControlFeedbackContent
    occurred_at: datetime
    actor_id: str

    def __post_init__(self) -> None:
        if not all(
            (
                self.session_id,
                self.workspace_id,
                self.target_root_turn_id,
                self.entry_id,
                self.actor_id,
            )
        ) or self.writer_generation < 1:
            raise ValueError("user control feedback installation is incomplete")
        if self.content.session_id != self.session_id:
            raise ValueError("user control feedback session conflicts")
        if self.content.target_root_turn_id != self.target_root_turn_id:
            raise ValueError("user control feedback target conflicts")


__all__ = [
    "USER_CONTROL_FEEDBACK_MEDIA_TYPE",
    "ProcessControlFeedbackContent",
    "WorkspaceRecreatedFeedbackContent",
    "UserControlFeedbackContent",
    "UserControlFeedbackInstallationAttempt",
    "UserControlMonitorFact",
    "UserControlProcessFact",
    "project_user_control_feedback_for_provider",
]


def project_user_control_feedback_for_provider(content: bytes) -> str:
    """Validate stored v2 content and lower it to one attributed user fact."""

    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("user control feedback content is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError("user control feedback body must be an object")
    if value.get("kind") == "workspace_recreated":
        if set(value) != {"schema_version", "kind", "source", "action", "session_id",
                          "workspace_root", "created_at", "target_root_turn_id"}:
            raise ValueError("workspace recreation feedback fields are invalid")
        try:
            fact = WorkspaceRecreatedFeedbackContent(
                session_id=value["session_id"], workspace_root=value["workspace_root"],
                created_at=datetime.fromisoformat(value["created_at"]),
                target_root_turn_id=value["target_root_turn_id"], kind=value["kind"],
                schema_version=value["schema_version"], source=value["source"], action=value["action"],
            )
        except (TypeError, KeyError, ValueError) as exc:
            raise ValueError("workspace recreation feedback is invalid") from exc
        return json.dumps({"pulsara_user_control_feedback": fact.canonical_mapping(),
                           "handling": "The user explicitly asked the Host to recreate an empty workspace at the original path. Original files were not recovered. Historical file content does not establish what currently exists on disk; inspect actual files before using them."},
                          ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    fields = {
        "kind",
        "schema_version",
        "source",
        "command_id",
        "session_id",
        "host_session_id",
        "process_id",
        "command",
        "cwd",
        "origin_turn_id",
        "target_root_turn_id",
        "process",
        "public_code",
        "public_detail",
        "new_control_attempt",
        "other_work_stopped",
        "origin_subagent_task_id",
        "monitor",
    }
    if set(value) != fields or (
        value.get("kind") != "process_control"
        or value.get("schema_version") != "user_control_feedback.v2"
        or value.get("source") != "USER_CONTROL"
        or value.get("new_control_attempt") is not True
        or value.get("other_work_stopped") is not False
    ):
        raise ValueError("user control feedback identity is invalid")
    for field in (
        "command_id",
        "session_id",
        "host_session_id",
        "process_id",
        "command",
        "cwd",
        "origin_turn_id",
        "target_root_turn_id",
        "public_code",
        "public_detail",
    ):
        if not isinstance(value.get(field), str) or not value[field]:
            raise ValueError(f"user control feedback {field} is invalid")
    origin_subagent_task_id = value["origin_subagent_task_id"]
    if origin_subagent_task_id is not None and (
        not isinstance(origin_subagent_task_id, str) or not origin_subagent_task_id
    ):
        raise ValueError("user control feedback subagent origin is invalid")
    _validate_process_mapping(value["process"])
    monitor = value["monitor"]
    if monitor is not None:
        _validate_monitor_mapping(monitor)
    provider_value = {
        field: field_value
        for field, field_value in value.items()
        if field != "schema_version"
    }
    return json.dumps(
        {
            "pulsara_user_control_feedback": provider_value,
            "handling": (
                "This is a runtime-attributed fact about an explicit human control "
                "of one background process. Other work was not stopped. Do not "
                "interpret it as authorization to restart, retry, or control another "
                "process."
            ),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _validate_process_mapping(value: object) -> None:
    if not isinstance(value, dict) or set(value) != {
        "disposition",
        "status",
        "exit_code",
        "physical_state",
        "group_alive",
    }:
        raise ValueError("user control feedback process is invalid")
    for field in ("disposition", "status", "physical_state"):
        if not isinstance(value[field], str) or not value[field]:
            raise ValueError(f"user control feedback process {field} is invalid")
    exit_code = value["exit_code"]
    if exit_code is not None and (
        isinstance(exit_code, bool) or not isinstance(exit_code, int)
    ):
        raise ValueError("user control feedback process exit_code is invalid")
    if value["group_alive"] is not None and not isinstance(
        value["group_alive"], bool
    ):
        raise ValueError("user control feedback process group_alive is invalid")


def _validate_monitor_mapping(value: object) -> None:
    if not isinstance(value, dict) or set(value) != {
        "monitor_id",
        "outcome",
        "in_flight_observation_ids",
        "detail",
    }:
        raise ValueError("user control feedback monitor is invalid")
    if not isinstance(value["monitor_id"], str) or not value["monitor_id"]:
        raise ValueError("user control feedback monitor identity is invalid")
    if not isinstance(value["outcome"], str) or not value["outcome"]:
        raise ValueError("user control feedback monitor outcome is invalid")
    in_flight = value["in_flight_observation_ids"]
    if (
        not isinstance(in_flight, list)
        or any(not isinstance(item, str) or not item for item in in_flight)
        or len(set(in_flight)) != len(in_flight)
    ):
        raise ValueError("user control feedback monitor observations are invalid")
    if value["detail"] is not None and not isinstance(value["detail"], str):
        raise ValueError("user control feedback monitor detail is invalid")
