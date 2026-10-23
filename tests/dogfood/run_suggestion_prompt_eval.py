"""Paired live prompt-quality samples; no Host, database, or production writes.

These authored conversations isolate suggestion wording. They do not replace
the separate exact-prefix/Host integration dogfood. Rubrics are never sent.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path

from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.conversation_kernel.direct_model import DirectKernelModelPort
from pulsara_agent.conversation_kernel.ephemeral import SUGGESTION_PROMPT
from pulsara_agent.identity import random_id
from pulsara_agent.llm.ephemeral import (
    PreparedEphemeralRequest,
    execute_ephemeral_request,
)
from pulsara_agent.llm.frozen_target import _freeze_provider_physical_call_target
from pulsara_agent.llm.input import LLMMessage, ToolSpec
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, ModelsDevCatalogClient
from pulsara_agent.llm.model_connections import (
    ModelCallBinding,
    ReasoningEffortSelection,
)
from pulsara_agent.llm.request import LLMContext
from pulsara_agent.llm.resolution import resolve_model_call
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.primitives.model_call import ModelCallPurpose
from pulsara_agent.settings import LocalSettingsStore
from tests.dogfood.credentials import secret_scrubber
from tests.dogfood.run_ephemeral_products_dogfood import _RecordedBorrow
from tests.dogfood.run_model_switch_handover_dogfood import _ReadOnlySettingsStore


CASES = (
    {
        "id": "temporary_capabilities",
        "description": "根据本次产品讨论整理的评测对话，非逐字历史",
        "rubric_not_sent": "能否综合低成本、可丢弃、不影响主任务等反复约束，提出临时能力的统一边界，而非只问缓存怎么实现。",
        "conversation": (
            (
                "user",
                "我想在会话输入框里显示追问建议。正式回答结束后，用当前模型和上下文临时请求一次；响应不能入数据库，随时丢弃都可以。",
            ),
            ("assistant", "可以抽象为临时请求，独立执行，结果只存在内存中。"),
            (
                "user",
                "我又想到起标题也能用：总结第一份输入，成功就替换默认标题，不成功就继续用默认的。不过起标题时把整套系统提示塞进去太浪费了。",
            ),
            (
                "assistant",
                "标题可以用独立的简短提示词和第一份输入，建议则复用主会话的上下文。",
            ),
            (
                "user",
                "建议其实只是一个可以丢弃的小分叉。要保持原来的输入前缀，但追加指令如果超限就算了，不要为了建议触发主会话压缩。",
            ),
            ("assistant", "会保留原前缀；超限跳过，不修改主会话。"),
            (
                "user",
                "而且只在用户这一轮真正结束时做，不要任何idle会话都请求一次。正式回答也不要等它，失败或者慢了都不该影响用户继续聊。",
            ),
            (
                "assistant",
                "建议只在用户执行链正常结束后后台生成，正式回答照常流式显示。",
            ),
            (
                "user",
                "用户在输入框写了东西，不代表不会删掉。已经生成的建议应该留在前端，清空草稿后仍能看到。",
            ),
            (
                "assistant",
                "可以将建议保存在会话级前端内存里，编辑草稿只隐藏它；清空时恢复显示，正式提交新输入时使旧建议失效。",
            ),
        ),
    },
    {
        "id": "autonomous_decisions",
        "description": "构造的多轮纠正场景：自主执行与用户掌握决策之间的张力",
        "rubric_not_sent": "能否从多次否定堆日志、频繁确认和事后补救，提出自主决策边界/何时通知或确认，而非泛问如何做报告。",
        "conversation": (
            (
                "user",
                "编程agent跑几个小时我没问题，也不想每一步都确认。但它最后给一堆改动，我经常不知道为什么放弃了另外一种方案。",
            ),
            ("assistant", "可以在最终报告里列出修改内容和测试结果。"),
            (
                "user",
                "不是改了哪些文件。我想知道它在哪个地方替我做了取舍。如果失败的路线没写进最终代码，我就看不到了。",
            ),
            ("assistant", "那可以保留每一步的完整日志，之后供你检查。"),
            (
                "user",
                "完整日志太多了，几十页我也不会读。还有，我也不想因为这个又回到每个小决定都打断我。",
            ),
            ("assistant", "可以在执行中记录关键取舍，最后给出简短的决策摘要。"),
            (
                "user",
                "但有些取舍最后才告诉我已经晚了，比如它为了赶进度把我真正关心的可维护性换掉了。反过来，变量怎么命名这种事我不想管。",
            ),
            (
                "assistant",
                "决策摘要可以记录选了什么、放弃了什么、为什么选择，以及对可维护性等目标的影响。",
            ),
        ),
    },
    {
        "id": "resolved_control",
        "description": "已明确结束的简单请求，用于观察是否强行编造核心问题",
        "rubric_not_sent": "应返回空文本；任务已解决且用户明确不要扩展。",
        "conversation": (
            ("user", "2加2等于多少？只要答案，不需要解释或延伸，这就是我全部的问题。"),
            ("assistant", "4。"),
        ),
    },
)


async def run(output: Path):
    require_pulsara_home()
    saved = LocalSettingsStore().read()
    scrubber = secret_scrubber(saved)
    configured = {
        c.target.wire_api.value: c
        for c in saved.model_connections
        if c.target.model_id == "deepseek-flash" and c.target.route_id == "deepseek"
    }
    assert set(configured) == {"openai_chat_completions", "openai_responses"}
    catalog = ModelCatalogOwner(ModelsDevCatalogClient())
    await catalog.refresh()
    runtime = ModelRuntime.production(
        settings=_ReadOnlySettingsStore(saved), catalog=catalog
    )
    timeout = DirectKernelModelPort(model_runtime=runtime).transport_timeout_policy
    # An advertised test tool makes auto meaningful. The executor has no tool dispatcher.
    tool = ToolSpec(
        "read_file",
        "Read a file from the workspace.",
        {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    )
    records, results = [], []
    evidence = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "prompt": SUGGESTION_PROMPT,
        "method": "Authored identical histories, minimal neutral SYSTEM, one advertised tool, auto, high; no live Host or DB. Rubrics excluded from requests.",
        "cases": CASES,
        "records": records,
        "results": results,
    }
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    original_borrow = ModelRuntime.borrow_transport
    ModelRuntime.borrow_transport = lambda self, permit: _RecordedBorrow(
        original_borrow(self, permit), records
    )
    try:
        for case in CASES:
            for wire in ("openai_chat_completions", "openai_responses"):
                binding = ModelCallBinding(
                    configured[wire].id, ReasoningEffortSelection("high")
                )
                target = runtime.resolve_target(binding, timeout_policy=timeout)
                call = resolve_model_call(
                    target=target,
                    binding=binding,
                    purpose=ModelCallPurpose.CONVERSATION_SUGGESTION,
                )
                messages = tuple(
                    (LLMMessage.user if role == "user" else LLMMessage.assistant)(text)
                    for role, text in case["conversation"]
                )
                context = LLMContext(
                    messages=(*messages, LLMMessage.user(SUGGESTION_PROMPT)),
                    system_prompt="You are a helpful assistant.",
                    tools=(tool,),
                    tool_choice="auto",
                    context_id=random_id("context"),
                    resolved_model_call_id=call.resolved_model_call_id,
                    model_call_index=None,
                )
                prepared = PreparedEphemeralRequest(
                    _freeze_provider_physical_call_target(
                        target=target,
                        call=call,
                        maximum_input_tokens=target.context_budget.input_budget_tokens,
                    ),
                    context,
                    timeout,
                )
                result = await execute_ephemeral_request(
                    runtime=runtime, prepared=prepared
                )
                records[-1]["case_id"] = case["id"]
                assert records[-1]["payload"]["tool_choice"] == "auto"
                value = {
                    "case_id": case["id"],
                    "wire_api": wire,
                    "status": result.status.value,
                    "text": result.text,
                }
                results.append(value)
                print(
                    scrubber.scrub_text(json.dumps(value, ensure_ascii=False)),
                    flush=True,
                )
        assert not runtime._active_borrows
        evidence["status"] = "completed_samples_not_quality_assertions"
    except BaseException as error:
        evidence["status"], evidence["error"] = (
            "failed",
            f"{type(error).__name__}: {error}",
        )
        raise
    finally:
        ModelRuntime.borrow_transport = original_borrow
        evidence["finished_at"] = datetime.now(timezone.utc).isoformat()
        output.write_text(
            scrubber.scrub_text(
                json.dumps(evidence, ensure_ascii=False, indent=2, default=str)
            )
            + "\n"
        )
    print(json.dumps({"evidence": str(output)}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("output/ephemeral-products-20261023/suggestion-prompt-eval.json"),
    )
    asyncio.run(run(parser.parse_args().output))
