from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from pulsara_agent.event import (
    ToolCallDeltaEvent,
    ToolCallStartEvent,
    ToolResultTextDeltaEvent,
)
from pulsara_agent.host import HostCore, HostWorkspaceInput
from pulsara_agent.llm import ModelRole
from pulsara_agent.llm.request import LLMOptions
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.runtime.permission import preset_to_policy
from pulsara_agent.settings import PulsaraSettings


pytestmark = pytest.mark.real_llm


REPO_ID = "plumliu/kuairand-semantic-id-rqvae"
DOWNLOAD_DIR = "hf_cli_dogfood_download"
DOWNLOAD_SENTINEL = "PULSARA_HF_CLI_DOGFOOD_DOWNLOAD_OK"
DELETE_SENTINEL = "PULSARA_HF_CLI_DOGFOOD_DELETE_OK"


def test_real_llm_hf_cli_skill_downloads_and_deletes_model_repo(tmp_path: Path) -> None:
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip(
            "Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider."
        )
    if os.getenv("PULSARA_RUN_HF_CLI_DOGFOOD") != "1":
        pytest.skip(
            "Set PULSARA_RUN_HF_CLI_DOGFOOD=1 to run the Hugging Face CLI skill dogfood."
        )
    if shutil.which("hf") is None:
        pytest.skip("The hf CLI is not available on Pulsara process PATH.")

    settings = _settings()
    _connect_or_skip(settings.storage.postgres_dsn)

    result = asyncio.run(_run_hf_cli_skill_dogfood(settings, tmp_path))
    print(
        "\nREAL_LLM_HF_CLI_SKILL_DOGFOOD="
        + json.dumps(_report(result), ensure_ascii=False, sort_keys=True)
    )

    assert result["status"] == "finished", result
    assert result["download_dir_exists_after"] is False, result
    assert result["tool_names"].count("terminal") >= 1, result
    # Terminal output may be archived before the follow-up model call. Reading
    # that Pulsara-owned artifact is part of the normal terminal observation
    # path, not an unrelated way to perform the Hugging Face operation.
    assert set(result["tool_names"]).issubset(
        {"terminal", "terminal_process", "artifact_read"}
    ), result
    assert (
        sum(command.count("hf download") for command in result["terminal_commands"])
        == 1
    ), result
    assert (
        sum(command.count("rm -rf") for command in result["terminal_commands"]) == 1
    ), result
    assert any(
        "hf download" in command
        and REPO_ID in command
        and "rm -rf" in command
        and DOWNLOAD_DIR in command
        for command in result["terminal_commands"]
    ), result
    assert DOWNLOAD_SENTINEL in result["terminal_output"], result
    assert DELETE_SENTINEL in result["terminal_output"], result
    assert DOWNLOAD_SENTINEL in result["final_text"], result
    assert DELETE_SENTINEL in result["final_text"], result


def _report(result: dict[str, object]) -> dict[str, object]:
    return {
        "status": result["status"],
        "stop_reason": result["stop_reason"],
        "tool_names": result["tool_names"],
        "terminal_commands": result["terminal_commands"],
        "download_dir_exists_after": result["download_dir_exists_after"],
        "terminal_process_used": result["terminal_process_used"],
        "final_text": result["final_text"],
        "download_sentinel_seen": DOWNLOAD_SENTINEL in str(result["terminal_output"]),
        "delete_sentinel_seen": DELETE_SENTINEL in str(result["terminal_output"]),
    }


async def _run_hf_cli_skill_dogfood(
    settings: PulsaraSettings, tmp_path: Path
) -> dict[str, object]:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    _write_hf_cli_skill(workspace_root)

    core = HostCore(settings=settings, durable=True)
    session = None
    try:
        session = await core.open_session(
            HostWorkspaceInput(
                workspace_kind="project",
                workspace_root=workspace_root,
                display_label="real-hf-cli-skill-dogfood",
                memory_domain_id=f"u_hf_cli_dogfood_{uuid4().hex[:12]}",
            ),
            host_session_id=f"host:hf-cli-dogfood:{uuid4().hex[:12]}",
            conversation_id=f"conversation:hf-cli-dogfood:{uuid4().hex[:12]}",
            model_role=ModelRole.FLASH,
            options=LLMOptions(),
            memory_reflection=False,
            system_prompt=_system_prompt(),
            permission_policy=preset_to_policy(PermissionMode.BYPASS_PERMISSIONS),
        )
        result = await session.run_turn(
            (
                "$hf-cli 请使用 Hugging Face CLI 下载 "
                f"{REPO_ID} 到当前 workspace 的 {DOWNLOAD_DIR}/ 目录，确认下载成功后删除这个目录。"
                f"完成后最终回答中必须包含 {DOWNLOAD_SENTINEL} 和 {DELETE_SENTINEL}。"
            ),
            active_skill_names=frozenset({"hf-cli"}),
        )
        events = list(
            session.wiring.runtime_wiring.event_log.iter(run_id=result.state.run_id)
        )
        terminal_commands = _terminal_commands(events)
        terminal_output = "".join(
            event.delta
            for event in events
            if isinstance(event, ToolResultTextDeltaEvent)
        )
        return {
            "status": result.status.value,
            "stop_reason": result.stop_reason,
            "final_text": result.final_text,
            "tool_names": [
                event.tool_call_name
                for event in events
                if isinstance(event, ToolCallStartEvent)
            ],
            "terminal_commands": terminal_commands,
            "terminal_output": terminal_output,
            "terminal_process_used": any(
                isinstance(event, ToolCallStartEvent)
                and event.tool_call_name == "terminal_process"
                for event in events
            ),
            "download_dir_exists_after": (workspace_root / DOWNLOAD_DIR).exists(),
        }
    finally:
        if session is not None:
            await core.close_session(session.host_session_id, close_conversation=True)
        await core.shutdown()
        await asyncio.sleep(0)


def _write_hf_cli_skill(workspace_root: Path) -> None:
    skill_dir = workspace_root / ".agents" / "skills" / "hf-cli"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"""---
name: hf-cli
description: Use the Hugging Face Hub CLI (`hf`) for model repository download and cache/local-dir workflows.
suggested_tools: [terminal]
required_binaries: [hf]
external_services: [huggingface]
network_required: true
auth_required: optional
cli_usage_kind: read
---
# Hugging Face CLI dogfood helper

Use the `hf` command, not the deprecated `huggingface-cli`.

For model repository downloads, use:

```bash
hf download REPO_ID --local-dir LOCAL_DIR
```

For this dogfood, the exact repository is `{REPO_ID}` and the local directory is `{DOWNLOAD_DIR}`.
The repository is small enough for a full download.

Use one compound `terminal` command for the download/verify/delete mutation. If Pulsara returns a
managed process id because the command is still running, continue observing that same process with
`terminal_process` (`poll`, `log`, or `wait`) until it finishes. A bounded read-only follow-up
verification is acceptable, but never repeat `hf download` or `rm -rf`.

If network access to Hugging Face is flaky in the user's local shell, prefix the command with:

```bash
source ~/.zshrc && proxy_on >/dev/null 2>&1 || true
```

After downloading, verify at least `README.md` exists, list the downloaded files, delete `{DOWNLOAD_DIR}`,
verify the directory no longer exists, and print both sentinels from the terminal command itself.
The terminal output is the evidence source for this dogfood. Do not only mention the sentinels in
your final answer.

Recommended shell shape:

```bash
hf download {REPO_ID} --local-dir {DOWNLOAD_DIR} && \
test -f {DOWNLOAD_DIR}/README.md && \
echo {DOWNLOAD_SENTINEL} && \
rm -rf {DOWNLOAD_DIR} && \
test ! -d {DOWNLOAD_DIR} && \
echo {DELETE_SENTINEL}
```

Terminal output must contain:

- `{DOWNLOAD_SENTINEL}`
- `{DELETE_SENTINEL}`
""",
        encoding="utf-8",
    )


def _system_prompt() -> str:
    return f"""
You are Pulsara running a real Hugging Face CLI active-skill dogfood.

Rules:
- Use the active hf-cli skill as your CLI usage guide.
- Use one compound terminal command for all download/delete mutations.
- Work only inside the current workspace.
- Download {REPO_ID} into relative directory {DOWNLOAD_DIR}/.
- Verify the download happened before deleting it.
- Delete {DOWNLOAD_DIR}/ before your final answer.
- If the compound command yields a managed process id, use terminal_process to observe/wait for
  that same process until completion. A bounded read-only follow-up verification is acceptable;
  never repeat the download or deletion mutation.
- The terminal command output itself must contain these exact evidence markers after successful verification:
  {DOWNLOAD_SENTINEL}
  {DELETE_SENTINEL}
- Do not invent these markers in the final answer unless they appeared in terminal output.
- In the final answer, repeat these evidence markers if the terminal command succeeded:
  {DOWNLOAD_SENTINEL}
  {DELETE_SENTINEL}
""".strip()


def _terminal_commands(events) -> list[str]:
    names_by_call: dict[str, str] = {}
    deltas_by_call: dict[str, list[str]] = {}
    for event in events:
        if isinstance(event, ToolCallStartEvent):
            names_by_call[event.tool_call_id] = event.tool_call_name
        elif isinstance(event, ToolCallDeltaEvent):
            deltas_by_call.setdefault(event.tool_call_id, []).append(event.delta)
    commands: list[str] = []
    for tool_call_id, deltas in deltas_by_call.items():
        if names_by_call.get(tool_call_id) != "terminal":
            continue
        try:
            payload = json.loads("".join(deltas))
        except json.JSONDecodeError:
            continue
        command = payload.get("command")
        if isinstance(command, str):
            commands.append(command)
    return commands


def _settings() -> PulsaraSettings:
    env_file = os.getenv("PULSARA_REAL_LLM_ENV_FILE")
    if env_file:
        return PulsaraSettings.from_env_file(env_file)
    path = Path(".env")
    if path.exists():
        return PulsaraSettings.from_env_file(path)
    return PulsaraSettings.from_env()


def _connect_or_skip(dsn: str) -> None:
    try:
        with psycopg.connect(dsn, connect_timeout=2) as connection:
            with connection.cursor() as cursor:
                cursor.execute("select 1")
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres is not available at configured DSN: {exc}")
