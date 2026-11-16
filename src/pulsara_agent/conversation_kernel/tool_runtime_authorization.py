"""Permission decisions and process-local MCP dispatch admission."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Mapping
from jsonschema import ValidationError
from .capability_management_execution import CapabilityManagementCall
from pulsara_agent.capability.builtin_catalog import execution_catalog_entry
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.ports.terminal import (
    parse_exec_process_input,
    parse_manage_process_input,
    parse_query_processes_input,
)
from pulsara_agent.tools.builtins.filesystem import parse_view_image_source
from pulsara_agent.tools.builtins.media import (
    MEDIA_TOOL_NAMES,
    validate_media_arguments,
)
from pulsara_agent.conversation_kernel.tool_invocations import (
    current_tool_invocation,
    ToolInvocationSourceKind,
)
from pulsara_agent.tools.builtins.todo import (
    TodoValidationError,
    parse_todo_replacement,
)
from pulsara_agent.conversation_kernel.interaction_arbiter import (
    InteractionAdmissionHooks,
)
from pulsara_agent.conversation_kernel.memory.contracts import (
    FrozenModelCallMemoryContext,
)
from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    context_fingerprint,
    freeze_json,
    thaw_json,
)
from pulsara_agent.primitives.run_permission import FrozenRunPermissionSnapshot
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.conversation_kernel.tool_policy import (
    ToolDispatchAuthorizationRequest,
    ToolDispatchDecisionKind,
)
from pulsara_agent.conversation_kernel.tool_surface import (
    McpEffectKind,
    ProcessLocalToolSurfaceBorrow,
    execution_policy_fingerprint,
)
from .tool_contracts import (
    KernelToolAuthorization,
    KernelToolAuthorizationKind,
    PreparedPermissionRequest,
)
from .mcp.supervisor import (
    McpBoundToolExecutor,
    McpDispatchAdmissionPermit,
    McpSnapshotStale,
)
from .mcp.schema import McpSchemaEvaluationFailure, validate_mcp_arguments
from .mcp.meta import NewMcpToolRef, executor_reference_basis
from .tool_runtime_surface import _mcp_executor_capability_version
from .tool_runtime_schema import (
    _tool_argument_error_message,
    _validate_builtin_call_schema as _validate_builtin_call_schema,
    _json_schema_value as _json_schema_value,
)


@dataclass(frozen=True, slots=True)
class _PendingMcpConfirmationAdmission:
    generation: int
    executor: McpBoundToolExecutor
    scope_kind: ModelInputScopeKind
    turn_id: str
    tool_call_id: str


@dataclass(frozen=True, slots=True)
class _PreparedMcpMetaInvocation:
    executor: McpBoundToolExecutor
    ref: NewMcpToolRef
    arguments: FrozenJsonObjectFact


class _ToolAuthorizationOperations:
    """The direct owner's single authorization and confirmation admission gate."""

    async def authorize(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, object],
        tool_call_id: str,
        turn_id: str,
        assistant_entry_id: str,
        permission_snapshot: FrozenRunPermissionSnapshot,
        surface_borrow: ProcessLocalToolSurfaceBorrow,
        memory_context: FrozenModelCallMemoryContext,
    ) -> KernelToolAuthorization:
        entry_error = self._entry_unavailable(tool_name, surface_borrow, arguments)
        if entry_error:
            return KernelToolAuthorization(KernelToolAuthorizationKind.TOOL_UNAVAILABLE,
                "tool-entry:code-mode-only", entry_error)
        if tool_name in MEDIA_TOOL_NAMES and (
            surface_borrow.code_cell_id is not None
            or surface_borrow.prepared.access.conversation_scope_kind is not ModelInputScopeKind.ROOT
            or (current_tool_invocation.get() is not None
                and current_tool_invocation.get().kind is not ToolInvocationSourceKind.MODEL_TOOL_CALL)
        ):
            return KernelToolAuthorization(KernelToolAuthorizationKind.PERMISSION_DENIED,
                "media:root-direct-only", "Media tools require a direct ROOT call")
        try:
            self._validate_surface_borrow(surface_borrow, tool_name)
        except (RuntimeError, KeyError):
            return KernelToolAuthorization(
                KernelToolAuthorizationKind.TOOL_UNAVAILABLE,
                "tool-surface:revoked",
                f"tool unavailable: {tool_name}",
            )
        tool = self._tools.get(tool_name)
        subagent = self._subagent is not None and tool_name in self._subagent.tool_names
        memory = self._memory is not None and tool_name in self._memory.tool_names
        decision = self._decision is not None and tool_name in self._decision.tool_names
        if (tool is None and not subagent and not memory and not decision) or self._closed:
            return KernelToolAuthorization(
                KernelToolAuthorizationKind.TOOL_UNAVAILABLE,
                "tool-surface:unavailable",
                f"tool unavailable: {tool_name}",
            )
        if memory and (
            (
                tool_name in {"remember", "manage_memory"}
                and not memory_context.memory_use_policy.allows_writes
            )
            or (
                tool_name not in {"remember", "manage_memory"}
                and not memory_context.memory_use_policy.allows_reads
            )
        ):
            return KernelToolAuthorization(
                KernelToolAuthorizationKind.PERMISSION_DENIED,
                context_fingerprint(
                    "pulsara:memory-user-opt-out-authorization:v1",
                    {
                        "policy": memory_context.memory_use_policy.value,
                        "tool_name": tool_name,
                    },
                ),
                "memory use was disabled by the user for this run",
            )
        entry = execution_catalog_entry(tool_name)
        schema = _json_schema_value(execution_catalog_entry(tool_name, code_mode=surface_borrow.code_cell_id is not None).descriptor.input_schema)
        try:
            _validate_builtin_call_schema(tool_name, schema, arguments)
        except ValidationError as exc:
            return KernelToolAuthorization(
                KernelToolAuthorizationKind.INVALID_ARGUMENTS,
                f"descriptor:{entry.descriptor.id}",
                f"invalid tool arguments: {_tool_argument_error_message(exc)}",
            )
        if decision:
            if permission_snapshot.effective_mode is not PermissionMode.BYPASS_PERMISSIONS:
                return KernelToolAuthorization(KernelToolAuthorizationKind.PERMISSION_DENIED,
                    "decision:root-bypass-required", "decide requires ROOT bypass-permissions mode")
            from pulsara_agent.conversation_kernel.decision_tools import validate_decision_arguments
            try:
                validate_decision_arguments(arguments)
            except ValueError as exc:
                return KernelToolAuthorization(KernelToolAuthorizationKind.INVALID_ARGUMENTS,
                    "decision-arguments", str(exc))
        if tool_name in MEDIA_TOOL_NAMES:
            from pulsara_agent.media.source import MediaError

            try:
                validate_media_arguments(tool_name, arguments)
            except ValueError as exc:
                return KernelToolAuthorization(KernelToolAuthorizationKind.INVALID_ARGUMENTS,
                    "media:invalid-arguments",
                    f"{exc.code}: {exc}" if isinstance(exc, MediaError) else str(exc))
        if tool_name == "open_in_pulsara" and surface_borrow.prepared.access.conversation_scope_kind is not ModelInputScopeKind.ROOT:
            return KernelToolAuthorization(KernelToolAuthorizationKind.PERMISSION_DENIED, "open_in_pulsara_root_only", "open_in_pulsara requires ROOT scope")
        if tool_name in {"view_image", "exec_process", "manage_process", "query_processes", "exec_cell", "query_cells", "manage_cell"}:
            try:
                if tool_name == "view_image":
                    parse_view_image_source(arguments)
                elif tool_name == "exec_process":
                    parse_exec_process_input(arguments)
                elif tool_name == "manage_process":
                    parse_manage_process_input(arguments)
                elif tool_name == "query_processes":
                    parse_query_processes_input(arguments)
                elif tool_name in {"query_cells", "manage_cell"}:
                    from pulsara_agent.code_mode.targets import parse_cell_target
                    if "target" in arguments:
                        parse_cell_target(arguments["target"])
            except ValueError as exc:
                return KernelToolAuthorization(
                    KernelToolAuthorizationKind.INVALID_ARGUMENTS,
                    f"descriptor:{entry.descriptor.id}",
                    f"invalid tool arguments: {exc}",
                )
        if tool_name in {"exec_cell", "query_cells", "manage_cell"}:
            if (surface_borrow.code_cell_id is not None
                or surface_borrow.prepared.access.conversation_scope_kind is not ModelInputScopeKind.ROOT
                or permission_snapshot.effective_mode is not PermissionMode.BYPASS_PERMISSIONS):
                return KernelToolAuthorization(KernelToolAuthorizationKind.PERMISSION_DENIED,
                    "code-mode:root-bypass-required", f"{tool_name} is available only in bypass-permissions mode.")
        if tool_name == "reload_capabilities":
            access = surface_borrow.prepared.access
            if (
                access.conversation_scope_kind is not ModelInputScopeKind.ROOT
                or permission_snapshot.effective_mode
                is not PermissionMode.BYPASS_PERMISSIONS
            ):
                return KernelToolAuthorization(
                    KernelToolAuthorizationKind.PERMISSION_DENIED,
                    f"{tool_name}_requires_root_bypass_mode",
                    f"{tool_name} requires ROOT bypass-permissions mode",
                )
            return KernelToolAuthorization(
                KernelToolAuthorizationKind.ALLOW,
                f"descriptor:{entry.descriptor.id}:{entry.entry_fingerprint}",
            )
        if subagent:
            access = surface_borrow.prepared.access
            root_tools = {"run_agent", "stop_agent"}
            if (permission_snapshot.effective_mode is not PermissionMode.BYPASS_PERMISSIONS
                or (access.conversation_scope_kind is not ModelInputScopeKind.ROOT and tool_name in root_tools)):
                return KernelToolAuthorization(
                    KernelToolAuthorizationKind.PERMISSION_DENIED,
                    "agent_requires_bypass", f"{tool_name} is available only in bypass-permissions mode.",
                )
            argument_error = self._subagent.validate_arguments(
                tool_name=tool_name,
                arguments=arguments,
            )
            if argument_error is not None:
                return KernelToolAuthorization(
                    KernelToolAuthorizationKind.INVALID_ARGUMENTS,
                    f"descriptor:{entry.descriptor.id}",
                    argument_error,
                )
        if tool_name == "todo":
            try:
                parse_todo_replacement(arguments)
            except TodoValidationError as exc:
                return KernelToolAuthorization(
                    KernelToolAuthorizationKind.INVALID_ARGUMENTS,
                    f"descriptor:{entry.descriptor.id}",
                    str(exc),
                )
            access = surface_borrow.prepared.access
            try:
                self._current_work_owner.require_active(
                    scope_kind=access.conversation_scope_kind,
                    exact_turn_id=turn_id,
                )
            except LookupError:
                return KernelToolAuthorization(
                    KernelToolAuthorizationKind.TOOL_UNAVAILABLE,
                    "todo-run:inactive",
                    "todo scope is no longer active",
                )
        if tool_name == "call_mcp_tool":
            return self._authorize_meta_mcp(
                arguments=arguments,
                tool_call_id=tool_call_id,
                turn_id=turn_id,
                permission_snapshot=permission_snapshot,
                surface_borrow=surface_borrow,
            )
        capability_call = None
        if tool_name == "manage_capability":
            access = surface_borrow.prepared.access
            if access.conversation_scope_kind is not ModelInputScopeKind.ROOT:
                return KernelToolAuthorization(
                    KernelToolAuthorizationKind.PERMISSION_DENIED,
                    "capability:root-only",
                    "Capability management is available only in ROOT",
                )
            if self._capability_management is None:
                return KernelToolAuthorization(
                    KernelToolAuthorizationKind.TOOL_UNAVAILABLE,
                    "capability:unavailable",
                    "Capability management is unavailable",
                )
            try:
                prepared = await self._capability_management.prepare(arguments)
            except ValueError as exc:
                return KernelToolAuthorization(
                    KernelToolAuthorizationKind.INVALID_ARGUMENTS,
                    "capability:invalid-target",
                    str(exc),
                )
            capability_call = CapabilityManagementCall(
                self._capability_management, prepared
            )
            capability_call.subject = (
                self._session_id,
                turn_id,
                assistant_entry_id,
                tool_call_id,
            )
        decision = await self._authorization_policy.decide(
            ToolDispatchAuthorizationRequest(
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                arguments=dict(arguments),
                turn_id=turn_id,
                assistant_entry_id=assistant_entry_id,
                permission_snapshot=permission_snapshot,
                workspace_root=self._workspace_root,
                capability_effects=capability_call.prepared.effects
                if capability_call
                else None,
            )
        )
        if capability_call is not None:
            readonly_form = (
                decision.kind is ToolDispatchDecisionKind.DENY
                and permission_snapshot.effective_mode is PermissionMode.READ_ONLY
            )
            if decision.kind is ToolDispatchDecisionKind.DENY and not readonly_form:
                capability_call.discard()
                return KernelToolAuthorization(
                    KernelToolAuthorizationKind.PERMISSION_DENIED,
                    decision.reference,
                    decision.public_message,
                )
            form = (
                readonly_form
                or bool(capability_call.prepared.user_inputs)
                or (decision.kind is ToolDispatchDecisionKind.REQUIRE_CONFIRMATION)
            )
            return KernelToolAuthorization(
                KernelToolAuthorizationKind.CAPABILITY_FORM_REQUIRED
                if form
                else KernelToolAuthorizationKind.ALLOW,
                decision.reference,
                decision.public_message,
                capability_call=capability_call,
                capability_permission_required=decision.kind
                is ToolDispatchDecisionKind.REQUIRE_CONFIRMATION,
            )
        if decision.kind is ToolDispatchDecisionKind.REQUIRE_CONFIRMATION:
            return KernelToolAuthorization(
                KernelToolAuthorizationKind.REQUIRE_CONFIRMATION,
                decision.reference,
                decision.public_message,
            )
        if decision.kind is ToolDispatchDecisionKind.DENY:
            return KernelToolAuthorization(
                KernelToolAuthorizationKind.PERMISSION_DENIED,
                decision.reference,
                decision.public_message,
            )
        if decision.kind is not ToolDispatchDecisionKind.ALLOW:
            raise RuntimeError("permission decision vocabulary is invalid")
        if tool_name in {"read_mcp_resource"}:
            if arguments.get("target", {}).get("kind") == "MCP_CONNECTION":
                return KernelToolAuthorization(
                    KernelToolAuthorizationKind.ALLOW,
                    decision.reference,
                    decision.public_message,
                )
            generation = surface_borrow.prepared.access.surface_generation
            runtime, _current_generation = self._current_mcp_runtime_for_borrow(surface_borrow)
            if runtime is None:
                return KernelToolAuthorization(
                    KernelToolAuthorizationKind.TOOL_UNAVAILABLE,
                    "mcp-runtime:unavailable",
                    "MCP runtime is unavailable",
                )
            try:
                permit = runtime.admit_standard_operation(
                    tool_name=tool_name,
                    arguments=arguments,
                    descriptor_fingerprint=entry.descriptor.fingerprint(),
                    session_id=self._session_id,
                    scope_kind=(surface_borrow.prepared.access.conversation_scope_kind),
                    turn_id=turn_id,
                    tool_call_id=tool_call_id,
                )
            except McpSnapshotStale:
                return KernelToolAuthorization(
                    KernelToolAuthorizationKind.TOOL_UNAVAILABLE,
                    "mcp-runtime:snapshot-stale",
                    "MCP_SNAPSHOT_STALE",
                )
            except ValueError:
                return KernelToolAuthorization(
                    KernelToolAuthorizationKind.INVALID_ARGUMENTS,
                    "mcp-runtime:catalog-join-invalid",
                    "MCP resource arguments do not match the exact catalog",
                )
            if permit is None:
                raise RuntimeError("MCP remote read did not create a permit")
            key = (generation, tool_call_id)
            if key in self._mcp_dispatch_permits:
                permit.release()
                raise RuntimeError("MCP standard operation was authorized twice")
            self._mcp_dispatch_permits[key] = permit
            self._mcp_dispatch_resources[key] = runtime
        return KernelToolAuthorization(
            KernelToolAuthorizationKind.ALLOW,
            f"descriptor:{entry.descriptor.id}:{entry.entry_fingerprint}",
        )


    def _resolve_meta_ref_executor(self, *, ref: NewMcpToolRef,
                                   surface_borrow: ProcessLocalToolSurfaceBorrow) -> tuple[McpBoundToolExecutor, int]:
        runtime, generation = self._current_mcp_runtime_for_borrow(surface_borrow)
        self._mcp_meta_refs.resolve_callable(
            ref.opaque_token, **self._mcp_ref_consumer_for_borrow(surface_borrow)
        )
        if runtime is None:
            raise LookupError("MCP connection or current directory is unavailable")
        specs = runtime.root_tool_specs if ref.conversation_scope_kind is ModelInputScopeKind.ROOT else runtime.subagent_tool_specs
        executor = next((runtime.executors.get((item.server_id, item.remote_tool_name)) for item in specs
            if (item.server_id, item.remote_tool_name) in runtime.executors
            and _mcp_executor_capability_version(runtime.executors[(item.server_id, item.remote_tool_name)]).identity == ref.capability_identity), None)
        if executor is None:
            raise LookupError("MCP tool is unavailable in the current directory")
        if executor_reference_basis(executor) != ref.binding:
            raise LookupError("MCP tool definition, target or execution conditions changed; query its current details")
        return executor, generation

    def _authorize_meta_mcp(
        self,
        *,
        arguments: Mapping[str, object],
        tool_call_id: str,
        turn_id: str,
        permission_snapshot: FrozenRunPermissionSnapshot,
        surface_borrow: ProcessLocalToolSurfaceBorrow,
    ) -> KernelToolAuthorization:
        target = arguments.get("target")
        token = target.get("tool_ref") if isinstance(target, Mapping) and target.get("kind") == "MCP_TOOL_REF" else None
        inner = arguments.get("arguments")
        if not isinstance(token, str) or not isinstance(inner, Mapping):
            return KernelToolAuthorization(
                KernelToolAuthorizationKind.INVALID_ARGUMENTS,
                "mcp-meta:arguments-invalid",
                "invalid new MCP tool reference or arguments",
            )
        try:
            ref = self._mcp_meta_refs.resolve_callable(
                token, **self._mcp_ref_consumer_for_borrow(surface_borrow)
            )
            executor, generation = self._resolve_meta_ref_executor(
                ref=ref, surface_borrow=surface_borrow
            )
        except LookupError as exc:
            return KernelToolAuthorization(
                KernelToolAuthorizationKind.TOOL_UNAVAILABLE,
                "mcp-meta:ref-unavailable",
                f"{exc}. This call was not dispatched. Inspect this tool again with query_capabilities and use its current parameters and invocation.target. Do not repeat earlier actions whose outcome is uncertain.",
            )
        schema = thaw_json(executor.semantic.input_schema)
        if not isinstance(schema, dict):
            raise RuntimeError("MCP meta schema did not thaw to an object")
        try:
            validate_mcp_arguments(schema, dict(inner))
        except McpSchemaEvaluationFailure as exc:
            return KernelToolAuthorization(
                KernelToolAuthorizationKind.TOOL_UNAVAILABLE,
                f"mcp-meta:{executor.semantic.descriptor_fingerprint}",
                str(exc),
            )
        except ValidationError as exc:
            return KernelToolAuthorization(
                KernelToolAuthorizationKind.INVALID_ARGUMENTS,
                f"mcp-meta:{executor.semantic.descriptor_fingerprint}",
                f"invalid MCP tool arguments: {_tool_argument_error_message(exc)}",
            )
        mode = permission_snapshot.effective_mode
        if (
            executor.policy.effect_kind is McpEffectKind.EXTERNAL_EFFECT
            and mode is PermissionMode.READ_ONLY
        ):
            return KernelToolAuthorization(
                KernelToolAuthorizationKind.PERMISSION_DENIED,
                f"mcp-policy:{execution_policy_fingerprint(executor.policy)}",
                "external MCP effects are denied in read-only mode",
            )
        key = (generation, tool_call_id)
        if (
            key in self._mcp_dispatch_permits
            or key in self._mcp_confirmation_admissions
            or key in self._mcp_meta_invocations
        ):
            raise RuntimeError("MCP meta tool call was authorized twice")
        frozen_arguments = freeze_json(dict(inner))
        if not isinstance(frozen_arguments, FrozenJsonObjectFact):
            raise TypeError("MCP meta arguments did not freeze to an object")
        prepared = _PreparedMcpMetaInvocation(
            executor=executor,
            ref=ref,
            arguments=frozen_arguments,
        )
        self._mcp_meta_invocations[key] = prepared
        if executor.policy.effect_kind is McpEffectKind.EXTERNAL_EFFECT and mode in {
            PermissionMode.ASK_PERMISSIONS,
            PermissionMode.ACCEPT_EDITS,
        }:
            self._mcp_confirmation_admissions[key] = _PendingMcpConfirmationAdmission(
                generation=generation,
                executor=executor,
                scope_kind=surface_borrow.prepared.access.conversation_scope_kind,
                turn_id=turn_id,
                tool_call_id=tool_call_id,
            )
            return KernelToolAuthorization(
                KernelToolAuthorizationKind.REQUIRE_CONFIRMATION,
                f"mcp-policy:{execution_policy_fingerprint(executor.policy)}",
                f"Allow external MCP action {executor.semantic.remote_tool_name}?",
            )
        try:
            permit = executor.admit(
                session_id=self._session_id,
                scope_kind=surface_borrow.prepared.access.conversation_scope_kind,
                turn_id=turn_id,
                tool_call_id=tool_call_id,
            )
        except McpSnapshotStale:
            self._mcp_meta_invocations.pop(key, None)
            return KernelToolAuthorization(
                KernelToolAuthorizationKind.TOOL_UNAVAILABLE,
                "mcp-runtime:snapshot-stale",
                "MCP_SNAPSHOT_STALE",
            )
        self._mcp_dispatch_permits[key] = permit
        return KernelToolAuthorization(
            KernelToolAuthorizationKind.ALLOW,
            f"mcp-policy:{execution_policy_fingerprint(executor.policy)}",
        )


    def prepare_permission_request(
        self,
        *,
        tool_call_id: str,
        turn_id: str,
        surface_borrow: ProcessLocalToolSurfaceBorrow,
    ) -> PreparedPermissionRequest:
        access = surface_borrow.prepared.access
        state_key = (access.surface_generation, tool_call_id)
        admission = self._mcp_confirmation_admissions.get(state_key)
        permit = self._mcp_dispatch_permits.get(state_key)
        if admission is not None and (
            admission.generation != access.surface_generation
            or admission.turn_id != turn_id
            or admission.scope_kind is not access.conversation_scope_kind
        ):
            raise RuntimeError("permission request admission does not exact-join")
        if permit is not None and (
            permit.turn_id != turn_id
            or permit.scope_kind is not access.conversation_scope_kind
        ):
            raise RuntimeError("permission request permit does not exact-join")
        if admission is None and permit is None:
            state_key = None
        return PreparedPermissionRequest(
            tool_call_id=tool_call_id,
            turn_id=turn_id,
            scope_kind=access.conversation_scope_kind,
            request_nonce=object(),
            pending_state_key=state_key,
            pending_admission=admission,
            pending_permit=permit,
            pending_meta_invocation=(
                None if state_key is None else self._mcp_meta_invocations.get(state_key)
            ),
        )


    def _validate_permission_request(
        self, prepared: PreparedPermissionRequest
    ) -> tuple[
        tuple[int, str] | None,
        _PendingMcpConfirmationAdmission | None,
        McpDispatchAdmissionPermit | None,
    ]:
        key = prepared.pending_state_key
        if key is None:
            return None, None, None
        if not isinstance(key, tuple) or len(key) != 2:
            raise RuntimeError("permission request state key is invalid")
        admission = self._mcp_confirmation_admissions.get(key)
        permit = self._mcp_dispatch_permits.get(key)
        if (
            admission is not prepared.pending_admission
            or permit is not prepared.pending_permit
            or self._mcp_meta_invocations.get(key)
            is not prepared.pending_meta_invocation
        ):
            raise RuntimeError("permission request owner changed")
        return key, admission, permit


    def resolve_hook_permission(
        self, *, prepared_request: PreparedPermissionRequest, allow: bool
    ) -> KernelToolAuthorization:
        key, admission, permit = self._validate_permission_request(prepared_request)
        if not allow:
            if key is not None and admission is not None:
                self._mcp_confirmation_admissions.pop(key, None)
                self._mcp_meta_invocations.pop(key, None)
            if key is not None and permit is not None:
                self._mcp_dispatch_resources.pop(key, None)
                self._mcp_dispatch_permits.pop(key, None)
                if permit.state.value == "ADMITTED":
                    permit.release()
            return KernelToolAuthorization(
                KernelToolAuthorizationKind.PERMISSION_DENIED,
                "hook:permission-deny",
                "Hook denied this exact permission request",
            )
        if key is not None and admission is not None:
            if self._mcp_confirmation_admissions.pop(key, None) is not admission:
                raise RuntimeError("MCP Hook permission owner changed")
            try:
                permit = admission.executor.admit(
                    session_id=self._session_id,
                    scope_kind=admission.scope_kind,
                    turn_id=admission.turn_id,
                    tool_call_id=admission.tool_call_id,
                )
            except BaseException:
                self._mcp_meta_invocations.pop(key, None)
                raise
            if key in self._mcp_dispatch_permits:
                permit.release()
                raise RuntimeError("MCP Hook permission permit already exists")
            self._mcp_dispatch_permits[key] = permit
            self._mcp_dispatch_resources[key] = admission.executor
        return KernelToolAuthorization(
            KernelToolAuthorizationKind.ALLOW,
            "hook:permission-allow",
        )


    async def request_capability_form(
        self,
        *,
        authorization,
        turn_id,
        assistant_entry_id,
        tool_call_id,
        permission_snapshot,
        cell_cancel_event=None,
    ):
        call = authorization.capability_call
        if (
            call is None
            or authorization.kind
            is not KernelToolAuthorizationKind.CAPABILITY_FORM_REQUIRED
        ):
            raise RuntimeError("capability form has no prepared call")
        if self._interaction is None:
            call.discard()
            return KernelToolAuthorization(
                KernelToolAuthorizationKind.TOOL_UNAVAILABLE,
                "interaction:no-controller",
                "Capability configuration requires a controller",
            )
        try:
            resolution = await self._interaction.request_capability_form(
                turn_id=turn_id,
                assistant_entry_id=assistant_entry_id,
                tool_call_id=tool_call_id,
                permission_snapshot=permission_snapshot,
                form=call.form(authorization.public_message),
                cell_cancel_event=cell_cancel_event,
            )
        except BaseException:
            call.discard()
            raise
        if (
            resolution.decision == "SUBMIT"
            and resolution.capability_submission is not None
        ):
            call.accept(resolution.capability_submission)
            return KernelToolAuthorization(
                KernelToolAuthorizationKind.ALLOW,
                resolution.reference,
                resolution.public_message,
                capability_call=call,
                capability_user_submission=True,
            )
        call.discard()
        return KernelToolAuthorization(
            KernelToolAuthorizationKind.TOOL_UNAVAILABLE
            if "no-controller" in resolution.reference
            else KernelToolAuthorizationKind.CANCELLED_BEFORE_DISPATCH,
            resolution.reference,
            resolution.public_message,
        )


    async def request_confirmation(
        self,
        *,
        prepared_request: PreparedPermissionRequest,
        tool_name: str,
        assistant_entry_id: str,
        permission_snapshot: FrozenRunPermissionSnapshot,
        cell_cancel_event: asyncio.Event | None = None,
    ) -> KernelToolAuthorization:
        state_key, admission, permit = self._validate_permission_request(
            prepared_request
        )
        admission_entry = (
            None if state_key is None or admission is None else (state_key, admission)
        )
        permit_entry = (
            None if state_key is None or permit is None else (state_key, permit)
        )
        tool_call_id = prepared_request.tool_call_id
        turn_id = prepared_request.turn_id
        if self._interaction is None:
            if admission_entry is not None:
                self._mcp_confirmation_admissions.pop(admission_entry[0], None)
                self._mcp_meta_invocations.pop(admission_entry[0], None)
            if permit_entry is not None:
                self._mcp_dispatch_resources.pop(permit_entry[0], None)
                self._mcp_dispatch_permits.pop(permit_entry[0], None)
                permit_entry[1].release()
            return KernelToolAuthorization(
                KernelToolAuthorizationKind.PERMISSION_DENIED,
                "interaction:no-controller-owner",
                "tool execution requires confirmation but no controller is attached",
            )

        def admit_before_publish() -> None:
            if admission_entry is None:
                return
            key, admission = admission_entry
            if self._mcp_confirmation_admissions.pop(key, None) is not admission:
                raise RuntimeError("MCP confirmation admission owner changed")
            permit = admission.executor.admit(
                session_id=self._session_id,
                scope_kind=admission.scope_kind,
                turn_id=admission.turn_id,
                tool_call_id=admission.tool_call_id,
            )
            if key in self._mcp_dispatch_permits:
                permit.release()
                raise RuntimeError("MCP confirmation permit already exists")
            self._mcp_dispatch_permits[key] = permit
            self._mcp_dispatch_resources[key] = admission.executor

        def discard_admission() -> None:
            if admission_entry is not None:
                self._mcp_confirmation_admissions.pop(admission_entry[0], None)
                self._mcp_meta_invocations.pop(admission_entry[0], None)
                self._mcp_dispatch_resources.pop(admission_entry[0], None)
                current = self._mcp_dispatch_permits.pop(admission_entry[0], None)
                if current is not None and current.state.value == "ADMITTED":
                    current.release()

        resolution = await self._interaction.request_tool_confirmation(
            turn_id=turn_id,
            assistant_entry_id=assistant_entry_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            permission_snapshot=permission_snapshot,
            cell_cancel_event=cell_cancel_event,
            admission_hooks=(
                InteractionAdmissionHooks(
                    before_publish=admit_before_publish,
                    discard=discard_admission,
                    owner_key=(
                        "mcp-server:" + admission_entry[1].executor.semantic.server_id
                    ),
                )
                if admission_entry is not None
                else None
            ),
        )
        permit_entry = (
            None
            if state_key is None
            else (
                (state_key, current)
                if (current := self._mcp_dispatch_permits.get(state_key)) is not None
                else None
            )
        )
        if resolution.decision == "ALLOW":
            if permit_entry is not None:
                permit_entry[1].mark_attempt_accepted()
            return KernelToolAuthorization(
                KernelToolAuthorizationKind.ALLOW,
                resolution.reference,
                resolution.public_message,
                accepted_attempt_id=resolution.attempt_id,
                accepted_permission_snapshot_fingerprint=(
                    resolution.permission_snapshot_fingerprint
                ),
            )
        if resolution.decision == "DENY":
            if permit_entry is not None:
                self._mcp_dispatch_resources.pop(permit_entry[0], None)
                self._mcp_dispatch_permits.pop(permit_entry[0], None)
                self._mcp_meta_invocations.pop(permit_entry[0], None)
                if permit_entry[1].state.value == "ADMITTED":
                    permit_entry[1].release()
            return KernelToolAuthorization(
                (KernelToolAuthorizationKind.CANCELLED_BEFORE_DISPATCH
                 if resolution.reference == "interaction:cell-stopped" and resolution.result_entry_id is None
                 else KernelToolAuthorizationKind.PERMISSION_DENIED),
                resolution.reference,
                resolution.public_message,
                accepted_result_entry_id=resolution.result_entry_id,
                accepted_result_id=resolution.result_id,
                accepted_result_entry_sequence=(resolution.result_entry_sequence),
                accepted_result_observed_at=resolution.result_observed_at,
                accepted_result_public_body=resolution.result_public_body,
            )
        raise RuntimeError("interaction resolution vocabulary is invalid")
