"""Prepare current runtime notices; provider installation owns their comparison state."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone, tzinfo, timedelta
from pathlib import Path
from typing import Callable, Protocol

from pulsara_agent.capability.contracts import (
    CapabilitySourceSnapshotDisposition,
    FrozenToolCapabilityExposurePlan,
    FrozenSkillCapabilityDispatchView,
)
from pulsara_agent.capability.types import PluginSkillOrigin
from pulsara_agent.conversation_kernel.capability_composition import (
    PreparedSkillCatalogSourceSnapshot,
    PreparedMcpCapabilitySourceSnapshotSet,
)
from pulsara_agent.conversation_kernel.capability import KernelSkillProjectionComposer
from pulsara_agent.hooks.context import HookContextOwner, HookContextReservation
from pulsara_agent.llm.input import (
    LLMMessage,
    LLMTextPart,
    PromptAnnotationPart,
    prompt_provider_parts,
)
from pulsara_agent.model_input.contracts import (
    CapabilityActivationSubjectKind,
    CollectedContextSources,
    ContextBudgetClass,
    ContextChannel,
    ContextSourceCandidate,
    ContextSourceKind,
    FrozenCanonicalCompileSnapshot,
    ContextSourceCollectionDiagnostic,
)
from pulsara_agent.model_input.continuity import (
    InstalledObservationState,
    InstalledMcpObservationSnapshot,
    RuntimeObservationItem,
    encode_runtime_observation,
)
from pulsara_agent.primitives.context import canonical_json_bytes
from pulsara_agent.mcp_config import ManagedPackageMcpRuntimeSource


class ContextSourceCollectorPort(Protocol):
    def freeze_skill_capability_source_snapshot(
        self, *, conversation_scope_kind, deadline_monotonic=None
    ): ...
    def freeze_skill_capability_projection_input(self, owner): ...
    def freeze_non_trigger_sources(self, **kwargs): ...
    def complete_frozen_sources(self, frozen, **kwargs): ...
    def freeze_hook_context_message(self, *, scope_kind, session_id, estimator): ...


@dataclass(frozen=True, slots=True)
class FrozenNonTriggerContextSources:
    candidates: tuple[ContextSourceCandidate, ...]
    diagnostics: tuple[ContextSourceCollectionDiagnostic, ...]
    tool_exposure_plan: FrozenToolCapabilityExposurePlan
    skill_dispatch_view: FrozenSkillCapabilityDispatchView
    skill_owner_snapshot: PreparedSkillCatalogSourceSnapshot
    mcp_owner_snapshot: PreparedMcpCapabilitySourceSnapshotSet | None
    canonical_facts: FrozenCanonicalCompileSnapshot
    previous_state: InstalledObservationState
    new_context: bool
    active_compaction: bool
    hook_context: LLMMessage | None = None
    hook_context_reservation: HookContextReservation | None = None


def observation_source(kind, body, placement, *, optional=False):
    message = encode_runtime_observation(source_kind=kind, body=tuple(body))
    return ContextSourceCandidate(
        kind,
        ContextChannel.RUNTIME_OBSERVATION,
        message.content[0].text,
        placement,
        ContextBudgetClass.OPTIONAL if optional else ContextBudgetClass.MUST_KEEP,
    )


def _plugin_suffix(origin):
    return (
        f"（来自插件 {origin.plugin_id}）"
        if isinstance(origin, PluginSkillOrigin)
        else ""
    )


def _skill_fields(item):
    return (item.name, item.description, item.skill_path, item.origin)


def _skill_changes(previous, current):
    new = {item.name: item for item in current.inspection.winners}
    if previous is None:
        if not new:
            return (RuntimeObservationItem("技能目录已恢复可用；当前没有可用技能。"),)
        return tuple(
            RuntimeObservationItem(
                f"技能目录已恢复可用：{item.name}{_plugin_suffix(item.origin)}：{item.description}。",
                {"kind": "SKILL", "skill_path": item.skill_path},
            )
            for item in new.values()
        )
    old = {item.name: item for item in previous.inspection.winners}
    disabled = {item.name: item for item in current.disabled_entries}
    result = []
    for name, item in new.items():
        before = old.get(name)
        if before is not None and _skill_fields(before) == _skill_fields(item):
            continue
        verb = "新增或启用" if before is None else "已更新"
        result.append(
            RuntimeObservationItem(
                f"技能 {name}{_plugin_suffix(item.origin)}{verb}：{item.description}。",
                {"kind": "SKILL", "skill_path": item.skill_path},
            )
        )
    for name, item in old.items():
        if name in new:
            continue
        if name in disabled:
            result.append(
                RuntimeObservationItem(
                    f"技能 {name}{_plugin_suffix(item.origin)}已禁用，请勿继续读取或使用该技能。",
                    {"kind": "SKILL", "skill_path": disabled[name].skill_path},
                )
            )
        elif isinstance(item.origin, PluginSkillOrigin):
            result.append(
                RuntimeObservationItem(
                    f"技能 {name}{_plugin_suffix(item.origin)}当前已不可用，请勿继续读取或使用该技能。"
                )
            )
        else:
            result.append(RuntimeObservationItem(f"技能 {name}已移除。"))
    return tuple(result)


def _mcp_label(server):
    origin = server.runtime_source
    suffix = (
        f"（来自插件 {origin.package_owner_key}）"
        if isinstance(origin, ManagedPackageMcpRuntimeSource)
        else ""
    )
    return server.display_name + suffix


def _mcp_tool_facts(owner, server_id):
    return {
        item.semantic.remote_tool_name: item
        for item in owner.inspection_inputs
        if item.semantic.server_id == server_id
    }


def _mcp_comparison_snapshot(previous, current):
    if current is None:
        return previous
    servers = {item.server_id: item for item in current.catalog_snapshot.servers}
    pending = {key for key, item in servers.items() if item.status.value != "READY"}
    retained = (
        ()
        if previous is None
        else tuple(
            item
            for item in previous.inspection_inputs
            if item.semantic.server_id in pending
        )
    )
    current_tools = tuple(
        item
        for item in current.inspection_inputs
        if item.semantic.server_id not in pending
    )
    return InstalledMcpObservationSnapshot(
        current.catalog_snapshot, (*current_tools, *retained)
    )


def _mcp_changes(previous, current):
    new = {
        item.server_id: item
        for item in current.catalog_snapshot.servers
        if item.connection_target is not None
    }
    old = (
        {}
        if previous is None
        else {
            item.server_id: item
            for item in previous.catalog_snapshot.servers
            if item.connection_target is not None
        }
    )
    result = []
    for server_id, server in new.items():
        before = old.get(server_id)
        target = server.connection_target
        label = _mcp_label(server)
        notes = []
        ready = server.status.value == "READY"
        disabled = server.status.value == "DISABLED"
        if before is None:
            notes.append(
                "当前恢复可用"
                if previous is None
                else "已添加" + ("，目前停用" if disabled else "并启用")
            )
        else:
            if (server.status.value == "DISABLED") != (
                before.status.value == "DISABLED"
            ):
                notes.append("已停用，后续使用可能受影响" if disabled else "已启用")
            elif ready != (before.status.value == "READY"):
                notes.append(
                    "已恢复可用" if ready else "当前暂不可用，后续调用可能受影响"
                )
            if (
                server.display_name != before.display_name
                or server.sanitized_instructions != before.sanitized_instructions
            ):
                notes.append("名称或使用说明已更新")
            def connection_facts(item):
                config = item.configuration
                return None if config is None else (config.transport, config.auth, config.public_headers)

            if connection_facts(server) != connection_facts(before):
                notes.append("连接配置或实际目标已更新，可能影响此前的调用信息")
            if server.scope_subagents != before.scope_subagents:
                notes.append("服务可见范围已更新")
            if ready:
                now_tools = _mcp_tool_facts(current, server_id)
                old_tools = _mcp_tool_facts(previous, server_id)
                if set(now_tools) != set(old_tools):
                    notes.append("工具目录已更新")
                categories = set()
                for name, tool in now_tools.items():
                    prior = old_tools.get(name)
                    if prior is None:
                        continue
                    a, b = prior.semantic, tool.semantic
                    if a.description != b.description:
                        categories.add("工具使用说明")
                    if a.input_schema != b.input_schema:
                        categories.add("工具输入参数定义")
                    if a.output_schema != b.output_schema:
                        categories.add("工具声明的返回结构")
                    if (
                        a.root_visible,
                        a.subagent_visible,
                        prior.policy.effect_kind,
                        prior.policy.parallel_safe,
                        prior.policy.timeout_ms,
                    ) != (
                        b.root_visible,
                        b.subagent_visible,
                        tool.policy.effect_kind,
                        tool.policy.parallel_safe,
                        tool.policy.timeout_ms,
                    ):
                        categories.add("工具使用条件或可见范围")
                if categories:
                    notes.append(
                        "、".join(sorted(categories))
                        + "已更新，相关调用信息可能与此前不同"
                    )
        if notes:
            result.append(
                RuntimeObservationItem(
                    f"MCP 服务 {label}：{'；'.join(notes)}。如有需要，可以用 query_capabilities 查询当前服务工具目录或相关工具详情。",
                    target,
                )
            )
    for server_id, server in old.items():
        if server_id not in new:
            result.append(
                RuntimeObservationItem(
                    f"MCP 服务 {_mcp_label(server)}已移除。如有需要，可以用 query_capabilities 查询当前可用服务。"
                )
            )
    return tuple(result)


def _permission_text(effective_mode):
    text = {
        "read-only": "允许读取与调查；不允许文件修改、终端命令、进程变更及有外部效果的 MCP 调用。",
        "ask-permissions": "文件写入、终端命令、进程变更及有外部效果的 MCP 调用须沿实际权限流程获得确认。",
        "accept-edits": "允许工作区内文件写入；工作区外写入、终端命令、进程变更及有外部效果的 MCP 调用须确认。",
        "bypass-permissions": "文件写入、终端操作及有外部效果的 MCP 调用无需本模式下的权限确认，仍须遵循实际工具和任务约束。",
    }[effective_mode.value]
    return f"本 turn 冻结权限：{effective_mode.value}。{text}"


def _previous_turn_body(fact):
    text = {
        "EXECUTION_FAILED": "紧邻前轮在任务完成前因执行失败结束。",
        "USER_STOPPED": "用户停止了紧邻前轮；除非用户要求，否则不要自动恢复。",
        "HOST_SESSION_CLOSED": "紧邻前轮在 Host 关闭时未完成。",
        "HOST_REPLACED": "紧邻前轮因 Host 失去所有权而未完成。",
        "PROVIDER_INPUT_CONFLICT": "紧邻前轮在 provider 输入连续性边界停止。",
        "RESOURCE_BOUNDARY": "紧邻前轮因实际资源边界停止。",
        "PLAN_CONTINUATION_FAILED": "紧邻前轮已接受规划续接，但未能继续。",
        "UNKNOWN_INTERRUPTION": "紧邻前轮因未明确的中断结束。",
    }[fact.outcome_kind.value]
    if fact.accepted_assistant_entry_count:
        text += " 已保存的部分助手回复不表示任务已完成。"
    if fact.definitely_not_dispatched_tool_count:
        text += f" 有 {fact.definitely_not_dispatched_tool_count} 项工具调用可确认尚未派发。"
    if fact.outcome_unknown_tool_count:
        text += f" 有 {fact.outcome_unknown_tool_count} 项工具调用的实际效果未知，请勿自动重试。"
    body = [RuntimeObservationItem(text)]
    if fact.terminal_public_detail:
        body.append(
            RuntimeObservationItem(
                "前轮诊断（作为引用材料）：" + fact.terminal_public_detail
            )
        )
    return tuple(body)


class KernelContextSourceCollector:
    def __init__(
        self,
        *,
        workspace_kind,
        workspace_root,
        capability_composer,
        base_system_prompt,
        display_timezone,
        clock: Callable[[], datetime] | None = None,
        hook_context_owner=None,
    ):
        if workspace_kind not in {"project", "transient"} or not base_system_prompt:
            raise ValueError("runtime environment or base instruction is invalid")
        self._workspace_kind = workspace_kind
        self._workspace_root = Path(workspace_root).expanduser().resolve()
        self._capability: KernelSkillProjectionComposer = capability_composer
        self._base = base_system_prompt
        self._timezone, self._timezone_name = _freeze_display_timezone(display_timezone)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._hook_context_owner: HookContextOwner | None = hook_context_owner

    def freeze_skill_capability_source_snapshot(
        self, *, conversation_scope_kind, deadline_monotonic=None
    ):
        return self._capability.freeze_owner_snapshot(
            conversation_scope_kind=conversation_scope_kind,
            deadline_monotonic=deadline_monotonic,
        )

    def freeze_skill_capability_projection_input(self, owner):
        return self._capability.freeze_projection_input(owner)

    def _turn_context(self, effective_mode):
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("runtime clock must be timezone-aware")
        local = now.astimezone(self._timezone)
        offset = local.utcoffset()
        if offset is None:
            raise ValueError("runtime timezone has no offset")
        seconds = int(offset.total_seconds())
        sign = "+" if seconds >= 0 else "-"
        minutes = abs(seconds) // 60
        return (
            RuntimeObservationItem(
                f"工作区类型：{self._workspace_kind}；根路径：{self._workspace_root}；时区：{self._timezone_name}；UTC 偏移：{sign}{minutes // 60:02d}:{minutes % 60:02d}。"
            ),
            RuntimeObservationItem(_permission_text(effective_mode)),
            RuntimeObservationItem("当前当地时间：" + local.isoformat() + "。"),
        )

    def collect(
        self, *, activation_subject, activation_text, skill_reference_surfaces, **kwargs
    ):
        return self.complete_frozen_sources(
            self.freeze_non_trigger_sources(**kwargs),
            activation_subject=activation_subject,
            activation_text=activation_text,
            skill_reference_surfaces=skill_reference_surfaces,
        )

    def freeze_non_trigger_sources(
        self,
        *,
        tool_surface,
        canonical_facts,
        tool_exposure_plan,
        skill_dispatch_view,
        skill_owner_snapshot,
        mcp_owner_snapshot=None,
        user_actions=None,
        hook_context_estimator=None,
        previous_state=None,
        new_context=False,
        active_compaction=False,
        allow_turn_observations=True,
        deadline_monotonic=None,
    ):
        previous_state = previous_state or InstalledObservationState()
        identity = canonical_facts.canonical_input.identity
        new_turn = (
            allow_turn_observations and previous_state.last_turn_id != identity.turn_id
        )
        candidates = [
            ContextSourceCandidate(
                ContextSourceKind.BASE_SYSTEM, ContextChannel.SYSTEM, self._base, 0
            )
        ]
        if (
            (new_turn and not new_context)
            or (new_context and not active_compaction and new_turn)
            or active_compaction
        ):
            candidates.append(
                observation_source(
                    ContextSourceKind.TURN_CONTEXT,
                    self._turn_context(
                        canonical_facts.run_permission_snapshot.effective_mode
                    ),
                    30,
                )
            )
        if active_compaction and canonical_facts.plan_workflow_fact is not None:
            body = [
                RuntimeObservationItem(
                    "当前仍处于计划模式。请继续只读调查和规划，不执行修改。"
                )
            ]
            original = canonical_facts.plan_mode_original_input
            if original is not None:
                from pulsara_agent.llm.input import LLMImagePart

                body.append(RuntimeObservationItem("进入规划前的用户输入："))
                for part in original:
                    if isinstance(part, LLMImagePart):
                        body.append(
                            RuntimeObservationItem(
                                "进入规划前的用户图片。",
                                {"kind": "IMAGE_REF", "image_ref": part.image_ref},
                            )
                        )
                    elif isinstance(part, LLMTextPart):
                        body.append(RuntimeObservationItem(part.text))
                    elif isinstance(part, PromptAnnotationPart):
                        body.extend(
                            RuntimeObservationItem(value.text)
                            for value in prompt_provider_parts((part,))
                        )
            candidates.append(observation_source(ContextSourceKind.PLAN_MODE, body, 31))
        approved = canonical_facts.approved_plan_materialization_fact
        if approved is not None and (new_turn or active_compaction):
            candidates.append(
                observation_source(
                    ContextSourceKind.APPROVED_PLAN,
                    (
                        RuntimeObservationItem(
                            "用户已批准以下计划。部分工作可能已经完成，请结合已有进展继续，避免重复执行。"
                        ),
                        RuntimeObservationItem(
                            approved.exact_plan_utf8.decode("utf-8")
                        ),
                    ),
                    31,
                )
            )
        if (
            new_turn or active_compaction
        ) and canonical_facts.previous_turn_outcome_fact is not None:
            candidates.append(
                observation_source(
                    ContextSourceKind.PREVIOUS_TURN_OUTCOME,
                    _previous_turn_body(canonical_facts.previous_turn_outcome_fact),
                    32,
                )
            )
        if user_actions:
            candidates.append(
                observation_source(
                    ContextSourceKind.USER_ACTIONS, user_actions, 90, optional=True
                )
            )
        hook, reservation = (
            (None, None)
            if hook_context_estimator is None
            else self.freeze_hook_context_message(
                scope_kind=identity.conversation_scope_kind.value,
                session_id=identity.session_id,
                estimator=hook_context_estimator,
            )
        )
        return FrozenNonTriggerContextSources(
            tuple(candidates),
            (),
            tool_exposure_plan,
            skill_dispatch_view,
            skill_owner_snapshot,
            mcp_owner_snapshot,
            canonical_facts,
            previous_state,
            new_context,
            active_compaction,
            hook,
            reservation,
        )

    def freeze_hook_context_message(self, *, scope_kind, session_id, estimator):
        if self._hook_context_owner is None or estimator is None:
            return None, None
        prepared = self._hook_context_owner.freeze_for_target(
            scope_kind=scope_kind, session_id=session_id, estimator=estimator
        )
        return (
            (None, None)
            if prepared is None
            else (LLMMessage.user(prepared.text), prepared.reservation)
        )

    def complete_frozen_sources(
        self,
        frozen,
        *,
        activation_subject,
        activation_text,
        skill_reference_surfaces,
        deadline_monotonic=None,
    ):
        human = activation_subject is CapabilityActivationSubjectKind.ROOT_HUMAN_PROMPT
        output = self._capability.compose(
            view=frozen.skill_dispatch_view,
            owner=frozen.skill_owner_snapshot,
            activation_subject=self._capability.activation_context(
                user_input=skill_reference_surfaces if human else ()
            ),
        )
        candidates = list(frozen.candidates)
        state = frozen.previous_state
        identity = frozen.canonical_facts.canonical_input.identity
        eligible = state.last_turn_id != identity.turn_id or human
        available = (
            frozen.skill_owner_snapshot.source_snapshot.disposition
            is CapabilitySourceSnapshotDisposition.COMPLETE
            and not output.catalog_unavailable_causes
        )
        if frozen.new_context:
            if available:
                text = "当前可用技能：\n\n" + "\n\n".join(
                    f"{entry.name}{_plugin_suffix(entry.origin)}：{entry.description}\n"
                    + canonical_json_bytes(
                        {"kind": "SKILL", "skill_path": entry.skill_path}
                    ).decode("utf-8")
                    for entry in output.catalog_entries
                )
                if not output.catalog_entries:
                    text = "当前没有可用技能。"
                state = replace(
                    state,
                    skill_catalog=frozen.skill_owner_snapshot,
                    mcp_catalog=_mcp_comparison_snapshot(
                        None, frozen.mcp_owner_snapshot
                    ),
                    memory=None,
                )
            else:
                text = "当前无法获取技能目录。"
                state = replace(
                    state,
                    skill_catalog=None,
                    mcp_catalog=_mcp_comparison_snapshot(
                        None, frozen.mcp_owner_snapshot
                    ),
                    memory=None,
                )
            candidates.append(
                ContextSourceCandidate(
                    ContextSourceKind.SKILL_CATALOG,
                    ContextChannel.CONTEXT_MESSAGE,
                    text,
                    1,
                )
            )
        elif eligible:
            if available:
                changes = _skill_changes(
                    state.skill_catalog, frozen.skill_owner_snapshot
                )
                if changes:
                    candidates.append(
                        observation_source(
                            ContextSourceKind.SKILL_CATALOG_CHANGES, changes, 50
                        )
                    )
                if changes:
                    state = replace(state, skill_catalog=frozen.skill_owner_snapshot)
            else:
                candidates.append(
                    observation_source(
                        ContextSourceKind.SKILL_CATALOG_CHANGES,
                        (
                            RuntimeObservationItem(
                                "当前无法可靠获取技能目录；这不表示原有技能已全部移除。"
                            ),
                        ),
                        50,
                    )
                )
            if frozen.mcp_owner_snapshot is not None:
                current_mcp = _mcp_comparison_snapshot(
                    state.mcp_catalog, frozen.mcp_owner_snapshot
                )
                changes = _mcp_changes(state.mcp_catalog, current_mcp)
                if changes:
                    candidates.append(
                        observation_source(
                            ContextSourceKind.MCP_CATALOG_CHANGES, changes, 51
                        )
                    )
                if changes:
                    state = replace(state, mcp_catalog=current_mcp)
        if human and (
            output.active_skills or output.active_unavailable_reason is not None
        ):
            body = tuple(
                RuntimeObservationItem(
                    f"用户通过 slash 指定使用 {entry.name}{_plugin_suffix(entry.origin)} 技能。优先使用 read_file 工具读取该技能说明；必须先完成阅读，再继续相关工作。",
                    {"kind": "SKILL", "skill_path": entry.skill_path},
                )
                for entry in output.active_skills
            )
            if output.active_unavailable_reason is not None:
                body = (
                    *body,
                    RuntimeObservationItem(
                        "本次无法可靠解析用户指定的技能。请先查询当前可用技能目录；获得合法 Skill Target 后，优先用 read_file 读取说明，再继续相关工作。"
                    ),
                )
            candidates.append(
                observation_source(ContextSourceKind.ACTIVE_SKILL, body, 52)
            )
        return CollectedContextSources(
            tuple(candidates), frozen.diagnostics, frozen.hook_context, state
        )


def build_compaction_context_source(*, text):
    if not text:
        return None
    return ContextSourceCandidate(
        ContextSourceKind.COMPACTION_RUNTIME_HANDOFF,
        ContextChannel.CONTEXT_MESSAGE,
        text,
        20,
    )


def replace_memory_context_sources(sources, replacements):
    kinds = {ContextSourceKind.MEMORY_RECALL, ContextSourceKind.MEMORY_WRITE_HINT}
    return replace(
        sources,
        candidates=tuple(
            item for item in sources.candidates if item.source_kind not in kinds
        )
        + tuple(replacements),
    )


def replace_compaction_context_sources(sources, replacements):
    replacements = tuple(item for item in replacements if item is not None)
    kinds = {item.source_kind for item in replacements}
    return replace(
        sources,
        candidates=tuple(
            item for item in sources.candidates if item.source_kind not in kinds
        )
        + replacements,
    )


def replace_frozen_compaction_context_sources(frozen, replacements):
    return replace_compaction_context_sources(frozen, replacements)


def replace_hook_context_message(sources, message):
    return replace(sources, hook_context=message)


def replace_frozen_hook_context_message(frozen, message, *, reservation=None):
    return replace(frozen, hook_context=message, hook_context_reservation=reservation)


def _freeze_display_timezone(value: tzinfo) -> tuple[tzinfo, str]:
    """Keep IANA rules, but freeze an unkeyed zone to its opening offset."""

    key = getattr(value, "key", None)
    if isinstance(key, str) and key:
        return value, key
    now = datetime.now(timezone.utc).astimezone(value)
    offset = now.utcoffset()
    if offset is None:
        raise ValueError("session display timezone has no UTC offset")
    seconds = offset.total_seconds()
    if not seconds.is_integer() or int(seconds) % 60:
        raise ValueError("session display timezone offset is not minute-aligned")
    minutes = int(seconds) // 60
    sign = "+" if minutes >= 0 else "-"
    hours, remainder = divmod(abs(minutes), 60)
    name = f"UTC{sign}{hours:02d}:{remainder:02d}"
    return timezone(timedelta(minutes=minutes), name), name
