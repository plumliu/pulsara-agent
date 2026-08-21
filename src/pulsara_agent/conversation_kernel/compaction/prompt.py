"""Round 5B summary guidance, tolerant normalization and snapshot carrier."""

from __future__ import annotations

import re
from hashlib import sha256

from pulsara_agent.conversation_kernel.compaction.contracts import (
    COMPACTION_SUMMARY_PROMPT_CONTRACT,
    CompactionSnapshotCarrier,
    FrozenCompactionSummary,
)
from pulsara_agent.primitives.context import canonical_json_bytes, context_fingerprint


SUMMARY_REQUEST = """CRITICAL: Respond with text only. Do not call any tool.
You already have the conversation material required for this task. Tool calls are
not available to this summary request and will not be executed.

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

REMINDER: Return text only. Do not call any tool.
"""

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
) -> CompactionSnapshotCarrier:
    body = canonical_json_bytes(
        {
            "earlier_context_summary": summary.body,
            "recent_user_messages": recent_user_messages,
        }
    )
    return CompactionSnapshotCarrier(
        earlier_context_summary=summary.body,
        recent_user_messages=recent_user_messages,
        body=body,
        content_digest="sha256:" + sha256(body).hexdigest(),
    )


def summary_request_fingerprint() -> str:
    return context_fingerprint(
        COMPACTION_SUMMARY_PROMPT_CONTRACT,
        {"request": SUMMARY_REQUEST},
    )


__all__ = [
    "SUMMARY_REQUEST",
    "build_compaction_snapshot_carrier",
    "freeze_compaction_summary_output",
    "summary_request_fingerprint",
]
