"""Pure rendering of source-owned semantic payloads for compatibility lowering."""

from __future__ import annotations

from typing import Mapping

from pulsara_agent.primitives.context import ContextSectionCandidate
from pulsara_agent.primitives.context_source import (
    ArtifactContextSourceContentSemanticFact,
    RuntimeClockProposalPayloadFact,
    RuntimeEnvironmentPayloadFact,
    context_source_payload_content,
    raw_content_sha256,
)


def render_context_source_candidate(
    candidate: ContextSectionCandidate,
    *,
    hydrated_contents: Mapping[str, str] | None = None,
) -> str:
    payload = candidate.attribution.semantic.payload
    if isinstance(payload, RuntimeEnvironmentPayloadFact):
        workspace_mode = (
            "project workspace; treat workspace facts as durable project context."
            if payload.workspace_kind == "project"
            else "transient scratch workspace; do not treat workspace facts as durable project context."
        )
        timezone = payload.session_timezone or "UTC"
        text = "\n".join(
            (
                "<runtime-context>",
                f"Local timezone: {timezone}",
                f"Workspace kind: {payload.workspace_kind} ({workspace_mode})",
                f"Workspace root: {payload.model_visible_workspace_root}",
                f"Terminal current cwd: {payload.terminal_current_cwd}",
                "Terminal workdir, when provided, must stay inside workspace_root; when unsure, omit workdir or run pwd.",
                "Relative terminal workdir values resolve from workspace_root.",
                "Read-only filesystem tools may read ordinary text files outside workspace_root, but write/edit tools and terminal workdir remain workspace-scoped.",
                "</runtime-context>",
            )
        )
    elif isinstance(payload, RuntimeClockProposalPayloadFact):
        text = "\n".join(
            (
                f"Current date: {payload.local_date}",
                f"Local timezone: {payload.timezone_name}",
                f"Observed at UTC: {payload.observed_at_utc}",
            )
        )
    else:
        content = context_source_payload_content(payload)
        if content is None:
            text = ""
        elif isinstance(content, ArtifactContextSourceContentSemanticFact):
            text = (hydrated_contents or {}).get(content.semantic_fingerprint)
            if text is None:
                raise ValueError(
                    "artifact source content must be hydrated before compile"
                )
            encoded = text.encode("utf-8")
            if (
                len(text) != content.expected_chars
                or len(encoded) != content.expected_utf8_bytes
                or raw_content_sha256(encoded) != content.content_sha256
            ):
                raise ValueError("hydrated artifact source content identity drifted")
        else:
            text = content.text
    return text


__all__ = [
    "render_context_source_candidate",
]
