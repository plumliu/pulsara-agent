"""Formal structured-input test consumers; production never imports this module."""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator

from pulsara_agent.conversation_kernel.direct_model import (
    DirectKernelModelPort,
    KernelModelExecutionRequest,
    KernelModelPreparationRequest,
    PreparedKernelModelCall,
)
from pulsara_agent.conversation_kernel.runner import KernelToolInvocationContext
from pulsara_agent.conversation_kernel.tool_surface import (
    PreparedKernelToolSurface,
    ProcessLocalToolSurfaceAccess,
    ProcessLocalToolSurfaceBorrow,
)
from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
from pulsara_agent.model_input.contracts import (
    CollectedContextSources,
    ContextBudgetClass,
    ContextChannel,
    ContextRenderMode,
    ContextRenderVariant,
    ContextSourceCandidate,
    ContextSourceKind,
    ContextTrustClass,
    FrozenModelToolSurface,
    FrozenToolSpec,
    ModelInputScopeKind,
    model_tool_surface_fingerprint,
)
from pulsara_agent.primitives.context import context_fingerprint, freeze_json
from tests.support.model_config import test_llm_config


class ScriptedKernelModel:
    def __init__(self, calls: list[list[object]]) -> None:
        self._calls = calls
        self.requests: list[KernelModelExecutionRequest] = []
        self.preparation_requests: list[KernelModelPreparationRequest] = []
        self._preparer = DirectKernelModelPort(
            config=test_llm_config(
                api_key="test",
                base_url="https://example.invalid/v1",
                pro_model="test-pro",
                flash_model="test-flash",
                api="openai_chat_completions",
            )
        )

    def prepare_call(
        self, request: KernelModelPreparationRequest
    ) -> PreparedKernelModelCall:
        self.preparation_requests.append(request)
        return self._preparer.prepare_call(request)

    async def stream(
        self, request: KernelModelExecutionRequest
    ) -> AsyncIterator[object]:
        self.requests.append(request)
        for item in self._calls.pop(0):
            yield item


@dataclass(frozen=True, slots=True)
class _Cwd:
    value: str = "/test/workspace"


class StaticContextSourceCollector:
    """Two required first-party facts with the production contracts."""

    @property
    def registry_fingerprint(self) -> str:
        return context_fingerprint(
            "test-context-source-registry:v1",
            (
                ContextSourceKind.BASE_SYSTEM.value,
                ContextSourceKind.RUNTIME_ENVIRONMENT.value,
            ),
        )

    def collect(self, **_kwargs: object) -> CollectedContextSources:
        candidates = (
            _candidate(
                kind=ContextSourceKind.BASE_SYSTEM,
                version="pulsara.base-system.v1",
                channel=ContextChannel.SYSTEM,
                trust=ContextTrustClass.ROOT_INSTRUCTION,
                budget=ContextBudgetClass.MUST_KEEP,
                placement=0,
                degradation=0,
                variants=((ContextRenderMode.FULL, "ROOT SYSTEM"),),
            ),
            _candidate(
                kind=ContextSourceKind.RUNTIME_ENVIRONMENT,
                version="pulsara.runtime-environment.v1",
                channel=ContextChannel.SYSTEM,
                trust=ContextTrustClass.TRUSTED_RUNTIME_FACT,
                budget=ContextBudgetClass.MUST_KEEP,
                placement=10,
                degradation=10,
                variants=(
                    (ContextRenderMode.FULL, "runtime workspace=/test/workspace"),
                    (ContextRenderMode.COMPACT, "runtime=/test/workspace"),
                ),
            ),
        )
        registry = self.registry_fingerprint
        collection = context_fingerprint(
            "collected-context-sources:v1",
            {
                "registry_fingerprint": registry,
                "candidates": tuple(
                    item.source_semantic_fingerprint for item in candidates
                ),
                "diagnostics": (),
            },
        )
        return CollectedContextSources(candidates, (), registry, collection)


class StructuredToolPort:
    def __init__(
        self,
        delegate: object,
        *,
        tool_names: tuple[str, ...] = ("terminal", "test_tool"),
    ) -> None:
        self.delegate = delegate
        self._authority = object()
        self._active: set[str] = set()
        specs = tuple(
            FrozenToolSpec(
                name=name,
                description=f"Test tool {name}",
                parameters=freeze_json(
                    {"type": "object", "additionalProperties": True}
                ),
                descriptor_fingerprint=context_fingerprint(
                    "test-tool-descriptor:v1", name
                ),
                executor_binding_fingerprint=context_fingerprint(
                    "test-tool-binding:v1", name
                ),
            )
            for name in sorted(tool_names)
        )
        self._surfaces = {
            scope: FrozenModelToolSurface(
                scope,
                specs,
                model_tool_surface_fingerprint(scope, specs),
            )
            for scope in ModelInputScopeKind
        }

    def snapshot_tool_surface(
        self,
        *,
        conversation_scope_kind: ModelInputScopeKind,
        scope_subagent_task_id: str | None,
    ) -> PreparedKernelToolSurface:
        surface = self._surfaces[conversation_scope_kind]
        access = ProcessLocalToolSurfaceAccess(
            owner_epoch=1,
            surface_generation=1,
            conversation_scope_kind=conversation_scope_kind,
            scope_subagent_task_id=scope_subagent_task_id,
            surface_fingerprint=surface.surface_fingerprint,
            _authority=self._authority,
        )
        return PreparedKernelToolSurface(
            surface,
            tuple(item.executor_binding_fingerprint for item in surface.tool_specs),
            access,
        )

    def borrow_tool_surface(
        self, prepared: PreparedKernelToolSurface
    ) -> ProcessLocalToolSurfaceBorrow:
        borrow_id = f"test-borrow:{len(self._active) + 1}"
        self._active.add(borrow_id)

        def validate(borrow: ProcessLocalToolSurfaceBorrow, tool_name: str) -> str:
            if borrow.borrow_id not in self._active:
                raise RuntimeError("test tool surface borrow is inactive")
            for item in prepared.model_surface.tool_specs:
                if item.name == tool_name:
                    return item.executor_binding_fingerprint
            raise RuntimeError("test tool was not advertised")

        def release(borrow: ProcessLocalToolSurfaceBorrow) -> None:
            self._active.discard(borrow.borrow_id)

        return ProcessLocalToolSurfaceBorrow(
            prepared=prepared,
            borrow_id=borrow_id,
            _authority=self._authority,
            _validate=validate,
            _release=release,
        )

    async def authorize(self, **kwargs: object):
        kwargs.pop("surface_borrow")
        return await self.delegate.authorize(**kwargs)

    async def request_confirmation(self, **kwargs: object):
        return await self.delegate.request_confirmation(**kwargs)

    async def invoke(self, **kwargs: object):
        return await self.delegate.invoke(**kwargs)

    async def settle_process_local_effect(self, *args: object) -> None:
        method = getattr(self.delegate, "settle_process_local_effect", None)
        if method is not None:
            await method(*args)


def direct_tool_invocation_context(
    port: DirectKernelToolPort,
    *,
    session_id: str,
    tool_name: str,
    tool_call_id: str,
    attempt_id: str,
    turn_id: str,
    assistant_entry_id: str,
    workspace_id: str = "workspace:test",
    conversation_scope_kind: ModelInputScopeKind = ModelInputScopeKind.ROOT,
    scope_subagent_task_id: str | None = None,
) -> tuple[ProcessLocalToolSurfaceBorrow, KernelToolInvocationContext]:
    """Acquire the same narrow surface authority used by the runner."""

    prepared = port.snapshot_tool_surface(
        conversation_scope_kind=conversation_scope_kind,
        scope_subagent_task_id=scope_subagent_task_id,
    )
    borrow = port.borrow_tool_surface(prepared)
    try:
        binding_fingerprint = borrow.binding_fingerprint(tool_name)
        return borrow, KernelToolInvocationContext(
            session_id=session_id,
            workspace_id=workspace_id,
            turn_id=turn_id,
            assistant_entry_id=assistant_entry_id,
            tool_call_id=tool_call_id,
            attempt_id=attempt_id,
            result_entry_id=f"result:{attempt_id}",
            conversation_scope_kind=conversation_scope_kind.value,
            scope_subagent_task_id=scope_subagent_task_id,
            host_owner_epoch=1,
            authorization_reference="test:authorized",
            tool_surface_fingerprint=prepared.model_surface.surface_fingerprint,
            executor_binding_fingerprint=binding_fingerprint,
            surface_borrow=borrow,
        )
    except BaseException:
        borrow.close()
        raise


async def invoke_direct_tool(
    port: DirectKernelToolPort,
    *,
    session_id: str,
    tool_name: str,
    arguments: dict[str, object],
    tool_call_id: str,
    attempt_id: str,
    turn_id: str,
    assistant_entry_id: str,
    workspace_id: str = "workspace:test",
    conversation_scope_kind: ModelInputScopeKind = ModelInputScopeKind.ROOT,
    scope_subagent_task_id: str | None = None,
    **kwargs: object,
):
    """Invoke through the same short-lived binding borrow as production."""

    borrow, invocation_context = direct_tool_invocation_context(
        port,
        session_id=session_id,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        attempt_id=attempt_id,
        turn_id=turn_id,
        assistant_entry_id=assistant_entry_id,
        workspace_id=workspace_id,
        conversation_scope_kind=conversation_scope_kind,
        scope_subagent_task_id=scope_subagent_task_id,
    )
    try:
        return await port.invoke(
            tool_name=tool_name,
            arguments=arguments,
            tool_call_id=tool_call_id,
            attempt_id=attempt_id,
            turn_id=turn_id,
            assistant_entry_id=assistant_entry_id,
            invocation_context=invocation_context,
            **kwargs,
        )
    finally:
        borrow.close()


async def authorize_direct_tool(
    port: DirectKernelToolPort,
    *,
    session_id: str,
    tool_name: str,
    arguments: dict[str, object],
    tool_call_id: str,
    turn_id: str,
    assistant_entry_id: str,
    conversation_scope_kind: ModelInputScopeKind = ModelInputScopeKind.ROOT,
    scope_subagent_task_id: str | None = None,
):
    """Authorize under a formally acquired immutable surface borrow."""

    prepared = port.snapshot_tool_surface(
        conversation_scope_kind=conversation_scope_kind,
        scope_subagent_task_id=scope_subagent_task_id,
    )
    borrow = port.borrow_tool_surface(prepared)
    try:
        return await port.authorize(
            tool_name=tool_name,
            arguments=arguments,
            tool_call_id=tool_call_id,
            turn_id=turn_id,
            assistant_entry_id=assistant_entry_id,
            surface_borrow=borrow,
        )
    finally:
        borrow.close()


def _candidate(
    *,
    kind: ContextSourceKind,
    version: str,
    channel: ContextChannel,
    trust: ContextTrustClass,
    budget: ContextBudgetClass,
    placement: int,
    degradation: int,
    variants: tuple[tuple[ContextRenderMode, str], ...],
) -> ContextSourceCandidate:
    modes = tuple(mode for mode, _text in variants)
    contract = context_fingerprint(
        "context-source-contract:v1",
        {
            "kind": kind.value,
            "version": version,
            "channel": channel.value,
            "trust": trust.value,
            "budget": budget.value,
            "placement": placement,
            "degradation": degradation,
            "modes": tuple(mode.value for mode in modes),
        },
    )
    rendered = tuple(
        ContextRenderVariant(
            mode,
            text,
            len(text.encode("utf-8")),
            context_fingerprint(
                "context-render-variant:v1", {"mode": mode.value, "text": text}
            ),
        )
        for mode, text in variants
    )
    instance = f"context-source:{kind.value.lower()}"
    semantic = context_fingerprint(
        "context-source-candidate:v1",
        {
            "source_kind": kind.value,
            "source_instance_id": instance,
            "source_contract_fingerprint": contract,
            "variants": tuple(item.semantic_fingerprint for item in rendered),
        },
    )
    return ContextSourceCandidate(
        kind,
        instance,
        version,
        contract,
        semantic,
        channel,
        trust,
        budget,
        placement,
        degradation,
        rendered,
    )


__all__ = [
    "ScriptedKernelModel",
    "StaticContextSourceCollector",
    "StructuredToolPort",
    "direct_tool_invocation_context",
]
