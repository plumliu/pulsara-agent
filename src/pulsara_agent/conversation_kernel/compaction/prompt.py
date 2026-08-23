"""Round 5B summary guidance, tolerant normalization and snapshot carrier."""

from __future__ import annotations

import json
import re
from hashlib import sha256

from pulsara_agent.conversation_kernel.compaction.contracts import (
    COMPACTION_SUMMARY_PROMPT_CONTRACT,
    CompactionActiveRequestLocation,
    CompactionContinuationMode,
    CompactionSnapshotCarrier,
    CompactionTargetBranch,
    FrozenCompactionActiveRequest,
    FrozenCompactionSummary,
)
from pulsara_agent.primitives.context import canonical_json_bytes, context_fingerprint


_SUMMARY_REQUEST_COMMON = """CRITICAL: Respond with text only. Do not call any tool.
This is a temporary summary-only call. For this response only, this checkpoint
request supersedes earlier requests to inspect, read, search, invoke, or otherwise
use a tool. Do not continue or execute task work in this response. You already
have the conversation material available to this request; tool calls will not be
executed and cannot provide additional context.

TEMPORAL HANDOFF RULES:
- The no-tool and no-execution restrictions above end when this summary response
  ends. They are not state of the user's task.
- Never say that a user request is queued, deferred, blocked, waiting for another
  agent, or awaiting a later turn merely because this summary-only call cannot
  execute it.
- Preserve the actual task status from the conversation. A later correction,
  cancellation, or replacement supersedes conflicting earlier work; do not turn
  superseded work back into a next step.

你正在执行一次CONTEXT CHECKPOINT COMPACTION。请为稍后继续同一任务的Agent生成一份忠实、紧凑但信息充分的语义交接，使它不必重新猜测已经完成的工作、用户意图或当前诊断。

请在适用时覆盖以下内容；这些是内容指导，不是固定标题或输出模板：
- 用户当前的主要目标、最新纠正、明确约束、scope边界、偏好和不可违反的决定；
- 已完成的工作、关键技术或架构决定、采用这些决定的原因，以及确实通过的验证；
- 对继续工作真正必要的文件路径、symbol、命令、配置、数据、示例或opaque handle；
- 遇到的错误、失败方案、已确认根因、已经尝试或完成的修复，以及仍在评估的假设；
- 尚未完成的任务、正在进行的工作、当前精确诊断和最直接的下一步。

准确性与继承规则：
- 特别重视用户的后续纠正和反馈；当前canonical内容优先于更早的描述；
- 若上下文中已有旧的compaction summary，请继承其中仍相关的语义，不要让重要信息在重复压缩中丢失；
- 明确区分已完成、已验证、仅尝试、失败、待确认和待执行；不要把计划写成事实，也不要发明用户没有要求的工作；
- 优先写能让后续Agent继续行动的精确信息。保持经济，避免大段逐字代码、重复消息和无关历史，但不要为了短而遗漏关键约束或当前状态。

Runtime会在交接后另外提供最近真实用户原话、受保护的最近tool group，以及当前工具、权限、Plan、MCP、Skill、memory和运行任务等动态事实。因此：
- 不要逐条复述全部用户消息或复制最近原话；
- 不要枚举或猜测动态目录、catalog或当前Runtime状态；
- 不要复制Skill正文；
- 只有继续明确pending工作确实需要时，才保留一个已经出现且可操作的精确引用或handle；
- 不要声称某项运行时状态在交接后仍然current。

先在内部组织和核对信息，不要输出analysis或思考过程。最终只输出语义交接正文；可以自然使用短段落、项目符号或小标题，但没有必需的标题、编号、XML标签或固定格式。不要问候，不要向用户作答，不要把本指令当成用户的新需求，也不要在正文后添加closing text。
"""

_ACTIVE_SUMMARY_REQUEST_SUFFIX = """
ACTIVE-TURN HANDOFF:
Runtime has a RUNNING target turn. After this summary is durably adopted, Runtime
will immediately open a normal successor call. Describe unresolved work as the
active objective to resume now, never as work delayed by compaction. Runtime will
separately and mechanically identify the exact active request; do not guess a new
request or instruct the successor to wait for another user message.

REMINDER: Produce the checkpoint handoff now as text only. Do not resume task
work and do not call, inspect, retry, or request any tool in this summary response.
"""

_IDLE_SUMMARY_REQUEST_SUFFIX = """
IDLE HANDOFF:
Runtime's target turn is already terminal. This call only creates a durable base
for a future user turn; it does not create a new active request or an immediate
successor model call. Describe old work as pending only when the conversation
itself left it pending, never merely because this summary call cannot execute it.

REMINDER: Produce the checkpoint handoff now as text only. Do not resume task
work and do not call, inspect, retry, or request any tool in this summary response.
"""


def compaction_summary_request(target_branch: CompactionTargetBranch) -> str:
    if target_branch is CompactionTargetBranch.ACTIVE_INSTALLATION:
        return _SUMMARY_REQUEST_COMMON + _ACTIVE_SUMMARY_REQUEST_SUFFIX
    if target_branch is CompactionTargetBranch.IDLE_BASE_ONLY:
        return _SUMMARY_REQUEST_COMMON + _IDLE_SUMMARY_REQUEST_SUFFIX
    raise ValueError("unsupported compaction target branch")

_LEADING_ANALYSIS = re.compile(
    r"\A\s*<analysis>.*?</analysis>\s*", re.DOTALL
)
_OUTER_MARKDOWN_FENCE = re.compile(
    r"\A```[^\n]*\n(?P<body>.*)\n```\s*\Z", re.DOTALL
)


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
    recent_user_messages: tuple[str, ...],
    continuation_mode: CompactionContinuationMode,
    active_request: FrozenCompactionActiveRequest | None,
) -> CompactionSnapshotCarrier:
    handoff_instruction = compaction_handoff_instruction(
        continuation_mode=continuation_mode,
        active_request=active_request,
    )
    body = canonical_json_bytes(
        {
            "continuation": {
                "mode": continuation_mode.value,
                "instruction": handoff_instruction,
                "active_request": (
                    None
                    if active_request is None
                    else active_request.canonical_value()
                ),
            },
            "earlier_context_summary": summary.body,
            "recent_user_messages": recent_user_messages,
        }
    )
    return CompactionSnapshotCarrier(
        continuation_mode=continuation_mode,
        handoff_instruction=handoff_instruction,
        active_request=active_request,
        earlier_context_summary=summary.body,
        recent_user_messages=recent_user_messages,
        body=body,
        content_digest="sha256:" + sha256(body).hexdigest(),
    )


def compaction_handoff_instruction(
    *,
    continuation_mode: CompactionContinuationMode,
    active_request: FrozenCompactionActiveRequest | None,
) -> str:
    if continuation_mode is CompactionContinuationMode.AWAIT_NEXT_USER:
        if active_request is not None:
            raise ValueError("idle handoff cannot carry an active request")
        return (
            "HANDOFF COMPLETE / AWAIT NEXT USER. Compaction is durably complete "
            "while no turn is active. No model response or task execution is due "
            "solely because this snapshot exists. This instruction survives a "
            "process restart. When a later canonical user message arrives, it "
            "drives the next normal call; use the summary only as advisory history. "
            "Do not resume completed or superseded work merely because the summary "
            "calls it pending, and do not treat the summary call's temporary "
            "no-tool restriction as task state."
        )
    if continuation_mode is not CompactionContinuationMode.RESUME_ACTIVE_TURN:
        raise ValueError("unsupported compaction continuation mode")
    if active_request is None:
        raise ValueError("active handoff lacks its active request")
    if active_request.location is CompactionActiveRequestLocation.SNAPSHOT_EXACT:
        request_location = (
            "continuation.active_request.text below is the exact mechanically "
            "identified request driving this turn."
        )
    else:
        request_location = (
            "The exact mechanically identified active request remains as a later "
            "canonical user-role message after this snapshot; it is not duplicated "
            "inside continuation.active_request."
        )
    return (
        "HANDOFF COMPLETE / RESUME NOW. Compaction is complete. This is a normal "
        "continuation call, not the summary-only call, so the summarizer's temporary "
        "no-tool and no-execution restrictions have ended. "
        + request_location
        + " Resume that request now, subject to current system policy, permissions, "
        "and available tools. This RESUME directive describes the turn for which "
        "the snapshot was adopted: if a newer canonical turn activation or user "
        "request appears after the mechanically identified request, the newer "
        "request supersedes this directive. Apply later user corrections or "
        "cancellations and current Runtime facts. A summary statement that work was "
        "queued, deferred, or not executed because of compaction is descriptive "
        "noise, not an instruction to wait. The summary is advisory; do not repeat "
        "completed or superseded work merely to verify it."
    )


def parse_compaction_snapshot_carrier(
    value: str | bytes,
) -> CompactionSnapshotCarrier:
    encoded = value.encode("utf-8") if isinstance(value, str) else value
    try:
        raw = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("snapshot carrier is not valid UTF-8 JSON") from error
    if not isinstance(raw, dict) or set(raw) != {
        "continuation",
        "earlier_context_summary",
        "recent_user_messages",
    }:
        raise ValueError("snapshot carrier fields do not match its contract")
    continuation = raw["continuation"]
    if not isinstance(continuation, dict) or set(continuation) != {
        "mode",
        "instruction",
        "active_request",
    }:
        raise ValueError("snapshot continuation fields do not match its contract")
    try:
        mode = CompactionContinuationMode(continuation["mode"])
    except (TypeError, ValueError) as error:
        raise ValueError("snapshot continuation mode is invalid") from error
    active_value = continuation["active_request"]
    active_request: FrozenCompactionActiveRequest | None
    if active_value is None:
        active_request = None
    else:
        if not isinstance(active_value, dict) or set(active_value) != {
            "entry_id",
            "entry_sequence",
            "location",
            "text",
        }:
            raise ValueError("snapshot active-request fields do not match its contract")
        entry_sequence = active_value["entry_sequence"]
        if isinstance(entry_sequence, bool) or not isinstance(entry_sequence, int):
            raise ValueError("snapshot active-request sequence is invalid")
        try:
            location = CompactionActiveRequestLocation(active_value["location"])
        except (TypeError, ValueError) as error:
            raise ValueError("snapshot active-request location is invalid") from error
        entry_id = active_value["entry_id"]
        text = active_value["text"]
        if not isinstance(entry_id, str) or not (
            text is None or isinstance(text, str)
        ):
            raise ValueError("snapshot active-request values are invalid")
        active_request = FrozenCompactionActiveRequest(
            entry_id=entry_id,
            entry_sequence=entry_sequence,
            location=location,
            text=text,
        )
    instruction = continuation["instruction"]
    summary = raw["earlier_context_summary"]
    recent = raw["recent_user_messages"]
    if (
        not isinstance(instruction, str)
        or not isinstance(summary, str)
        or not isinstance(recent, list)
        or any(not isinstance(item, str) for item in recent)
    ):
        raise ValueError("snapshot carrier values are invalid")
    expected_instruction = compaction_handoff_instruction(
        continuation_mode=mode,
        active_request=active_request,
    )
    if instruction != expected_instruction:
        raise ValueError("snapshot handoff instruction differs from its contract")
    canonical = canonical_json_bytes(raw)
    if encoded != canonical:
        raise ValueError("snapshot carrier is not canonical JSON")
    return CompactionSnapshotCarrier(
        continuation_mode=mode,
        handoff_instruction=instruction,
        active_request=active_request,
        earlier_context_summary=summary,
        recent_user_messages=tuple(recent),
        body=canonical,
        content_digest="sha256:" + sha256(canonical).hexdigest(),
    )


def summary_request_fingerprint(summary_request: str) -> str:
    if not summary_request:
        raise ValueError("summary request is empty")
    return context_fingerprint(
        COMPACTION_SUMMARY_PROMPT_CONTRACT,
        {"request": summary_request},
    )


__all__ = [
    "build_compaction_snapshot_carrier",
    "compaction_handoff_instruction",
    "compaction_summary_request",
    "freeze_compaction_summary_output",
    "parse_compaction_snapshot_carrier",
    "summary_request_fingerprint",
]
