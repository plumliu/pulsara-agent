"""Pure derivation of same-run Skill bodies proven FULL in the old epoch."""

from __future__ import annotations

from dataclasses import dataclass, field
import json

from pulsara_agent.capability.local_skills import (
    parse_skill_document,
    validate_skill_candidate_placement,
)
from pulsara_agent.capability.resolver import EffectiveSkillCatalogInspection
from pulsara_agent.capability.contracts import LocalSkillRootKind
from pulsara_agent.conversation_kernel.compaction.contracts import (
    FrozenCompactionCanonicalRead,
)
from pulsara_agent.llm.input import (
    LLMMessage,
    llm_content_identity_value,
    text_part_values,
)
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
    ProviderRuntimeObservation,
    SourceObservationLifecycle,
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
    evidence_source_entry_fingerprint: str | None = field(
        default=None, repr=False
    )

    def __post_init__(self) -> None:
        if (
            not self.name
            or not self.catalog_location
            or self.delivery_sequence < 0
            or (
                self.evidence_source_entry_fingerprint is not None
                and not self.evidence_source_entry_fingerprint.startswith("sha256:")
            )
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
                "items": tuple(
                    (
                        item.name,
                        item.catalog_location,
                        item.body,
                        item.delivery_sequence,
                        item.evidence_source_entry_fingerprint,
                    )
                    for item in self.ordered_items
                ),
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
    inspection: EffectiveSkillCatalogInspection,
    estimator: ModelInputTokenEstimator,
) -> FrozenRetainedSkillContextSelection:
    """Select exact FULL ordinary reads and the immediate installed successor."""

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
    calls: dict[str, tuple[FrozenProviderInputItem, str, object]] = {}
    duplicate_call_ids: set[str] = set()
    canonical = canonical_read.dispatch_read.compile_snapshot.canonical_input
    for item in canonical.items:
        if item.item_kind is FrozenProviderInputItemKind.ASSISTANT_TOOL_REQUEST:
            for call in item.tool_calls:
                if call.tool_call_id in calls:
                    duplicate_call_ids.add(call.tool_call_id)
                calls[call.tool_call_id] = (
                    item,
                    call.tool_name,
                    thaw_json(call.arguments),
                )

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
        call_id = item.tool_call_id or ""
        if call_id in duplicate_call_ids:
            continue
        call = calls.get(call_id)
        if call is None or call[1] != "read_file" or not isinstance(call[2], dict):
            continue
        if item.tool_request_entry_id != call[0].source_entry_id:
            continue
        path = call[2].get("path")
        offset = call[2].get("offset", 1)
        if not isinstance(path, str) or offset != 1:
            continue
        request_ordinal = _assistant_request_message_ordinal(
            call[0],
            tool_call_id=item.tool_call_id or "",
            predecessor=predecessor_epoch,
        )
        if request_ordinal is None:
            continue
        catalog_row = _historical_catalog_row(
            predecessor_epoch,
            before_message_ordinal=request_ordinal,
            location=path,
        )
        if catalog_row is None:
            continue
        delivered = _exact_historical_read(
            item,
            catalog_row=catalog_row,
            root_policy=inspection.root_policy,
        )
        if delivered is None:
            continue
        retained = FrozenRetainedSkillContextItem(
            name=catalog_row["name"],
            catalog_location=catalog_row["location"],
            body=delivered,
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
        for item in _installed_retained_items(
            predecessor_epoch,
            target_turn_id=canonical_read.scope.turn_id,
        )
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
        actual_payload = dict(
            decode_tool_result_observation(text_part_values(actual.content)[0])
        )
        expected_payload = dict(
            decode_tool_result_observation(text_part_values(full.content)[0])
        )
    except ValueError:
        return False
    return actual_payload == expected_payload


def _assistant_request_message_ordinal(
    request_item: FrozenProviderInputItem,
    *,
    tool_call_id: str,
    predecessor: FrozenProviderInputEpochView,
) -> int | None:
    key = (
        request_item.source_entry_id,
        provider_input_item_fingerprint(request_item),
    )
    matches = []
    for message, placement in zip(
        predecessor.messages, predecessor.message_placements, strict=True
    ):
        if (
            (placement.origin_entry_id, placement.origin_item_fingerprint) == key
            and any(call.id == tool_call_id for call in message.tool_calls)
        ):
            matches.append(placement.message_ordinal)
    return matches[0] if len(matches) == 1 else None


def _historical_catalog_row(
    predecessor: FrozenProviderInputEpochView,
    *,
    before_message_ordinal: int,
    location: str,
) -> dict[str, str] | None:
    observations: list[tuple[int, ProviderRuntimeObservation]] = []
    for message, placement in zip(
        predecessor.messages, predecessor.message_placements, strict=True
    ):
        if placement.message_ordinal >= before_message_ordinal:
            continue
        try:
            observation = decode_runtime_observation(message)
        except ValueError:
            continue
        if observation.source_kind is not ContextSourceKind.SKILL_CATALOG:
            continue
        observations.append((placement.message_ordinal, observation))
    if not observations:
        return None
    _ordinal, latest = max(observations, key=lambda item: item[0])
    if (
        latest.lifecycle is not SourceObservationLifecycle.SNAPSHOT
        or latest.presence is not SourceObservationPresence.VALUE
    ):
        return None
    rows = _decode_catalog_rows(latest.body)
    if rows is None:
        return None
    matches = tuple(item for item in rows if item["location"] == location)
    return matches[0] if len(matches) == 1 else None


def _decode_catalog_rows(body: str) -> tuple[dict[str, str], ...] | None:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or set(payload) != {"skills"}:
        return None
    raw_rows = payload["skills"]
    if not isinstance(raw_rows, list):
        return None
    rows: list[dict[str, str]] = []
    for row in raw_rows:
        if not isinstance(row, dict) or set(row) != {
            "name",
            "description",
            "location",
        }:
            return None
        if not all(isinstance(row[key], str) and row[key] for key in row):
            return None
        rows.append(row)
    keys = tuple((item["name"], item["location"]) for item in rows)
    if len(keys) != len(set(keys)) or len({item[0] for item in keys}) != len(keys):
        return None
    return tuple(rows)


def _exact_historical_read(
    item: FrozenProviderInputItem,
    *,
    catalog_row: dict[str, str],
    root_policy,
) -> str | None:
    if item.tool_result_body_text is None:
        return None
    try:
        payload = json.loads(item.tool_result_body_text)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
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
        return None
    content = payload.get("content")
    total_lines = payload.get("total_lines")
    if not (
        payload.get("status") == "ok"
        and payload.get("path")
        == _project_historical_public_path(catalog_row["location"], root_policy)
        and payload.get("offset") == 1
        and isinstance(payload.get("limit"), int)
        and isinstance(total_lines, int)
        and isinstance(payload.get("file_size"), int)
        and payload["file_size"] >= 0
        and total_lines >= 0
        and payload["limit"] >= total_lines
        and payload.get("truncated") is False
        and isinstance(content, str)
        and isinstance(payload.get("had_utf8_bom", False), bool)
    ):
        return None
    delivered_lines: list[str] = []
    if content:
        records = content.split("\n")
        if len(records) != total_lines:
            return None
        for ordinal, record in enumerate(records, start=1):
            prefix = f"{ordinal}|"
            if not record.startswith(prefix):
                return None
            delivered_lines.append(record.removeprefix(prefix))
    elif total_lines != 0:
        return None
    delivered_document = "\n".join(delivered_lines)
    parsed = parse_skill_document(delivered_document.encode("utf-8"))
    if parsed.parsed is None:
        return None
    location_parts = catalog_row["location"].replace("\\", "/").split("/")
    if len(location_parts) < 2 or location_parts[-1] != "SKILL.md":
        return None
    placement = validate_skill_candidate_placement(parsed.parsed, location_parts[-2])
    if not placement.valid or (
        parsed.parsed.name != catalog_row["name"]
        or parsed.parsed.description != catalog_row["description"]
    ):
        return None
    return parsed.parsed.body


def _project_historical_public_path(location: str, root_policy) -> str:
    if location.startswith(".pulsara/skills/") or location.startswith(
        ".agents/skills/"
    ):
        return location
    by_kind = {item.root_kind: item for item in root_policy.roots}
    if location.startswith("${PULSARA_HOME}/skills/"):
        root = by_kind.get(LocalSkillRootKind.USER_PULSARA)
        if root is None:
            return ""
        suffix = location.removeprefix("${PULSARA_HOME}/skills/")
        return str(root.path / suffix)
    if location.startswith("~/.agents/skills/"):
        root = by_kind.get(LocalSkillRootKind.USER_AGENTS)
        if root is None:
            return ""
        suffix = location.removeprefix("~/.agents/skills/")
        return str(root.path / suffix)
    return location if location.startswith("/") else ""


def _installed_retained_items(
    predecessor: FrozenProviderInputEpochView,
    *,
    target_turn_id: str,
) -> tuple[FrozenRetainedSkillContextItem, ...]:
    head = next(
        (
            item
            for item in predecessor.source_heads
            if item.source_kind is ContextSourceKind.RETAINED_SKILL_CONTEXT
        ),
        None,
    )
    if (
        head is None
        or head.presence is not SourceObservationPresence.VALUE
        or head.last_emitted_turn_id != target_turn_id
    ):
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
            location = row["catalog_location"]
            body = row["body"]
            if not all(isinstance(value, str) and value for value in (name, location)):
                return ()
            if not isinstance(body, str):
                return ()
            result.append(
                FrozenRetainedSkillContextItem(
                    name=name,
                    catalog_location=location,
                    body=body,
                    delivery_sequence=0,
                )
            )
        keys = tuple((item.name, item.catalog_location) for item in result)
        if len(keys) != len(set(keys)) or len({item[0] for item in keys}) != len(keys):
            return ()
        return tuple(result)
    return ()


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
                "items": tuple(
                    (
                        item.name,
                        item.catalog_location,
                        item.body,
                        item.delivery_sequence,
                        item.evidence_source_entry_fingerprint,
                    )
                    for item in items
                ),
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
                "content": llm_content_identity_value(message.content),
                # Preserve the established fingerprint value after removing
                # the superseded semantic-thinking DTO slot.
                "thinking": (),
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
