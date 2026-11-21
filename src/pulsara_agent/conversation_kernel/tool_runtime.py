"""Host-scoped tool surface with process-local physical execution ownership."""

from __future__ import annotations

from pulsara_agent.conversation_kernel.prompt_content import bind_prompt_images, MAXIMUM_PROMPT_MULTIPART_BYTES
from pulsara_agent.identity import derive_id

from pulsara_agent.identity import random_id

import asyncio
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Condition, Event, Lock, RLock
from time import monotonic
from typing import TYPE_CHECKING, Mapping, Protocol

from jsonschema import ValidationError

from pulsara_agent.code_mode.contracts import CellParticipationObserver

if TYPE_CHECKING:
    from pulsara_agent.code_mode.host import CodeModeHost
    from pulsara_agent.conversation_kernel.decision_tools import KernelDecisionToolPort
    from .capability_management_execution import CapabilityManagementCall


from .capability_management import CapabilityManagementPreparation
from pulsara_agent.capability.management_form import (
    PendingCapabilityForm,
    AcceptedCapabilityFormSubmission,
)

from pulsara_agent.capability.builtin_catalog import (
    execution_catalog_entry,
)
from pulsara_agent.capability.pulsara_home import (
    UserHomeResolution,
    resolve_user_home,
)

from pulsara_agent.model_input.contracts import (
    ModelInputScopeKind,
)
from pulsara_agent.message import ToolResultState
from pulsara_agent.mcp_config import McpServerConfig
from pulsara_agent.ports.tool_output import ToolOutputReadPort
from pulsara_agent.ports.terminal import (
    parse_exec_process_input,
    parse_manage_process_input,
    parse_query_processes_input,
)
from pulsara_agent.ports.tool_execution import (
    Tool,
    ToolCall,
    ToolExecutionResult,
    encode_tool_public_json,
)
from pulsara_agent.terminal_process import (
    TerminalCwdScope,
    TerminalProcessInfo,
    TerminalProcessOrigin,
    TerminalRequest,
    TerminalManager,
)
from pulsara_agent.terminal_process.output import (
    TerminalOutputSnapshot,
)
from pulsara_agent.tools.builtins.filesystem import (
    EditFileTool,
    LocalImageReadCandidate,
    ReadFileTool,
    SearchContentTool,
    FindFilesTool,
    ViewImageSourceKind,
    ViewImageTool,
    WriteFileTool,
    parse_view_image_source,
)
from pulsara_agent.conversation_kernel.html_resources import (
    LocalHtmlReadCandidate,
    freeze_html_resources,
)
from pulsara_agent.conversation_kernel.visualization import (
    FrozenVisualizationOccurrence, materialize_visualizations, read_visualization_text, validate_html_path,
)
from pulsara_agent.conversation_kernel.visualization_screenshot import (
    VisualizationScreenshotError,
    VisualizationScreenshotOwner,
)
from pulsara_agent.conversation_kernel.prompt_storage import (
    CanonicalImageReferenceReadPort,
    CanonicalImageReferenceResourceExceeded,
    CanonicalImageReferenceUnavailable,
)
from pulsara_agent.conversation_kernel.image_validation import (
    HostPromptImageValidator,
    PromptImageValidationError,
)
from pulsara_agent.llm.input import (
    FrozenPromptContent,
    LLMImagePart,
)
from pulsara_agent.tools.builtins.workspace import WritePathScope
from pulsara_agent.tools.builtins.media import MEDIA_TOOL_NAMES, MediaTool
from pulsara_agent.conversation_kernel.tool_invocations import current_tool_invocation, ToolInvocationSourceKind
from pulsara_agent.tools.builtins.presentation import OpenInPulsaraTool
from pulsara_agent.tools.builtins.todo import (
    TodoTool,
)
from pulsara_agent.conversation_kernel.io import (
    KernelSessionIO,
    PhysicalToolInvocationDisposition,
)
from pulsara_agent.conversation_kernel.compaction.runtime_handoff import (
    FrozenCompactionRuntimeHandoff,
    FrozenTerminalProcessHandoffFact,
    CompactionRuntimeHandoffBuilder,
    handoff_process_rows,
)
from pulsara_agent.conversation_kernel.capability_composition import (
    BuiltinCompositionState,
)
from pulsara_agent.conversation_kernel.interaction_arbiter import (
    InteractionAdmissionHooks,
)
from pulsara_agent.conversation_kernel.execution_watchdogs import (
    KernelExecutionDeadlineFactory,
    KernelWatchdogOwner,
)
from pulsara_agent.conversation_kernel.live import (
    LiveAgentEventBus,
    LiveBlockKind,
    LiveChannelKind,
)
from pulsara_agent.ports.live_agent_event import (
    TerminalProcessCompletedPayload,
    CurrentWorkSnapshotUpdatedPayload,
    live_digest,
)
from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    freeze_json,
    thaw_json,
)
from pulsara_agent.primitives.run_permission import FrozenRunPermissionSnapshot
from pulsara_agent.primitives.tool_observation import TrustedToolObservationSupplement
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.conversation_kernel.vocabulary import LiveEventType
from pulsara_agent.conversation_kernel.tool_policy import (
    ToolDispatchAuthorizationPolicy,
)
from pulsara_agent.conversation_kernel.tool_surface import (
    BuiltinExecutionPolicyRef,
    McpEffectKind,
    PreparedKernelToolSurface,
    ProcessLocalToolSurfaceBorrow,
    tool_observation_origin_for_binding,
)

from .tool_contracts import (
    KernelToolInvocationContext,
    KernelToolLiveSink,
    KernelToolAuthorization,
    KernelToolAuthorizationKind,
    KernelToolPhysicalInvocationError,
    KernelToolResult,
    ProcessLocalEffectSettlementDisposition,
    ProcessLocalEffectSettlementOutcome,
    ProcessLocalEffectSettlementResult,
    ProcessLocalEffectSettlementToken,
    PreparedResolvedToolInvocation,
    PreparedToolInvocation,
    PreparedToolPreparationRejection,
)
from .current_work_runtime import (
    FrozenCurrentWorkSnapshot,
    FrozenCurrentWorkCloseProjection,
    PreparedTodoReplacement,
    TodoInstallation,
    CurrentWorkStateOwner,
)
from .mcp.supervisor import (
    McpBoundToolExecutor,
    McpDispatchAdmissionPermit,
    McpHostSupervisor,
    McpInstalledRuntimeGeneration,
)
from .mcp.schema import McpSchemaEvaluationFailure, validate_mcp_arguments
from .capability_query import CapabilitySourceQuery
from pulsara_agent.capability.source_query import (
    parse_query,
    CapabilityQueryError,
)
from .mcp.meta import (
    PreparedNewMcpToolRefSettlement,
    ProcessLocalNewMcpToolRefOwner,
    executor_reference_basis,
)


from .tool_runtime_bindings import (
    _TERMINAL_PROCESS_ACTION_EFFECTS as _TERMINAL_PROCESS_ACTION_EFFECTS,
    ProductionBuiltinExecutorBinding as ProductionBuiltinExecutorBinding,
    _builtin_physical_effect_contract as _builtin_physical_effect_contract,
    production_builtin_executor_binding_identity_fingerprint as production_builtin_executor_binding_identity_fingerprint,
    _production_executor_binding as _production_executor_binding,
    _qualified_executor_identity as _qualified_executor_identity,
)

from .tool_runtime_terminal import (
    _terminal_process_payload as _terminal_process_payload,
    _terminal_execution_result as _terminal_execution_result,
    _success as _success,
)

from .tool_runtime_results import (
    _await_mcp_operation as _await_mcp_operation,
    _cancelled_mcp_before_dispatch as _cancelled_mcp_before_dispatch,
    _kernel_tool_state as _kernel_tool_state,
    _kernel_result_from_mcp_known as _kernel_result_from_mcp_known,
    _freeze_physical_observation as _freeze_physical_observation,
)

from .tool_runtime_surface import _ToolSurfaceOperations, _mcp_executor_capability_version

from .tool_runtime_schema import (
    _tool_argument_error_message,
    _validate_builtin_call_schema as _validate_builtin_call_schema,
    _json_schema_value as _json_schema_value,
    _thaw_json as _thaw_json,
)

from .tool_runtime_management import _ToolManagementOperations
from .tool_runtime_web import _WebToolOperations
from pulsara_agent.web.contracts import WEB_TOOL_NAMES

from .tool_runtime_authorization import (
    _ToolAuthorizationOperations,
    _PendingMcpConfirmationAdmission as _PendingMcpConfirmationAdmission,
    _PreparedMcpMetaInvocation as _PreparedMcpMetaInvocation,
)

class KernelSubagentToolPort(Protocol):
    @property
    def tool_names(self) -> frozenset[str]: ...

    def validate_arguments(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, object],
    ) -> str | None: ...

    async def invoke(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, object],
        invocation_context: KernelToolInvocationContext,
    ) -> KernelToolResult: ...

    def bind_cell_observer(self, observer: CellParticipationObserver) -> None: ...

    async def read_cell_work_page(self, *, source_exec_attempt_id: str,
                                  cursor: str | None, limit: int): ...

    async def visible_cell_participants(self, participants): ...

    async def resolve_visible_agent(self, agent_id: str): ...

    async def freeze_compaction_handoff(
        self, *, cursor: str | None = None,
    ) -> tuple[dict[str, object], ...]: ...


class KernelMemoryToolPort(Protocol):
    @property
    def tool_names(self) -> frozenset[str]: ...

    async def invoke(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, object],
        invocation_context: KernelToolInvocationContext,
    ) -> KernelToolResult: ...


class KernelToolInteractionResolution(Protocol):
    capability_submission: AcceptedCapabilityFormSubmission | None
    decision: str
    reference: str
    public_message: str
    attempt_id: str | None
    result_entry_id: str | None


class KernelToolInteractionPort(Protocol):
    async def request_capability_form(
        self,
        *,
        turn_id: str,
        assistant_entry_id: str,
        tool_call_id: str,
        permission_snapshot: FrozenRunPermissionSnapshot,
        form: PendingCapabilityForm,
        cell_cancel_event: asyncio.Event | None = None,
    ) -> KernelToolInteractionResolution: ...

    async def request_tool_confirmation(
        self,
        *,
        turn_id: str,
        assistant_entry_id: str,
        tool_call_id: str,
        tool_name: str,
        public_prompt: str,
        permission_snapshot: FrozenRunPermissionSnapshot,
        admission_hooks: InteractionAdmissionHooks | None = None,
        cell_cancel_event: asyncio.Event | None = None,
    ) -> KernelToolInteractionResolution: ...

    async def cancel_tool_confirmations(
        self,
        *,
        owner_keys: frozenset[str],
        reference: str,
        public_message: str,
    ) -> None: ...


class KernelCapabilityReloadPort(Protocol):
    async def adopt_capability_management_change(
        self, *, workspace_root: Path | None
    ) -> Mapping[str, object]: ...

    async def reload_capabilities(
        self, *, deadline_monotonic: float | None
    ) -> Mapping[str, object]: ...


def _terminal_acceptance(manager, owner, result):
    if result.process_id is None or result.output_cursor is None:
        return None
    return lambda: manager.accept_model_observation(
        result.process_id, result.output_cursor, owner_host_session_id=owner
    )


@dataclass(slots=True)
class _DirectTerminalTool:
    manager: TerminalManager
    owner_host_session_id: str
    session_id: str
    name: str = "exec_process"

    def execute(
        self,
        call,
        *,
        live_sink=None,
        origin,
        decision_attempt_id,
        decision_deadline_monotonic,
        effective_permission_mode,
        initial_observation=None,
    ):
        if origin.session_id != self.session_id:
            raise ValueError("Terminal source does not belong to this session")
        request = parse_exec_process_input(call.arguments)
        result = self.manager.execute(
            TerminalRequest(
                command=request.command,
                workdir=request.workdir,
                yield_time_ms=request.yield_time_ms,
                tty=request.tty,
            ),
            owner_host_session_id=self.owner_host_session_id,
            output_subscriber=None
            if live_sink is None
            else lambda value, _start, _end: live_sink.offer_text(
                value.decode("utf-8")
            ),
            origin=origin,
            decision_attempt_id=decision_attempt_id,
            decision_deadline_monotonic=decision_deadline_monotonic,
            cwd_scope=TerminalCwdScope.WORKSPACE
            if effective_permission_mode is PermissionMode.READ_ONLY
            else TerminalCwdScope.HOST_LOCAL,
            initial_observation=initial_observation,
        )
        return _terminal_execution_result(
            call,
            result,
            acceptance_succeeded=_terminal_acceptance(
                self.manager, self.owner_host_session_id, result
            ),
        )


@dataclass(slots=True)
class _DirectTerminalProcessTool:
    manager: TerminalManager
    owner_host_session_id: str
    session_id: str
    name: str = "manage_process"

    def execute(self, call, *, live_sink=None):
        request = parse_manage_process_input(call.arguments)
        action = request.action
        process_id = request.target.process_id
        info = self.manager.get_process_info(
            process_id, owner_host_session_id=self.owner_host_session_id
        )
        if info.origin.session_id != self.session_id:
            raise KeyError(process_id)
        common = {"owner_host_session_id": self.owner_host_session_id}
        if action in {"wait", "write"}:
            common.update(
                max_output_chars=None,
                since_cursor=self.manager.model_observation_cursor(
                    process_id, **common
                ),
            )
        before = (
            self.manager.get_process_info(
                process_id, owner_host_session_id=self.owner_host_session_id
            )
            if action in {"kill", "close_stdin"}
            else None
        )
        if action == "wait":
            result = self.manager.wait_process(
                process_id,
                timeout_seconds=request.timeout_seconds,
                **common,
                output_subscriber=None
                if live_sink is None
                else lambda value, _start, _end: live_sink.offer_text(
                    value.decode("utf-8")
                ),
            )
        elif action == "write":
            result = self.manager.write_process(
                process_id,
                request.data,
                append_newline=False,
                yield_time_ms=request.yield_time_ms,
                **common,
            )
        elif action == "close_stdin":
            result = self.manager.close_process_stdin(process_id, **common)
        else:
            termination = self.manager.terminate_process_if_running(
                process_id, **common
            )
            if termination.disposition.value == "PHYSICAL_SETTLEMENT_INCOMPLETE":
                raise OSError(
                    "进程终止操作已请求，但物理结束状态未确认；请查询该进程，不应重复启动命令。"
                )
            result = termination.result
        control_text = None
        if action == "kill":
            control_text = (
                "进程此前已退出；未发送新的终止请求。"
                if termination.disposition.value == "ALREADY_TERMINAL"
                else "已停止进程及其子进程。"
            )
        elif action == "close_stdin":
            control_text = (
                "进程输入此前已关闭。" if before.stdin_closed else "已关闭进程输入。"
            )
        return _terminal_execution_result(
            call,
            result,
            action=action,
            control_text=control_text,
            acceptance_succeeded=_terminal_acceptance(
                self.manager, self.owner_host_session_id, result
            ),
        )


@dataclass(slots=True)
class _DirectTerminalQueryTool:
    manager: TerminalManager
    owner_host_session_id: str
    session_id: str
    name: str = "query_processes"

    def execute(self, call):
        from pulsara_agent.conversation_kernel.content_parts import ResultPart

        request = parse_query_processes_input(call.arguments)
        if "target" in call.arguments:
            try:
                record = self.manager.get_process_info(
                    request.target.process_id,
                    owner_host_session_id=self.owner_host_session_id,
                )
                if record.origin.session_id != self.session_id:
                    raise KeyError(request.target.process_id)
                records = [record]
            except KeyError:
                from pulsara_agent.ports.tool_execution import ToolBusinessValue

                return ToolExecutionResult(
                    call_id=call.id,
                    tool_name=call.name,
                    status=ToolResultState.ERROR,
                    business_value=ToolBusinessValue(
                        {}, (ResultPart("text", "未找到该进程或进程已被回收。"),)
                    ),
                )
        else:
            records = [
                item
                for item in self.manager.list_processes(
                    owner_host_session_id=self.owner_host_session_id,
                    include_running=True,
                    include_finished=True,
                )
                if item.origin.session_id == self.session_id
            ]
        return _success(
            call,
            [ResultPart("object", _terminal_process_payload(item)) for item in records]
            or [ResultPart("text", "当前会话没有保留的进程。")],
        )
@dataclass(slots=True)
class _DirectPlanControlTool:
    name: str

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        del call
        raise RuntimeError("Plan control must be consumed by the runner batch barrier")


@dataclass(slots=True)
class _DirectCapabilityControlTool:
    name: str

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        del call
        raise RuntimeError("Hook control escaped its async Host adapter")


@dataclass(slots=True)
class _DirectMcpCatalogTool:
    name: str

    def execute(self, call: ToolCall) -> ToolExecutionResult:
        del call
        raise RuntimeError("MCP catalog tool escaped its generation-bound adapter")




class DirectKernelToolPort(
    _ToolSurfaceOperations, _ToolAuthorizationOperations, _ToolManagementOperations, _WebToolOperations
):
    """Permission-before-attempt and process-local physical execution owner."""

    def __init__(
        self,
        *,
        workspace_root: Path,
        host_owner_id: str,
        authorization_policy: ToolDispatchAuthorizationPolicy,
        session_id: str,
        memory_domain_id: str = "u_local",
        live_bus: LiveAgentEventBus,
        result_source_reader: ToolOutputReadPort | None = None,
        image_reference_read_port: CanonicalImageReferenceReadPort | None = None,
        deadline_factory: KernelExecutionDeadlineFactory | None = None,
        user_home_resolution: UserHomeResolution | None = None,
        image_validator: HostPromptImageValidator | None = None,
        scheduled_tasks,
        session_content_query=None,
        media_cache=None,
        read_media_settings=None,
        media_asr_runtime=None,
        web_service=None,
        shared_terminal_owner: "DirectKernelToolPort | None" = None,
    ) -> None:
        root = workspace_root.expanduser().resolve()
        frozen_user_home = user_home_resolution or resolve_user_home()
        self._host_owner_id = host_owner_id
        self._session_id = session_id
        self._memory_domain_id = memory_domain_id
        self._workspace_root = root
        self._live_bus = live_bus
        self._physical_io = KernelSessionIO()
        from pulsara_agent.media.worker import MediaWorker
        self._media_worker = MediaWorker()
        self._media_cache = media_cache
        self._read_media_settings = read_media_settings
        self._media_asr_runtime = media_asr_runtime
        self._media_tasks = set()
        self._web_service = web_service
        self._web_tasks = set()
        self._deadlines = deadline_factory or KernelExecutionDeadlineFactory()
        self._image_validator = image_validator
        self._image_reference_read_port = image_reference_read_port
        self._visualization_screenshots = VisualizationScreenshotOwner()
        self._owns_terminal = shared_terminal_owner is None
        self._terminal_owner_id = host_owner_id if shared_terminal_owner is None else shared_terminal_owner._terminal_owner_id
        if shared_terminal_owner is None:
            self._terminal_live_buses = {session_id: live_bus}
            self._terminal = TerminalManager(workspace_root=root,
                completion_subscriber=self._terminal_process_completed)
            self._terminal.activate_owner(self._terminal_owner_id)
        else:
            self._terminal = shared_terminal_owner._terminal
            self._terminal_live_buses = shared_terminal_owner._terminal_live_buses
            self._terminal_live_buses[session_id] = live_bus
        tools: tuple[Tool, ...] = (
            ReadFileTool(
                root,
                result_source_reader=result_source_reader,
                user_home_resolution=frozen_user_home,
            ),
            ViewImageTool(
                root,
                user_home_resolution=frozen_user_home,
            ),
            SearchContentTool(
                root,
                user_home_resolution=frozen_user_home,
            ),
            FindFilesTool(
                root,
                user_home_resolution=frozen_user_home,
            ),
            EditFileTool(
                root,
                user_home_resolution=frozen_user_home,
            ),
            WriteFileTool(
                root,
                result_source_reader=result_source_reader,
                user_home_resolution=frozen_user_home,
            ),
            OpenInPulsaraTool(root, user_home_resolution=frozen_user_home, host_session_id=host_owner_id, root_session_id=session_id),
            *(MediaTool(root, user_home_resolution=frozen_user_home, name=name) for name in sorted(MEDIA_TOOL_NAMES)),
            TodoTool(),
            _DirectTerminalTool(self._terminal, self._terminal_owner_id, session_id=session_id),
            _DirectTerminalProcessTool(self._terminal, self._terminal_owner_id, session_id=session_id),
            _DirectTerminalQueryTool(self._terminal, self._terminal_owner_id, session_id=session_id),
            _DirectPlanControlTool("enter_plan"),
            _DirectPlanControlTool("ask_plan_question"),
            _DirectPlanControlTool("exit_plan"),
            _DirectCapabilityControlTool("reload_capabilities"),
            _DirectCapabilityControlTool("manage_capability"),
            _DirectMcpCatalogTool("query_capabilities"),
            _DirectMcpCatalogTool("call_mcp_tool"),
            _DirectMcpCatalogTool("read_mcp_resource"),
            *(_DirectCapabilityControlTool(name) for name in sorted(WEB_TOOL_NAMES)),
        )
        self._scheduled_tasks = scheduled_tasks
        self._session_content_query = session_content_query
        if session_content_query is not None:
            from pulsara_agent.ports.session_content import SESSION_QUERY_TOOL_NAMES
            tools = (*tools, *(_DirectCapabilityControlTool(name) for name in sorted(SESSION_QUERY_TOOL_NAMES)))
        if scheduled_tasks is None:
            raise TypeError("production tools require the kernel scheduled task service")
        tools = (*tools, _DirectCapabilityControlTool("query_schedules"), _DirectCapabilityControlTool("manage_schedule"))
        self._tools = {tool.name: tool for tool in tools}
        self._authorization_policy = authorization_policy
        self._close_lock = Lock()
        self._surface_lock = RLock()
        self._surface_condition = Condition(self._surface_lock)
        self._surface_authority = object()
        self._surface_generation = 1
        self._surface_owner_epoch = 1
        self._surface_borrows: dict[str, int] = {}
        self._surface_borrow_owners: dict[str, ProcessLocalToolSurfaceBorrow] = {}
        self._execution_borrows: dict[str, ProcessLocalToolSurfaceBorrow] = {}
        self._prepared_surfaces: dict[
            tuple[int, ModelInputScopeKind], PreparedKernelToolSurface
        ] = {}
        self._closed = False
        self._builtin_composition_state = BuiltinCompositionState.PREPARING
        self._builtin_composition_seal: object | None = None
        self._sealed_builtin_bindings: (
            tuple[ProductionBuiltinExecutorBinding, ...] | None
        ) = None
        self._physically_closed = False
        self._terminal_physically_closed = False
        self._close_async_lock = asyncio.Lock()
        self._terminal_release_task: asyncio.Task[object] | None = None
        self._todo_settlements: dict[str, ProcessLocalEffectSettlementToken] = {}
        self._mcp_ref_settlements: dict[str, ProcessLocalEffectSettlementToken] = {}
        self._current_work_owner = CurrentWorkStateOwner(
            session_id=session_id,
            owner_epoch=host_owner_id,
        )
        self._subagent: KernelSubagentToolPort | None = None
        self._code_mode = None
        self._code_mode_invoke = None
        self._code_mode_freeze_source = None
        self._code_mode_read_source = None
        self._code_mode_read_outcome = None
        self._memory: KernelMemoryToolPort | None = None
        self._decision: KernelDecisionToolPort | None = None
        self._interaction: KernelToolInteractionPort | None = None
        self._capability_reload: KernelCapabilityReloadPort | None = None
        self._capability_management = None

        self._mcp_supervisor: McpHostSupervisor | None = None
        self._mcp_current: McpInstalledRuntimeGeneration | None = None
        self._mcp_runtime_by_surface_generation: dict[
            int, McpInstalledRuntimeGeneration
        ] = {}
        self._mcp_dispatch_resources: dict[tuple[int, str], McpBoundToolExecutor | McpInstalledRuntimeGeneration] = {}
        self._mcp_dispatch_permits: dict[
            tuple[int, str], McpDispatchAdmissionPermit
        ] = {}
        self._mcp_confirmation_admissions: dict[
            tuple[int, str], _PendingMcpConfirmationAdmission
        ] = {}
        self._mcp_meta_invocations: dict[
            tuple[int, str], _PreparedMcpMetaInvocation
        ] = {}
        self._mcp_meta_refs = ProcessLocalNewMcpToolRefOwner()

    async def prepare_visualizations(self, body: str, *, io: KernelSessionIO,
                                     workspace_id: str, deadline_monotonic: float) -> tuple[FrozenVisualizationOccurrence, ...]:
        return await materialize_visualizations(
            body, self._tools["view_image"]._read_access_path, io=io,
            validator=self._image_validator, image_reader=self._image_reference_read_port,
            deadline_monotonic=deadline_monotonic,
        )

    def bind_subagent_port(self, port: KernelSubagentToolPort) -> None:
        with self._surface_lock:
            self._require_builtin_composition_preparing_locked()
            if self._subagent is not None:
                raise RuntimeError("subagent tool port is already bound")
            self._subagent = port
            if self._code_mode is not None:
                port.bind_cell_observer(self._code_mode)
            self._surface_generation += 1

    def bind_memory_port(self, port: KernelMemoryToolPort) -> None:
        with self._surface_lock:
            self._require_builtin_composition_preparing_locked()
            if self._memory is not None:
                raise RuntimeError("memory tool port is already bound")
            self._memory = port
            self._surface_generation += 1

    def bind_code_mode_executor(self, *, invoke, freeze_source, read_source, save_outcome, read_outcome) -> None:
        self._code_mode_invoke = invoke
        self._code_mode_freeze_source = freeze_source
        self._code_mode_read_source = read_source
        self._code_mode_read_outcome = read_outcome
        if self._code_mode is not None:
            self._code_mode.set_outcome_saver(save_outcome)

    def bind_decision_port(self, port: KernelDecisionToolPort) -> None:
        with self._surface_lock:
            self._require_builtin_composition_preparing_locked()
            if self._decision is not None:
                raise RuntimeError("Decision tool port is already bound")
            self._decision = port
            self._surface_generation += 1

    def decision_available(self) -> bool:
        if self._decision is None:
            raise RuntimeError("Decision tool port is not bound")
        value = self._decision.available()
        if type(value) is not bool:
            raise TypeError("Decision availability must be a boolean")
        return value

    def bind_code_mode_port(self, port: CodeModeHost) -> None:
        with self._surface_lock:
            self._require_builtin_composition_preparing_locked()
            self._code_mode = port
            if self._subagent is not None:
                self._subagent.bind_cell_observer(port)
            for name in ("exec_cell", "query_cells", "manage_cell"):
                self._tools[name] = _DirectCapabilityControlTool(name)
            self._surface_generation += 1

    def offer_memory_embedding_wake(self) -> None:
        memory = self._memory
        if memory is not None:
            memory.offer_embedding_wake()

    def bind_interaction_port(self, port: KernelToolInteractionPort) -> None:
        with self._surface_lock:
            self._require_builtin_composition_preparing_locked()
            if self._interaction is not None:
                raise RuntimeError("interaction tool port is already bound")
            self._interaction = port

    def bind_capability_reload_port(self, port: KernelCapabilityReloadPort) -> None:
        with self._surface_lock:
            self._require_builtin_composition_preparing_locked()
            if self._capability_reload is not None:
                raise RuntimeError("Capability reload port is already bound")
            self._capability_reload = port

    def bind_capability_management(
        self, service: CapabilityManagementPreparation
    ) -> None:
        with self._surface_lock:
            self._require_builtin_composition_preparing_locked()
            if self._capability_management is not None:
                raise RuntimeError("Capability management is already bound")
            self._capability_management = service

    def bind_mcp_supervisor(self, supervisor: McpHostSupervisor) -> None:
        with self._surface_lock:
            self._require_builtin_composition_preparing_locked()
            if self._mcp_supervisor is not None:
                raise RuntimeError("MCP supervisor is already bound")
            if self._closed:
                raise RuntimeError("tool surface is closed")
            self._mcp_supervisor = supervisor

    async def reload_mcp_configs(
        self,
        configs: tuple[McpServerConfig, ...],
        *,
        deadline_monotonic: float,
    ) -> frozenset[str]:
        """Fence a config epoch and cancel only not-yet-FULL confirmations."""

        supervisor = self._mcp_supervisor
        if supervisor is None:
            raise RuntimeError("MCP supervisor is not bound")
        if monotonic() >= deadline_monotonic:
            raise TimeoutError("MCP config reload deadline expired before cut")
        old_configs = {item.server_id: item for item in supervisor.configs}
        new_configs = {item.server_id: item for item in configs}
        changed = supervisor.reload_configs(configs)
        if not changed:
            return changed
        disabled_or_removed = frozenset(
            server_id
            for server_id in changed
            if old_configs.get(server_id) is not None
            and old_configs[server_id].enabled
            and (server_id not in new_configs or not new_configs[server_id].enabled)
        )
        owner_keys = frozenset(
            f"mcp-server:{server_id}" for server_id in disabled_or_removed
        )
        interaction = self._interaction
        if interaction is not None and owner_keys:
            await interaction.cancel_tool_confirmations(
                owner_keys=owner_keys,
                reference="interaction:mcp-config-changed",
                public_message=(
                    "MCP confirmation ended because its server configuration changed"
                ),
            )
        for key, admission in tuple(self._mcp_confirmation_admissions.items()):
            if admission.executor.semantic.server_id in disabled_or_removed:
                self._mcp_confirmation_admissions.pop(key, None)
                self._mcp_meta_invocations.pop(key, None)
        return changed


    def list_background_terminal_processes(self):
        return self._terminal.list_background_processes(
            owner_host_session_id=self._terminal_owner_id
        )

    def read_background_terminal_log(
        self, process_id: str, *, maximum_chars: int, since_cursor: str | None = None
    ):
        return self._terminal.log_process(
            process_id,
            max_output_chars=maximum_chars,
            owner_host_session_id=self._terminal_owner_id,
            since_cursor=since_cursor,
        )

    def terminate_background_terminal_process(
        self,
        process_id: str,
        *,
        deadline_monotonic: float | None = None,
    ):
        del deadline_monotonic
        return self._terminal.terminate_process_if_running(
            process_id,
            owner_host_session_id=self._terminal_owner_id,
        )

    @property
    def current_work_owner(self) -> CurrentWorkStateOwner:
        return self._current_work_owner


    def snapshot_workspace_root(self) -> Path:
        return self._terminal.workspace_root


    @staticmethod
    def _entry_unavailable(tool_name: str, borrow: ProcessLocalToolSurfaceBorrow, arguments: Mapping[str, object]) -> str | None:
        scope = borrow.prepared.access.conversation_scope_kind
        source = current_tool_invocation.get()
        code = (borrow.code_cell_id is not None and source is not None
            and source.kind is ToolInvocationSourceKind.CODE_MODE_CALL
            and source.tool_name == tool_name and scope is ModelInputScopeKind.ROOT)
        if tool_name in {"decide", "run_agent", "wait_agent"}:
            if not code:
                if scope is ModelInputScopeKind.SUBAGENT:
                    return f"tool unavailable: {tool_name}"
                return f"{tool_name} requires Code Mode; use exec_cell with tools.{tool_name}(...)"
        if tool_name == "wait_message" and (
            scope is not ModelInputScopeKind.SUBAGENT or borrow.code_cell_id is not None
        ):
            return "wait_message requires a native subagent invocation."
        return None

    def prepare_resolved_invocation(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, object],
        surface_borrow: ProcessLocalToolSurfaceBorrow,
    ) -> PreparedToolInvocation:
        frozen_original = freeze_json(dict(arguments))
        if not isinstance(frozen_original, FrozenJsonObjectFact):
            raise TypeError("Tool arguments did not freeze to an object")

        def reject(
            kind: KernelToolAuthorizationKind,
            reference: str,
            message: str,
            *,
            post_name: str = tool_name,
            post_arguments: FrozenJsonObjectFact = frozen_original,
        ) -> PreparedToolPreparationRejection:
            return PreparedToolPreparationRejection(
                tool_name,
                post_name,
                post_arguments,
                KernelToolAuthorization(kind, reference, message),
            )

        entry_error = self._entry_unavailable(tool_name, surface_borrow, arguments)
        if entry_error:
            return reject(KernelToolAuthorizationKind.TOOL_UNAVAILABLE, "tool-entry:code-mode-only", entry_error)
        if tool_name in MEDIA_TOOL_NAMES and (
            surface_borrow.prepared.access.conversation_scope_kind is not ModelInputScopeKind.ROOT
            or (tool_name == "annotate_media" and (
                surface_borrow.code_cell_id is not None
                or (current_tool_invocation.get() is not None
                    and current_tool_invocation.get().kind is not ToolInvocationSourceKind.MODEL_TOOL_CALL)))
        ):
            return reject(KernelToolAuthorizationKind.TOOL_UNAVAILABLE,
                "media:root-authority", "Media tools require ROOT authority; annotate_media is native only")
        try:
            binding = self._validate_surface_borrow(surface_borrow, tool_name)
        except (RuntimeError, KeyError):
            return reject(
                KernelToolAuthorizationKind.TOOL_UNAVAILABLE,
                "tool-surface:revoked",
                f"tool unavailable: {tool_name}",
            )
        if self._subagent is not None and tool_name in self._subagent.tool_names:
            error = self._subagent.validate_arguments(tool_name=tool_name, arguments=arguments)
            if error:
                return reject(KernelToolAuthorizationKind.INVALID_ARGUMENTS, "agent-arguments", error)
        if tool_name == "decide":
            from pulsara_agent.conversation_kernel.decision_tools import validate_decision_arguments
            try:
                validate_decision_arguments(arguments)
            except ValueError as exc:
                return reject(KernelToolAuthorizationKind.INVALID_ARGUMENTS, "decision-arguments", str(exc))
        entry = (
            execution_catalog_entry(tool_name, code_mode=surface_borrow.code_cell_id is not None) if isinstance(binding.execution_policy, BuiltinExecutionPolicyRef) else None
        )
        if entry is not None:
            schema = _json_schema_value(execution_catalog_entry(tool_name, code_mode=surface_borrow.code_cell_id is not None).descriptor.input_schema)
            try:
                _validate_builtin_call_schema(tool_name, schema, arguments)
            except ValidationError as exc:
                return reject(
                    KernelToolAuthorizationKind.INVALID_ARGUMENTS,
                    f"descriptor:{entry.descriptor.id}",
                    f"invalid tool arguments: {_tool_argument_error_message(exc)}",
                )
        if tool_name == "call_mcp_tool":
            target = arguments.get("target")
            token = target.get("tool_ref") if isinstance(target, Mapping) and target.get("kind") == "MCP_TOOL_REF" else None
            inner = arguments.get("arguments")
            if not isinstance(token, str) or not isinstance(inner, Mapping):
                return reject(
                    KernelToolAuthorizationKind.INVALID_ARGUMENTS,
                    "mcp-meta:arguments-invalid",
                    "invalid new MCP tool reference or arguments",
                )
            try:
                ref = self._mcp_meta_refs.resolve_callable(
                    token, **self._mcp_ref_consumer_for_borrow(surface_borrow)
                )
                executor, _generation = self._resolve_meta_ref_executor(
                    ref=ref, surface_borrow=surface_borrow
                )
            except LookupError as exc:
                return reject(
                    KernelToolAuthorizationKind.TOOL_UNAVAILABLE,
                    "mcp-meta:ref-unavailable",
                    f"{exc}. This call was not dispatched. Inspect this tool again with query_capabilities and use its current parameters and invocation.target. Do not repeat earlier actions whose outcome is uncertain.",
                )
            resolved_name = executor.semantic.provider_tool_name
            frozen_inner = freeze_json(dict(inner))
            if not isinstance(frozen_inner, FrozenJsonObjectFact):
                raise TypeError("MCP meta arguments did not freeze to an object")
            schema = thaw_json(executor.semantic.input_schema)
            if not isinstance(schema, dict):
                raise RuntimeError("MCP meta schema did not thaw to an object")
            try:
                validate_mcp_arguments(schema, dict(inner))
            except McpSchemaEvaluationFailure as exc:
                return reject(
                    KernelToolAuthorizationKind.TOOL_UNAVAILABLE,
                    f"mcp-meta:{executor.semantic.descriptor_fingerprint}",
                    str(exc),
                    post_name=resolved_name,
                    post_arguments=frozen_inner,
                )
            except ValidationError as exc:
                return reject(
                    KernelToolAuthorizationKind.INVALID_ARGUMENTS,
                    f"mcp-meta:{executor.semantic.descriptor_fingerprint}",
                    f"invalid MCP tool arguments: {_tool_argument_error_message(exc)}",
                    post_name=resolved_name,
                    post_arguments=frozen_inner,
                )
            return PreparedResolvedToolInvocation(
                requested_tool_name=tool_name,
                canonical_tool_name=resolved_name,
                resolved_arguments=frozen_inner,
            )
        return PreparedResolvedToolInvocation(
            requested_tool_name=tool_name,
            canonical_tool_name=tool_name,
            resolved_arguments=frozen_original,
        )

    async def freeze_compaction_runtime_handoff(
        self,
        *,
        conversation_scope_kind: ModelInputScopeKind,
        session_id: str,
        work_turn_id: str,
    ) -> FrozenCompactionRuntimeHandoff | None:
        """Freeze scoped owner facts; admit complete PCA entries lazily."""
        if session_id != self._session_id or not work_turn_id:
            raise ValueError("runtime handoff scope differs from its tool owner")
        is_root = conversation_scope_kind is ModelInputScopeKind.ROOT
        process_facts = tuple(
            FrozenTerminalProcessHandoffFact(process_id=item.process_id, status="running")
            for item in self._terminal.freeze_compaction_handoff(
                owner_host_session_id=self._terminal_owner_id
            )
            if is_root or item.origin.session_id == session_id
        )
        todo = self._current_work_owner.freeze_compaction_handoff(scope_kind=conversation_scope_kind)
        code_cells = (
            self._code_mode.freeze_compaction_handoff()
            if is_root and self._code_mode is not None else ()
        )
        builder = CompactionRuntimeHandoffBuilder(
            todo=todo, conversation_scope_kind=conversation_scope_kind
        )
        streams = {
            "terminal_processes": iter(handoff_process_rows(process_facts)),
            "code_cells": iter(code_cells),
        }

        async def agents():
            if self._subagent is None:
                return
            cursor = None
            while True:
                page = await self._subagent.freeze_compaction_handoff(cursor=cursor)
                for row in page:
                    yield row
                if len(page) < 50:
                    return
                cursor = page[-1]["agent"]["agent_id"]

        agent_rows = agents()
        agents_done = False
        # Process/Cell/Agent is the specified display order. No next
        # page is read until its next Agent entry is actually needed.
        try:
            while streams or not agents_done:
                for key in ("terminal_processes", "code_cells", "agents"):
                    if key == "agents":
                        if agents_done:
                            continue
                        item = await anext(agent_rows, None)
                        if item is None:
                            agents_done = True
                            continue
                    else:
                        if key not in streams:
                            continue
                        item = next(streams[key], None)
                        if item is None:
                            del streams[key]
                            continue
                    if not builder.add(key, item):
                        return builder.finish()
            return builder.finish()
        finally:
            await agent_rows.aclose()





    def _mcp_ref_consumer_for_borrow(self, borrow: ProcessLocalToolSurfaceBorrow) -> dict[str, object]:
        access = borrow.prepared.access
        return {"conversation_scope_kind": access.conversation_scope_kind, "session_id": access.session_id}

    def _current_mcp_runtime_for_borrow(self, borrow: ProcessLocalToolSurfaceBorrow):
        self.prepare_tool_surface_safe_point()
        with self._surface_lock:
            runtime = self._mcp_current
            if runtime is not None:
                scope = borrow.prepared.access.conversation_scope_kind
                servers = runtime.catalog_for_scope(scope).servers
                specs = runtime.root_tool_specs if scope is ModelInputScopeKind.ROOT else runtime.subagent_tool_specs
                executors = [runtime.executors[(item.server_id, item.remote_tool_name)] for item in specs
                    if (item.server_id, item.remote_tool_name) in runtime.executors]
                self._mcp_meta_refs.reconcile(
                    conversation_scope_kind=scope, session_id=borrow.prepared.access.session_id,
                    server_ids={item.server_id for item in servers},
                    complete_server_ids={item.server_id for item in servers if item.status.value == "READY"},
                    bindings={_mcp_executor_capability_version(item).identity: executor_reference_basis(item)
                        for item in executors},
                )
            return runtime, borrow.prepared.access.surface_generation




    def _mcp_resource_directory(self, arguments, context):
        from pulsara_agent.conversation_kernel.content_parts import ResultPart
        from pulsara_agent.ports.tool_execution import ToolBusinessValue

        target = arguments["target"]
        runtime, _generation = self._current_mcp_runtime_for_borrow(context.surface_borrow)
        scope = context.surface_borrow.prepared.access.conversation_scope_kind
        if runtime is None:
            return KernelToolResult(state="APPLICATION_ERROR", content=ToolBusinessValue(
                content=(ResultPart("text", "当前 MCP 目录不可用；本次未向服务发送请求。"),)))
        query = CapabilitySourceQuery(self._capability_management, runtime=runtime, scope=scope,
            inspect_tool=None, mcp_configs=self._mcp_supervisor.configs)
        server = next((item for item in query._visible_runtime_servers({})
            if item.connection_target is not None and dict(item.connection_target) == target), None)
        candidate = (query._accepted_directory(server.server_id)
            if server is not None and query._matching_config(server) is not None else None)
        if candidate is None:
            return KernelToolResult(state="APPLICATION_ERROR", content=ToolBusinessValue(
                content=(ResultPart("text", "未找到该连接的可靠资源目录；本次未向服务发送请求。"),)))
        parts = []
        for item in candidate.discovery_snapshot.resources:
            value = {"target": {"kind": "MCP_RESOURCE", "server_id": server.server_id, "uri": item.uri}, "name": item.name}
            if item.description:
                value["description"] = item.description
            if item.mime_type:
                value["mime_type"] = item.mime_type
            parts.append(ResultPart("object", value))
        for item in candidate.discovery_snapshot.resource_templates:
            value = {"server_id": server.server_id, "uri_template": item.uri_template, "name": item.name}
            if item.description:
                value["description"] = item.description
            if item.mime_type:
                value["mime_type"] = item.mime_type
            parts.append(ResultPart("object", value))
        return KernelToolResult(state="SUCCESS", content=ToolBusinessValue(content=tuple(
            parts or [ResultPart("text", "该服务当前没有资源或资源模板。")])) )

    async def _query_capabilities_result(self, *, tool_name, arguments, invocation_context):
        from pulsara_agent.conversation_kernel.content_parts import ResultPart
        from pulsara_agent.ports.tool_execution import ToolBusinessValue
        from pulsara_agent.identity import random_id

        borrow = invocation_context.surface_borrow
        scope = borrow.prepared.access.conversation_scope_kind
        prepared_refs = []

        def inspect_tool(executor):
            prepared = self._mcp_meta_refs.prepare(
                **self._mcp_ref_consumer_for_borrow(borrow),
                capability_identity=_mcp_executor_capability_version(executor).identity,
                binding=executor_reference_basis(executor),
                result_entry_id=invocation_context.result_entry_id)
            prepared_refs.append(prepared)
            semantic = executor.semantic
            value = {"target": {"kind": "MCP_TOOL", "server_id": semantic.server_id,
                     "remote_tool_name": semantic.remote_tool_name},
                     "description": semantic.description,
                     "input_schema": thaw_json(semantic.input_schema),
                     "invocation": {"mode": "META", "target": {
                         "kind": "MCP_TOOL_REF", "tool_ref": prepared.ref.opaque_token}}}
            if semantic.output_schema is not None:
                value["output_schema"] = thaw_json(semantic.output_schema)
            return value

        try:
            values = parse_query(arguments, subagent=scope is ModelInputScopeKind.SUBAGENT)
            runtime, _ = self._current_mcp_runtime_for_borrow(borrow)
            result = await CapabilitySourceQuery(self._capability_management,
                runtime=runtime, scope=scope, inspect_tool=inspect_tool,
                mcp_configs=self._mcp_supervisor.configs).execute(values)
            token = None
            if prepared_refs:
                token = ProcessLocalEffectSettlementToken(random_id("settlement"), tuple(prepared_refs))
                self._mcp_ref_settlements[token.token_id] = token
            return KernelToolResult(state="APPLICATION_ERROR" if result["meta"]["status"] == "ERROR" else result["meta"]["status"],
                content=ToolBusinessValue(content=tuple(ResultPart(item["type"], item["value"])
                    for item in result["content"])),
                process_local_settlement=token, effect_class="read_only")
        except (CapabilityQueryError, OSError, ValueError) as exc:
            for prepared in prepared_refs:
                self._mcp_meta_refs.settle(prepared=prepared, committed=False)
            message = str(exc)
            if self._capability_management is not None:
                from pulsara_agent.plugins.contracts import InspectLocalPluginsRequest
                scrub = await asyncio.to_thread(self._capability_management.plugins._capture_scrub_set,
                    InspectLocalPluginsRequest(self._capability_management.deadline()))
                message = scrub.scrub_text(message)
            return KernelToolResult(state="APPLICATION_ERROR",
                content=ToolBusinessValue(content=(ResultPart("text", message),)), effect_class="read_only")
        except BaseException:
            for prepared in prepared_refs:
                self._mcp_meta_refs.settle(prepared=prepared, committed=False)
            raise



    def query_code_cells(self, *, cursor=None, max_items=50):
        return self._code_mode.list_cell_snapshots(cursor=cursor, max_items=max_items)

    def capture_code_cell(self, cell_id):
        return self._code_mode.capture_cell(cell_id)

    async def read_code_cell_source_page(self, *, session_id, capture, offset_chars=0, max_chars=20000):
        """Read one frozen source page from an already captured Cell fact."""
        from pulsara_agent.code_mode.contracts import CodeModeError
        if type(offset_chars) is not int or offset_chars < 0:
            raise CodeModeError("invalid_arguments", "offset_chars must be a nonnegative integer")
        if type(max_chars) is not int or not 1 <= max_chars <= 32000:
            raise CodeModeError("invalid_arguments", "max_chars must be an integer between 1 and 32000")
        cell = capture.public_value()
        if self._code_mode_read_source is None:
            raise RuntimeError("Cell source reader is not bound")
        try:
            source = await self._code_mode_read_source(
                session_id=session_id, attempt_id=capture.source_exec_attempt_id)
            if source is None:
                raise LookupError("The frozen code_source is unavailable for this Cell's source attempt.")
        except Exception as exc:
            return {"cell": cell, "source": {"status": "UNAVAILABLE", "reason": str(exc)}}
        available = source[offset_chars:offset_chars + max_chars]
        end = offset_chars + len(available)
        has_more = end < len(source)
        return {"cell": cell, "source": {"status": "AVAILABLE", "text": available,
            "offset_chars": offset_chars, "returned_chars": len(available),
            "total_chars": len(source), "has_more": has_more,
            "next_offset_chars": end if has_more else None}}

    async def read_code_cell_graph(self, *, cell_id, section, cursor=None, max_items=50):
        """Page the live Cell's actual Work or verified participant seeds."""
        from pulsara_agent.code_mode.contracts import CodeModeError
        if self._subagent is None:
            raise CodeModeError("unavailable", "Agent collaboration is unavailable")
        if section not in {"works", "participants"}:
            raise CodeModeError("invalid_arguments", "unknown Cell graph section")
        if type(max_items) is not int or not 1 <= max_items <= 50:
            raise CodeModeError("invalid_arguments", "max_items must be in 1..50")
        snapshot = self.capture_code_cell(cell_id)
        observed = self._code_mode.query_cell_participants(cell_id, cursor=cursor if section == "participants" else None,
                                                           max_items=max_items)
        if section == "works":
            rows = await self._subagent.read_cell_work_page(
                source_exec_attempt_id=snapshot.source_exec_attempt_id,
                cursor=cursor, limit=max_items + 1)
            items = list(rows[:max_items])
            next_cursor = items[-1]["work_id"] if len(rows) > max_items else None
        else:
            items = await self._subagent.visible_cell_participants(observed["items"])
            next_cursor = observed["next_cursor"]
        return {"cell": snapshot.public_value(), "participation_coverage": observed["participation_coverage"],
                "section": section, "items": items, "next_cursor": next_cursor}

    async def _read_saved_code_cell_outcome(self, *, session_id, cell_id):
        from pulsara_agent.code_mode.contracts import CodeModeError

        try:
            outcome = await self._code_mode_read_outcome(session_id=session_id, cell_id=cell_id)
        except MemoryError:
            raise
        except Exception as error:
            raise CodeModeError('unavailable', str(error)) from error
        if outcome is None:
            raise CodeModeError('unavailable', '当前会话中找不到该 Cell。')
        return outcome

    async def _query_cells(self, arguments, context, *, capture=None, outcome=None):
        from pulsara_agent.code_mode.contracts import CodeModeError
        from pulsara_agent.conversation_kernel.content_parts import ResultPart
        from pulsara_agent.ports.tool_execution import ToolBusinessValue
        if 'target' not in arguments:
            rows = self._code_mode.list_cell_snapshots(max_items=None)['cells']
            parts = [ResultPart('object', {key:row[key] for key in ('target','state','pending_calls')}) for row in rows]
            return ToolBusinessValue(content=tuple(parts or [ResultPart('text', '没有当前仍持有的 Cell。')]))
        if capture is not None:
            attempt_id = capture.source_exec_attempt_id
            meta = {'cell_status': capture.state, 'pending_calls': capture.pending_calls}
            diagnostic = str(capture.error.get('message', capture.error)) if capture.error else None
        else:
            attempt_id, state, diagnostic = outcome
            meta = {'cell_status': state or 'unknown'}
            if state is not None:
                meta['pending_calls'] = 0
        try:
            source = await self._code_mode_read_source(session_id=context.session_id, attempt_id=attempt_id)
        except MemoryError:
            raise
        except Exception as error:
            raise CodeModeError('unavailable', str(error)) from error
        if source is None:
            raise LookupError('The frozen source for this Cell is unavailable')
        parts = []
        if diagnostic:
            parts.append(ResultPart('text', f'执行诊断：{diagnostic}\n\n执行源码：\n'))
        if source:
            parts.append(ResultPart('text', source))
        return ToolBusinessValue(public_fields={'target':arguments['target'], 'meta':meta}, content=tuple(parts))

    async def _invoke_media(self, tool, arguments, context, started, origin):
        """Bind media orchestration to this exact direct tool invocation."""
        from pulsara_agent.media.execution import execute_media

        if context.conversation_scope_kind != ModelInputScopeKind.ROOT.value:
            raise RuntimeError("Media tools require ROOT authority")
        if tool.name == "annotate_media" and (
            context.code_cell_id is not None or context.surface_borrow.code_cell_id is not None
            or (context.invocation_source is not None and context.invocation_source.kind is not ToolInvocationSourceKind.MODEL_TOOL_CALL)
        ):
            raise RuntimeError("annotate_media requires direct native ROOT authority")
        current = asyncio.current_task()
        self._media_tasks.add(current)
        try:
            result = await execute_media(
                tool,
                arguments,
                io=self._physical_io,
                worker=self._media_worker,
                image_validator=self._image_validator,
                cache=self._media_cache,
                read_settings=self._read_media_settings,
                asr_runtime=self._media_asr_runtime,
                memory_domain_id=self._memory_domain_id,
                deadline=self._deadlines.deadline(
                    KernelWatchdogOwner.NONTERMINAL_TOOL_INVOCATION
                ),
                reserve_read=lambda **facts: self.reserve_media_read_start(
                    exact_turn_id=context.turn_id,
                    attempt_id=context.attempt_id,
                    **facts,
                ),
                observe_read=lambda **facts: self.observe_media_read_start(
                    exact_turn_id=context.turn_id,
                    attempt_id=context.attempt_id,
                    **facts,
                ),
            )
            return KernelToolResult(
                state=result.state,
                content=result.content,
                result_facts=result.result_facts,
                pending_images=result.pending_images,
                effect_class="read_only",
                physical_observation=_freeze_physical_observation(started, origin),
            )
        finally:
            self._media_tasks.discard(current)

    async def _invoke_code_mode_control(self, tool_name, arguments, context):
        from pulsara_agent.code_mode.contracts import CodeModeError
        from pulsara_agent.conversation_kernel.content_parts import ResultPart
        from pulsara_agent.ports.tool_execution import ToolBusinessValue
        target = None
        try:
            if self._code_mode is None:
                raise CodeModeError('unavailable', 'Code Mode Host is unavailable')
            if tool_name == 'query_cells':
                capture = None
                outcome = None
                if 'target' in arguments:
                    cell_id = arguments['target']['cell_id']
                    capture = self._code_mode.find_cell_snapshot(cell_id)
                    if capture is None:
                        outcome = await self._read_saved_code_cell_outcome(
                            session_id=context.session_id, cell_id=cell_id)
                    target = arguments['target']
                business = await self._query_cells(arguments, context, capture=capture, outcome=outcome)
            elif tool_name == 'exec_cell':
                if self._code_mode_invoke is None or context.code_mode_snapshot is None:
                    raise RuntimeError('exec_cell lost its common execution snapshot')
                source = arguments['code'] if 'code' in arguments else await asyncio.to_thread(
                    self._tools['read_file'].read_code_mode_source, arguments['target']['path'])
                await self._code_mode_freeze_source(attempt_id=context.attempt_id, source=source)
                directory = context.code_mode_snapshot.tool_directory
                catalog = {spec.name:{'parameters':thaw_json(spec.parameters)} for spec in directory.tool_specs}
                cell_id = await self._code_mode.start(source=source, source_exec_attempt_id=context.attempt_id,
                    turn_id=context.turn_id, snapshot=context.code_mode_snapshot,
                    decision_available=context.code_mode_snapshot.decision_available,
                    tool_catalog=catalog, invoke=self._code_mode_invoke)
                target = {'kind':'CELL','cell_id':cell_id}
                business = ToolBusinessValue(public_fields={'target':target}, content=(ResultPart('text','已接纳脚本执行。'),))
            else:
                cell_id = arguments['target']['cell_id']
                action = arguments['action']
                if action == 'wait' and self._code_mode.find_cell_snapshot(cell_id) is None:
                    _, state, diagnostic = await self._read_saved_code_cell_outcome(
                        session_id=context.session_id, cell_id=cell_id)
                    target = arguments['target']
                    if state is None:
                        raise CodeModeError('unavailable',
                            '当前 Host 已没有这个 Cell 的执行实例，无法确认其最终状态。')
                    pending_calls = 0
                    parts = [ResultPart('text', {
                        'completed': '这个 Cell 已完成，执行实例已回收。',
                        'failed': '这个 Cell 执行失败，执行实例已回收。',
                        'terminated': '这个 Cell 已终止，执行实例已回收。',
                    }[state])]
                    if diagnostic:
                        parts.append(ResultPart('text', diagnostic))
                else:
                    captured = self._code_mode.capture_cell(cell_id)
                    target = arguments['target']
                    observation = (await self._code_mode.stop(cell_id) if action == 'stop'
                                   else await self._code_mode.wait_state(cell_id, arguments.get('yield_time_ms', 1000)))
                    state, pending_calls = observation['state'], observation['pending_calls']
                    parts = []
                    if action == 'stop':
                        parts.append(ResultPart('text', '已停止脚本执行。' if state == 'terminated' else '脚本已结束。'))
                    error = observation.get('error') or captured.error
                    if error:
                        parts.append(ResultPart('text', str(error.get('message', error))))
                business = ToolBusinessValue(public_fields={'target':target,
                    'meta':{'cell_status':state,'pending_calls':pending_calls}}, content=tuple(parts))
            return KernelToolResult(state='SUCCESS', content=business)
        except (CodeModeError, ValueError, OSError, UnicodeError, LookupError) as error:
            return KernelToolResult(state='APPLICATION_ERROR', content=ToolBusinessValue(
                public_fields={} if target is None else {'target':target}, content=(ResultPart('text',str(error)),)))

    def discard_tool_admission(
        self,
        *,
        tool_call_id: str,
        surface_borrow: ProcessLocalToolSurfaceBorrow,
        capability_call: CapabilityManagementCall | None = None,
    ) -> None:
        """Release process-local preparation for a call that will not dispatch."""
        key = (surface_borrow.prepared.access.surface_generation, tool_call_id)
        self._mcp_confirmation_admissions.pop(key, None)
        self._mcp_meta_invocations.pop(key, None)
        self._mcp_dispatch_resources.pop(key, None)
        permit = self._mcp_dispatch_permits.pop(key, None)
        if permit is not None:
            permit.release()
        if capability_call is not None:
            capability_call.discard()

    async def invoke(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, object],
        tool_call_id: str,
        attempt_id: str,
        turn_id: str,
        assistant_entry_id: str,
        invocation_context: KernelToolInvocationContext,
        live_sink: KernelToolLiveSink | None = None,
    ) -> KernelToolResult:
        if self._closed:
            raise RuntimeError("tool surface is closed")
        if (
            invocation_context.session_id != self._session_id
            or invocation_context.turn_id != turn_id
            or invocation_context.assistant_entry_id != assistant_entry_id
            or invocation_context.tool_call_id != tool_call_id
            or invocation_context.attempt_id != attempt_id
            or invocation_context.attempt_permission_snapshot_fingerprint
            != invocation_context.permission_snapshot_fingerprint
        ):
            # This check precedes every adapter dispatch.  A mismatched
            # process-local authority must never create a process or
            # any other physical effect before being rejected.
            raise RuntimeError("tool invocation context does not exact-join request")
        if (
            invocation_context.conversation_scope_kind
            != invocation_context.surface_borrow.prepared.access.conversation_scope_kind.value
        ):
            raise RuntimeError("tool invocation surface binding does not exact-join")
        self._validate_surface_borrow(invocation_context.surface_borrow, tool_name)
        entry_error = self._entry_unavailable(tool_name, invocation_context.surface_borrow, arguments)
        if entry_error:
            raise RuntimeError(entry_error)
        if tool_name == "decide" and invocation_context.effective_permission_mode is not PermissionMode.BYPASS_PERMISSIONS:
            raise RuntimeError("decide requires ROOT bypass-permissions mode")
        binding = invocation_context.surface_borrow.execution_binding(tool_name)
        if invocation_context.cell_cancel_event is not None and invocation_context.cell_cancel_event.is_set():
            self.discard_tool_admission(
                tool_call_id=tool_call_id,
                surface_borrow=invocation_context.surface_borrow,
                capability_call=invocation_context.capability_call,
            )
            from pulsara_agent.ports.tool_execution import ToolBusinessValue
            from pulsara_agent.conversation_kernel.content_parts import ResultPart
            return KernelToolResult(state="CANCELLED_BEFORE_DISPATCH", content=ToolBusinessValue(content=(ResultPart("text", "Cell 已停止"),)))
        invocation_started = monotonic()
        observation_origin = tool_observation_origin_for_binding(binding)
        from pulsara_agent.ports.session_content import SESSION_QUERY_TOOL_NAMES

        if tool_name in WEB_TOOL_NAMES:
            return await self._invoke_web(tool_name=tool_name, arguments=arguments,
                invocation_context=invocation_context, invocation_started=invocation_started,
                observation_origin=observation_origin)
        if tool_name in {"exec_cell", "query_cells", "manage_cell"}:
            return replace(
                await self._invoke_code_mode_control(tool_name, arguments, invocation_context),
                physical_observation=_freeze_physical_observation(
                    invocation_started, observation_origin
                ),
            )
        if tool_name in SESSION_QUERY_TOOL_NAMES:
            return await self._invoke_session_query(
                tool_name=tool_name,
                arguments=arguments,
                tool_call_id=tool_call_id,
                invocation_context=invocation_context,
                observation_origin=observation_origin,
            )
        if tool_name in {'query_schedules', 'manage_schedule'}:
            return await self._invoke_scheduled_management(
                tool_name=tool_name,
                arguments=arguments,
                tool_call_id=tool_call_id,
                invocation_context=invocation_context,
                invocation_started=invocation_started,
                observation_origin=observation_origin,
            )
        if tool_name == 'manage_capability':
            return await self._invoke_capability_management(
                turn_id=turn_id,
                assistant_entry_id=assistant_entry_id,
                tool_call_id=tool_call_id,
                invocation_context=invocation_context,
                invocation_started=invocation_started,
                observation_origin=observation_origin,
            )
        if tool_name == 'reload_capabilities':
            return await self._invoke_capability_reload(
                invocation_started=invocation_started,
                observation_origin=observation_origin,
            )
        if tool_name == "query_capabilities":
            return replace(
                await self._query_capabilities_result(
                    tool_name=tool_name,
                    arguments=arguments,
                    invocation_context=invocation_context,
                ),
                physical_observation=_freeze_physical_observation(
                    invocation_started, observation_origin
                ),
            )
        if tool_name == "call_mcp_tool":
            generation = (
                invocation_context.surface_borrow.prepared.access.surface_generation
            )
            key = (generation, tool_call_id)
            prepared_meta = self._mcp_meta_invocations.pop(key, None)
            self._mcp_dispatch_resources.pop(key, None)
            permit = self._mcp_dispatch_permits.pop(key, None)
            if prepared_meta is None or permit is None:
                if permit is not None and permit.state.value == "ADMITTED":
                    permit.release()
                raise RuntimeError("MCP meta invocation admission is incomplete")
            inner = arguments.get("arguments")
            frozen_inner = freeze_json(dict(inner)) if isinstance(inner, Mapping) else None
            if frozen_inner != prepared_meta.arguments:
                permit.release()
                raise RuntimeError("MCP meta arguments changed after authorization")
            if permit.state.value == "ADMITTED":
                permit.mark_attempt_accepted()
            operation_task = asyncio.create_task(
                prepared_meta.executor.invoke(
                    permit, thaw_json(prepared_meta.arguments), cell_cancel_event=invocation_context.cell_cancel_event,
                ),
                name=f"mcp-meta-operation:{tool_call_id}",
            )
            known, caller_cancelled = await _await_mcp_operation(operation_task, permit=permit)
            if known is None:
                return _cancelled_mcp_before_dispatch(prepared_meta.executor.policy.effect_kind)
            return await _kernel_result_from_mcp_known(
                known,
                caller_cancelled=caller_cancelled,
                effect_kind=prepared_meta.executor.policy.effect_kind,
                physical_observation=_freeze_physical_observation(
                    invocation_started, observation_origin
                ),
                image_validator=self._image_validator,
                deadline_monotonic=self._deadlines.deadline(KernelWatchdogOwner.NONTERMINAL_TOOL_INVOCATION),
            )
        if tool_name in {
                "read_mcp_resource",
        }:
            if arguments["target"]["kind"] == "MCP_CONNECTION":
                return replace(self._mcp_resource_directory(arguments, invocation_context),
                    physical_observation=_freeze_physical_observation(invocation_started, observation_origin))
            generation = (
                invocation_context.surface_borrow.prepared.access.surface_generation
            )
            key = (generation, tool_call_id)
            runtime = self._mcp_dispatch_resources.pop(key, None)
            permit = self._mcp_dispatch_permits.pop(key, None)
            if not isinstance(runtime, McpInstalledRuntimeGeneration) or permit is None:
                if permit is not None:
                    permit.release()
                raise RuntimeError("MCP standard admission resource is missing")
            operation_task = asyncio.create_task(
                runtime.invoke_standard(
                    tool_name=tool_name,
                    arguments=arguments,
                    permit=permit,
                    cell_cancel_event=invocation_context.cell_cancel_event,
                    scope_kind=(
                        invocation_context.surface_borrow.prepared.access.conversation_scope_kind
                    ),
                ),
                name=f"mcp-standard-operation:{tool_call_id}",
            )
            try:
                known, caller_cancelled = await _await_mcp_operation(operation_task, permit=permit)
            except BaseException:
                if permit is not None and permit.state.value != "RELEASED":
                    with suppress(RuntimeError):
                        permit.release()
                raise
            if known is None:
                return _cancelled_mcp_before_dispatch(McpEffectKind.READ_ONLY)
            return await _kernel_result_from_mcp_known(
                known, caller_cancelled=caller_cancelled, effect_kind=McpEffectKind.READ_ONLY,
                physical_observation=_freeze_physical_observation(
                    invocation_started, observation_origin
                ),
                image_validator=self._image_validator,
                deadline_monotonic=self._deadlines.deadline(KernelWatchdogOwner.NONTERMINAL_TOOL_INVOCATION),
            )
        if self._subagent is not None and tool_name in self._subagent.tool_names:
            result = await self._subagent.invoke(
                tool_name=tool_name,
                arguments=arguments,
                invocation_context=invocation_context,
            )
            return replace(
                result,
                physical_observation=_freeze_physical_observation(
                    invocation_started, observation_origin
                ),
            )
        if self._decision is not None and tool_name in self._decision.tool_names:
            result = await self._decision.invoke(arguments=arguments, invocation_context=invocation_context)
            return replace(result, physical_observation=_freeze_physical_observation(
                invocation_started, observation_origin))
        if self._memory is not None and tool_name in self._memory.tool_names:
            result = await self._memory.invoke(
                tool_name=tool_name,
                arguments=arguments,
                invocation_context=invocation_context,
            )
            return replace(
                result,
                physical_observation=_freeze_physical_observation(
                    invocation_started, observation_origin
                ),
            )
        tool = self._tools[tool_name]
        if isinstance(tool, _DirectPlanControlTool):
            raise RuntimeError("Plan control escaped the runner batch barrier")
        if isinstance(tool, OpenInPulsaraTool) and invocation_context.conversation_scope_kind != "ROOT":
            raise RuntimeError("open_in_pulsara requires ROOT scope")
        if isinstance(tool, TodoTool):
            return await self._invoke_todo_replacement(
                arguments=arguments,
                turn_id=turn_id,
                attempt_id=attempt_id,
                invocation_context=invocation_context,
                invocation_started=invocation_started,
                observation_origin=observation_origin,
            )
        cancel_requested = Event()
        call = ToolCall(
            id=tool_call_id,
            name=tool_name,
            arguments=dict(arguments),
            defer_file_observation=invocation_context.code_cell_id is not None,
            cancel_requested=lambda: (cancel_requested.is_set() or (
                invocation_context.cell_cancel_event is not None and invocation_context.cell_cancel_event.is_set()
            )),
        )
        if isinstance(tool, MediaTool):
            return await self._invoke_media(tool, arguments, invocation_context,
                invocation_started, observation_origin)
        if isinstance(tool, OpenInPulsaraTool):
            return await self._invoke_presentation(
                tool=tool,
                call=call,
                invocation_started=invocation_started,
                observation_origin=observation_origin,
            )
        if isinstance(tool, ViewImageTool):
            return await self._invoke_image(
                tool=tool, call=call, arguments=arguments,
                invocation_started=invocation_started, observation_origin=observation_origin,
            )
        return await self._invoke_physical_tool(
            tool=tool,
            call=call,
            tool_name=tool_name,
            arguments=arguments,
            turn_id=turn_id,
            attempt_id=attempt_id,
            invocation_context=invocation_context,
            live_sink=live_sink,
            observation_origin=observation_origin,
            cancel_requested=cancel_requested,
        )

    async def _invoke_image(
        self, *, tool, call, arguments, invocation_started, observation_origin,
    ) -> KernelToolResult:
        from pulsara_agent.conversation_kernel.content_parts import ResultPart
        from pulsara_agent.conversation_kernel.repository_errors import ConversationKernelConflict
        from pulsara_agent.ports.tool_execution import ToolBusinessValue
        from pulsara_agent.tools.builtins.filesystem import _relpath

        target = None
        def result(state, message, *, image=None):
            parts = [ResultPart("text", message)]
            if image is not None:
                parts.append(ResultPart("image", {"kind": "IMAGE_REF", "image_ref": image.image_ref}))
            return KernelToolResult(state=state,
                content=ToolBusinessValue({"target": target} if target is not None else {}, tuple(parts)),
                pending_images=(image,) if image is not None else (),
                effect_class="read_only", physical_observation=_freeze_physical_observation(invocation_started, observation_origin))

        try:
            source = parse_view_image_source(arguments)
        except ValueError as exc:
            return result("APPLICATION_ERROR", f"图片请求无效：{exc}。原输入：{encode_tool_public_json(dict(arguments)).decode('utf-8')}\n")
        if source.kind is ViewImageSourceKind.PATH:
            target = {"kind": "PATH", "path": source.value}
        else:
            target = {"kind": "IMAGE_REF", "image_ref": source.value}
        deadline = self._deadlines.deadline(KernelWatchdogOwner.NONTERMINAL_TOOL_INVOCATION)
        maximum_bytes = MAXIMUM_PROMPT_MULTIPART_BYTES
        html_source = source.kind is ViewImageSourceKind.PATH and Path(source.value).suffix.lower() in {".html", ".htm"}
        svg_source = source.kind is ViewImageSourceKind.PATH and Path(source.value).suffix.lower() == ".svg"
        browser_source = html_source or svg_source
        try:
            if source.kind is ViewImageSourceKind.PATH:
                target = {"kind": "PATH", "path": _relpath(tool._resolve_read_path(source.value), tool.workspace_root)}
                if self._image_validator is None:
                    raise RuntimeError("view_image validation owner is unavailable")
                physical = await self._physical_io.run_tool_invocation(
                    _read_browser_image_candidate if browser_source else _read_local_image_candidate,
                    tool, call, maximum_bytes, deadline_monotonic=deadline)
            else:
                if self._image_reference_read_port is None:
                    raise RuntimeError("view_image reference read owner is unavailable")
                physical = await self._physical_io.run_tool_invocation(
                    _read_canonical_image_reference, self._image_reference_read_port,
                    source.value, maximum_bytes, deadline_monotonic=deadline)
            if physical.caller_cancelled:
                raise asyncio.CancelledError
            if physical.disposition is PhysicalToolInvocationDisposition.RAISED:
                raise physical.error
            candidate = physical.value
            if isinstance(candidate, ToolExecutionResult):
                if candidate.business_value is None:
                    raise TypeError("image read failure lost its declared value")
                return KernelToolResult(state="APPLICATION_ERROR", content=candidate.business_value,
                    effect_class="read_only", physical_observation=_freeze_physical_observation(invocation_started, observation_origin))
            if source.kind is ViewImageSourceKind.PATH:
                if browser_source:
                    if not isinstance(candidate, LocalHtmlReadCandidate):
                        raise TypeError("browser image read returned an invalid candidate")
                    if svg_source:
                        payload = await self._visualization_screenshots.render(candidate.payload, deadline_monotonic=deadline, svg=True)
                    else:
                        html = await freeze_html_resources(candidate.payload, candidate.access_path,
                            io=self._physical_io, validator=self._image_validator, deadline_monotonic=deadline)
                        payload = await self._visualization_screenshots.render(html, deadline_monotonic=deadline)
                else:
                    if not isinstance(candidate, LocalImageReadCandidate):
                        raise TypeError("local image read returned an invalid candidate")
                    payload = candidate.payload
                image = await self._image_validator.freeze_local_image(payload, deadline_monotonic=deadline)
            else:
                if not isinstance(candidate, LLMImagePart):
                    raise TypeError("image reference read returned an invalid candidate")
                image = candidate
            image = bind_prompt_images(FrozenPromptContent((image,))).parts[0]
            message = f"已载入图片（{image.width}×{image.height}，{image.media_type}）。\n"
            if html_source:
                message += "HTML静态截图；1200×800浅色视口，减少动态效果，可能裁至内容根；未验证播放。\n"
            elif svg_source:
                message += "SVG按尺寸或viewBox比例静态渲染，无裁剪；脚本和外部资源已禁用。\n"
            return result("SUCCESS", message, image=image)
        except asyncio.CancelledError:
            return result("CANCELLED", "图片读取已取消，未交付实际图片。\n")
        except CanonicalImageReferenceUnavailable:
            return result("APPLICATION_ERROR", "图片不存在或已不可用，未交付实际图片。\n")
        except TimeoutError:
            return result("APPLICATION_ERROR", "图片暂时无法读取，未交付实际图片。\n")
        except (PromptImageValidationError, VisualizationScreenshotError, CanonicalImageReferenceResourceExceeded,
                ConversationKernelConflict, ValueError, OSError) as exc:
            return result("APPLICATION_ERROR", f"无法读取图片：{exc}；未交付实际图片。\n")

    async def _invoke_physical_tool(
        self, *, tool, call, tool_name, arguments, turn_id, attempt_id, invocation_context, live_sink, observation_origin, cancel_requested,
    ) -> KernelToolResult:
        if not isinstance(tool, (_DirectTerminalTool, _DirectTerminalProcessTool)):
            return await self._invoke_physical_tool_uncoordinated(
                tool=tool, call=call, tool_name=tool_name, arguments=arguments,
                turn_id=turn_id, attempt_id=attempt_id, invocation_context=invocation_context,
                live_sink=live_sink, observation_origin=observation_origin,
                cancel_requested=cancel_requested,
            )
        initial = asyncio.Event() if isinstance(tool, _DirectTerminalTool) else None
        lock = None
        if isinstance(tool, _DirectTerminalProcessTool):
            request = parse_manage_process_input(arguments)
            if request.action in {"wait", "write"}:
                try:
                    info = tool.manager.get_process_info(request.target.process_id, owner_host_session_id=tool.owner_host_session_id)
                    if info.origin.session_id == tool.session_id:
                        output = tool.manager.process_registry.output_owner(request.target.process_id, owner_host_session_id=tool.owner_host_session_id)
                        if output.initial_observation is not None:
                            await output.initial_observation.wait()
                        await output.model_observation_lock.acquire()
                        lock = output.model_observation_lock
                except KeyError:
                    pass  # The normal terminal path reports an unavailable process.
        released = False

        def release():
            nonlocal released
            if released:
                return
            released = True
            if initial is not None:
                initial.set()
            if lock is not None:
                lock.release()

        handed_off = False
        try:
            result = await self._invoke_physical_tool_uncoordinated(
                tool=tool, call=call, tool_name=tool_name, arguments=arguments,
                turn_id=turn_id, attempt_id=attempt_id, invocation_context=invocation_context,
                live_sink=live_sink, observation_origin=observation_origin,
                cancel_requested=cancel_requested, initial_observation=initial,
            )
            result = replace(result, acceptance_finished=release)
            handed_off = True
            return result
        finally:
            if not handed_off:
                release()

    async def _invoke_physical_tool_uncoordinated(
        self, *, tool, call, tool_name, arguments, turn_id, attempt_id, invocation_context, live_sink, observation_origin, cancel_requested,
        initial_observation=None,
    ) -> KernelToolResult:
        if isinstance(tool, (_DirectTerminalTool, _DirectTerminalProcessTool)):
            origin = TerminalProcessOrigin(
                turn_id=turn_id,
                conversation_scope_kind=(invocation_context.conversation_scope_kind),
                session_id=invocation_context.session_id,
            )
            owner = (
                KernelWatchdogOwner.TERMINAL_FOREGROUND_DECISION
                if isinstance(tool, _DirectTerminalTool)
                else KernelWatchdogOwner.NONTERMINAL_TOOL_INVOCATION
            )
            physical = await self._physical_io.run_tool_invocation(
                _execute_terminal_tool_call,
                tool,
                call,
                live_sink,
                origin,
                attempt_id,
                invocation_context.effective_permission_mode,
                initial_observation,
                deadline_monotonic=self._deadlines.deadline(owner),
                on_caller_cancelled=(
                    lambda: (
                        tool.manager.process_registry.abort_foreground_decision(
                            attempt_id
                        )
                        if isinstance(tool, _DirectTerminalTool)
                        else None
                    )
                ),
            )
        else:
            physical = await self._physical_io.run_tool_invocation(
                _execute_tool_call,
                tool,
                call,
                invocation_context.effective_permission_mode,
                invocation_context.permission_confirmation_granted,
                deadline_monotonic=self._deadlines.deadline(
                    KernelWatchdogOwner.NONTERMINAL_TOOL_INVOCATION
                ),
                on_caller_cancelled=cancel_requested.set,
            )
        if physical.disposition is PhysicalToolInvocationDisposition.RAISED:
            assert physical.error is not None
            effect_class = _physical_effect_class(tool_name, arguments)
            observation = (
                None
                if physical.observation is None
                else replace(
                    physical.observation,
                    observation_origin_kind=observation_origin,
                )
            )
            raise KernelToolPhysicalInvocationError(
                effect_class=effect_class,
                error=physical.error,
                timing=physical.timing.value,
                caller_cancelled=physical.caller_cancelled,
                physical_observation=observation,
            )
        result = physical.value
        if not isinstance(result, ToolExecutionResult):
            raise TypeError("physical tool returned an invalid result carrier")
        state = _kernel_tool_state(result.status)
        remote_identity = result.metadata.get("process_id") if tool_name in {"exec_process", "manage_process"} else None
        if remote_identity is not None and not isinstance(remote_identity, str):
            raise TypeError("terminal process identity must be explicitly reported")
        return KernelToolResult(
            state=state,
            content=result.business_value,
            result_facts=result.result_facts,
            acceptance_succeeded=result.acceptance_succeeded,
            native_continuation=result.native_continuation,
            remote_identity=remote_identity,
            physical_timing=physical.timing.value,
            caller_cancelled_while_running=physical.caller_cancelled,
            physical_observation=(None if physical.observation is None else replace(
                physical.observation, observation_origin_kind=observation_origin)),
            trusted_observation=_validated_terminal_observation_supplement(
                tool=tool,
                tool_name=tool_name,
                arguments=arguments,
                claimed=result.trusted_observation,
            ),
            delivery_succeeded=result.delivery_succeeded,
        )


    def _terminal_process_completed(
        self,
        info: TerminalProcessInfo,
        snapshot: TerminalOutputSnapshot,
    ) -> None:
        process_id = info.process_id
        generation_id = derive_id("generation", "channel", "terminal-process", process_id)
        origin = info.origin
        live_bus = self._terminal_live_buses.get(origin.session_id)
        if live_bus is not None:
            live_bus.offer_nowait(
            event_type=LiveEventType.TERMINAL_PROCESS_COMPLETED,
            session_id=origin.session_id,
            turn_id=origin.turn_id,
            draft_identity=generation_id,
            payload=TerminalProcessCompletedPayload(
                process_id,
                info.status,
                info.exit_code,
                len(snapshot.text.encode("utf-8")),
                live_digest(snapshot.text),
            ),
            scope_kind=origin.conversation_scope_kind,
            channel_kind=LiveChannelKind.TERMINAL_EXTENSION,
            generation_id=generation_id,
            block_id=process_id,
            block_ordinal=0,
            block_kind=LiveBlockKind.OPERATIONAL,
            )

    async def settle_process_local_effect(
        self,
        token: ProcessLocalEffectSettlementToken,
        disposition: ProcessLocalEffectSettlementDisposition,
    ) -> ProcessLocalEffectSettlementResult:
        if isinstance(token.prepared, tuple) and all(isinstance(item, PreparedNewMcpToolRefSettlement) for item in token.prepared):
            retained = self._mcp_ref_settlements.pop(token.token_id, None)
            if retained is not token:
                if disposition is ProcessLocalEffectSettlementDisposition.COMMITTED:
                    raise RuntimeError("committed MCP reference settlement is absent")
                return ProcessLocalEffectSettlementResult(ProcessLocalEffectSettlementOutcome.DISCARDED)
            committed = disposition is ProcessLocalEffectSettlementDisposition.COMMITTED
            for prepared in token.prepared:
                self._mcp_meta_refs.settle(prepared=prepared, committed=committed)
            return ProcessLocalEffectSettlementResult(ProcessLocalEffectSettlementOutcome.INSTALLED
                if committed else ProcessLocalEffectSettlementOutcome.DISCARDED)
        if isinstance(token.prepared, PreparedTodoReplacement):
            retained = self._todo_settlements.get(token.token_id)
            if retained is not token:
                if disposition is ProcessLocalEffectSettlementDisposition.COMMITTED:
                    raise RuntimeError("committed TODO settlement token is absent")
                return ProcessLocalEffectSettlementResult(
                    ProcessLocalEffectSettlementOutcome.DISCARDED
                )
            if disposition is ProcessLocalEffectSettlementDisposition.COMMITTED:
                installation = self._current_work_owner.commit(token.prepared)
                outcome = ProcessLocalEffectSettlementOutcome.INSTALLED
            else:
                self._current_work_owner.discard(token.prepared)
                installation = None
                outcome = ProcessLocalEffectSettlementOutcome.DISCARDED
            self._todo_settlements.pop(token.token_id, None)
            if installation is not None:
                self.offer_todo_installation(installation)
            return ProcessLocalEffectSettlementResult(outcome)
        raise TypeError("process-local settlement token kind is unknown")

    def offer_current_work_close(self, projection: FrozenCurrentWorkCloseProjection | None) -> None:
        if projection is not None:
            self._offer_current_work_snapshot(
                projection.run_identity.work_run_id, projection.last_turn_id)

    def offer_current_work_idle(self, snapshot: FrozenCurrentWorkSnapshot | None, turn_id: str) -> None:
        if snapshot is not None and snapshot.todos:
            self._offer_current_work_snapshot(snapshot.todos[0].run_identity.work_run_id, turn_id)

    def offer_todo_installation(self, installation: TodoInstallation | None) -> None:
        if installation is not None:
            self._offer_current_work_snapshot(
                installation.installed_snapshot.run_identity.work_run_id, installation.turn_id)

    def reserve_media_read_start(
        self, *, exact_turn_id: str, attempt_id: str, canonical_path: str, file_mtime_ns: str,
    ) -> bool:
        return self._current_work_owner.reserve_media_read_start(
            exact_turn_id=exact_turn_id, attempt_id=attempt_id,
            canonical_path=canonical_path, file_mtime_ns=file_mtime_ns)

    def observe_media_read_start(
        self, *, exact_turn_id: str, attempt_id: str, canonical_path: str,
        file_mtime_ns: str, from_seconds: float, to_seconds: float,
    ) -> bool:
        accepted = self._current_work_owner.observe_media_read_start(
            exact_turn_id=exact_turn_id, attempt_id=attempt_id, canonical_path=canonical_path,
            file_mtime_ns=file_mtime_ns, from_seconds=from_seconds, to_seconds=to_seconds)
        if accepted:
            snapshot = self._current_work_owner.snapshot(scope_kind=ModelInputScopeKind.ROOT)
            if snapshot is not None:
                self._offer_current_work_snapshot(snapshot.run_identity.work_run_id, exact_turn_id, attempt_id)
        return accepted

    def _offer_current_work_snapshot(self, root_run_id: str, turn_id: str, media_read_attempt_id: str | None = None) -> None:
        # This one notification invalidates TODO and media observations together.
        # Only the live owner snapshot carries current values.
        draft_identity = random_id("block")
        try:
            self._live_bus.offer_nowait(
                event_type=LiveEventType.CURRENT_WORK_SNAPSHOT_UPDATED,
                session_id=self._session_id,
                turn_id=turn_id,
                draft_identity=draft_identity,
                payload=CurrentWorkSnapshotUpdatedPayload(root_run_id=root_run_id, media_read_attempt_id=media_read_attempt_id),
                scope_kind=ModelInputScopeKind.ROOT.value,
                channel_kind=LiveChannelKind.TERMINAL_EXTENSION,
                generation_id=derive_id("generation", "channel", "current-work", root_run_id),
                proposed_entry_id=None,
                block_id=draft_identity,
                block_ordinal=0,
                block_kind=LiveBlockKind.OPERATIONAL,
            )
        except BaseException:
            # Losing an advisory invalidation cannot undo current state.
            pass

    @property
    def local_children_stopped(self) -> bool:
        # Filesystem search owns rg inside the existing physical worker. That
        # lane must be sealed and joined before process exit can prove no such
        # local child remains; a cancelled logical waiter is insufficient.
        return (self._terminal_physically_closed
                and self._physical_io.physical_close_complete
                and self._visualization_screenshots.local_children_stopped
                and self._media_worker.physical_close_complete and not self._web_tasks)

    def stop_local_process_admission(self) -> None:
        self._physical_io.stop_admission()
        self._media_worker.stop_admission()
        self._visualization_screenshots.stop_admission()

    async def _stop_media_physical_owners(self) -> None:
        await self._media_worker.aclose()
        tasks = tuple(
            task for task in self._media_tasks if task is not asyncio.current_task()
        )
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def stop_visualization_physical_owners(self) -> None:
        await self._stop_media_physical_owners()
        await self._visualization_screenshots.aclose()

    @property
    def physical_close_complete(self) -> bool:
        with self._surface_lock:
            return (self._physically_closed and self._terminal_physically_closed
                    and not self._surface_borrows and not self._execution_borrows
                    and self._visualization_screenshots.local_children_stopped
                    and self._physical_io.physical_close_complete
                    and self._media_worker.physical_close_complete and not self._web_tasks)

    async def aclose(self, *, timeout_seconds: float = 5.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("tool close timeout must be positive")
        async with self._close_async_lock:
            if self._physically_closed:
                return
            self._terminal_live_buses.pop(self._session_id, None)
            deadline = monotonic() + timeout_seconds
            with self._surface_condition:
                self._closed = True
                self._builtin_composition_state = BuiltinCompositionState.CLOSED
                self._surface_condition.notify_all()
            # A surface borrow can be held by an in-flight Terminal call whose
            # bounded physical wait only exits after its process group is
            # terminated.  Seal the surface first, stop those process-local
            # owners, then drain the immutable borrow before closing the
            # general thread owner.  Non-Terminal tools remain bounded by the
            # same borrow deadline and are never replaced or detached.
            close_error: BaseException | None = None
            try:
                await self._stop_web_physical_owners()
                await self._stop_media_physical_owners()
                await self._visualization_screenshots.aclose()
                await self._stop_terminal_physical_owners_locked(deadline)
            except BaseException as exc:
                close_error = exc
            try:
                if await asyncio.to_thread(self._wait_for_surface_borrows, deadline):
                    close_error = close_error or TimeoutError(
                        "tool surface borrows exited after close deadline"
                    )
            except BaseException as exc:
                close_error = close_error or exc
            with self._surface_lock:
                permits = tuple(self._mcp_dispatch_permits.values())
                self._mcp_dispatch_permits.clear()
                self._mcp_dispatch_resources.clear()
                self._mcp_confirmation_admissions.clear()
                self._mcp_meta_invocations.clear()
                runtimes = tuple(
                    dict.fromkeys(self._mcp_runtime_by_surface_generation.values())
                )
                self._mcp_runtime_by_surface_generation.clear()
                self._mcp_current = None
            self._mcp_meta_refs.close()
            self._mcp_ref_settlements.clear()
            self._todo_settlements.clear()
            for permit in permits:
                try:
                    permit.release()
                except BaseException as exc:
                    close_error = close_error or exc
            for runtime in runtimes:
                try:
                    runtime.release()
                except BaseException as exc:
                    close_error = close_error or exc
            try:
                await self._physical_io.aclose(deadline_monotonic=deadline)
            except BaseException as exc:
                close_error = close_error or exc
            self._physically_closed = True
            if close_error is not None:
                raise close_error

    async def stop_terminal_physical_owners(
        self, *, timeout_seconds: float = 5.0
    ) -> None:
        """Stop process owners before awaiting cancelled tool threads.

        ``asyncio`` cannot cancel a thread blocked in a foreground Terminal
        wait.  Host close calls this seam first so process-group termination
        makes that exact physical invocation return; the normal ``aclose``
        then drains KernelSessionIO without starting a replacement owner.
        """

        if timeout_seconds <= 0:
            raise ValueError("tool close timeout must be positive")
        async with self._close_async_lock:
            await self._stop_terminal_physical_owners_locked(
                monotonic() + timeout_seconds
            )

    async def _stop_terminal_physical_owners_locked(
        self, deadline_monotonic: float
    ) -> None:
        if self._terminal_physically_closed:
            return
        if not self._owns_terminal:
            self._terminal_physically_closed = True
            return
        with self._close_lock:
            self._closed = True
        with self._surface_condition:
            self._closed = True
            self._surface_condition.notify_all()
        if self._terminal_release_task is None:
            self._terminal_release_task = asyncio.create_task(
                self._release_terminal_owner_worker(deadline_monotonic)
            )
        release_late = await _join_close_task(
            self._terminal_release_task,
            deadline_monotonic=deadline_monotonic,
        )
        self._terminal_release_task.result()
        self._terminal_physically_closed = True
        if release_late:
            raise TimeoutError("Terminal owner exited after close deadline")


    async def _release_terminal_owner_worker(self, deadline_monotonic: float) -> None:
        try:
            await asyncio.to_thread(
                self._terminal.release_owner,
                self._terminal_owner_id,
                timeout_seconds=max(0.001, deadline_monotonic - monotonic()),
            )
        except TimeoutError:
            await asyncio.to_thread(
                self._terminal.release_owner_and_join,
                self._terminal_owner_id,
            )
            self._terminal_physically_closed = True
            raise
        self._terminal_physically_closed = True

    def _wait_for_surface_borrows(self, deadline_monotonic: float) -> bool:
        deadline_expired = False
        with self._surface_condition:
            while self._surface_borrows or self._execution_borrows:
                remaining = deadline_monotonic - monotonic()
                if remaining <= 0:
                    deadline_expired = True
                    self._surface_condition.wait()
                else:
                    self._surface_condition.wait(timeout=remaining)
        return deadline_expired


async def _join_close_task(
    task: asyncio.Task[object], *, deadline_monotonic: float
) -> bool:
    """Join an admitted close worker and report logical watchdog expiry."""

    deadline_expired = False
    if not task.done():
        remaining = deadline_monotonic - monotonic()
        if remaining > 0:
            done, _pending = await asyncio.wait((task,), timeout=remaining)
            deadline_expired = not done
        else:
            deadline_expired = True
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except BaseException:
            break
    return deadline_expired


def _execute_tool_call(
    tool: Tool,
    call: ToolCall,
    effective_permission_mode: PermissionMode,
    permission_confirmation_granted: bool,
    *,
    deadline_monotonic: float,
) -> ToolExecutionResult:
    # The closed Tool API predates absolute deadlines.  KernelSessionIO owns
    # the physical thread and makes a close timeout explicit; adapters with
    # their own timeouts continue to enforce them inside execute().
    del deadline_monotonic
    if isinstance(tool, (EditFileTool, WriteFileTool)):
        write_scope = (
            WritePathScope.HOST_LOCAL
            if effective_permission_mode is PermissionMode.BYPASS_PERMISSIONS
            or permission_confirmation_granted
            else WritePathScope.WORKSPACE
        )
        return tool.execute(call, write_scope=write_scope)
    return tool.execute(call)


def _read_browser_image_candidate(tool: ViewImageTool, call: ToolCall, maximum_bytes: int, *, deadline_monotonic: float) -> LocalHtmlReadCandidate | ToolExecutionResult:
    # maximum_bytes is the PNG budget and must not constrain source text.
    source = parse_view_image_source(dict(call.arguments))
    try:
        if Path(source.value).suffix.lower() != ".svg":
            validate_html_path(source.value)
        path = tool._read_access_path(source.value)
        kind = "SVG" if path.suffix.lower() == ".svg" else "HTML"
        body = read_visualization_text(path.resolve(), kind=kind, allow_empty=False)
        if body is None:
            raise ValueError(f"{kind}_NOT_FOUND")
        return LocalHtmlReadCandidate(body, path, source.value)
    except (ValueError, OSError) as exc:
        return tool._error(call, str(exc) if isinstance(exc, ValueError) else "HTML_READ_FAILED", source.value)


def _read_local_image_candidate(
    tool: ViewImageTool,
    call: ToolCall,
    maximum_bytes: int,
    *,
    deadline_monotonic: float,
) -> LocalImageReadCandidate | ToolExecutionResult:
    del deadline_monotonic
    return tool.read_bounded(call, maximum_bytes=maximum_bytes)


def _read_canonical_image_reference(
    port: CanonicalImageReferenceReadPort,
    image_ref: str,
    maximum_bytes: int,
    *,
    deadline_monotonic: float,
) -> LLMImagePart:
    return port.read_image(
        image_ref=image_ref,
        maximum_encoded_bytes=maximum_bytes,
        deadline_monotonic=deadline_monotonic,
    )


def _physical_effect_class(tool_name: str, arguments: Mapping[str, object]) -> str:
    if tool_name == "manage_process":
        action = arguments.get("action")
        for candidate, effect_class in _TERMINAL_PROCESS_ACTION_EFFECTS:
            if action == candidate:
                return effect_class
        raise RuntimeError("manage_process action escaped its closed catalog")
    if tool_name == "exec_process":
        return "TERMINAL_EFFECT"
    return execution_catalog_entry(tool_name).recovery_contract.severity


def _execute_terminal_tool_call(
    tool: _DirectTerminalTool | _DirectTerminalProcessTool,
    call: ToolCall,
    live_sink: KernelToolLiveSink | None,
    origin: TerminalProcessOrigin,
    decision_attempt_id: str,
    effective_permission_mode: PermissionMode,
    initial_observation=None,
    *,
    deadline_monotonic: float,
) -> ToolExecutionResult:
    from pulsara_agent.terminal_process.manager import ProcessInputClosedError

    try:
        if isinstance(tool, _DirectTerminalTool):
            return tool.execute(
                call,
                live_sink=live_sink,
                origin=origin,
                decision_attempt_id=decision_attempt_id,
                decision_deadline_monotonic=deadline_monotonic,
                effective_permission_mode=effective_permission_mode,
                initial_observation=initial_observation,
            )
        return tool.execute(call, live_sink=live_sink)
    except (ProcessInputClosedError,KeyError,OSError,ValueError) as error:
        from pulsara_agent.conversation_kernel.content_parts import ResultPart
        from pulsara_agent.ports.tool_execution import ToolBusinessValue
        fields={"target":dict(call.arguments["target"])} if "target" in call.arguments else {}
        return ToolExecutionResult(call_id=call.id,tool_name=call.name,status=ToolResultState.ERROR,
            business_value=ToolBusinessValue(fields,(ResultPart("text",str(error)),)))


def _validated_terminal_observation_supplement(
    *,
    tool: Tool,
    tool_name: str,
    arguments: Mapping[str, object],
    claimed: TrustedToolObservationSupplement | None,
) -> TrustedToolObservationSupplement | None:
    """Admit trusted duration only from the pinned Terminal binding matrix.

    ``ToolExecutionResult`` remains a neutral physical-result carrier, so a
    custom/builtin implementation can syntactically attach a supplement.  The
    Host tool owner must not trust it: only the exact Terminal executor binding
    and an observation action can promote that value into model-visible timing.
    Invalid claims are ignored so metadata cannot negate an already-known tool
    outcome.
    """

    if claimed is None:
        return None
    if tool_name == "exec_process" and isinstance(tool, _DirectTerminalTool):
        return claimed
    if tool_name == "manage_process" and isinstance(tool, _DirectTerminalProcessTool):
        action = parse_manage_process_input(arguments).action
        if action == "wait":
            return claimed
    return None








__all__ = [
    "DirectKernelToolPort",
    "KernelToolInteractionPort",
    "ProductionBuiltinExecutorBinding",
    "production_builtin_executor_binding_identity_fingerprint",
]
