from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from pulsara_agent.host import HostCore, HostWorkspaceInput
from pulsara_agent.llm import ModelRole
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.runtime.permission import preset_to_policy
from pulsara_agent.settings import PulsaraSettings


pytestmark = pytest.mark.real_llm


FIRST_SENTINEL = "PULSARA_RESUME_FIRST_OK"
SECOND_SENTINEL = "PULSARA_RESUME_SECOND_OK"


def test_real_llm_resume_reopens_durable_thread_and_preserves_context(tmp_path) -> None:
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")

    settings = _settings()
    _connect_or_skip(settings.storage.postgres_dsn)
    result = asyncio.run(_run_real_resume_dogfood(settings, tmp_path))

    assert result["runtime_session_id"].startswith("runtime:")
    assert result["first_host_session_id"] != result["second_host_session_id"]
    assert FIRST_SENTINEL in result["first_text"]
    assert SECOND_SENTINEL in result["second_text"]
    assert result["event_count_after_resume"] > result["event_count_after_first"]


async def _run_real_resume_dogfood(settings: PulsaraSettings, tmp_path: Path) -> dict[str, object]:
    runtime_session_id: str | None = None
    first_host_session_id: str | None = None
    second_host_session_id: str | None = None
    workspace = HostWorkspaceInput(
        workspace_kind="project",
        workspace_root=tmp_path,
        display_label="real-resume-dogfood",
        memory_domain_id=f"u_resume_real_{uuid4().hex}",
    )
    policy = preset_to_policy(PermissionMode.READ_ONLY)
    system_prompt = f"""
You are validating Pulsara conversation resume.
Follow these rules exactly:
- If the user says this is resume round one, reply with exactly: {FIRST_SENTINEL}
- If the user asks after resume whether you remember round one, and the prior conversation contains {FIRST_SENTINEL}, reply with exactly: {SECOND_SENTINEL}
- Do not call tools for this test.
""".strip()

    first_core = HostCore(settings=settings)
    try:
        first = await first_core.open_session(
            workspace,
            model_role=ModelRole.FLASH,
            system_prompt=system_prompt,
            permission_policy=policy,
        )
        first_host_session_id = first.host_session_id
        first_result = await first.run_turn(
            f"This is resume round one. Reply exactly {FIRST_SENTINEL}."
        )
        runtime_session_id = first.runtime_session_id
        event_count_after_first = len(first.wiring.runtime_wiring.event_log.iter())
    finally:
        await first_core.shutdown()

    assert runtime_session_id is not None
    second_core = HostCore(settings=settings)
    try:
        resumed = await second_core.resume_session(
            runtime_session_id,
            model_role=ModelRole.FLASH,
            system_prompt=system_prompt,
            permission_policy=policy,
        )
        second_host_session_id = resumed.host_session_id
        second_result = await resumed.run_turn(
            "After resume, do you remember the exact sentinel from round one? "
            f"If yes, reply exactly {SECOND_SENTINEL}."
        )
        event_count_after_resume = len(resumed.wiring.runtime_wiring.event_log.iter())
        await second_core.close_session(resumed.host_session_id, close_conversation=True)
    finally:
        await second_core.shutdown()
        _delete_session(settings.storage.postgres_dsn, runtime_session_id)

    return {
        "runtime_session_id": runtime_session_id,
        "first_host_session_id": first_host_session_id,
        "second_host_session_id": second_host_session_id,
        "first_text": first_result.final_text,
        "second_text": second_result.final_text,
        "event_count_after_first": event_count_after_first,
        "event_count_after_resume": event_count_after_resume,
    }


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


def _delete_session(dsn: str, runtime_session_id: str) -> None:
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute("delete from sessions where id = %s", (runtime_session_id,))
