"""Host admission, command, lifecycle, and inspection value types."""

from __future__ import annotations


import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from pulsara_agent.capability.resolver import EffectiveSkillCatalogInspection
from pulsara_agent.conversation_kernel.contracts import PromptDeliveryMode
from pulsara_agent.conversation_kernel.mcp.contracts import (
    McpCatalogSnapshot,
    McpConfiguredServerInspection,
)
from pulsara_agent.conversation_kernel.prompt_content import FrozenCanonicalPrompt
from pulsara_agent.conversation_kernel.queued_prompt_actions import QueuedPromptAction
from pulsara_agent.conversation_kernel.request_execution import RequestExecution
from pulsara_agent.conversation_kernel.runner import KernelRunResult
from pulsara_agent.conversation_kernel.steer import StartedTurnInputGroup
from pulsara_agent.conversation_kernel.user_control import (
    ControlQueryStatus,
    ProcessControlResult,
    UserControlOutcome,
    UserControlRequest,
)
from pulsara_agent.llm.model_connections import (
    ModelCallBinding,
    model_call_binding_to_dict,
)
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.plan_workflow import PlanDraftDecision, PlanHandoffKind
from pulsara_agent.scheduling.contracts import ScheduledAdmission
from pulsara_agent.terminal_process.models import TerminalProcessInfo


@dataclass(frozen=True, slots=True)
class KernelSessionSummary:
    session_id: str
    workspace_id: str
    workspace_kind: str
    workspace_root: str
    workspace_label: str
    memory_domain_id: str
    lifecycle: str
    writer_generation: int
    latest_entry_sequence: int
    updated_at: datetime
    title: str | None = None
    model_call_binding: ModelCallBinding | None = None
    latest_root_turn: dict[str, str] | None = None
    subagent_count: int | None = None

    @property
    def runtime_session_id(self) -> str:
        return self.session_id

    def to_dict(self) -> dict[str, object]:
        return {
            "runtime_session_id": self.session_id,
            "title": self.title,
            "workspace_id": self.workspace_id,
            "workspace_kind": self.workspace_kind,
            "workspace_root": self.workspace_root,
            "workspace_label": self.workspace_label,
            "memory_domain_id": self.memory_domain_id,
            "lifecycle": self.lifecycle,
            "writer_generation": self.writer_generation,
            "latest_entry_sequence": self.latest_entry_sequence,
            "latest_root_turn": self.latest_root_turn,
            "updated_at": self.updated_at.isoformat(),
            "model_call_binding": model_call_binding_to_dict(self.model_call_binding),
        }


@dataclass(frozen=True, slots=True)
class KernelPromptDelivery:
    queue_item_id: str
    queue_status: str
    consumed_entry_id: str | None
    delivery_mode: str
    execution: RequestExecution | None


@dataclass(frozen=True, slots=True)
class KernelCommandOutcome:
    command_id: str
    status: str
    target_id: str
    public_code: str
    public_message: str
    plan_workflow_status: str | None = None
    resume_permission_mode: PermissionMode | None = None
    handoff_created_at_commit: bool = False
    plan_workflow_revision: int | None = None
    plan_draft_decision: PlanDraftDecision | None = None
    plan_continuation_turn_id: str | None = None
    prompt_delivery: KernelPromptDelivery | None = None
    user_control: UserControlOutcome | None = None


@dataclass(frozen=True, slots=True)
class KernelControlQueryResult:
    status: ControlQueryStatus
    outcome: KernelCommandOutcome | None


@dataclass(slots=True)
class _UserControlAttempt:
    request: UserControlRequest
    outcome: KernelCommandOutcome
    task: asyncio.Task[None] | None = None
    finished_monotonic_ms: int | None = None
    retain_until_monotonic_ms: int | None = None
    process_at_admission: TerminalProcessInfo | None = None


@dataclass(frozen=True, slots=True)
class _ProcessControlObservation:
    process_id: str
    origin: str | tuple[str, str]
    observed_at: datetime
    result: ProcessControlResult
    public_detail: str | None


@dataclass(frozen=True, slots=True)
class KernelMcpToolCatalogItem:
    """Renderer-neutral public facts for one currently discovered MCP tool."""

    server_id: str
    remote_name: str
    provider_name: str
    description: str
    effect: str
    available_to_subagents: bool
    parallel_safe: bool


@dataclass(frozen=True, slots=True)
class KernelCapabilityCatalogInspection:
    """Disposable product projection frozen directly from capability owners."""

    mcp_catalog: McpCatalogSnapshot
    mcp_configured_servers: tuple[McpConfiguredServerInspection, ...]
    mcp_tools: tuple[KernelMcpToolCatalogItem, ...]
    skill_catalog: EffectiveSkillCatalogInspection
    configured_active_skill_names: frozenset[str]


class PromptBlockedByHook(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class _IngressHookApiVariant(StrEnum):
    DIRECT = "DIRECT"
    QUEUED = "QUEUED"


@dataclass(frozen=True, slots=True)
class _IngressHookReservationKey:
    api_variant: _IngressHookApiVariant
    command_id: str
    canonical_prompt: FrozenCanonicalPrompt = field(repr=False)
    requested_permission_mode: PermissionMode | None
    queue_item_id: str | None
    scheduled: ScheduledAdmission | None = None
    delivery_mode: PromptDeliveryMode = PromptDeliveryMode.NEW_TURN
    target_turn_id: str | None = None


@dataclass(slots=True)
class _PromptAdmissionAttempt:
    key: _IngressHookReservationKey | QueuedPromptAction
    future: asyncio.Future[KernelRunResult | KernelCommandOutcome]
    task: asyncio.Task[KernelCommandOutcome] | None = None


class RootChainPhase(StrEnum):
    ORIGIN_RUNNING = "ORIGIN_RUNNING"
    SUCCESSOR_COMMITTED_PENDING_HANDOFF = "SUCCESSOR_COMMITTED_PENDING_HANDOFF"
    SUCCESSOR_RUNNING = "SUCCESSOR_RUNNING"


@dataclass(frozen=True, slots=True)
class _PendingRootSuccessorHandoff:
    owner_task: asyncio.Task[KernelRunResult]
    attempt_id: str
    origin_turn_id: str
    successor_turn_id: str
    successor_entry_id: str
    workflow_id: str
    interaction_id: str | None
    handoff_kind: PlanHandoffKind
    started_input_group: StartedTurnInputGroup


class KernelCompositionUnavailable(ValueError):
    pass


class HostSessionCloseState(StrEnum):
    TASK_INSTALLED = "TASK_INSTALLED"
    CLOSED = "CLOSED"
    CLOSED_SETTLEMENT_UNCONFIRMED = "CLOSED_SETTLEMENT_UNCONFIRMED"
    CLOSE_FAILED_QUARANTINED = "CLOSE_FAILED_QUARANTINED"


class KernelSessionSettlementUnconfirmed(RuntimeError):
    """Physical work stopped, but necessary canonical settlement is unconfirmed."""


class KernelHostCoreClosing(RuntimeError):
    """A new Host session was rejected after process shutdown won admission."""


class SessionControlRejected(RuntimeError):
    """A current kernel lifecycle operation owns this canonical session."""

    def __init__(self, public_code: str, message: str) -> None:
        super().__init__(message)
        self.public_code = public_code
