"""Pure derivation of same-run Skill bodies proven FULL in the old epoch."""

from __future__ import annotations

from dataclasses import dataclass, field
import json

from pulsara_agent.capability.local_skills import LocalSkillDiscovery
from pulsara_agent.capability.types import LocalSkillManifest
from pulsara_agent.conversation_kernel.compaction.contracts import (
    FrozenCompactionCanonicalRead,
)
from pulsara_agent.llm.input import LLMMessage
from pulsara_agent.model_input.contracts import (
    ContextSourceKind,
    FrozenProviderInputItem,
    FrozenProviderInputItemKind,
    ModelInputTokenEstimator,
    STRUCTURED_MODEL_INPUT_LIMITS,
    ToolResultProviderRenderMode,
    compiled_tool_result_source_fingerprint,
    provider_input_item_fingerprint,
)
from pulsara_agent.model_input.lowering import (
    decode_tool_result_observation,
    lower_canonical_item,
)
from pulsara_agent.ports.artifact import ToolResultDisplayKind
from pulsara_agent.model_input.continuity import (
    FrozenProviderInputEpochView,
    SourceObservationPresence,
    decode_runtime_observation,
)
from pulsara_agent.primitives.context import (
    canonical_json_bytes,
    context_fingerprint,
    thaw_json,
)


MAX_RETAINED_SKILL_CONTEXT_ITEMS = 8
MAX_RETAINED_SKILL_CONTEXT_TOKENS = 40_000


@dataclass(frozen=True, slots=True)
class FrozenRetainedSkillContextItem:
    name: str
    catalog_location: str
    body: str = field(repr=False)
    delivery_sequence: int
    item_fingerprint: str
    evidence_source_entry_fingerprint: str | None = field(
        default=None, repr=False
    )

    def __post_init__(self) -> None:
        expected = context_fingerprint(
            "pulsara.retained-skill-context-item.v1",
            {
                "name": self.name,
                "catalog_location": self.catalog_location,
                "body": self.body,
                "delivery_sequence": self.delivery_sequence,
                "evidence_source_entry_fingerprint": (
                    self.evidence_source_entry_fingerprint
                ),
            },
        )
        if (
            not self.name
            or not self.catalog_location
            or self.delivery_sequence < 0
            or (
                self.evidence_source_entry_fingerprint is not None
                and not self.evidence_source_entry_fingerprint.startswith("sha256:")
            )
            or self.item_fingerprint != expected
        ):
            raise ValueError("retained Skill context item is invalid")


@dataclass(frozen=True, slots=True)
class FrozenRetainedSkillContextSelection:
    ordered_items: tuple[FrozenRetainedSkillContextItem, ...] = field(repr=False)
    rendered_body: str = field(repr=False)
    estimated_tokens: int
    selection_fingerprint: str

    def __post_init__(self) -> None:
        if (
            len(self.ordered_items) > MAX_RETAINED_SKILL_CONTEXT_ITEMS
            or not 0 <= self.estimated_tokens <= MAX_RETAINED_SKILL_CONTEXT_TOKENS
        ):
            raise ValueError("retained Skill selection is out of bounds")
        expected_body = _render(self.ordered_items)
        if self.rendered_body != expected_body:
            raise ValueError("retained Skill body differs from its items")
        expected = context_fingerprint(
            "pulsara.retained-skill-context-selection.v1",
            {
                "items": tuple(item.item_fingerprint for item in self.ordered_items),
                "body": self.rendered_body,
                "tokens": self.estimated_tokens,
            },
        )
        if self.selection_fingerprint != expected:
            raise ValueError("retained Skill selection fingerprint mismatch")


def freeze_retained_skill_context(
    *,
    canonical_read: FrozenCompactionCanonicalRead,
    predecessor_epoch: FrozenProviderInputEpochView | None,
    discovery: LocalSkillDiscovery,
    estimator: ModelInputTokenEstimator,
) -> FrozenRetainedSkillContextSelection:
    """Select exact FULL ordinary reads and the immediate installed successor."""

    manifests = {item.name: item for item in discovery.skills}
    if predecessor_epoch is None:
        return _selection((), estimator)
    identity = canonical_read.dispatch_read.compile_snapshot.canonical_input.identity
    if predecessor_epoch.scope.session_id != identity.session_id or (
        predecessor_epoch.scope.scope_kind is not identity.conversation_scope_kind
        or predecessor_epoch.scope.scope_subagent_task_id
        != identity.scope_subagent_task_id
    ):
        raise ValueError("retained Skill predecessor belongs to another scope")

    decisions = {
        item.source_entry_fingerprint: item
        for item in predecessor_epoch.tool_result_decisions
    }
    if len(decisions) != len(predecessor_epoch.tool_result_decisions):
        raise ValueError("installed ToolResult decisions are duplicated")
    installed_messages: dict[tuple[str | None, str], tuple[LLMMessage, ...]] = {}
    for message, placement in zip(
        predecessor_epoch.messages,
        predecessor_epoch.message_placements,
        strict=True,
    ):
        key = (placement.origin_entry_id, placement.origin_item_fingerprint)
        installed_messages[key] = (*installed_messages.get(key, ()), message)
    calls: dict[str, tuple[str, object]] = {}
    canonical = canonical_read.dispatch_read.compile_snapshot.canonical_input
    for item in canonical.items:
        if item.item_kind is FrozenProviderInputItemKind.ASSISTANT_TOOL_REQUEST:
            for call in item.tool_calls:
                calls[call.tool_call_id] = (call.tool_name, thaw_json(call.arguments))

    newest_by_name: dict[str, FrozenRetainedSkillContextItem] = {}
    floor = canonical_read.lineage_base.effective_materialization_lineage_floor
    for item in canonical.items:
        if (
            item.item_kind is not FrozenProviderInputItemKind.TOOL_RESULT
            or item.source_entry_sequence is None
            or item.source_entry_sequence <= floor
            or item.source_turn_id != canonical_read.scope.turn_id
            or item.tool_result_context is None
            or item.tool_result_context.result_state != "SUCCESS"
            or item.tool_result_context.display_kind
            is not ToolResultDisplayKind.COMPLETE
        ):
            continue
        if not _was_installed_full(
            item,
            decisions=decisions,
            installed_messages=installed_messages,
        ):
            continue
        call = calls.get(item.tool_call_id or "")
        if call is None or call[0] != "read_file" or not isinstance(call[1], dict):
            continue
        path = call[1].get("path")
        offset = call[1].get("offset", 1)
        if not isinstance(path, str) or offset != 1:
            continue
        manifest = next(
            (candidate for candidate in discovery.skills if candidate.location == path),
            None,
        )
        if manifest is None or not _exact_read_matches(item, manifest):
            continue
        retained = _item(
            manifest,
            delivery_sequence=item.source_entry_sequence,
            evidence_source_entry_fingerprint=(
                compiled_tool_result_source_fingerprint(item)
            ),
        )
        previous = newest_by_name.get(retained.name)
        if previous is None or retained.delivery_sequence > previous.delivery_sequence:
            newest_by_name[retained.name] = retained

    new_recent = tuple(
        sorted(
            newest_by_name.values(),
            key=lambda item: (-item.delivery_sequence, item.name),
        )
    )
    inherited = tuple(
        item
        for item in _installed_retained_items(predecessor_epoch, manifests)
        if item.name not in newest_by_name
    )
    candidates = new_recent + inherited
    selected: list[FrozenRetainedSkillContextItem] = []
    for candidate in candidates:
        if len(selected) >= MAX_RETAINED_SKILL_CONTEXT_ITEMS:
            break
        trial = tuple(selected) + (candidate,)
        if estimator.estimate_text(_render(_render_order(trial))) > (
            MAX_RETAINED_SKILL_CONTEXT_TOKENS
        ):
            break
        selected.append(candidate)
    return _selection(_render_order(tuple(selected)), estimator)


def _was_installed_full(
    item: FrozenProviderInputItem,
    *,
    decisions: dict[str, object],
    installed_messages: dict[tuple[str | None, str], tuple[LLMMessage, ...]],
) -> bool:
    """Prove FULL from the latest decision or the exact installed placement.

    Compatible appends intentionally retain old messages/placements but only
    expose decisions for the newly compiled suffix.  Matching the canonical
    item fingerprint and exact FULL lowering against the installed message is
    therefore the bounded, ledger-free proof for an older ordinary read.
    """

    decision = decisions.get(compiled_tool_result_source_fingerprint(item))
    if (
        decision is not None
        and getattr(decision, "selected_mode", None)
        is ToolResultProviderRenderMode.FULL
    ):
        return True
    key = (item.source_entry_id, provider_input_item_fingerprint(item))
    matches = installed_messages.get(key, ())
    if len(matches) != 1:
        return False
    try:
        lowered = lower_canonical_item(
            item,
            artifact_read_available=True,
            limits=STRUCTURED_MODEL_INPUT_LIMITS,
        )
    except ValueError:
        return False
    full = next(
        (
            variant.message
            for variant in lowered.tool_result_variants
            if variant.mode is ToolResultProviderRenderMode.FULL
        ),
        None,
    )
    if full is None:
        return False
    actual = matches[0]
    if actual == full:
        return True
    if (
        actual.role is not full.role
        or actual.tool_call_id != full.tool_call_id
        or len(actual.content) != 1
        or len(full.content) != 1
    ):
        return False
    try:
        actual_payload = dict(decode_tool_result_observation(actual.content[0]))
        expected_payload = dict(decode_tool_result_observation(full.content[0]))
    except ValueError:
        return False
    # Citation handles are sealed process-local references.  They can differ
    # between the historical call that installed the message and this pure
    # proof reconstruction without changing the delivered result body or any
    # canonical provenance.  Every other typed envelope field remains exact.
    actual_payload.pop("citation_handle", None)
    expected_payload.pop("citation_handle", None)
    return actual_payload == expected_payload


def _exact_read_matches(
    item: FrozenProviderInputItem, manifest: LocalSkillManifest
) -> bool:
    if not manifest.raw_document or item.tool_result_body_text is None:
        return False
    try:
        payload = json.loads(item.tool_result_body_text)
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    required = {
        "status",
        "path",
        "access_scope",
        "workspace_relative",
        "offset",
        "limit",
        "total_lines",
        "file_size",
        "truncated",
        "content",
    }
    if not required.issubset(payload) or not (
        set(payload) - required
    ).issubset({"had_utf8_bom", "_hint", "_warning"}):
        return False
    text = manifest.raw_document
    had_bom = text.startswith("\ufeff")
    if had_bom:
        text = text[1:]
    lines = text.splitlines()
    content = "\n".join(
        f"{ordinal}|{line}" for ordinal, line in enumerate(lines, start=1)
    )
    expected_paths = {manifest.location, str(manifest.path.resolve())}
    return (
        payload.get("status") == "ok"
        and payload.get("path") in expected_paths
        and payload.get("offset") == 1
        and isinstance(payload.get("limit"), int)
        and payload["limit"] >= len(lines)
        and payload.get("total_lines") == len(lines)
        and payload.get("file_size") == len(manifest.raw_document.encode("utf-8"))
        and payload.get("truncated") is False
        and payload.get("content") == content
        and bool(payload.get("had_utf8_bom", False)) is had_bom
    )


def _installed_retained_items(
    predecessor: FrozenProviderInputEpochView,
    manifests: dict[str, LocalSkillManifest],
) -> tuple[FrozenRetainedSkillContextItem, ...]:
    head = next(
        (
            item
            for item in predecessor.source_heads
            if item.source_kind is ContextSourceKind.RETAINED_SKILL_CONTEXT
        ),
        None,
    )
    if head is None or head.presence is not SourceObservationPresence.VALUE:
        return ()
    for message in reversed(predecessor.messages):
        if _installed_observation_fingerprint(message) != (
            head.installed_observation_fingerprint
        ):
            continue
        observation = decode_runtime_observation(message)
        if (
            observation.source_kind is not ContextSourceKind.RETAINED_SKILL_CONTEXT
            or observation.presence is not SourceObservationPresence.VALUE
        ):
            return ()
        try:
            payload = json.loads(observation.body)
        except json.JSONDecodeError:
            return ()
        if not isinstance(payload, dict) or set(payload) != {"skills"}:
            return ()
        rows = payload["skills"]
        if not isinstance(rows, list) or len(rows) > MAX_RETAINED_SKILL_CONTEXT_ITEMS:
            return ()
        result: list[FrozenRetainedSkillContextItem] = []
        for row in rows:
            if not isinstance(row, dict) or set(row) != {
                "name",
                "catalog_location",
                "body",
            }:
                return ()
            name = row["name"]
            manifest = manifests.get(name) if isinstance(name, str) else None
            if (
                manifest is None
                or row["catalog_location"] != manifest.location
                or row["body"] != manifest.body
            ):
                continue
            result.append(_item(manifest, delivery_sequence=0))
        return tuple(result)
    return ()


def _item(
    manifest: LocalSkillManifest,
    *,
    delivery_sequence: int,
    evidence_source_entry_fingerprint: str | None = None,
) -> FrozenRetainedSkillContextItem:
    values = {
        "name": manifest.name,
        "catalog_location": manifest.location,
        "body": manifest.body,
        "delivery_sequence": delivery_sequence,
        "evidence_source_entry_fingerprint": evidence_source_entry_fingerprint,
    }
    return FrozenRetainedSkillContextItem(
        **values,
        item_fingerprint=context_fingerprint(
            "pulsara.retained-skill-context-item.v1", values
        ),
    )


def remove_full_tail_duplicates(
    selection: FrozenRetainedSkillContextSelection,
    *,
    full_source_entry_fingerprints: frozenset[str],
    estimator: ModelInputTokenEstimator,
) -> FrozenRetainedSkillContextSelection:
    """Drop only bodies proven FULL in the successor protected tail."""

    retained = tuple(
        item
        for item in selection.ordered_items
        if item.evidence_source_entry_fingerprint
        not in full_source_entry_fingerprints
    )
    if retained == selection.ordered_items:
        return selection
    return _selection(retained, estimator)


def _render_order(
    items: tuple[FrozenRetainedSkillContextItem, ...],
) -> tuple[FrozenRetainedSkillContextItem, ...]:
    # New reads carry real canonical sequences; inherited items use small
    # ordinals and remain after the new-read cohort in their existing order.
    new = tuple(item for item in items if item.delivery_sequence >= 1)
    inherited = tuple(item for item in items if item.delivery_sequence < 1)
    return tuple(sorted(new, key=lambda item: item.delivery_sequence)) + inherited


def _render(items: tuple[FrozenRetainedSkillContextItem, ...]) -> str:
    return canonical_json_bytes(
        {
            "skills": tuple(
                {
                    "name": item.name,
                    "catalog_location": item.catalog_location,
                    "body": item.body,
                }
                for item in items
            )
        }
    ).decode("utf-8")


def _selection(
    items: tuple[FrozenRetainedSkillContextItem, ...],
    estimator: ModelInputTokenEstimator,
) -> FrozenRetainedSkillContextSelection:
    body = _render(items)
    tokens = estimator.estimate_text(body) if items else 0
    values = {
        "ordered_items": items,
        "rendered_body": body,
        "estimated_tokens": tokens,
    }
    return FrozenRetainedSkillContextSelection(
        **values,
        selection_fingerprint=context_fingerprint(
            "pulsara.retained-skill-context-selection.v1",
            {
                "items": tuple(item.item_fingerprint for item in items),
                "body": body,
                "tokens": tokens,
            },
        ),
    )


def _installed_observation_fingerprint(message: LLMMessage) -> str:
    return context_fingerprint(
        "pulsara:installed-runtime-observation:v1",
        {
            "message": {
                "role": message.role.value,
                "content": message.content,
                "thinking": message.thinking,
                "tool_calls": tuple(
                    (call.id, call.name, call.arguments)
                    for call in message.tool_calls
                ),
                "tool_call_id": message.tool_call_id,
                "name": message.name,
                "arguments": message.arguments,
            }
        },
    )


__all__ = [
    "FrozenRetainedSkillContextItem",
    "FrozenRetainedSkillContextSelection",
    "MAX_RETAINED_SKILL_CONTEXT_ITEMS",
    "MAX_RETAINED_SKILL_CONTEXT_TOKENS",
    "freeze_retained_skill_context",
    "remove_full_tail_duplicates",
]
