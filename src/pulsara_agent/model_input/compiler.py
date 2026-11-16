"""Single-value context compilation with immutable installed provider prefixes."""

from __future__ import annotations

from dataclasses import dataclass, replace
from time import monotonic

from pulsara_agent.llm.input import (
    LLMImagePart,
    LLMMessage,
    LLMTextPart,
    MessageRole,
    content_has_image,
)
from pulsara_agent.model_input._stable_input_identity import compiled_input_identity
from pulsara_agent.model_input.contracts import (
    CompiledSourceDecision,
    CompiledToolResultDecision,
    ContextBudgetClass,
    ContextChannel,
    ContextCompileReport,
    ContextPublicDiagnosticCode,
    ContextSourceKind,
    FrozenCompiledMessagePlacement,
    FrozenCompiledModelInput,
    FrozenModelInputSemanticProjection,
    FrozenProviderInputItem,
    FrozenProviderInputItemKind,
    ModelInputCompileFailureKind,
    StructuredModelInputCompileError,
    StructuredModelInputLimits,
    STRUCTURED_MODEL_INPUT_LIMITS,
    ToolAttachmentSource,
    ToolAttachmentSourceMember,
    compiled_tool_result_source_fingerprint,
    provider_input_item_fingerprint,
)
from pulsara_agent.model_input.continuity import (
    FrozenProviderInputAppendCompileResult,
    FrozenProviderInputAppendSemanticProjection,
    NewTriggerAnchor,
    ProcessLocalCanonicalFrontier,
    InstalledObservationState,
)
from pulsara_agent.model_input.lowering import (
    LoweredCanonicalItem,
    lower_canonical_item,
)
from pulsara_agent.primitives.context import (
    canonical_json_bytes,
    context_fingerprint,
    freeze_json,
)


def estimate_runtime_handoff_pca_tokens(body: str) -> int:
    """Quote the real handoff message through both supported wire serializers."""
    from pulsara_agent.llm.adapters.openai.chat_completions import (
        chat_semantic_wire_group,
    )
    from pulsara_agent.llm.adapters.openai.responses import (
        responses_semantic_wire_group,
    )
    from pulsara_agent.llm.estimator import PulsaraHeuristicTokenEstimatorV3

    estimator = PulsaraHeuristicTokenEstimatorV3()
    message = LLMMessage.user(body)
    return max(
        sum(
            estimator.estimate_wire_json_component(item) for item in serializer(message)
        )
        for serializer in (chat_semantic_wire_group, responses_semantic_wire_group)
    )


@dataclass(frozen=True, slots=True)
class _ExpandedTranscriptMessage:
    message: LLMMessage
    origin_entry_id: str | None
    origin_item_fingerprint: str | None
    tool_attachment_source: ToolAttachmentSource | None = None


def _tool_attachment_message(source: ToolAttachmentSource) -> LLMMessage:
    images = []
    for member in source.members:
        assert isinstance(member.source.content, tuple)
        if member.source.tool_call_id is None:
            raise ValueError("tool attachment lacks its actual call identity")
        images.extend(
            (member.source.tool_call_id, image)
            for image in member.source.content
            if isinstance(image, LLMImagePart)
        )
    return tool_image_attachment_message(tuple(images))


def tool_image_attachment_message(
    members: tuple[tuple[str, LLMImagePart], ...],
) -> LLMMessage:
    """One associated user carrier for the selected logical tool-result page."""
    if not members:
        raise ValueError("tool image attachment requires at least one image")
    parts = []
    for tool_call_id, image in members:
        if (
            not tool_call_id
            or not isinstance(image, LLMImagePart)
            or image.image_ref is None
        ):
            raise ValueError("tool image attachment member lacks its saved identity")
        parts.append(
            LLMTextPart(
                canonical_json_bytes(
                    {
                        "tool_call_id": tool_call_id,
                        "image": {"kind": "IMAGE_REF", "image_ref": image.image_ref},
                    }
                ).decode("utf-8")
            )
        )
        parts.append(image)
    return LLMMessage(role=MessageRole.USER, content=tuple(parts))


def _expand_lowered_transcript(
    lowered: tuple[LoweredCanonicalItem, ...],
) -> tuple[_ExpandedTranscriptMessage, ...]:
    """Expand selected canonical items, merging image results by assistant batch."""

    members_by_group: dict[tuple[str, bool], list[ToolAttachmentSourceMember]] = {}
    last_index_by_group: dict[tuple[str, bool], int] = {}
    for index, item in enumerate(lowered):
        source = item.source
        if source.tool_request_entry_id is not None:
            key = (
                source.tool_request_entry_id,
                source.item_kind is FrozenProviderInputItemKind.LATE_TOOL_OUTCOME,
            )
            last_index_by_group[key] = index
        if (
            source.item_kind
            in {
                FrozenProviderInputItemKind.TOOL_RESULT,
                FrozenProviderInputItemKind.LATE_TOOL_OUTCOME,
            }
            and isinstance(source.content, tuple)
            and content_has_image(source.content)
        ):
            if source.tool_request_entry_id is None or source.tool_call_ordinal is None:
                raise ValueError("image ToolResult source is incomplete")
            key = (
                source.tool_request_entry_id,
                source.item_kind is FrozenProviderInputItemKind.LATE_TOOL_OUTCOME,
            )
            members_by_group.setdefault(key, []).append(
                ToolAttachmentSourceMember(source.tool_call_ordinal, source)
            )

    expanded: list[_ExpandedTranscriptMessage] = []
    for index, item in enumerate(lowered):
        message = item.message
        expanded.append(
            _ExpandedTranscriptMessage(
                message=message,
                origin_entry_id=item.source.source_entry_id,
                origin_item_fingerprint=provider_input_item_fingerprint(item.source),
            )
        )
        groups = tuple(
            key
            for key, last_index in last_index_by_group.items()
            if last_index == index and key in members_by_group
        )
        for key in sorted(groups):
            members = tuple(
                sorted(members_by_group[key], key=lambda member: member.call_ordinal)
            )
            attachment = ToolAttachmentSource(key[0], members)
            expanded.append(
                _ExpandedTranscriptMessage(
                    message=_tool_attachment_message(attachment),
                    origin_entry_id=None,
                    origin_item_fingerprint=None,
                    tool_attachment_source=attachment,
                )
            )
    return tuple(expanded)


@dataclass(frozen=True, slots=True)
class _CompileDeadline:
    value: float | None

    def check(self) -> None:
        if self.value is not None and monotonic() >= self.value:
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.DEADLINE_EXPIRED
            )


def _compiled_message_placements(
    *,
    prefix: tuple[FrozenCompiledMessagePlacement, ...],
    values: tuple[
        tuple[LLMMessage, str | None, str | None, ToolAttachmentSource | None], ...
    ],
) -> tuple[FrozenCompiledMessagePlacement, ...]:
    placements = list(prefix)
    previous_entry = prefix[-1].origin_entry_id if prefix else None
    previous_within = prefix[-1].within_origin_ordinal if prefix else -1
    if previous_within is None:
        previous_within = -1
    for message, origin_entry_id, origin_item_fingerprint, attachment in values:
        within = (
            previous_within + 1
            if attachment is None
            and origin_entry_id is not None
            and origin_entry_id == previous_entry
            else 0
        )
        message_ordinal = len(placements)
        placements.append(
            FrozenCompiledMessagePlacement(
                message_ordinal=message_ordinal,
                origin_entry_id=origin_entry_id,
                origin_item_fingerprint=origin_item_fingerprint,
                within_origin_ordinal=None if attachment is not None else within,
                role=message.role,
                tool_attachment_source=attachment,
            )
        )
        previous_entry = origin_entry_id
        previous_within = -1 if attachment is not None else within
    return tuple(placements)


class StructuredModelInputCompiler:
    def __init__(
        self, *, limits: StructuredModelInputLimits = STRUCTURED_MODEL_INPUT_LIMITS
    ) -> None:
        self._limits = limits

    def next_wire_omissions(
        self,
        *,
        request,
        compiled,
        omitted_sources=(),
        omit_hook_context=False,
        deadline_monotonic=None,
    ):
        _CompileDeadline(deadline_monotonic).check()
        for candidate in request.sources.candidates:
            if (
                candidate.source_kind not in omitted_sources
                and candidate.budget_class is not ContextBudgetClass.MUST_KEEP
            ):
                return ((*omitted_sources, candidate.source_kind), omit_hook_context)
        if request.sources.hook_context is not None and not omit_hook_context:
            return (tuple(omitted_sources), True)
        return None

    def compile(
        self,
        request,
        *,
        deadline_monotonic=None,
        omitted_sources=(),
        omit_hook_context=False,
        wire_selection=None,
        unavailable_native_pages=False,
    ):
        return self._compile(
            request,
            deadline_monotonic=deadline_monotonic,
            omitted_sources=omitted_sources,
            omit_hook_context=omit_hook_context,
            wire_selection=wire_selection,
            unavailable_native_pages=unavailable_native_pages,
            projection=False,
        )

    def project(self, request, *, deadline_monotonic=None):
        return self._compile(
            request, deadline_monotonic=deadline_monotonic, projection=True
        )

    def compile_installed_append(
        self,
        request,
        *,
        planning,
        deadline_monotonic=None,
        omitted_sources=(),
        omit_hook_context=False,
        wire_selection=None,
        unavailable_native_pages=False,
    ):
        return self._compile(
            request,
            planning=planning,
            new_epoch=False,
            deadline_monotonic=deadline_monotonic,
            omitted_sources=omitted_sources,
            omit_hook_context=omit_hook_context,
            wire_selection=wire_selection,
            unavailable_native_pages=unavailable_native_pages,
            projection=False,
        )

    def compile_new_epoch(
        self,
        request,
        *,
        planning,
        deadline_monotonic=None,
        omitted_sources=(),
        omit_hook_context=False,
        wire_selection=None,
        unavailable_native_pages=False,
    ):
        return self._compile(
            request,
            planning=planning,
            new_epoch=True,
            deadline_monotonic=deadline_monotonic,
            omitted_sources=omitted_sources,
            omit_hook_context=omit_hook_context,
            wire_selection=wire_selection,
            unavailable_native_pages=unavailable_native_pages,
            projection=False,
        )

    def project_installed_append(self, request, *, planning, deadline_monotonic=None):
        return self._compile(
            request,
            planning=planning,
            new_epoch=False,
            deadline_monotonic=deadline_monotonic,
            projection=True,
        )

    def project_new_epoch(self, request, *, planning, deadline_monotonic=None):
        return self._compile(
            request,
            planning=planning,
            new_epoch=True,
            deadline_monotonic=deadline_monotonic,
            projection=True,
        )

    def _compile(
        self,
        request,
        *,
        planning=None,
        new_epoch=True,
        deadline_monotonic=None,
        omitted_sources=(),
        omit_hook_context=False,
        wire_selection=None,
        unavailable_native_pages=False,
        projection=False,
    ):
        deadline = _CompileDeadline(deadline_monotonic)
        deadline.check()
        if wire_selection is not None and (
            wire_selection.canonical_input_identity != request.canonical_input.identity
            or wire_selection.compile_binding_fingerprint
            != request.compile_binding.binding_fingerprint
            or wire_selection.source_collection_fingerprint
            != request.sources.collection_fingerprint
        ):
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.CANONICAL_PREFIX_CONFLICT
            )
        identity = request.canonical_input.identity
        surface = request.compile_binding.tool_surface
        if surface.conversation_scope_kind is not identity.conversation_scope_kind:
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.TOOL_SURFACE_INVALID
            )
        base = tuple(
            item
            for item in request.sources.candidates
            if item.channel is ContextChannel.SYSTEM
        )
        if len(base) != 1 or base[0].source_kind is not ContextSourceKind.BASE_SYSTEM:
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.REQUIRED_SOURCE_UNAVAILABLE
            )
        if any(kind is not ContextSourceKind.USER_ACTIONS for kind in omitted_sources):
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.SOURCE_CONTRACT_INVALID
            )
        fingerprints = []
        for item in request.canonical_input.items:
            deadline.check()
            fingerprints.append(provider_input_item_fingerprint(item))
        fingerprints = tuple(fingerprints)
        frontier = ProcessLocalCanonicalFrontier(
            request.canonical_facts.context_binding_fact.binding_revision_id,
            request.canonical_facts.context_binding_fact.context_base_semantic_identity,
            identity.provider_input_through_sequence,
            fingerprints,
        )
        predecessor = None if planning is None else planning.predecessor_view
        old_count = (
            0
            if predecessor is None
            else len(predecessor.canonical_frontier.ordered_item_fingerprints)
        )
        same_base = (
            predecessor is None
            or predecessor.canonical_frontier.context_base_semantic_identity
            == frontier.context_base_semantic_identity
        )
        if (
            predecessor is not None
            and same_base
            and fingerprints[:old_count]
            != predecessor.canonical_frontier.ordered_item_fingerprints
        ):
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.CANONICAL_PREFIX_CONFLICT
            )
        if planning is not None:
            if (
                planning.scope.session_id != identity.session_id
                or planning.scope.scope_kind is not identity.conversation_scope_kind
                or planning.canonical_delta_fingerprints
                != (
                    fingerprints[old_count:]
                    if predecessor is not None and same_base
                    else fingerprints
                )
            ):
                raise StructuredModelInputCompileError(
                    ModelInputCompileFailureKind.SOURCE_CONTRACT_INVALID
                )
        if not new_epoch:
            if predecessor is None:
                raise StructuredModelInputCompileError(
                    ModelInputCompileFailureKind.CANONICAL_PREFIX_CONFLICT
                )
            try:
                predecessor.canonical_frontier.require_prefix_of(frontier)
            except ValueError as error:
                raise StructuredModelInputCompileError(
                    ModelInputCompileFailureKind.CANONICAL_PREFIX_CONFLICT
                ) from error
            if (
                predecessor.system_prompt != base[0].text
                or predecessor.tools != surface.tool_specs
            ):
                raise StructuredModelInputCompileError(
                    ModelInputCompileFailureKind.CANONICAL_PREFIX_CONFLICT
                )
        items = (
            request.canonical_input.items
            if new_epoch
            else request.canonical_input.items[old_count:]
        )
        if not new_epoch:
            self._require_provider_safe_delta(items)
        lowered = []
        for item in items:
            deadline.check()
            if item.item_kind is FrozenProviderInputItemKind.PLAN_CONTINUATION:
                continue
            modalities = request.compile_binding.target_fact.input_modalities
            if (
                item.native_page is not None
                and modalities is not None
                and "image" not in modalities
            ):
                from pulsara_agent.conversation_kernel.native_tool_delivery import (
                    prepare_unsupported_native_page,
                )

                page = prepare_unsupported_native_page(item.native_page)
                text = page.canonical_text
                item = replace(
                    item,
                    content=(LLMTextPart(text),),
                    tool_result_body_text=text,
                    native_page=page,
                )
            if unavailable_native_pages and item.native_page is not None:
                from pulsara_agent.conversation_kernel.native_tool_delivery import (
                    prepare_unavailable_native_page,
                )

                page = prepare_unavailable_native_page(
                    item.native_page,
                    session_id=identity.session_id,
                    error="实际 provider 输入在合法压缩后仍无法容纳当前页。",
                )
                text = page.canonical_text
                item = replace(
                    item,
                    content=(LLMTextPart(text),),
                    tool_result_body_text=text,
                    native_page=page,
                )
            lowered.append(lower_canonical_item(item,
                supports_images=modalities is None or "image" in modalities))
        tool_states = tuple(
            item
            for item in lowered
            if item.source.item_kind
            in {
                FrozenProviderInputItemKind.TOOL_RESULT,
                FrozenProviderInputItemKind.LATE_TOOL_OUTCOME,
            }
        )
        expanded = list(_expand_lowered_transcript(tuple(lowered)))
        transcript = [
            (
                item.message,
                item.origin_entry_id,
                item.origin_item_fingerprint,
                item.tool_attachment_source,
            )
            for item in expanded
        ]
        candidates = tuple(
            sorted(
                request.sources.candidates,
                key=lambda item: (item.placement_ordinal, item.source_kind.value),
            )
        )
        decisions = tuple(
            CompiledSourceDecision(
                item.source_kind,
                item.source_semantic_fingerprint,
                item.channel,
                item.source_kind not in omitted_sources,
            )
            for item in candidates
        )
        source_messages = []
        for item in candidates:
            if (
                item.channel is ContextChannel.SYSTEM
                or item.source_kind in omitted_sources
            ):
                continue
            message = LLMMessage.user(item.text)
            origin = item.source_semantic_fingerprint
            source_messages.append((item.source_kind, (message, None, origin, None)))
        # A cold catalogue starts the context. Active-compaction restoration is
        # adjacent to the summary and handoff; new input notices stay at its suffix.
        catalogue = [
            value
            for kind, value in source_messages
            if kind is ContextSourceKind.SKILL_CATALOG
        ]
        remaining = [
            (kind, value)
            for kind, value in source_messages
            if kind is not ContextSourceKind.SKILL_CATALOG
        ]
        if not new_epoch and catalogue:
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.CANONICAL_PREFIX_CONFLICT
            )
        restoration = []
        if new_epoch and any(
            item.source.item_kind is FrozenProviderInputItemKind.CONTEXT_SNAPSHOT
            for item in lowered
        ):
            kinds = {
                ContextSourceKind.COMPACTION_RUNTIME_HANDOFF,
                ContextSourceKind.TURN_CONTEXT,
                ContextSourceKind.PLAN_MODE,
                ContextSourceKind.APPROVED_PLAN,
                ContextSourceKind.PREVIOUS_TURN_OUTCOME,
            }
            restoration = [value for kind, value in remaining if kind in kinds]
            remaining = [
                (kind, value) for kind, value in remaining if kind not in kinds
            ]
            index = (
                next(
                    i
                    for i, item in enumerate(expanded)
                    if item.origin_entry_id
                    == next(
                        v.source.source_entry_id
                        for v in lowered
                        if v.source.item_kind
                        is FrozenProviderInputItemKind.CONTEXT_SNAPSHOT
                    )
                )
                + 1
            )
            transcript[index:index] = restoration
        anchor = None if planning is None else planning.dispatch_anchor
        anchor_entry_id = (
            anchor.source_entry_id
            if isinstance(anchor, NewTriggerAnchor)
            else request.dispatch_anchor_entry_id
        )
        insertion = len(transcript)
        if anchor_entry_id is not None:
            matching = [
                i for i, value in enumerate(transcript) if value[1] == anchor_entry_id
            ]
            if len(matching) == 1:
                insertion = matching[0]
            elif not matching and any(
                item.source_entry_id == anchor_entry_id
                and item.item_kind is FrozenProviderInputItemKind.PLAN_CONTINUATION
                for item in request.canonical_input.items
            ):
                insertion = len(transcript)
            else:
                raise StructuredModelInputCompileError(
                    ModelInputCompileFailureKind.SOURCE_CONTRACT_INVALID
                )
        transcript[insertion:insertion] = [value for _, value in remaining]
        prefix_messages = () if new_epoch else predecessor.messages
        prefix_placements = () if new_epoch else predecessor.message_placements
        ordered = [*catalogue, *transcript]
        hook = request.sources.hook_context
        if hook is not None and not omit_hook_context:
            ordered.append(
                (
                    hook,
                    None,
                    context_fingerprint(
                        "pulsara.hook-message", {"text": hook.content[0].text}
                    ),
                    None,
                )
            )
        messages = (*prefix_messages, *(value[0] for value in ordered))
        placements = _compiled_message_placements(
            prefix=prefix_placements, values=tuple(ordered)
        )
        tools = surface.tool_specs if new_epoch else predecessor.tools
        system_prompt = base[0].text if new_epoch else predecessor.system_prompt
        tool_decisions = tuple(
            CompiledToolResultDecision(
                compiled_tool_result_source_fingerprint(item.source),
                item.source.source_turn_id == identity.turn_id,
                result_entry_id=item.source.source_entry_id
                if item.source.native_page is not None
                else None,
                source_result_entry_id=item.source.native_page.source_entry_id
                if item.source.native_page is not None
                else None,
                native_projection=freeze_json(
                    item.source.native_page.projection_facts()
                )
                if item.source.native_page is not None
                else None,
            )
            for item in tool_states
        )
        diagnostics = tuple(
            dict.fromkeys(item.code for item in request.sources.diagnostics)
        )
        if omitted_sources or (hook is not None and omit_hook_context):
            diagnostics = tuple(
                dict.fromkeys(
                    (*diagnostics, ContextPublicDiagnosticCode.SOURCE_OMITTED)
                )
            )
        diagnostics = diagnostics[: self._limits.maximum_diagnostics]
        common = dict(
            canonical_input_identity=identity,
            system_prompt=system_prompt,
            messages=messages,
            message_placements=placements,
            tools=tools,
            source_decisions=decisions,
            tool_result_decisions=tool_decisions,
            diagnostic_codes=diagnostics,
            source_collection_fingerprint=request.sources.collection_fingerprint,
            compile_binding_fingerprint=request.compile_binding.binding_fingerprint,
        )
        if projection:
            value = FrozenModelInputSemanticProjection(**common)
            return (
                value
                if planning is None
                else FrozenProviderInputAppendSemanticProjection(
                    value, frontier, len(messages) - len(prefix_messages)
                )
            )
        report = ContextCompileReport(
            surface.surface_fingerprint,
            len(prefix_messages),
            0 if new_epoch else predecessor.logical_bytes,
            None if not prefix_messages else predecessor.semantic_prefix_fingerprint,
            len(omitted_sources) + int(hook is not None and omit_hook_context),
            context_fingerprint(
                "pulsara.compiled-source-selection",
                {
                    "sources": tuple(
                        (item.source_kind.value, item.included) for item in decisions
                    ),
                    "hook": hook is not None and not omit_hook_context,
                },
            ),
        )
        source_contents = tuple(
            None
            if item.source_kind in omitted_sources
            else item.text
            if item.channel is ContextChannel.SYSTEM
            else LLMMessage.user(item.text)
            for item in candidates
        )
        # Hook is a separate advisory message, not a source decision or source registry.
        compiled_fingerprint = compiled_input_identity(
            context_id=request.context_id,
            **{
                key: value
                for key, value in common.items()
                if key != "message_placements"
            },
            compile_report=report,
            estimator_fingerprint=request.compile_binding.estimator_fingerprint,
            effective_input_budget_tokens=request.compile_binding.effective_input_budget_tokens,
            source_contents=source_contents,
            tool_messages=tuple(item.message for item in tool_states),
            protected_messages=prefix_messages,
            context_messages=tuple(v[0] for _, v in remaining)
            + tuple(v[0] for v in restoration),
            checkpoint=deadline.check,
        )
        compiled = FrozenCompiledModelInput(
            context_id=request.context_id,
            **common,
            compile_report=report,
            compiled_semantic_fingerprint=compiled_fingerprint,
        )
        if planning is None:
            return compiled
        state = replace(
            request.sources.observation_state or InstalledObservationState(),
            last_turn_id=identity.turn_id,
        )
        return FrozenProviderInputAppendCompileResult(
            compiled, frontier, state, len(messages) - len(prefix_messages)
        )

    @staticmethod
    def _require_provider_safe_delta(
        delta: tuple[FrozenProviderInputItem, ...],
    ) -> None:
        for index, item in enumerate(delta):
            if item.item_kind is not FrozenProviderInputItemKind.ASSISTANT_TOOL_REQUEST:
                continue
            expected = tuple(call.tool_call_id for call in item.tool_calls)
            observed: list[str] = []
            cursor = index + 1
            while cursor < len(delta):
                successor = delta[cursor]
                if successor.item_kind not in {
                    FrozenProviderInputItemKind.TOOL_RESULT,
                    FrozenProviderInputItemKind.TOOL_RESULT_CLOSURE,
                }:
                    break
                if successor.tool_call_id is not None:
                    observed.append(successor.tool_call_id)
                cursor += 1
            if tuple(observed) != expected:
                raise StructuredModelInputCompileError(
                    ModelInputCompileFailureKind.CANONICAL_DELTA_NOT_PROVIDER_SAFE
                )


__all__ = [
    "StructuredModelInputCompiler",
    "estimate_runtime_handoff_pca_tokens",
    "tool_image_attachment_message",
]
