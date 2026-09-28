"""Exercise typed annotations and cold resume with saved Luna settings in a disposable DB."""

from __future__ import annotations
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import httpx

from run_model_switch_handover_dogfood import (
    _ReadOnlySettingsStore,
    _create_database,
    _drop_database,
)
from run_pr04_prompt_queue_dogfood import _secrets, _write
from pulsara_agent.settings import LocalSettingsStore, LocalPostgresConfig
from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, ModelsDevCatalogClient
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.llm.input import (
    PromptContent,
    PromptAnnotationPart,
    PromptAnnotationSource,
    LLMTextPart,
)
from pulsara_agent.llm.model_connections import (
    ModelCallBinding,
    ReasoningEffortSelection,
)
from pulsara_agent.workspace_identity import HostWorkspaceInput


ROOT = Path(__file__).resolve().parents[1]


async def main():
    saved = LocalSettingsStore(require_pulsara_home() / "local-settings.yaml").read()
    connection = next(
        x for x in saved.model_connections if x.target.model_id == "openai/gpt-6-luna"
    )
    secrets = _secrets(saved)
    report = {
        "model": connection.target.model_id,
        "reasoning": "max",
        "requests": [],
        "results": [],
    }
    path = ROOT / "output/playwright/annotation-provider-evidence.json"

    def emit():
        _write(path, report, secrets)

    original_send = httpx.AsyncClient.send

    async def send(client, request, **kwargs):
        if request.url.path.endswith("/chat/completions"):
            value = json.loads(request.content)
            report["requests"].append(value)
            emit()
            print(
                "PROVIDER_REQUEST",
                len(report["requests"]),
                value.get("model"),
                flush=True,
            )
        return await original_send(client, request, **kwargs)

    name, _, admin, runtime = _create_database(saved)
    old_home = os.environ.get("PULSARA_HOME")
    core = None
    try:
        httpx.AsyncClient.send = send
        with TemporaryDirectory(prefix="pulsara-annotations-") as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir()
            workspace = root / "workspace"
            workspace.mkdir()
            os.environ["PULSARA_HOME"] = str(home)
            settings = _ReadOnlySettingsStore(
                replace(saved, postgres=LocalPostgresConfig(runtime, admin))
            )
            catalog = ModelCatalogOwner(ModelsDevCatalogClient())
            await catalog.refresh()
            model_runtime = ModelRuntime.production(settings=settings, catalog=catalog)
            core = KernelHostCore.production(model_runtime=model_runtime)
            workspace_input = HostWorkspaceInput(
                workspace_kind="project",
                workspace_root=workspace,
                trust_workspace_mcp_config=False,
            )
            session = await core.open_session(workspace_input)
            await session.update_model_call_binding(
                ModelCallBinding(connection.id, ReasoningEffortSelection("max"))
            )
            await session.attach_controller("annotations-dogfood")
            first = await session.run_turn(
                PromptContent.text(
                    "请只输出下面两行原文，不调用工具，不添加其他文字：\n苹果属于水果。\n正方形有四条边。"
                )
            )
            report["results"].append(first.final_text)
            emit()
            source = first.final_text
            parts = []
            for quote, comment in [
                ("苹果属于水果。", "请以“引用1：”开头，用一句话补充另一种常见水果。"),
                (
                    "正方形有四条边。",
                    "请以“引用2：”开头，用一句话说明四条边长度的关系。",
                ),
            ]:
                start = source.index(quote)
                end = start + len(quote)
                parts.append(
                    PromptAnnotationPart(
                        quote,
                        PromptAnnotationSource(
                            first.final_entry_id,
                            len(source[:start].encode("utf-16-le")) // 2,
                            len(source[:end].encode("utf-16-le")) // 2,
                        ),
                        comment,
                    )
                )
            parts.append(LLMTextPart("分别回应两条批注，不调用工具。"))
            second = await session.run_turn(PromptContent(tuple(parts)))
            report["results"].append(second.final_text)
            emit()
            # Fresh runtime keeps canonical sources. A new quote is a new suffix.
            sid = session.session_id
            await core.close_session(session.host_session_id)
            session = await core.resume_session(sid, workspace_input=workspace_input)
            await session.attach_controller("annotations-dogfood")
            third = await session.run_turn(
                PromptContent(
                    (parts[1], LLMTextPart("简短回应此批注即可，不调用工具。"))
                )
            )
            report["results"].append(third.final_text)
            requests = report["requests"]
            assert len(requests) >= 3
            encoded = json.dumps(requests[1], ensure_ascii=False)
            assert first.final_entry_id not in encoded
            assert '"source"' not in json.dumps(
                [m for m in requests[1]["messages"] if m["role"] == "user"],
                ensure_ascii=False,
            )
            assert "引用1" in second.final_text and "引用2" in second.final_text
            assert requests[0]["tools"] == requests[1]["tools"]
            assert (
                requests[1]["messages"][: len(requests[0]["messages"])]
                == requests[0]["messages"]
            )
            report["passed"] = True
            emit()
            print("PASSED", second.final_text, third.final_text, flush=True)
    except BaseException as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        emit()
        raise
    finally:
        try:
            if core is not None:
                await core.shutdown()
        finally:
            if old_home is None:
                os.environ.pop("PULSARA_HOME", None)
            else:
                os.environ["PULSARA_HOME"] = old_home
            httpx.AsyncClient.send = original_send
            _drop_database(saved, name)


if __name__ == "__main__":
    asyncio.run(main())
