"""One immutable-view dispatcher for all eleven Hook lifecycle events."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import replace
import logging
from pathlib import Path
from time import monotonic
from typing import Protocol

from pulsara_agent.hooks.contracts import (
    ContextOutcome,
    ContinuationDecision,
    ContinuationOutcome,
    EVENT_OUTCOME_FAMILY,
    FrozenHookDefinitionView,
    FrozenHookSourceSnapshot,
    GateDecision,
    GateOutcome,
    HookContextEntry,
    HookDiagnostic,
    HookDispatchCausalRef,
    HookDispatchEnvelope,
    HookDispatchOutcome,
    HookDispatchScopeRef,
    HookEventType,
    HookSourceKind,
    HookSourceSnapshotDisposition,
    HookSourceTrustAssessment,
    HookTrustDisposition,
    ObserveOutcome,
    PermissionDecision,
    PermissionOutcome,
)
from pulsara_agent.hooks.executor import HookCommandExecutor, HookExecutionRequest
from pulsara_agent.hooks.matcher import (
    FrozenHookMatcherSubject,
    definition_matches,
)
from pulsara_agent.hooks.output_parser import (
    ParsedHandlerOutput,
    ValidHandlerContribution,
    parse_handler_output,
)
from pulsara_agent.hooks.source import LocalHookSourceProvider
from pulsara_agent.hooks.trust import normalized_definition_digest
from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary


class HookDiagnosticAdapter(Protocol):
    def offer(self, diagnostics: tuple[HookDiagnostic, ...]) -> None: ...


class HookBackgroundContextPort(Protocol):
    async def accept_background(
        self,
        *,
        scope: HookDispatchScopeRef,
        causal_ref: HookDispatchCausalRef,
        entry: HookContextEntry,
    ) -> None: ...


class _NullDiagnosticAdapter:
    def offer(self, diagnostics: tuple[HookDiagnostic, ...]) -> None:
        del diagnostics


class LoggingHookDiagnosticAdapter:
    def __init__(self) -> None:
        self._logger = logging.getLogger("pulsara_agent.hooks")

    def offer(self, diagnostics: tuple[HookDiagnostic, ...]) -> None:
        from pulsara_agent.hooks.executor import HookSecretScrubSet

        scrub = HookSecretScrubSet.capture()
        for item in diagnostics:
            try:
                code = scrub.scrub_text(item.code)
                event = scrub.scrub_text(
                    "" if item.event_type is None else item.event_type.external_name
                )
                source = scrub.scrub_text(item.source_label or "")
                detail = scrub.scrub_text(item.message)
            except (UnicodeError, ValueError):
                continue
            self._logger.warning(
                "hook code=%s event=%s source=%s detail=%s",
                code,
                event or None,
                source or None,
                detail,
            )


async def _shielded_to_thread(operation, /, *args):
    task = asyncio.create_task(
        asyncio.to_thread(operation, *args), name="hook-source-filesystem"
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        with suppress(BaseException):
            await asyncio.shield(task)
        raise


def _build_local_reload_candidate(
    source_provider: LocalHookSourceProvider,
    predecessor: FrozenHookDefinitionView,
    deadline_monotonic: float | None,
) -> FrozenHookDefinitionView:
    local = source_provider.discover(deadline_monotonic=deadline_monotonic)
    plugin = tuple(
        item
        for item in predecessor.source_snapshots
        if item.provenance.identity.kind is HookSourceKind.PLUGIN
    )
    if not plugin:
        return local
    trust_store = source_provider.trust_store
    assessed: list[FrozenHookSourceSnapshot] = []
    for snapshot in plugin:
        if snapshot.disposition is not HookSourceSnapshotDisposition.COMPLETE:
            assessed.append(snapshot)
            continue
        digest = normalized_definition_digest(
            snapshot.provenance, snapshot.definitions
        )
        try:
            trust = trust_store.assess(
                snapshot.provenance.trust_subject, digest
            )
        except (OSError, ValueError):
            trust = HookSourceTrustAssessment(
                HookTrustDisposition.UNAVAILABLE,
                digest,
                None,
                True,
                None,
            )
        assessed.append(replace(snapshot, trust=trust))
    return FrozenHookDefinitionView((*local.source_snapshots, *assessed))


class KernelHookDispatcher:
    def __init__(
        self,
        *,
        initial_view: FrozenHookDefinitionView,
        workspace_root: Path,
        source_provider: LocalHookSourceProvider | None = None,
        credential_boundary: ProcessCredentialBoundary,
        executor: HookCommandExecutor | None = None,
        diagnostic_adapter: HookDiagnosticAdapter | None = None,
        background_context: HookBackgroundContextPort | None = None,
    ) -> None:
        self._current_view = initial_view
        self._workspace_root = workspace_root.expanduser().resolve()
        self._source_provider = source_provider
        self._executor = executor or HookCommandExecutor(
            credential_boundary=credential_boundary
        )
        self._diagnostics = diagnostic_adapter or _NullDiagnosticAdapter()
        self._background_context = background_context
        self._publication_lock = asyncio.Lock()
        self._lane = asyncio.Condition()
        self._ordinary_active = 0
        self._ordinary_fenced = False
        self._closed = False
        self._next_event_dispatch_ordinal = 0
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._offer_view_diagnostics(initial_view, phase="cold")

    @property
    def current_view(self) -> FrozenHookDefinitionView:
        return self._current_view

    def capture_view(self) -> FrozenHookDefinitionView:
        return self._current_view

    def begin_host_close(self, *, deadline_monotonic: float) -> None:
        """Cancel/bound physical ordinary attempts without fencing admission."""

        self._executor.begin_host_close(deadline_monotonic=deadline_monotonic)

    async def reload(
        self,
        *,
        deadline_monotonic: float | None,
        publish_scanned_view: Callable[
            [FrozenHookDefinitionView, FrozenHookDefinitionView], Awaitable[bool]
        ],
    ) -> FrozenHookDefinitionView:
        if self._source_provider is None:
            raise RuntimeError("dispatcher has no local Hook source provider")
        while True:
            predecessor = self._current_view
            replacement = await _shielded_to_thread(
                _build_local_reload_candidate,
                self._source_provider,
                predecessor,
                deadline_monotonic,
            )
            if deadline_monotonic is not None and monotonic() >= deadline_monotonic:
                raise TimeoutError("Hook reload deadline expired")
            await _acquire_lock_before_deadline(
                self._publication_lock,
                deadline_monotonic,
                "Hook reload publication deadline expired",
            )
            try:
                if self._current_view is not predecessor:
                    continue
                if not await publish_scanned_view(predecessor, replacement):
                    raise RuntimeError("Hook reload owner is no longer current")
                self._offer_view_diagnostics(replacement, phase="reload")
                return replacement
            finally:
                self._publication_lock.release()

    async def publish_plugin_slice(
        self,
        *,
        plugin_snapshots: tuple[FrozenHookSourceSnapshot, ...],
        deadline_monotonic: float,
        publish_scanned_view: Callable[
            [FrozenHookDefinitionView, FrozenHookDefinitionView], Awaitable[bool]
        ],
    ) -> FrozenHookDefinitionView:
        """Merge one already-built Plugin slice in the only publication lane."""

        if any(
            snapshot.provenance.identity.kind is not HookSourceKind.PLUGIN
            for snapshot in plugin_snapshots
        ):
            raise ValueError("Plugin Hook publication contains a local source")
        await _acquire_lock_before_deadline(
            self._publication_lock,
            deadline_monotonic,
            "Plugin Hook publication deadline expired",
        )
        try:
            predecessor = self._current_view
            local = tuple(
                snapshot
                for snapshot in predecessor.source_snapshots
                if snapshot.provenance.identity.kind is not HookSourceKind.PLUGIN
            )
            replacement = FrozenHookDefinitionView((*local, *plugin_snapshots))
            if not await publish_scanned_view(predecessor, replacement):
                raise RuntimeError("Hook Plugin publication owner is no longer current")
            self._offer_view_diagnostics(replacement, phase="plugin-reload")
            return replacement
        finally:
            self._publication_lock.release()

    def publish_scanned_view(
        self,
        predecessor: FrozenHookDefinitionView,
        replacement: FrozenHookDefinitionView,
    ) -> bool:
        """Publish while the Host owner lock is held by the caller.

        The publication mutex remains held by :meth:`reload`; this method only
        performs the exact-object and close revalidation plus atomic pointer swap.
        """

        if self._closed or self._current_view is not predecessor:
            return False
        self._current_view = replacement
        return True

    async def dispatch(
        self,
        envelope: HookDispatchEnvelope,
        *,
        matcher_subject: FrozenHookMatcherSubject,
        continuation_already_used: bool = False,
    ) -> HookDispatchOutcome:
        await self._enter_ordinary()
        try:
            return await self._dispatch(
                envelope,
                matcher_subject=matcher_subject,
                continuation_already_used=continuation_already_used,
            )
        finally:
            await self._leave_ordinary()

    async def dispatch_terminal(
        self,
        envelope: HookDispatchEnvelope,
        *,
        matcher_subject: FrozenHookMatcherSubject,
    ) -> ObserveOutcome:
        if envelope.public_input.event_type is not HookEventType.SESSION_END_EVENT:
            raise ValueError("terminal Hook lane only accepts SessionEnd")
        async with self._lane:
            if not self._ordinary_fenced or self._ordinary_active:
                raise RuntimeError("ordinary Hook lane is not quiesced")
            if self._closed:
                raise RuntimeError("Hook dispatcher is closed")
        outcome = await self._dispatch(
            envelope,
            matcher_subject=matcher_subject,
            continuation_already_used=False,
        )
        assert isinstance(outcome, ObserveOutcome)
        return outcome

    async def _dispatch(
        self,
        envelope: HookDispatchEnvelope,
        *,
        matcher_subject: FrozenHookMatcherSubject,
        continuation_already_used: bool,
    ) -> HookDispatchOutcome:
        event_type = envelope.public_input.event_type
        expected_family = EVENT_OUTCOME_FAMILY[event_type]
        event_ordinal = self._next_event_dispatch_ordinal
        self._next_event_dispatch_ordinal += 1
        selected = tuple(
            (source_ordinal, definition)
            for source_ordinal, definition in envelope.definition_view.selected_definitions(
                event_type
            )
            if definition_matches(definition, matcher_subject)
        )
        synchronous: list[asyncio.Task[ParsedHandlerOutput]] = []
        for source_ordinal, definition in selected:
            request = HookExecutionRequest(
                definition=definition,
                public_stdin=envelope.public_input.to_wire(),
                scope=envelope.scope,
                event_dispatch_ordinal=event_ordinal,
                source_ordinal=source_ordinal,
                cwd=Path(envelope.public_input.cwd),
                project_dir=self._workspace_root,
                inherited_deadline_monotonic=envelope.deadline_monotonic,
                cancellation_signal=envelope.cancellation_signal,
            )
            if definition.status_message is not None:
                self._diagnostics.offer(
                    (
                        HookDiagnostic(
                            "HOOK_STATUS",
                            definition.status_message,
                            event_type=event_type,
                            source_label=definition.provenance.display_label,
                            event_dispatch_ordinal=event_ordinal,
                            source_ordinal=source_ordinal,
                            definition_ordinal=(
                                definition.source_local_definition_ordinal
                            ),
                        ),
                    )
                )
            if definition.asynchronous:
                task = asyncio.create_task(
                    self._settle_background(
                        request,
                        scope=envelope.scope,
                        causal_ref=envelope.causal_ref,
                    ),
                    name=(
                        "kernel-hook-background:"
                        f"{event_type.external_name}:"
                        f"{definition.source_local_definition_ordinal}"
                    ),
                )
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
            else:
                synchronous.append(
                    asyncio.create_task(
                        self._execute_and_parse(request),
                        name=(
                            "kernel-hook-sync:"
                            f"{event_type.external_name}:"
                            f"{definition.source_local_definition_ordinal}"
                        ),
                    )
                )
        try:
            parsed = tuple(await asyncio.gather(*synchronous))
        except BaseException:
            for task in synchronous:
                task.cancel()
            await asyncio.gather(*synchronous, return_exceptions=True)
            raise
        parsed = tuple(
            sorted(
                parsed,
                key=lambda item: (
                    item.source_ordinal,
                    item.definition.source_local_definition_ordinal,
                ),
            )
        )
        outcome = _aggregate(
            event_type,
            parsed,
            continuation_already_used=continuation_already_used,
        )
        if not isinstance(outcome, expected_family):
            raise RuntimeError("Hook outcome family drifted")
        self._diagnostics.offer(outcome.diagnostics)
        return outcome

    async def _execute_and_parse(
        self, request: HookExecutionRequest
    ) -> ParsedHandlerOutput:
        execution = await self._executor.execute(request)
        parsed = parse_handler_output(execution)
        if isinstance(parsed, ValidHandlerContribution):
            parsed = replace(parsed, scrub_set=execution.scrub_set)
        return parsed

    async def _settle_background(
        self,
        request: HookExecutionRequest,
        *,
        scope: HookDispatchScopeRef,
        causal_ref: HookDispatchCausalRef,
    ) -> None:
        try:
            parsed = await self._execute_and_parse(request)
        except asyncio.CancelledError:
            raise
        except Exception:
            return
        diagnostics = parsed.diagnostics
        self._diagnostics.offer(diagnostics)
        if (
            isinstance(parsed, ValidHandlerContribution)
            and parsed.context_text
            and self._background_context is not None
        ):
            if parsed.scrub_set is None:
                raise RuntimeError("valid Hook context lost its secret scrub set")
            await self._background_context.accept_background(
                scope=scope,
                causal_ref=causal_ref,
                entry=HookContextEntry(
                    parsed.definition,
                    parsed.source_ordinal,
                    parsed.event_dispatch_ordinal,
                    parsed.context_text,
                    parsed.scrub_set,
                ),
            )

    def _offer_view_diagnostics(
        self, view: FrozenHookDefinitionView, *, phase: str
    ) -> None:
        diagnostics: list[HookDiagnostic] = []
        for source_ordinal, snapshot in enumerate(view.source_snapshots):
            diagnostics.extend(snapshot.diagnostics)
            diagnostics.append(
                HookDiagnostic(
                    "HOOK_SOURCE_STATE",
                    (
                        f"{phase}: scan={snapshot.disposition.value} "
                        f"trust={snapshot.trust.disposition.value} "
                        f"enabled={snapshot.trust.enabled}"
                    ),
                    source_label=snapshot.provenance.display_label,
                    source_ordinal=source_ordinal,
                )
            )
        self._diagnostics.offer(tuple(diagnostics))

    async def fence_ordinary(self) -> FrozenHookDefinitionView:
        async with self._lane:
            self._ordinary_fenced = True
            while self._ordinary_active:
                await self._lane.wait()
            return self._current_view

    async def aclose(self, *, deadline_monotonic: float | None = None) -> None:
        async with self._lane:
            self._ordinary_fenced = True
            self._closed = True
            while self._ordinary_active:
                await self._lane.wait()
        tasks = tuple(self._background_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self._executor.aclose(deadline_monotonic=deadline_monotonic)
        # Drop the current definition graph only after ordinary/background
        # attempts have joined.  Plugin package anchors are ordinary RAII
        # leaves referenced by definitions and any retained Hook contexts;
        # their last consumer, rather than the publication pointer, closes
        # the shared package lock.
        self._current_view = FrozenHookDefinitionView(())

    async def _enter_ordinary(self) -> None:
        async with self._lane:
            if self._closed or self._ordinary_fenced:
                raise RuntimeError("ordinary Hook admission is fenced")
            self._ordinary_active += 1

    async def _leave_ordinary(self) -> None:
        async with self._lane:
            self._ordinary_active -= 1
            if not self._ordinary_active:
                self._lane.notify_all()


def _aggregate(
    event_type: HookEventType,
    parsed: tuple[ParsedHandlerOutput, ...],
    *,
    continuation_already_used: bool,
) -> HookDispatchOutcome:
    valid = tuple(item for item in parsed if isinstance(item, ValidHandlerContribution))
    if any(item.context_text and item.scrub_set is None for item in valid):
        raise RuntimeError("valid Hook context lost its secret scrub set")
    diagnostics = tuple(
        diagnostic
        for item in parsed
        for diagnostic in item.diagnostics
    )
    contexts = tuple(
        HookContextEntry(
            item.definition,
            item.source_ordinal,
            item.event_dispatch_ordinal,
            item.context_text,
            item.scrub_set,
        )
        for item in valid
        if item.context_text
    )
    if event_type is HookEventType.SESSION_END_EVENT:
        return ObserveOutcome(diagnostics)
    if event_type in {
        HookEventType.POST_TOOL_USE_EVENT,
        HookEventType.SUBAGENT_START_EVENT,
    }:
        return ContextOutcome(contexts, diagnostics)
    if event_type is HookEventType.PERMISSION_REQUEST_EVENT:
        for rank in (PermissionDecision.DENY, PermissionDecision.ALLOW):
            winners = tuple(
                item for item in valid if item.permission_decision is rank
            )
            if winners:
                return PermissionOutcome(
                    rank,
                    next((item.reason for item in winners if item.reason), None),
                    diagnostics,
                )
        return PermissionOutcome(diagnostics=diagnostics)
    if event_type in {
        HookEventType.SUBAGENT_STOP_EVENT,
        HookEventType.STOP_EVENT,
    }:
        vetoes = tuple(item for item in valid if item.explicit_terminalize)
        requests = tuple(item for item in valid if item.continuation_request)
        if vetoes:
            return ContinuationOutcome(
                decision=ContinuationDecision.TERMINALIZE,
                reason=next((item.reason for item in vetoes if item.reason), None),
                diagnostics=diagnostics,
            )
        reason = next((item.reason for item in requests if item.reason), None)
        if requests and not continuation_already_used:
            winner = (
                next(item for item in requests if item.reason)
                if reason is not None
                else requests[0]
            )
            if winner.scrub_set is None:
                raise RuntimeError("Hook continuation lost its secret scrub set")
            return ContinuationOutcome(
                decision=ContinuationDecision.CONTINUE_ONCE,
                reason=reason,
                diagnostics=diagnostics,
                continuation_source=HookContextEntry(
                    winner.definition,
                    winner.source_ordinal,
                    winner.event_dispatch_ordinal,
                    reason or "",
                    winner.scrub_set,
                ),
            )
        if requests and continuation_already_used:
            diagnostics = (
                *diagnostics,
                HookDiagnostic(
                    "HOOK_CONTINUATION_ALREADY_USED",
                    "Hook requested continuation after the once guard was used",
                    event_type=event_type,
                ),
            )
        return ContinuationOutcome(
            decision=ContinuationDecision.TERMINALIZE,
            reason=reason,
            diagnostics=diagnostics,
        )
    blockers = tuple(item for item in valid if item.gate_block)
    if blockers:
        return GateOutcome(
            GateDecision.BLOCK,
            next((item.reason for item in blockers if item.reason), None),
            contexts,
            diagnostics,
        )
    return GateOutcome(context_entries=contexts, diagnostics=diagnostics)


async def _acquire_lock_before_deadline(
    lock: asyncio.Lock,
    deadline_monotonic: float | None,
    message: str,
) -> None:
    if deadline_monotonic is None:
        await lock.acquire()
        return
    remaining = deadline_monotonic - monotonic()
    if remaining <= 0:
        raise TimeoutError(message)
    try:
        await asyncio.wait_for(lock.acquire(), timeout=remaining)
    except TimeoutError:
        raise TimeoutError(message) from None


__all__ = [
    "HookBackgroundContextPort",
    "HookDiagnosticAdapter",
    "KernelHookDispatcher",
    "LoggingHookDiagnosticAdapter",
]
