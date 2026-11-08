"""Round 5B summary guidance, tolerant normalization and snapshot carrier."""

from __future__ import annotations

import json
import re
from hashlib import sha256

from pulsara_agent.conversation_kernel.compaction.contracts import (
    COMPACTION_SUMMARY_PROMPT_CONTRACT,
    CompactionContinuationMode,
    CompactionSnapshotCarrier,
    FrozenCompactionSummary,
)
from pulsara_agent.model_input.lowering import (
    compaction_snapshot_display_header,
    compaction_snapshot_provider_content,
    display_json,
)
from pulsara_agent.primitives.context import canonical_json_bytes, context_fingerprint


_SUMMARY_REQUEST_COMMON = """CRITICAL: Respond with text only. Do not call any tool.
This is a temporary summary-only call. Do not continue or execute task work.
The material designated H is the earlier history to summarize. Material designated
R is retained original interaction supplied for reference: use its latest corrections
and outcomes to understand H, but do not copy its complete log into the summary.
Runtime supplies the exact H/R boundaries with this request and will preserve R
separately in its original message form. New input N is outside this summary scope.

Produce a faithful, compact summary of H that covers the following where relevant:
- The user's goals, constraints, preferences, decisions and corrections.
- Completed work, actual verification, conclusions and their supporting evidence.
- Files, symbols, commands, opaque handles and references needed to continue.
- Failed approaches, confirmed errors, unresolved questions and pending work.

Use R to keep H's status accurate. Do not revive cancelled or superseded tasks.
Distinguish completed, verified, attempted, failed and not-yet-executed work.
Carry forward still-relevant meaning from earlier summaries in H. Do not invent
facts, infer that work was queued or paused because of this summary call, copy
Skill bodies, or predict current runtime permissions, catalogs or resource state.
Preserve an exact previously seen image reference only when future work needs it.
Be concise without omitting information necessary to continue the actual task.

Output only the summary body. Prose, bullets and headings are all acceptable; no
fixed schema is required. Do not greet or answer the user or output analysis.
"""

_SUMMARY_REQUEST_SUFFIX = """
Runtime owns continuation and will provide current mechanical context separately.
The temporary no-tool restriction ends with this summary response. It does not
change task status. Produce the summary now as text only; do not execute tools.
"""


def compaction_summary_request() -> str:
    return _SUMMARY_REQUEST_COMMON + _SUMMARY_REQUEST_SUFFIX


_LEADING_ANALYSIS = re.compile(r"\A\s*<analysis>.*?</analysis>\s*", re.DOTALL)
_OUTER_MARKDOWN_FENCE = re.compile(r"\A```[^\n]*\n(?P<body>.*)\n```\s*\Z", re.DOTALL)


def freeze_compaction_summary_output(
    raw_text: str,
    *,
    maximum_utf8_bytes: int,
) -> FrozenCompactionSummary:
    """Freeze one bounded handoff without imposing a prose schema.

    Provider terminal atomicity and the no-tool outcome are enforced before
    this factory is called.  XML/Markdown wrappers are presentation hints, not
    authority: a complete wrapper is unwrapped when present and otherwise the
    model's normalized text is retained verbatim.
    """

    if not isinstance(raw_text, str):
        raise TypeError("compaction summary output must be text")
    if maximum_utf8_bytes <= 0:
        raise ValueError("compaction summary byte bound must be positive")
    value = raw_text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    value = _LEADING_ANALYSIS.sub("", value, count=1).strip()
    fenced = _OUTER_MARKDOWN_FENCE.fullmatch(value)
    if fenced is not None:
        value = fenced.group("body").strip()
        value = _LEADING_ANALYSIS.sub("", value, count=1).strip()
    opening = value.find("<summary>")
    closing = value.rfind("</summary>")
    if opening >= 0 and closing > opening:
        parts = (
            value[:opening].strip(),
            value[opening + len("<summary>") : closing].strip(),
            value[closing + len("</summary>") :].strip(),
        )
        body = "\n\n".join(part for part in parts if part)
    else:
        body = value
    # Tags echoed as prose are data inside the JSON snapshot, not control.
    body = (
        body.replace("</summary>", "<\\/summary>")
        .replace("<summary>", "<\\summary>")
        .replace("</analysis>", "<\\/analysis>")
        .replace("<analysis>", "<\\analysis>")
        .strip()
    )
    encoded = body.encode("utf-8")
    if not encoded or len(encoded) > maximum_utf8_bytes:
        raise ValueError("compaction summary is empty or exceeds its byte bound")
    return FrozenCompactionSummary(
        body=body,
        body_utf8_bytes=len(encoded),
        body_digest=context_fingerprint(
            "pulsara.frozen-compaction-summary.v2-guided-freeform", body
        ),
    )


def build_compaction_snapshot_carrier(
    *,
    summary: FrozenCompactionSummary,
    retained_entry_ids: tuple[str, ...],
    continuation_mode: CompactionContinuationMode,
    active_request_entry_id: str | None,
) -> CompactionSnapshotCarrier:
    """Encode S and original-entry selection, without copying R/N content."""

    body = canonical_json_bytes(
        {
            "continuation": {
                "mode": continuation_mode.value,
                "active_request_entry_id": active_request_entry_id,
            },
            "earlier_context_summary": summary.body,
            "retained_entry_ids": retained_entry_ids,
        }
    )
    return CompactionSnapshotCarrier(
        continuation_mode=continuation_mode,
        active_request_entry_id=active_request_entry_id,
        earlier_context_summary=summary.body,
        retained_entry_ids=retained_entry_ids,
        body=body,
        content_digest="sha256:" + sha256(body).hexdigest(),
    )


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("snapshot carrier JSON contains a duplicate key")
        value[key] = item
    return value


def parse_compaction_snapshot_carrier(value: str | bytes) -> CompactionSnapshotCarrier:
    """Strictly decode the single canonical refs carrier contract."""

    encoded = value.encode("utf-8") if isinstance(value, str) else value
    if not isinstance(encoded, bytes):
        raise TypeError("snapshot carrier must be immutable UTF-8 bytes")
    try:
        raw = json.loads(encoded, object_pairs_hook=_unique_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("snapshot carrier is not valid UTF-8 JSON") from error
    if not isinstance(raw, dict) or set(raw) != {
        "continuation", "earlier_context_summary", "retained_entry_ids"
    }:
        raise ValueError("snapshot carrier fields do not match its contract")
    continuation = raw["continuation"]
    if not isinstance(continuation, dict) or set(continuation) != {
        "mode", "active_request_entry_id"
    }:
        raise ValueError("snapshot continuation fields do not match its contract")
    try:
        mode = CompactionContinuationMode(continuation["mode"])
    except (TypeError, ValueError) as error:
        raise ValueError("snapshot continuation mode is invalid") from error
    retained_entry_ids = raw["retained_entry_ids"]
    if not isinstance(retained_entry_ids, list):
        raise ValueError("snapshot retained entry IDs must be an ordered list")
    return CompactionSnapshotCarrier(
        continuation_mode=mode,
        active_request_entry_id=continuation["active_request_entry_id"],
        earlier_context_summary=raw["earlier_context_summary"],
        retained_entry_ids=tuple(retained_entry_ids),
        body=encoded,
        content_digest="sha256:" + sha256(encoded).hexdigest(),
    )


def compaction_snapshot_canonical_expanded_bytes(value: str | bytes) -> int:
    """Charge this S-only body; canonical R/N rows have independent charges."""

    return parse_compaction_snapshot_carrier(value).canonical_expanded_bytes


def summary_request_fingerprint(summary_request: str) -> str:
    if not summary_request:
        raise ValueError("summary request is empty")
    return context_fingerprint(
        COMPACTION_SUMMARY_PROMPT_CONTRACT,
        {"request": summary_request},
    )


__all__ = [
    "build_compaction_snapshot_carrier",
    "compaction_snapshot_canonical_expanded_bytes",
    "compaction_snapshot_display_header",
    "compaction_snapshot_provider_content",
    "compaction_summary_request",
    "display_json",
    "freeze_compaction_summary_output",
    "parse_compaction_snapshot_carrier",
    "summary_request_fingerprint",
]
