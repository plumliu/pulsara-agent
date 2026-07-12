"""Build event-safe capability projection facts from exact rendered fragments."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import Literal

from pulsara_agent.capability.provider import CapabilityProjectionOutput
from pulsara_agent.capability.types import RenderedCapabilityPrompt
from pulsara_agent.memory.foundation.protocols import ArtifactStore
from pulsara_agent.primitives.capability import (
    CapabilityProjectionEntryFact,
    CapabilityProjectionFact,
    CapabilityRenderedProjectionFragmentFact,
    capability_projection_entry_id,
    capability_projection_semantic_fingerprint,
)
from pulsara_agent.primitives.model_call import canonical_json_bytes, sha256_fingerprint


@dataclass(frozen=True, slots=True)
class ProviderProjectionResult:
    provider_id: str
    output: CapabilityProjectionOutput


def build_capability_projection_fact(
    *,
    projection_type: Literal["catalog", "active_skill"],
    provider_results: tuple[ProviderProjectionResult, ...],
    archive: ArtifactStore,
    runtime_session_id: str,
    owner_id: str,
    exposure_id: str,
    persist_artifacts: bool = True,
) -> CapabilityProjectionFact:
    source_entry_count = 0
    entry_by_provider_name: dict[
        tuple[str, str], CapabilityProjectionEntryFact
    ] = {}
    fragment_inputs: list[
        tuple[
            str,
            str,
            Literal["prefix", "entry", "suffix", "static"],
            Literal["container_wrapper", "projection_wrapper"] | None,
            CapabilityProjectionEntryFact | None,
            str,
        ]
    ] = []
    nonempty_provider_count = 0

    for result in provider_results:
        rendered, source_payloads = _provider_rendered_projection(
            projection_type=projection_type,
            result=result,
        )
        source_entry_count += rendered.source_entry_count
        entries: dict[str, CapabilityProjectionEntryFact] = {}
        for stable_name, projection_kind, source_kind, payload in source_payloads:
            entry_id = capability_projection_entry_id(
                projection_kind=projection_kind,
                provider_id=result.provider_id,
                source_kind=source_kind,
                stable_name=stable_name,
            )
            content = canonical_json_bytes(payload)
            content_fingerprint = _raw_sha256(content)
            artifact_id = _artifact_id(
                "capability_projection_entry",
                [exposure_id, entry_id, content_fingerprint],
            )
            if persist_artifacts:
                archive.put_text_if_absent_or_confirm_identical(
                    artifact_id,
                    content.decode("utf-8"),
                    session_id=runtime_session_id,
                    run_id=None,
                    media_type="application/vnd.pulsara.capability-projection-entry+json",
                    semantic_metadata={
                        "artifact_kind": "capability_projection_entry",
                        "exposure_id": exposure_id,
                        "owner_id": owner_id,
                        "projection_entry_id": entry_id,
                        "content_fingerprint": content_fingerprint,
                    },
                )
            entry = CapabilityProjectionEntryFact(
                projection_entry_id=entry_id,
                projection_kind=projection_kind,
                stable_name=stable_name,
                provider_id=result.provider_id,
                source_kind=source_kind,
                content_fingerprint=content_fingerprint,
                content_artifact_id=artifact_id,
            )
            entries[stable_name] = entry
            entry_by_provider_name[(result.provider_id, stable_name)] = entry

        if rendered.text:
            if nonempty_provider_count:
                fragment_inputs.append(
                    (
                        "projection-root",
                        result.provider_id,
                        "static",
                        "projection_wrapper",
                        None,
                        "\n\n",
                    )
                )
            nonempty_provider_count += 1
        for source in rendered.fragments:
            entry = (
                entries.get(source.source_stable_name)
                if source.source_stable_name is not None
                else None
            )
            if source.fragment_role == "entry" and entry is None:
                raise ValueError(
                    "rendered capability entry fragment has no source entry fact"
                )
            fragment_inputs.append(
                (
                    f"{result.provider_id}:{source.container_id}",
                    result.provider_id,
                    source.fragment_role,
                    source.static_scope,
                    entry,
                    source.text,
                )
            )

    fragment_facts: list[CapabilityRenderedProjectionFragmentFact] = []
    prompt_parts: list[str] = []
    visible_entry_ids: set[str] = set()
    for order_index, (
        container_id,
        provider_id,
        role,
        static_scope,
        entry,
        text,
    ) in enumerate(fragment_inputs):
        content_fingerprint = _raw_sha256(text.encode("utf-8"))
        source_entry_id = entry.projection_entry_id if entry is not None else None
        fragment_id = sha256_fingerprint(
            "capability-projection-fragment:v1",
            [
                projection_type,
                container_id,
                role,
                static_scope,
                source_entry_id,
                content_fingerprint,
                order_index,
            ],
        )
        artifact_id = _artifact_id(
            "capability_projection_fragment",
            [exposure_id, fragment_id, content_fingerprint],
        )
        if persist_artifacts:
            archive.put_text_if_absent_or_confirm_identical(
                artifact_id,
                text,
                session_id=runtime_session_id,
                run_id=None,
                media_type="text/plain",
                semantic_metadata={
                    "artifact_kind": "capability_projection_fragment",
                    "exposure_id": exposure_id,
                    "owner_id": owner_id,
                    "fragment_id": fragment_id,
                    "fragment_fingerprint": content_fingerprint,
                    "provider_id": provider_id,
                },
            )
        fragment_facts.append(
            CapabilityRenderedProjectionFragmentFact(
                fragment_id=fragment_id,
                container_id=container_id,
                fragment_role=role,
                static_scope=static_scope,
                source_entry_id=source_entry_id,
                source_content_fingerprint=(
                    entry.content_fingerprint if entry is not None else None
                ),
                fragment_fingerprint=content_fingerprint,
                fragment_artifact_id=artifact_id,
                order_index=order_index,
            )
        )
        if entry is not None:
            visible_entry_ids.add(entry.projection_entry_id)
        prompt_parts.append(text)

    visible_entries = tuple(
        sorted(
            (
                entry
                for entry in entry_by_provider_name.values()
                if entry.projection_entry_id in visible_entry_ids
            ),
            key=lambda entry: entry.projection_entry_id,
        )
    )
    prompt = "".join(prompt_parts)
    if prompt:
        prompt_fingerprint = _raw_sha256(prompt.encode("utf-8"))
        prompt_artifact_id = _artifact_id(
            "capability_projection_prompt",
            [exposure_id, projection_type, prompt_fingerprint],
        )
        if persist_artifacts:
            archive.put_text_if_absent_or_confirm_identical(
                prompt_artifact_id,
                prompt,
                session_id=runtime_session_id,
                run_id=None,
                media_type="text/plain",
                semantic_metadata={
                    "artifact_kind": "capability_projection_prompt",
                    "exposure_id": exposure_id,
                    "owner_id": owner_id,
                    "projection_type": projection_type,
                    "rendered_prompt_fingerprint": prompt_fingerprint,
                },
            )
    else:
        prompt_fingerprint = None
        prompt_artifact_id = None
    rendered_count = len(visible_entries)
    omitted_count = source_entry_count - rendered_count
    if omitted_count < 0:
        raise ValueError("rendered capability entry count exceeds source count")
    fragments = tuple(fragment_facts)
    semantic_fingerprint = capability_projection_semantic_fingerprint(
        visible_source_entries=visible_entries,
        rendered_fragments=fragments,
        source_entry_count=source_entry_count,
        rendered_entry_count=rendered_count,
        omitted_entry_count=omitted_count,
        rendered_prompt_fingerprint=prompt_fingerprint,
        rendered_prompt_chars=len(prompt),
    )
    return CapabilityProjectionFact(
        visible_source_entries=visible_entries,
        rendered_fragments=fragments,
        source_entry_count=source_entry_count,
        rendered_entry_count=rendered_count,
        omitted_entry_count=omitted_count,
        projection_semantic_fingerprint=semantic_fingerprint,
        rendered_prompt_fingerprint=prompt_fingerprint,
        rendered_prompt_artifact_id=prompt_artifact_id,
        rendered_prompt_chars=len(prompt),
    )


def narrow_capability_projection_fact(
    *,
    projection_type: Literal["catalog", "active_skill"],
    original: CapabilityProjectionFact,
    current_candidate: CapabilityProjectionFact,
    archive: ArtifactStore,
    runtime_session_id: str,
    owner_id: str,
    exposure_id: str,
) -> tuple[CapabilityProjectionFact, str | None]:
    """Intersect a continuation projection without re-rendering source entries.

    Only exact original source-entry identities/content survive.  Their original
    rendered fragment bytes are replayed from artifacts; current candidate text
    is never promoted into the continuation prompt.
    """

    current_by_id = {
        entry.projection_entry_id: entry
        for entry in current_candidate.visible_source_entries
    }
    retained_entries = tuple(
        entry
        for entry in original.visible_source_entries
        if (
            entry.projection_entry_id in current_by_id
            and current_by_id[entry.projection_entry_id].content_fingerprint
            == entry.content_fingerprint
        )
    )
    retained_entry_ids = {
        entry.projection_entry_id for entry in retained_entries
    }
    retained_containers = {
        fragment.container_id
        for fragment in original.rendered_fragments
        if fragment.fragment_role == "entry"
        and fragment.source_entry_id in retained_entry_ids
    }

    selected: list[CapabilityRenderedProjectionFragmentFact] = []
    for fragment in original.rendered_fragments:
        if fragment.fragment_role == "entry":
            keep = fragment.source_entry_id in retained_entry_ids
        elif fragment.fragment_role in {"prefix", "suffix"}:
            keep = fragment.container_id in retained_containers
        elif fragment.static_scope == "container_wrapper":
            keep = fragment.container_id in retained_containers
        elif fragment.static_scope == "projection_wrapper":
            keep = bool(retained_entry_ids)
        else:
            # A source-less static fragment is legal only as a wrapper.  Do not
            # silently retain free-standing provider content during continuation.
            keep = False
        if keep:
            selected.append(fragment)

    fragments = tuple(
        fragment.model_copy(update={"order_index": index})
        for index, fragment in enumerate(selected)
    )
    prompt = "".join(
        archive.get_text(
            fragment.fragment_artifact_id,
            session_id=runtime_session_id,
        )
        for fragment in fragments
    )
    if prompt:
        prompt_fingerprint = _raw_sha256(prompt.encode("utf-8"))
        prompt_artifact_id = _artifact_id(
            "capability_projection_prompt",
            [exposure_id, projection_type, prompt_fingerprint],
        )
        archive.put_text_if_absent_or_confirm_identical(
            prompt_artifact_id,
            prompt,
            session_id=runtime_session_id,
            run_id=None,
            media_type="text/plain",
            semantic_metadata={
                "artifact_kind": "capability_projection_prompt",
                "exposure_id": exposure_id,
                "owner_id": owner_id,
                "projection_type": projection_type,
                "rendered_prompt_fingerprint": prompt_fingerprint,
                "render_mode": "continuation_fragment_intersection:v1",
            },
        )
    else:
        prompt_fingerprint = None
        prompt_artifact_id = None

    rendered_count = len(retained_entries)
    source_count = original.source_entry_count
    omitted_count = source_count - rendered_count
    fact = CapabilityProjectionFact(
        visible_source_entries=retained_entries,
        rendered_fragments=fragments,
        source_entry_count=source_count,
        rendered_entry_count=rendered_count,
        omitted_entry_count=omitted_count,
        projection_semantic_fingerprint=capability_projection_semantic_fingerprint(
            visible_source_entries=retained_entries,
            rendered_fragments=fragments,
            source_entry_count=source_count,
            rendered_entry_count=rendered_count,
            omitted_entry_count=omitted_count,
            rendered_prompt_fingerprint=prompt_fingerprint,
            rendered_prompt_chars=len(prompt),
        ),
        rendered_prompt_fingerprint=prompt_fingerprint,
        rendered_prompt_artifact_id=prompt_artifact_id,
        rendered_prompt_chars=len(prompt),
    )
    return fact, prompt or None


def _provider_rendered_projection(
    *,
    projection_type: Literal["catalog", "active_skill"],
    result: ProviderProjectionResult,
) -> tuple[
    RenderedCapabilityPrompt,
    tuple[tuple[str, str, str, dict[str, object]], ...],
]:
    output = result.output
    if projection_type == "catalog":
        rendered = output.catalog_rendered
        payloads = tuple(
            (
                entry.name,
                "catalog_entry",
                entry.source,
                asdict(entry),
            )
            for entry in output.catalog_entries
        )
        prompt = output.catalog_prompt
    else:
        rendered = output.active_skill_rendered
        payloads = tuple(
            (
                injection.name,
                "active_skill_injection",
                injection.source,
                {
                    **asdict(injection),
                    "path": str(injection.path),
                    "base_dir": str(injection.base_dir),
                },
            )
            for injection in output.active_injections
        )
        prompt = output.active_skill_prompt
    if rendered is not None:
        if rendered.text != prompt:
            raise ValueError("provider rendered projection differs from prompt payload")
        return rendered, payloads
    if not prompt:
        return RenderedCapabilityPrompt(text=None), payloads
    stable_name = f"provider-prompt:{projection_type}:{result.provider_id}"
    source_kind = "mcp" if result.provider_id == "mcp" else "custom"
    provider_payload = {
        "provider_id": result.provider_id,
        "projection_type": projection_type,
        "text": prompt,
    }
    return (
        RenderedCapabilityPrompt(
            text=prompt,
            fragments=(
                _provider_prompt_fragment(stable_name=stable_name, text=prompt),
            ),
            source_entry_count=1,
        ),
        (*payloads, (stable_name, "provider_prompt_fragment", source_kind, provider_payload)),
    )


def _provider_prompt_fragment(*, stable_name: str, text: str):
    from pulsara_agent.capability.types import RenderedCapabilityPromptFragment

    return RenderedCapabilityPromptFragment(
        container_id=stable_name,
        fragment_role="entry",
        static_scope=None,
        source_stable_name=stable_name,
        text=text,
    )


def _raw_sha256(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _artifact_id(namespace: str, payload: object) -> str:
    digest = sha256_fingerprint(namespace, payload).removeprefix("sha256:")
    return f"artifact:{namespace}:{digest}"


__all__ = ["ProviderProjectionResult", "build_capability_projection_fact"]
