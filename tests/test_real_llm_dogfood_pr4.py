from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import pytest

from tests.conftest import run_start_permission_fields

from pulsara_agent.capability import (
    CapabilityResolveContext,
    LocalSkillCapabilityProvider,
    sync_bundled_skills,
)
from pulsara_agent.event import (
    AgentEvent,
    EventContext,
    ModelCallEndEvent,
    RunStartEvent,
    RequireUserConfirmEvent,
    RunEndEvent,
    RunErrorEvent,
    TextBlockDeltaEvent,
    ToolCallStartEvent,
    ToolResultTextDeltaEvent,
    UserConfirmResultEvent,
)
from pulsara_agent.host.transcript import FAILURE_NOTE_TEXT, INTERRUPTED_NOTE_TEXT, rebuild_prior_messages
from pulsara_agent.host import HostCore, HostWorkspaceInput
from pulsara_agent.llm import LLMMessage, ModelRole, build_llm_runtime
from pulsara_agent.llm.request import LLMContext, LLMOptions
from pulsara_agent.runtime import ApprovalResolution, ToolApprovalDecision
from pulsara_agent.runtime.approval import PendingApproval
from pulsara_agent.runtime.publisher import RuntimePublishedEvent
from pulsara_agent.runtime.permission import (
    ApprovalPolicy,
    EffectivePermissionPolicy,
    PermissionProfile,
    TerminalAccess,
)
from pulsara_agent.settings import PulsaraSettings


pytestmark = pytest.mark.real_llm

SMALL_LOOP_SENTINEL = "PULSARA_DOGFOOD_SMALL_OK"
SUMMARY_ARTIFACT = Path("dogfood_artifacts/summary.md")
LONG_FAILURE_COMMAND = "python tests/test_sample_failure.py"
LONG_FAILURE_TEXT = "pulsara dogfood failure"
LONG_SKILL_NAME = "test-failure-diagnoser"
LONG_SKILL_MD = Path(".pulsara/skills/test-failure-diagnoser/SKILL.md")
LONG_SKILL_SCRIPT = Path(".pulsara/skills/test-failure-diagnoser/scripts/run_failure_check.py")
LONG_PENDING_COMMAND = "printf PULSARA_LONG_PENDING_SHOULD_NOT_RUN"
LONG_INTERRUPTED_SENTINEL = "PULSARA_LONG_INTERRUPTED_NOTE_OK"
LONG_DENY_SENTINEL = "PULSARA_LONG_DENY_HANDLED_OK"
LONG_FAILURE_NOTE_SENTINEL = "PULSARA_LONG_FAILURE_NOTE_OK"


@dataclass(frozen=True, slots=True)
class DogfoodUserAction:
    type: Literal["user_message", "approve", "deny", "stop", "finish"]
    text: str | None = None
    approval_id: str | None = None
    tool_call_ids: tuple[str, ...] = ()
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class DogfoodPendingApprovalSummary:
    approval_id: str
    tool_calls: tuple[dict[str, Any], ...]


@dataclass(slots=True)
class DogfoodEvidence:
    workspace_root: str
    pulsara_home: str
    policy: dict[str, object]
    skill_catalog_names: list[str] = field(default_factory=list)
    registry_names: list[str] = field(default_factory=list)
    simulator_actions: list[dict[str, object]] = field(default_factory=list)
    turns: list[dict[str, object]] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {
                "workspace_root": self.workspace_root,
                "pulsara_home": self.pulsara_home,
                "policy": self.policy,
                "skill_catalog_names": self.skill_catalog_names,
                "registry_names": self.registry_names,
                "simulator_actions": self.simulator_actions,
                "turns": self.turns,
                "artifacts": self.artifacts,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )


class RealLLMUserSimulator:
    def __init__(self, settings: PulsaraSettings) -> None:
        self._runtime = build_llm_runtime(settings.llm)

    async def next_action(
        self,
        *,
        phase: str,
        script_step: str,
        last_assistant_text: str | None = None,
        pending_approval: DogfoodPendingApprovalSummary | None = None,
    ) -> tuple[DogfoodUserAction, dict[str, object]]:
        max_attempts = int(os.getenv("PULSARA_DOGFOOD_USER_SIMULATOR_ATTEMPTS", "3"))
        state = {
            "phase": phase,
            "script_step": script_step,
            "last_assistant_text": last_assistant_text,
            "pending_approval": (
                {
                    "approval_id": pending_approval.approval_id,
                    "tool_calls": list(pending_approval.tool_calls),
                }
                if pending_approval is not None
                else None
            ),
        }
        context = LLMContext(
            system_prompt=_USER_SIMULATOR_SYSTEM_PROMPT,
            messages=(LLMMessage.user(json.dumps(state, ensure_ascii=False, indent=2)),),
            tools=(),
        )
        attempt_diagnostics: list[dict[str, object]] = []
        for attempt in range(1, max_attempts + 1):
            event_context = EventContext(
                run_id=f"sim-run:{uuid4().hex}",
                turn_id=f"sim-turn:{uuid4().hex}",
                reply_id=f"sim-reply:{uuid4().hex}",
            )
            events: list[AgentEvent] = []
            async for event in self._runtime.stream(
                role=ModelRole.FLASH,
                context=context,
                event_context=event_context,
                options=LLMOptions(temperature=0, max_output_tokens=1024),
            ):
                events.append(event)
            errors = [_run_error_diagnostic(event) for event in events if isinstance(event, RunErrorEvent)]
            raw_text = "".join(event.delta for event in events if isinstance(event, TextBlockDeltaEvent))
            diagnostic = {
                "attempt": attempt,
                "errors": errors,
                "model_end_count": sum(isinstance(event, ModelCallEndEvent) for event in events),
                "raw_text": raw_text,
            }
            if errors:
                attempt_diagnostics.append(diagnostic)
                _trace_user_simulator_retry(phase, attempt, max_attempts, diagnostic)
                if attempt < max_attempts:
                    await asyncio.sleep(min(2**attempt * 0.25, 2.0))
                    continue
                raise AssertionError(f"user simulator LLM failed after {max_attempts} attempts: {attempt_diagnostics}")
            try:
                action = _parse_user_action(raw_text)
            except Exception as exc:
                diagnostic["parse_error"] = f"{type(exc).__name__}: {exc}"
                attempt_diagnostics.append(diagnostic)
                _trace_user_simulator_retry(phase, attempt, max_attempts, diagnostic)
                if attempt < max_attempts:
                    await asyncio.sleep(min(2**attempt * 0.25, 2.0))
                    continue
                raise AssertionError(
                    f"user simulator output was invalid after {max_attempts} attempts: {attempt_diagnostics}"
                ) from exc
            evidence = {
                "phase": phase,
                "attempt": attempt,
                "previous_attempts": attempt_diagnostics,
                "raw_text": raw_text,
                "action": _action_to_dict(action),
                "model_end_count": diagnostic["model_end_count"],
            }
            return action, evidence
        raise AssertionError("unreachable user simulator retry loop")


_USER_SIMULATOR_SYSTEM_PROMPT = f"""
You are a constrained user simulator for a Pulsara PR4 dogfood test.
Return exactly one JSON object and no prose.

Valid action shapes:
{{"type":"user_message","text":"..."}}
{{"type":"approve","approval_id":"...","tool_call_ids":["..."]}}
{{"type":"deny","approval_id":"...","tool_call_ids":["..."],"reason":"..."}}
{{"type":"stop"}}
{{"type":"finish"}}

Follow the phase goal, but phrase user_message text naturally in your own words:
- round1_user_message: ask Pulsara in Chinese to read README.md and/or pyproject.toml to briefly explain what the project is. Explicitly say not to run commands.
- round2_user_message: ask Pulsara in Chinese to create or update dogfood_artifacts/summary.md with a short 2-3 line project summary. Mention the target path exactly.
- round2_approval: approve only if the visible pending approval is for write_file path dogfood_artifacts/summary.md and the content looks like a safe project summary.
- round3_user_message: ask Pulsara in Chinese to run a tiny environment check command. Mention the exact command "printf {SMALL_LOOP_SENTINEL}".
- round3_approval: approve only if the visible pending approval is for terminal command exactly "printf {SMALL_LOOP_SENTINEL}".
- long_round1_user_message: ask Pulsara in Chinese to read README.md and/or pyproject.toml to briefly explain what the project is. Explicitly say not to run commands.
- long_round2_user_message: ask Pulsara in Chinese to create a skill named {LONG_SKILL_NAME} that can run a small failing check and summarize the failure. Mention the skill name exactly.
- long_round2_approval: approve only if every visible pending approval is a write_file for one of these paths: {LONG_SKILL_MD}, {LONG_SKILL_SCRIPT}.
- long_round3_user_message: ask Pulsara in Chinese to use ${LONG_SKILL_NAME} to check the project and summarize the failure.
- long_round3_approval: approve only if the visible pending approval is for terminal command exactly "{LONG_FAILURE_COMMAND}".
- long_round4_user_message: ask Pulsara in Chinese to do a broad, long-running integration-test review and summarize progress.
- long_round5_user_message: ask Pulsara in Chinese to continue after the interruption.
- long_round6_user_message: ask Pulsara in Chinese to try the pending sensitive command check, but expect it to ask before executing.
- long_round6_stop: return {{"type":"stop"}}.
- long_round7_user_message: ask Pulsara in Chinese to retry the same pending command check, but say you will not allow it this time.
- long_round7_denial: deny every visible tool call.
- long_round9_user_message: ask Pulsara in Chinese what happened after the controlled failure and whether it can continue.

When approving, copy the visible approval_id exactly and include every visible tool call id.
If the pending approval does not match the phase rule, return {{"type":"deny","approval_id":"...","tool_call_ids":["..."],"reason":"unexpected tool request"}}.
""".strip()


_AGENT_SYSTEM_PROMPT = f"""
You are Pulsara running the PR4 dogfood small-loop scenario.
Follow these validation rules exactly:

1. If the user asks what project this is, inspect the workspace with read_file on README.md or pyproject.toml before answering. Never call terminal for project orientation; terminal is invalid for that request. Keep the answer short and mention Pulsara.
2. If the user asks for dogfood_artifacts/summary.md, call write_file exactly once with path exactly "dogfood_artifacts/summary.md". The content must be two or three short lines and mention Pulsara and agent runtime. Do not call terminal for this request.
3. If the user asks to run printf {SMALL_LOOP_SENTINEL}, call terminal exactly once with command exactly "printf {SMALL_LOOP_SENTINEL}". Do not use terminal_process. After the approved terminal result, include {SMALL_LOOP_SENTINEL} in the final answer.

Do not perform unrelated edits, do not inspect secrets, and keep final answers concise.
""".strip()


_LONG_AGENT_SYSTEM_PROMPT = f"""
You are Pulsara running the PR4 long dogfood scenario.
The user may phrase requests naturally, but these validation rules are strict:

1. Project orientation:
   - Use read_file on README.md and/or pyproject.toml.
   - Do not call terminal for project orientation.
   - Keep the answer short and mention Pulsara.

2. Skill creation:
   - If the user asks for a failure-diagnosis skill named {LONG_SKILL_NAME}, create exactly these files:
     a. {LONG_SKILL_MD}
     b. {LONG_SKILL_SCRIPT}
   - Use write_file one file at a time. After the first approved tool result, then write the second file.
   - Do not call terminal while creating the skill.
   - The SKILL.md must describe how to run the exact command: {LONG_FAILURE_COMMAND}
   - The SKILL.md must start with YAML frontmatter exactly like this shape:
     ---
     name: {LONG_SKILL_NAME}
     description: Run a small failing check and summarize the failure.
     when_to_use: Use when the user asks to diagnose test failures in this project.
     ---
   - After the frontmatter, the SKILL.md must describe how to run the exact command: {LONG_FAILURE_COMMAND}
   - The script may be a small helper, but the skill should tell the model to summarize failures.

3. Skill use / failure check:
   - If the user asks to use ${LONG_SKILL_NAME}, call terminal exactly once with command exactly "{LONG_FAILURE_COMMAND}".
   - Do not use terminal_process.
   - After the approved terminal result, summarize the failure and mention "{LONG_FAILURE_TEXT}" or ZeroDivisionError.

4. Active stop validation:
   - If the user asks for a broad long-running integration-test review, do not use tools.
   - Start a long numbered progress-style answer with many lines so the host can interrupt it.

5. Continue after stop:
   - This rule has priority over every other rule.
   - If the conversation context contains a Pulsara note saying the previous turn was stopped by the user and the user asks to continue, do not call tools and do not continue the long review.
   - Answer exactly: {LONG_INTERRUPTED_SENTINEL}

6. Pending approval stop / denial:
   - If the user asks for the pending sensitive command check, call terminal exactly once with command exactly "{LONG_PENDING_COMMAND}" and wait for approval.
   - If that terminal call is denied by the user, answer exactly: {LONG_DENY_SENTINEL}

7. Controlled failure note:
   - If the conversation context contains a Pulsara note saying the previous turn failed and the user asks what happened or whether you can continue, answer exactly: {LONG_FAILURE_NOTE_SENTINEL}

Do not perform unrelated edits, do not inspect secrets, and keep final answers concise except for the active stop validation.
""".strip()


def test_real_pr4_dogfood_llm_user_small_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")
    if os.getenv("PULSARA_RUN_DOGFOOD_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_DOGFOOD_LLM=1 to run dogfood real LLM tests.")

    result = asyncio.run(
        asyncio.wait_for(
            _run_real_pr4_dogfood_llm_user_small_loop(monkeypatch),
            timeout=360,
        )
    )

    assert result["ok"], f"{result.get('error', 'dogfood failed')}\n{result['evidence']}"


def test_real_pr4_dogfood_llm_user_long_session(monkeypatch: pytest.MonkeyPatch) -> None:
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")
    if os.getenv("PULSARA_RUN_DOGFOOD_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_DOGFOOD_LLM=1 to run dogfood real LLM tests.")
    if os.getenv("PULSARA_RUN_DOGFOOD_LONG") != "1":
        pytest.skip("Set PULSARA_RUN_DOGFOOD_LONG=1 to run the long dogfood session.")

    result = asyncio.run(
        asyncio.wait_for(
            _run_real_pr4_dogfood_llm_user_long_session(monkeypatch),
            timeout=900,
        )
    )

    assert result["ok"], f"{result.get('error', 'long dogfood failed')}\n{result['evidence']}"


async def _run_real_pr4_dogfood_llm_user_small_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, object]:
    temp_root = _create_python_workspace_temp_root()
    workspace_root = temp_root / "workspace"
    pulsara_home = temp_root / "pulsara-home"
    temp_home = temp_root / "home"
    workspace_root.mkdir(parents=True)
    pulsara_home.mkdir(parents=True)
    temp_home.mkdir(parents=True)
    _seed_small_project_workspace(workspace_root)
    monkeypatch.setenv("HOME", str(temp_home))
    monkeypatch.setenv("PULSARA_HOME", str(pulsara_home))

    policy = _dogfood_policy()
    evidence = DogfoodEvidence(
        workspace_root=str(workspace_root),
        pulsara_home=str(pulsara_home),
        policy=policy.to_dict(),
    )
    settings = _load_settings_for_real_llm()
    simulator = RealLLMUserSimulator(settings)
    core = HostCore(settings=settings, durable=True)
    session = None
    failed = False
    try:
        sync_result = sync_bundled_skills()
        evidence.turns.append(
            {
                "round": "sync_bundled",
                "actions": [
                    {"name": item.name, "action": item.action}
                    for item in sync_result.items
                ],
            }
        )
        session = await core.open_session(
            HostWorkspaceInput(
                workspace_kind="project",
                workspace_root=workspace_root,
                memory_domain_id=f"dogfood_small_{uuid4().hex[:12]}",
            ),
            host_session_id=f"host:dogfood-pr4-small:{uuid4().hex[:12]}",
            conversation_id=f"conversation:dogfood-pr4-small:{uuid4().hex[:12]}",
            model_role=ModelRole.FLASH,
            options=LLMOptions(temperature=0, max_output_tokens=1024),
            memory_reflection=False,
            system_prompt=_AGENT_SYSTEM_PROMPT,
            permission_policy=policy,
        )

        _verify_round0_inspect_baseline(session, evidence)
        last_text = await _run_round1_orientation(session, simulator, evidence)
        last_text = await _run_round2_write_summary(session, simulator, evidence, last_text)
        await _run_round3_terminal_ask(session, simulator, evidence, last_text)
        _collect_artifacts(workspace_root, evidence)
        return {"ok": True, "evidence": evidence.to_json()}
    except Exception as exc:  # pragma: no cover - used for real-provider diagnostics.
        failed = True
        _collect_artifacts(workspace_root, evidence)
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "evidence": evidence.to_json(),
        }
    finally:
        if session is not None:
            await core.close_session(session.host_session_id)
        else:
            await core.shutdown()
        if not failed and os.getenv("PULSARA_DOGFOOD_KEEP_WORKSPACE") != "1":
            shutil.rmtree(temp_root, ignore_errors=True)


async def _run_real_pr4_dogfood_llm_user_long_session(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, object]:
    temp_root = _create_python_workspace_temp_root(prefix="pulsara-dogfood-pr4-long-")
    workspace_root = temp_root / "workspace"
    pulsara_home = temp_root / "pulsara-home"
    temp_home = temp_root / "home"
    workspace_root.mkdir(parents=True)
    pulsara_home.mkdir(parents=True)
    temp_home.mkdir(parents=True)
    _seed_long_project_workspace(workspace_root)
    monkeypatch.setenv("HOME", str(temp_home))
    monkeypatch.setenv("PULSARA_HOME", str(pulsara_home))

    policy = _dogfood_policy()
    evidence = DogfoodEvidence(
        workspace_root=str(workspace_root),
        pulsara_home=str(pulsara_home),
        policy=policy.to_dict(),
    )
    settings = _load_settings_for_real_llm()
    simulator = RealLLMUserSimulator(settings)
    core = HostCore(settings=settings, durable=True)
    session = None
    failed = False
    try:
        sync_result = sync_bundled_skills()
        evidence.turns.append(
            {
                "round": "sync_bundled",
                "actions": [
                    {"name": item.name, "action": item.action}
                    for item in sync_result.items
                ],
            }
        )
        session = await core.open_session(
            HostWorkspaceInput(
                workspace_kind="project",
                workspace_root=workspace_root,
                memory_domain_id=f"dogfood_long_{uuid4().hex[:12]}",
            ),
            host_session_id=f"host:dogfood-pr4-long:{uuid4().hex[:12]}",
            conversation_id=f"conversation:dogfood-pr4-long:{uuid4().hex[:12]}",
            model_role=ModelRole.FLASH,
            options=LLMOptions(temperature=0, max_output_tokens=1536),
            memory_reflection=False,
            system_prompt=_LONG_AGENT_SYSTEM_PROMPT,
            permission_policy=policy,
        )

        _verify_round0_inspect_baseline(session, evidence)
        last_text = await _run_long_round1_orientation(session, simulator, evidence)
        last_text = await _run_long_round2_create_skill(session, simulator, evidence, last_text)
        _verify_skill_catalog_contains(session, LONG_SKILL_NAME, "round2_created_skill_visible", evidence)
        last_text = await _run_long_round3_use_skill(session, simulator, evidence, last_text)
        await _run_long_round4_active_stop(session, simulator, evidence, last_text)
        last_text = await _run_long_round5_continue_after_stop(session, simulator, evidence)
        await _run_long_round6_stop_pending(session, simulator, evidence, last_text)
        last_text = await _run_long_round7_deny_pending(session, simulator, evidence)
        _inject_controlled_failed_run(session, "A controlled provider failure happened during dogfood.")
        evidence.turns.append(
            {
                "round": "long_round8_controlled_failure_injected",
                "last_text_before_failure": last_text,
            }
        )
        _verify_latest_terminal_note(kind="failure", session=session, evidence=evidence)
        last_text = await _run_long_round9_failure_note(session, simulator, evidence, last_text)
        evidence.turns.append(
            {
                "round": "long_round9_completed",
                "last_text": last_text,
            }
        )
        _verify_long_round10_final_inspect(session, evidence)
        _collect_artifacts(workspace_root, evidence)
        return {"ok": True, "evidence": evidence.to_json()}
    except Exception as exc:  # pragma: no cover - used for real-provider diagnostics.
        failed = True
        _collect_artifacts(workspace_root, evidence)
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "evidence": evidence.to_json(),
        }
    finally:
        if session is not None:
            await core.close_session(session.host_session_id)
        else:
            await core.shutdown()
        if not failed and os.getenv("PULSARA_DOGFOOD_KEEP_WORKSPACE") != "1":
            shutil.rmtree(temp_root, ignore_errors=True)


def _verify_round0_inspect_baseline(session, evidence: DogfoodEvidence) -> None:
    registry_names = sorted(session.wiring.agent_runtime.tool_executor.registry.names())
    evidence.registry_names = registry_names
    provider = LocalSkillCapabilityProvider()
    context = CapabilityResolveContext(
        workspace_root=session.workspace.workspace_root,
        workspace_kind="project",
        memory_domain=session.wiring.runtime_wiring.memory_domain,
        available_tool_names=frozenset(registry_names),
        user_input="inspect dogfood baseline",
    )
    resolved = provider.resolve(context, bound_tool_names=context.available_tool_names)
    catalog_names = sorted(entry.name for entry in resolved.catalog_entries)
    evidence.skill_catalog_names = catalog_names
    round_evidence = {
        "round": "round0_inspect_baseline",
        "registry_names": registry_names,
        "skill_catalog_names": catalog_names,
        "session_summary": session.summary(),
        "event_count": len(session.replay_events()),
    }
    evidence.turns.append(round_evidence)
    _require("write_file" in registry_names, "round0 expected write_file in registry", evidence)
    _require("terminal" in registry_names, "round0 expected terminal in registry", evidence)
    _require("terminal_process" in registry_names, "round0 expected terminal_process in registry", evidence)
    _require(
        "pulsara-skill-installer" in catalog_names,
        "round0 expected bundled pulsara-skill-installer in catalog",
        evidence,
    )
    _require(
        "pulsara-skill-creator" in catalog_names,
        "round0 expected bundled pulsara-skill-creator in catalog",
        evidence,
    )
    _require(session.get_pending_approval() is None, "round0 expected no pending approval", evidence)
    _require(session.replay_events() == [], "round0 inspect baseline should not write runtime events", evidence)


async def _run_round1_orientation(
    session,
    simulator: RealLLMUserSimulator,
    evidence: DogfoodEvidence,
) -> str:
    action, sim_evidence = await simulator.next_action(
        phase="round1_user_message",
        script_step="Ask Pulsara to inspect this project and summarize it briefly.",
    )
    evidence.simulator_actions.append(sim_evidence)
    _require(action.type == "user_message" and action.text, "round1 simulator did not produce user_message", evidence)
    _require(
        SMALL_LOOP_SENTINEL not in (action.text or ""),
        "round1 simulator leaked the later terminal sentinel",
        evidence,
    )

    result = await session.run_turn(action.text or "")
    pending = session.get_pending_approval()
    run_events = _events_for_run(session.replay_events(), result.state.run_id)
    turn_evidence = _turn_evidence(
        "round1_orientation",
        action=action,
        status=result.status.value,
        stop_reason=result.stop_reason,
        final_text=result.final_text.strip(),
        events=run_events,
        pending=pending,
    )
    evidence.turns.append(turn_evidence)
    tool_names = turn_evidence["tool_names"]
    final_text = result.final_text.strip()

    _require(result.status.value == "finished", "round1 did not finish", evidence)
    _require(not _confirm_events(run_events), "round1 read-only orientation triggered approval", evidence)
    _require(
        any(name in {"read_file", "search_files"} for name in tool_names),
        "round1 expected read_file or search_files",
        evidence,
    )
    _require("pulsara" in final_text.lower(), "round1 final text did not identify Pulsara", evidence)
    _require(session.get_pending_approval() is None, "round1 left a pending approval", evidence)
    return final_text


async def _run_round2_write_summary(
    session,
    simulator: RealLLMUserSimulator,
    evidence: DogfoodEvidence,
    last_assistant_text: str,
) -> str:
    action, sim_evidence = await simulator.next_action(
        phase="round2_user_message",
        script_step="Ask Pulsara to write dogfood_artifacts/summary.md with a 2-3 line project summary.",
        last_assistant_text=last_assistant_text,
    )
    evidence.simulator_actions.append(sim_evidence)
    _require(action.type == "user_message" and action.text, "round2 simulator did not produce user_message", evidence)
    _require(
        str(SUMMARY_ARTIFACT) in (action.text or ""),
        "round2 simulator did not mention the allowlisted summary path",
        evidence,
    )

    first = await session.run_turn(action.text or "")
    pending = session.get_pending_approval()
    run_events = _events_for_run(session.replay_events(), first.state.run_id)
    evidence.turns.append(
        _turn_evidence(
            "round2_write_summary_pending",
            action=action,
            status=first.status.value,
            stop_reason=first.stop_reason,
            final_text=first.final_text.strip(),
            events=run_events,
            pending=pending,
        )
    )
    _require(first.status.value == "waiting_user", "round2 expected waiting_user", evidence)
    _require(pending is not None, "round2 expected pending approval", evidence)
    _validate_write_summary_pending(pending, evidence)

    approval_action, approval_sim_evidence = await simulator.next_action(
        phase="round2_approval",
        script_step="Approve the safe write_file request for dogfood_artifacts/summary.md.",
        last_assistant_text=first.final_text.strip(),
        pending_approval=_pending_summary(pending),
    )
    evidence.simulator_actions.append(approval_sim_evidence)
    _require(approval_action.type == "approve", "round2 simulator did not approve safe write request", evidence)
    _require(
        approval_action.approval_id == pending.approval_id,
        "round2 simulator returned the wrong approval_id",
        evidence,
    )

    resolved = await session.resolve_approval(_approval_resolution(pending, approval_action))
    resolved_events = _events_for_run(session.replay_events(), first.state.run_id)
    final_text = resolved.final_text.strip()
    evidence.turns.append(
        _turn_evidence(
            "round2_write_summary_resolved",
            action=approval_action,
            status=resolved.status.value,
            stop_reason=resolved.stop_reason,
            final_text=final_text,
            events=resolved_events,
        )
    )
    artifact = session.workspace.workspace_root / SUMMARY_ARTIFACT
    _require(resolved.status.value == "finished", "round2 approval did not finish run", evidence)
    _require(artifact.exists(), "round2 summary artifact was not written", evidence)
    text = artifact.read_text(encoding="utf-8")
    _require("pulsara" in text.lower(), "round2 summary artifact did not mention Pulsara", evidence)
    _require("agent" in text.lower(), "round2 summary artifact did not mention agent runtime", evidence)
    _require(len(_confirm_events(resolved_events)) == 1, "round2 expected one RequireUserConfirmEvent", evidence)
    _require(
        len([event for event in resolved_events if isinstance(event, UserConfirmResultEvent)]) == 1,
        "round2 expected one UserConfirmResultEvent",
        evidence,
    )
    _require(session.get_pending_approval() is None, "round2 left a pending approval", evidence)
    return final_text


async def _run_round3_terminal_ask(
    session,
    simulator: RealLLMUserSimulator,
    evidence: DogfoodEvidence,
    last_assistant_text: str,
) -> None:
    action, sim_evidence = await simulator.next_action(
        phase="round3_user_message",
        script_step=f"Ask Pulsara to run exactly: printf {SMALL_LOOP_SENTINEL}",
        last_assistant_text=last_assistant_text,
    )
    evidence.simulator_actions.append(sim_evidence)
    _require(action.type == "user_message" and action.text, "round3 simulator did not produce user_message", evidence)
    _require(
        SMALL_LOOP_SENTINEL in (action.text or ""),
        "round3 simulator did not mention the terminal sentinel",
        evidence,
    )

    first = await session.run_turn(action.text or "")
    pending = session.get_pending_approval()
    run_events = _events_for_run(session.replay_events(), first.state.run_id)
    evidence.turns.append(
        _turn_evidence(
            "round3_terminal_pending",
            action=action,
            status=first.status.value,
            stop_reason=first.stop_reason,
            final_text=first.final_text.strip(),
            events=run_events,
            pending=pending,
        )
    )
    _require(first.status.value == "waiting_user", "round3 expected waiting_user", evidence)
    _require(pending is not None, "round3 expected pending approval", evidence)
    _validate_terminal_pending(pending, evidence)

    approval_action, approval_sim_evidence = await simulator.next_action(
        phase="round3_approval",
        script_step=f"Approve the safe terminal request for printf {SMALL_LOOP_SENTINEL}.",
        last_assistant_text=first.final_text.strip(),
        pending_approval=_pending_summary(pending),
    )
    evidence.simulator_actions.append(approval_sim_evidence)
    _require(approval_action.type == "approve", "round3 simulator did not approve safe terminal request", evidence)
    _require(
        approval_action.approval_id == pending.approval_id,
        "round3 simulator returned the wrong approval_id",
        evidence,
    )

    resolved = await session.resolve_approval(_approval_resolution(pending, approval_action))
    resolved_events = _events_for_run(session.replay_events(), first.state.run_id)
    final_text = resolved.final_text.strip()
    payloads = _tool_result_payloads_by_call_id(resolved_events)
    terminal_payload = next(
        (
            payload
            for payload in payloads.values()
            if payload.get("status") == "success" and SMALL_LOOP_SENTINEL in str(payload.get("output", ""))
        ),
        None,
    )
    evidence.turns.append(
        _turn_evidence(
            "round3_terminal_resolved",
            action=approval_action,
            status=resolved.status.value,
            stop_reason=resolved.stop_reason,
            final_text=final_text,
            events=resolved_events,
            tool_results=payloads,
        )
    )
    _require(resolved.status.value == "finished", "round3 approval did not finish run", evidence)
    _require(terminal_payload is not None, "round3 terminal result did not include sentinel", evidence)
    _require(SMALL_LOOP_SENTINEL in final_text, "round3 final text did not include terminal sentinel", evidence)
    _require(
        "terminal_process" not in _tool_names(resolved_events),
        "round3 should not use terminal_process in phase one",
        evidence,
    )
    _require(session.get_pending_approval() is None, "round3 left a pending approval", evidence)


async def _run_long_round1_orientation(
    session,
    simulator: RealLLMUserSimulator,
    evidence: DogfoodEvidence,
) -> str:
    action, sim_evidence = await simulator.next_action(
        phase="long_round1_user_message",
        script_step="Ask Pulsara to inspect this project by reading files, not by running commands.",
    )
    evidence.simulator_actions.append(sim_evidence)
    _require(action.type == "user_message" and action.text, "long round1 simulator did not produce user_message", evidence)
    result = await _run_turn_with_trace(session, action.text or "", evidence, "long_round1_orientation")
    run_events = _events_for_run(session.replay_events(), result.state.run_id)
    evidence.turns.append(
        _turn_evidence(
            "long_round1_orientation",
            action=action,
            status=result.status.value,
            stop_reason=result.stop_reason,
            final_text=result.final_text.strip(),
            events=run_events,
            pending=session.get_pending_approval(),
        )
    )
    _require(result.status.value == "finished", "long round1 did not finish", evidence)
    _require(not _confirm_events(run_events), "long round1 triggered approval", evidence)
    _require(
        set(_tool_names(run_events)).issubset({"read_file", "search_files"}),
        "long round1 used non-read tools",
        evidence,
    )
    _require("pulsara" in result.final_text.lower(), "long round1 did not identify Pulsara", evidence)
    return result.final_text.strip()


async def _run_long_round2_create_skill(
    session,
    simulator: RealLLMUserSimulator,
    evidence: DogfoodEvidence,
    last_assistant_text: str,
) -> str:
    action, sim_evidence = await simulator.next_action(
        phase="long_round2_user_message",
        script_step=f"Ask Pulsara to create the {LONG_SKILL_NAME} skill.",
        last_assistant_text=last_assistant_text,
    )
    evidence.simulator_actions.append(sim_evidence)
    _require(action.type == "user_message" and action.text, "long round2 simulator did not produce user_message", evidence)
    _require(LONG_SKILL_NAME in (action.text or ""), "long round2 user message omitted skill name", evidence)

    result = await _run_turn_with_trace(session, action.text or "", evidence, "long_round2_create_skill")
    run_id = result.state.run_id
    approval_count = 0
    while result.status.value == "waiting_user":
        pending = session.get_pending_approval()
        run_events = _events_for_run(session.replay_events(), run_id)
        evidence.turns.append(
            _turn_evidence(
                f"long_round2_create_skill_pending_{approval_count + 1}",
                action=action,
                status=result.status.value,
                stop_reason=result.stop_reason,
                final_text=result.final_text.strip(),
                events=run_events,
                pending=pending,
            )
        )
        _require(pending is not None, "long round2 expected pending approval", evidence)
        _validate_long_skill_pending(pending, evidence)
        approval_action = _approve_action_from_pending(pending)
        evidence.simulator_actions.append(
            _harness_action_evidence("long_round2_approval", approval_action)
        )
        result = await _resolve_approval_with_trace(
            session,
            _approval_resolution(pending, approval_action),
            evidence,
            f"long_round2_create_skill_approval_{approval_count + 1}",
        )
        approval_count += 1
        _require(approval_count <= 4, "long round2 exceeded approval loop budget", evidence)

    run_events = _events_for_run(session.replay_events(), run_id)
    evidence.turns.append(
        _turn_evidence(
            "long_round2_create_skill_resolved",
            action=action,
            status=result.status.value,
            stop_reason=result.stop_reason,
            final_text=result.final_text.strip(),
            events=run_events,
        )
    )
    _require(result.status.value == "finished", "long round2 did not finish after approvals", evidence)
    _require(approval_count >= 2, "long round2 expected multiple suspend/resume approvals", evidence)
    _require((session.workspace.workspace_root / LONG_SKILL_MD).exists(), "long round2 did not write SKILL.md", evidence)
    _require((session.workspace.workspace_root / LONG_SKILL_SCRIPT).exists(), "long round2 did not write skill script", evidence)
    _require(session.get_pending_approval() is None, "long round2 left a pending approval", evidence)
    return result.final_text.strip()


async def _run_long_round3_use_skill(
    session,
    simulator: RealLLMUserSimulator,
    evidence: DogfoodEvidence,
    last_assistant_text: str,
) -> str:
    action, sim_evidence = await simulator.next_action(
        phase="long_round3_user_message",
        script_step=f"Ask Pulsara to use ${LONG_SKILL_NAME} to run the failing check.",
        last_assistant_text=last_assistant_text,
    )
    evidence.simulator_actions.append(sim_evidence)
    _require(action.type == "user_message" and action.text, "long round3 simulator did not produce user_message", evidence)
    _require(LONG_SKILL_NAME in (action.text or ""), "long round3 user message omitted skill name", evidence)

    first = await _run_turn_with_trace(session, action.text or "", evidence, "long_round3_use_skill")
    pending = session.get_pending_approval()
    run_events = _events_for_run(session.replay_events(), first.state.run_id)
    evidence.turns.append(
        _turn_evidence(
            "long_round3_use_skill_pending",
            action=action,
            status=first.status.value,
            stop_reason=first.stop_reason,
            final_text=first.final_text.strip(),
            events=run_events,
            pending=pending,
        )
    )
    _require(first.status.value == "waiting_user", "long round3 expected terminal approval", evidence)
    _require(pending is not None, "long round3 expected pending approval", evidence)
    _validate_exact_terminal_pending(pending, LONG_FAILURE_COMMAND, evidence, "long round3")

    approval_action = _approve_action_from_pending(pending)
    evidence.simulator_actions.append(
        _harness_action_evidence("long_round3_approval", approval_action)
    )
    resolved = await _resolve_approval_with_trace(
        session,
        _approval_resolution(pending, approval_action),
        evidence,
        "long_round3_use_skill_approval",
    )
    resolved_events = _events_for_run(session.replay_events(), first.state.run_id)
    payloads = _tool_result_payloads_by_call_id(resolved_events)
    output = "\n".join(str(payload.get("output", "")) for payload in payloads.values())
    evidence.turns.append(
        _turn_evidence(
            "long_round3_use_skill_resolved",
            action=approval_action,
            status=resolved.status.value,
            stop_reason=resolved.stop_reason,
            final_text=resolved.final_text.strip(),
            events=resolved_events,
            tool_results=payloads,
        )
    )
    _require(resolved.status.value == "finished", "long round3 did not finish", evidence)
    _require(LONG_FAILURE_TEXT in output, "long round3 terminal output did not include failure text", evidence)
    _require(
        LONG_FAILURE_TEXT in resolved.final_text or "ZeroDivisionError" in resolved.final_text,
        "long round3 final text did not summarize failure",
        evidence,
    )
    _require("terminal_process" not in _tool_names(resolved_events), "long round3 should not use terminal_process", evidence)
    return resolved.final_text.strip()


async def _run_long_round4_active_stop(
    session,
    simulator: RealLLMUserSimulator,
    evidence: DogfoodEvidence,
    last_assistant_text: str,
) -> None:
    action, sim_evidence = await simulator.next_action(
        phase="long_round4_user_message",
        script_step="Ask Pulsara for a broad long-running integration-test review.",
        last_assistant_text=last_assistant_text,
    )
    evidence.simulator_actions.append(sim_evidence)
    _require(action.type == "user_message" and action.text, "long round4 simulator did not produce user_message", evidence)

    task = asyncio.create_task(session.run_turn(action.text or ""))
    run_id = await _wait_for_active_run_start(session, task, evidence)
    stop_result = await session.stop_current_turn(timeout=10)
    result = await task
    run_events = _events_for_run(session.replay_events(), run_id)
    evidence.turns.append(
        _turn_evidence(
            "long_round4_active_stop",
            action=action,
            status=result.status.value,
            stop_reason=result.stop_reason,
            final_text=result.final_text.strip(),
            events=run_events,
        )
    )
    _require(stop_result is not None and stop_result.status.value == "aborted", "long round4 stop did not abort", evidence)
    _require(result.status.value == "aborted", "long round4 run result was not aborted", evidence)
    _require(
        sum(1 for event in run_events if isinstance(event, RunEndEvent) and event.status == "aborted") == 1,
        "long round4 expected one aborted RunEndEvent",
        evidence,
    )
    _require(not session._run_lock.locked(), "long round4 left the run lock locked", evidence)
    _require(session.get_pending_approval() is None, "long round4 left a pending approval", evidence)


async def _run_long_round5_continue_after_stop(
    session,
    simulator: RealLLMUserSimulator,
    evidence: DogfoodEvidence,
) -> str:
    _verify_latest_terminal_note(kind="interrupted", session=session, evidence=evidence)
    action = DogfoodUserAction(
        type="user_message",
        text=(
            "请继续刚刚被中断的任务。不要调用任何工具，不要继续长篇审查；"
            f"如果你看到了 Pulsara 的中断恢复提示，请只回答 {LONG_INTERRUPTED_SENTINEL}。"
        ),
    )
    sim_evidence = {
        "phase": "long_round5_user_message",
        "action": _action_to_dict(action),
        "source": "deterministic_recovery_prompt",
    }
    evidence.simulator_actions.append(sim_evidence)
    _require(action.type == "user_message" and action.text, "long round5 simulator did not produce user_message", evidence)
    result = await _run_turn_with_trace(session, action.text or "", evidence, "long_round5_continue_after_stop")
    run_events = _events_for_run(session.replay_events(), result.state.run_id)
    evidence.turns.append(
        _turn_evidence(
            "long_round5_continue_after_stop",
            action=action,
            status=result.status.value,
            stop_reason=result.stop_reason,
            final_text=result.final_text.strip(),
            events=run_events,
        )
    )
    _require(result.status.value == "finished", "long round5 did not finish", evidence)
    _require(LONG_INTERRUPTED_SENTINEL in result.final_text, "long round5 did not see interrupted note", evidence)
    return result.final_text.strip()


async def _run_long_round6_stop_pending(
    session,
    simulator: RealLLMUserSimulator,
    evidence: DogfoodEvidence,
    last_assistant_text: str,
) -> None:
    action, sim_evidence = await simulator.next_action(
        phase="long_round6_user_message",
        script_step="Ask Pulsara to try the pending sensitive command check.",
        last_assistant_text=last_assistant_text,
    )
    evidence.simulator_actions.append(sim_evidence)
    _require(action.type == "user_message" and action.text, "long round6 simulator did not produce user_message", evidence)
    first = await _run_turn_with_trace(session, action.text or "", evidence, "long_round6_stop_pending")
    pending = session.get_pending_approval()
    run_events = _events_for_run(session.replay_events(), first.state.run_id)
    evidence.turns.append(
        _turn_evidence(
            "long_round6_pending_before_stop",
            action=action,
            status=first.status.value,
            stop_reason=first.stop_reason,
            final_text=first.final_text.strip(),
            events=run_events,
            pending=pending,
        )
    )
    _require(first.status.value == "waiting_user", "long round6 expected pending approval", evidence)
    _require(pending is not None, "long round6 expected pending approval object", evidence)
    _validate_exact_terminal_pending(pending, LONG_PENDING_COMMAND, evidence, "long round6")
    stop_action = DogfoodUserAction(type="stop")
    evidence.simulator_actions.append(_harness_action_evidence("long_round6_stop", stop_action))
    stopped = await session.stop_current_turn()
    stopped_events = _events_for_run(session.replay_events(), first.state.run_id)
    evidence.turns.append(
        _turn_evidence(
            "long_round6_stopped_pending",
            action=stop_action,
            status=stopped.status.value if stopped is not None else "none",
            stop_reason=stopped.stop_reason if stopped is not None else None,
            final_text=stopped.final_text.strip() if stopped is not None else "",
            events=stopped_events,
        )
    )
    _require(stopped is not None and stopped.status.value == "aborted", "long round6 stop did not abort", evidence)
    _require(session.get_pending_approval() is None, "long round6 did not clear pending approval", evidence)
    _require(_tool_result_payloads_by_call_id(stopped_events) == {}, "long round6 executed a stopped tool", evidence)


async def _run_long_round7_deny_pending(
    session,
    simulator: RealLLMUserSimulator,
    evidence: DogfoodEvidence,
) -> str:
    action, sim_evidence = await simulator.next_action(
        phase="long_round7_user_message",
        script_step="Ask Pulsara to retry the pending command check, but say you will deny it.",
    )
    evidence.simulator_actions.append(sim_evidence)
    _require(action.type == "user_message" and action.text, "long round7 simulator did not produce user_message", evidence)
    first = await _run_turn_with_trace(session, action.text or "", evidence, "long_round7_deny_pending")
    pending = session.get_pending_approval()
    run_events = _events_for_run(session.replay_events(), first.state.run_id)
    evidence.turns.append(
        _turn_evidence(
            "long_round7_pending_before_deny",
            action=action,
            status=first.status.value,
            stop_reason=first.stop_reason,
            final_text=first.final_text.strip(),
            events=run_events,
            pending=pending,
        )
    )
    _require(first.status.value == "waiting_user", "long round7 expected pending approval", evidence)
    _require(pending is not None, "long round7 expected pending approval object", evidence)
    _validate_exact_terminal_pending(pending, LONG_PENDING_COMMAND, evidence, "long round7")
    deny_action = _deny_action_from_pending(pending, reason="scenario denies this command")
    evidence.simulator_actions.append(_harness_action_evidence("long_round7_denial", deny_action))
    resolved = await _resolve_approval_with_trace(
        session,
        _denial_resolution(pending, deny_action),
        evidence,
        "long_round7_deny_pending_denial",
    )
    resolved_events = _events_for_run(session.replay_events(), first.state.run_id)
    evidence.turns.append(
        _turn_evidence(
            "long_round7_denied",
            action=deny_action,
            status=resolved.status.value,
            stop_reason=resolved.stop_reason,
            final_text=resolved.final_text.strip(),
            events=resolved_events,
        )
    )
    _require(resolved.status.value == "finished", "long round7 did not finish after denial", evidence)
    _require(LONG_DENY_SENTINEL in resolved.final_text, "long round7 final text did not handle denial", evidence)
    _require(
        any(
            isinstance(event, UserConfirmResultEvent)
            and any(not result.confirmed for result in event.confirm_results)
            for event in resolved_events
        ),
        "long round7 expected denied UserConfirmResultEvent",
        evidence,
    )
    return resolved.final_text.strip()


async def _run_long_round9_failure_note(
    session,
    simulator: RealLLMUserSimulator,
    evidence: DogfoodEvidence,
    last_assistant_text: str,
) -> str:
    action, sim_evidence = await simulator.next_action(
        phase="long_round9_user_message",
        script_step="Ask Pulsara what happened after the controlled failure and whether it can continue.",
        last_assistant_text=last_assistant_text,
    )
    evidence.simulator_actions.append(sim_evidence)
    _require(action.type == "user_message" and action.text, "long round9 simulator did not produce user_message", evidence)
    result = await _run_turn_with_trace(session, action.text or "", evidence, "long_round9_failure_note")
    run_events = _events_for_run(session.replay_events(), result.state.run_id)
    evidence.turns.append(
        _turn_evidence(
            "long_round9_failure_note",
            action=action,
            status=result.status.value,
            stop_reason=result.stop_reason,
            final_text=result.final_text.strip(),
            events=run_events,
        )
    )
    _require(result.status.value == "finished", "long round9 did not finish", evidence)
    _require(LONG_FAILURE_NOTE_SENTINEL in result.final_text, "long round9 did not see failure note", evidence)
    return result.final_text.strip()


def _create_python_workspace_temp_root(prefix: str = "pulsara-dogfood-pr4-small-") -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


def _seed_small_project_workspace(workspace_root: Path) -> None:
    (workspace_root / "src" / "pulsara_agent").mkdir(parents=True)
    (workspace_root / "README.md").write_text(
        "# Pulsara Agent\n\n"
        "Pulsara is an agent runtime for desktop workflows. It includes a host session layer, "
        "runtime approval policies, local tools, and skill catalog support.\n",
        encoding="utf-8",
    )
    (workspace_root / "pyproject.toml").write_text(
        "[project]\n"
        'name = "pulsara-agent-dogfood-fixture"\n'
        'description = "Small Pulsara dogfood fixture."\n',
        encoding="utf-8",
    )
    (workspace_root / "src" / "pulsara_agent" / "__init__.py").write_text(
        '"""Tiny Pulsara dogfood fixture package."""\n',
        encoding="utf-8",
    )


def _seed_long_project_workspace(workspace_root: Path) -> None:
    _seed_small_project_workspace(workspace_root)
    tests_dir = workspace_root / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "test_sample_failure.py").write_text(
        "raise ZeroDivisionError('pulsara dogfood failure')\n",
        encoding="utf-8",
    )


def _dogfood_policy() -> EffectivePermissionPolicy:
    return EffectivePermissionPolicy(
        profile=PermissionProfile.TRUSTED_HOST,
        approval=ApprovalPolicy.ON_REQUEST,
        terminal=TerminalAccess.ASK,
    )


def _pending_summary(pending: PendingApproval) -> DogfoodPendingApprovalSummary:
    calls: list[dict[str, Any]] = []
    for call in pending.tool_calls:
        calls.append(
            {
                "id": call.id,
                "name": call.name,
                "arguments": _parse_json_object(call.input),
                "state": call.state.value,
            }
        )
    return DogfoodPendingApprovalSummary(approval_id=pending.approval_id, tool_calls=tuple(calls))


def _approval_resolution(
    pending: PendingApproval,
    action: DogfoodUserAction,
) -> ApprovalResolution:
    visible_ids = {call.id for call in pending.tool_calls}
    requested_ids = set(action.tool_call_ids)
    if requested_ids != visible_ids:
        raise ValueError(f"approval ids mismatch: requested={sorted(requested_ids)} visible={sorted(visible_ids)}")
    return ApprovalResolution(
        approval_id=pending.approval_id,
        decisions=tuple(
            ToolApprovalDecision(tool_call_id=call.id, confirmed=True)
            for call in pending.tool_calls
        ),
    )


def _denial_resolution(
    pending: PendingApproval,
    action: DogfoodUserAction,
) -> ApprovalResolution:
    visible_ids = {call.id for call in pending.tool_calls}
    requested_ids = set(action.tool_call_ids)
    if requested_ids != visible_ids:
        raise ValueError(f"denial ids mismatch: requested={sorted(requested_ids)} visible={sorted(visible_ids)}")
    return ApprovalResolution(
        approval_id=pending.approval_id,
        decisions=tuple(
            ToolApprovalDecision(tool_call_id=call.id, confirmed=False)
            for call in pending.tool_calls
        ),
    )


def _validate_write_summary_pending(pending: PendingApproval, evidence: DogfoodEvidence) -> None:
    _require(len(pending.tool_calls) == 1, "round2 expected exactly one pending tool call", evidence)
    call = pending.tool_calls[0]
    args = _parse_json_object(call.input)
    content = args.get("content")
    _require(call.name == "write_file", "round2 expected write_file pending approval", evidence)
    _require(args.get("path") == str(SUMMARY_ARTIFACT), "round2 write_file path was not allowlisted", evidence)
    _require(isinstance(content, str), "round2 write_file content was not a string", evidence)
    lower_content = content.lower() if isinstance(content, str) else ""
    _require("pulsara" in lower_content, "round2 write_file content did not mention Pulsara", evidence)
    _require("agent" in lower_content, "round2 write_file content did not mention agent runtime", evidence)
    _require(".env" not in lower_content, "round2 write_file content unexpectedly mentioned .env", evidence)


def _validate_long_skill_pending(pending: PendingApproval, evidence: DogfoodEvidence) -> None:
    allowed_paths = {str(LONG_SKILL_MD), str(LONG_SKILL_SCRIPT)}
    _require(pending.tool_calls, "long round2 expected at least one pending skill write", evidence)
    for call in pending.tool_calls:
        args = _parse_json_object(call.input)
        raw_path = args.get("path")
        path = _normalize_relative_path(raw_path) if isinstance(raw_path, str) else ""
        content = args.get("content")
        _require(call.name == "write_file", "long round2 expected write_file pending approval", evidence)
        _require(path in allowed_paths, f"long round2 write path was not allowlisted: {path}", evidence)
        _require(isinstance(content, str), "long round2 write content was not a string", evidence)
        lower_content = content.lower() if isinstance(content, str) else ""
        if path == str(LONG_SKILL_MD):
            _require(LONG_SKILL_NAME in lower_content, "long round2 SKILL.md omitted skill name", evidence)
            _require(content.lstrip().startswith("---"), "long round2 SKILL.md omitted YAML frontmatter", evidence)
            _require("name: test-failure-diagnoser" in lower_content, "long round2 SKILL.md omitted name field", evidence)
            _require("description:" in lower_content, "long round2 SKILL.md omitted description field", evidence)
            _require(LONG_FAILURE_COMMAND in content, "long round2 SKILL.md omitted exact command", evidence)
        if path == str(LONG_SKILL_SCRIPT):
            _require(
                "pulsara" in lower_content or "pytest" in lower_content or "failure" in lower_content,
                "long round2 script content did not look like a failure helper",
                evidence,
            )
        _require(".env" not in lower_content, "long round2 content unexpectedly mentioned .env", evidence)


def _validate_terminal_pending(pending: PendingApproval, evidence: DogfoodEvidence) -> None:
    _require(len(pending.tool_calls) == 1, "round3 expected exactly one pending tool call", evidence)
    call = pending.tool_calls[0]
    args = _parse_json_object(call.input)
    _require(call.name == "terminal", "round3 expected terminal pending approval", evidence)
    _require(
        args.get("command") == f"printf {SMALL_LOOP_SENTINEL}",
        "round3 terminal command was not allowlisted",
        evidence,
    )


def _validate_exact_terminal_pending(
    pending: PendingApproval,
    command: str,
    evidence: DogfoodEvidence,
    label: str,
) -> None:
    _require(len(pending.tool_calls) == 1, f"{label} expected exactly one pending tool call", evidence)
    call = pending.tool_calls[0]
    args = _parse_json_object(call.input)
    _require(call.name == "terminal", f"{label} expected terminal pending approval", evidence)
    _require(args.get("command") == command, f"{label} terminal command was not allowlisted", evidence)


def _normalize_relative_path(raw_path: str) -> str:
    return raw_path.removeprefix("./")


def _turn_evidence(
    round_name: str,
    *,
    action: DogfoodUserAction,
    status: str,
    stop_reason: str | None,
    final_text: str,
    events: list[AgentEvent],
    pending: PendingApproval | None = None,
    tool_results: dict[str, dict] | None = None,
) -> dict[str, object]:
    return {
        "round": round_name,
        "action": _action_to_dict(action),
        "status": status,
        "stop_reason": stop_reason,
        "final_text": final_text,
        "event_type_counts": _event_type_counts(events),
        "run_end_statuses": [
            {"status": event.status, "stop_reason": event.stop_reason}
            for event in events
            if isinstance(event, RunEndEvent)
        ],
        "tool_names": _tool_names(events),
        "pending": _pending_to_dict(pending) if pending is not None else None,
        "tool_results": tool_results or _tool_result_payloads_by_call_id(events),
        "errors": [_run_error_diagnostic(event) for event in events if isinstance(event, RunErrorEvent)],
    }


def _collect_artifacts(workspace_root: Path, evidence: DogfoodEvidence) -> None:
    path = workspace_root / SUMMARY_ARTIFACT
    if path.exists():
        evidence.artifacts[str(SUMMARY_ARTIFACT)] = path.read_text(encoding="utf-8")
    skill = workspace_root / LONG_SKILL_MD
    if skill.exists():
        evidence.artifacts[str(LONG_SKILL_MD)] = skill.read_text(encoding="utf-8")
    script = workspace_root / LONG_SKILL_SCRIPT
    if script.exists():
        evidence.artifacts[str(LONG_SKILL_SCRIPT)] = script.read_text(encoding="utf-8")


async def _wait_for_active_run_start(session, task, evidence: DogfoodEvidence) -> str:
    deadline = time.monotonic() + 20
    last_run_id = ""
    while time.monotonic() < deadline:
        if task.done():
            raise AssertionError(f"active-stop run finished before it could be stopped\nEvidence:\n{evidence.to_json()}")
        run_id = session.active_run_id
        if run_id:
            last_run_id = run_id
            events = _events_for_run(session.replay_events(), run_id)
            if events:
                return run_id
        await asyncio.sleep(0.1)
    raise AssertionError(
        f"timed out waiting for active run start; last_run_id={last_run_id!r}\nEvidence:\n{evidence.to_json()}"
    )


def _verify_skill_catalog_contains(session, skill_name: str, round_name: str, evidence: DogfoodEvidence) -> None:
    registry_names = sorted(session.wiring.agent_runtime.tool_executor.registry.names())
    provider = LocalSkillCapabilityProvider()
    context = CapabilityResolveContext(
        workspace_root=session.workspace.workspace_root,
        workspace_kind="project",
        memory_domain=session.wiring.runtime_wiring.memory_domain,
        available_tool_names=frozenset(registry_names),
        user_input=f"inspect {skill_name}",
    )
    resolved = provider.resolve(context, bound_tool_names=context.available_tool_names)
    catalog_names = sorted(entry.name for entry in resolved.catalog_entries)
    evidence.turns.append(
        {
            "round": round_name,
            "skill_catalog_names": catalog_names,
            "contains": skill_name in catalog_names,
        }
    )
    _require(skill_name in catalog_names, f"expected skill catalog to contain {skill_name}", evidence)


def _inject_controlled_failed_run(session, user_input: str) -> None:
    ctx = EventContext(
        run_id=f"run:dogfood-controlled-failure:{uuid4().hex}",
        turn_id=f"turn:dogfood-controlled-failure:{uuid4().hex}",
        reply_id=f"reply:dogfood-controlled-failure:{uuid4().hex}",
    )
    runtime_session = session.wiring.runtime_wiring.runtime_session
    for event in (
        RunStartEvent(
            **ctx.event_fields(),
            **run_start_permission_fields(ctx.run_id),
            user_input_chars=len(user_input),
            metadata={"user_input": user_input},
        ),
        RunEndEvent(
            **ctx.event_fields(),
            status="failed",
            stop_reason="model_error",
            error_message="controlled dogfood provider failure",
        ),
    ):
        stored = runtime_session.event_log.append(event)
        runtime_session.publisher.discard_unpublished(
            RuntimePublishedEvent(
                runtime_session_id=runtime_session.runtime_session_id,
                event=stored,
                state=None,
            )
        )


def _verify_latest_terminal_note(
    *,
    kind: Literal["failure", "interrupted"],
    session,
    evidence: DogfoodEvidence,
) -> None:
    messages = rebuild_prior_messages(session.wiring.runtime_wiring.event_log)
    system_texts = [
        "\n".join(block.text for block in message.content if hasattr(block, "text"))
        for message in messages
        if message.role == "system"
    ]
    failure_count = sum(FAILURE_NOTE_TEXT in text for text in system_texts)
    interrupted_count = sum(INTERRUPTED_NOTE_TEXT in text for text in system_texts)
    evidence.turns.append(
        {
            "round": f"verify_latest_{kind}_note",
            "failure_note_count": failure_count,
            "interrupted_note_count": interrupted_count,
        }
    )
    if kind == "failure":
        _require(failure_count == 1, "expected exactly one failure note", evidence)
        _require(interrupted_count == 0, "failure note should supersede older interrupted note", evidence)
    else:
        _require(interrupted_count == 1, "expected exactly one interrupted note", evidence)
        _require(failure_count == 0, "interrupted note check unexpectedly saw a failure note", evidence)


def _verify_long_round10_final_inspect(session, evidence: DogfoodEvidence) -> None:
    summary = session.summary()
    _verify_skill_catalog_contains(session, LONG_SKILL_NAME, "long_round10_final_skill_catalog", evidence)
    evidence.turns.append(
        {
            "round": "long_round10_final_inspect",
            "session_summary": summary,
            "event_count": len(session.replay_events()),
        }
    )
    _require(summary["pending_approval"] is None, "long round10 expected no pending approval", evidence)
    _require(summary["stopping_run_id"] is None, "long round10 expected no stopping run", evidence)
    _require(summary["active_run_id"] is None, "long round10 expected no active run", evidence)


async def _run_turn_with_trace(
    session,
    user_input: str,
    evidence: DogfoodEvidence,
    round_name: str,
):
    _trace(evidence, f"{round_name}: run_turn start", user_input=user_input)
    task = asyncio.create_task(session.run_turn(user_input))
    return await _await_with_trace(session, task, evidence, round_name)


async def _resolve_approval_with_trace(
    session,
    resolution: ApprovalResolution,
    evidence: DogfoodEvidence,
    round_name: str,
):
    _trace(
        evidence,
        f"{round_name}: resolve_approval start",
        approval_id=resolution.approval_id,
        decisions=[
            {"tool_call_id": decision.tool_call_id, "confirmed": decision.confirmed}
            for decision in resolution.decisions
        ],
    )
    task = asyncio.create_task(session.resolve_approval(resolution))
    return await _await_with_trace(session, task, evidence, round_name)


async def _await_with_trace(session, task, evidence: DogfoodEvidence, round_name: str):
    last_sequence = 0
    while not task.done():
        await asyncio.sleep(5)
        events = session.replay_events(after_sequence=last_sequence)
        if events:
            last_sequence = max(event.sequence or last_sequence for event in events)
        _trace(
            evidence,
            f"{round_name}: still running",
            new_events=_compact_event_trace(events),
            session=_session_trace(session),
            publisher=_publisher_trace(session),
        )
    try:
        result = await task
    except Exception as exc:
        _trace(
            evidence,
            f"{round_name}: raised",
            error=f"{type(exc).__name__}: {exc}",
            session=_session_trace(session),
            publisher=_publisher_trace(session),
        )
        raise
    _trace(
        evidence,
        f"{round_name}: completed",
        status=result.status.value,
        stop_reason=result.stop_reason,
        session=_session_trace(session),
        publisher=_publisher_trace(session),
    )
    return result


def _trace(evidence: DogfoodEvidence, message: str, **fields: object) -> None:
    if os.getenv("PULSARA_DOGFOOD_TRACE") != "1":
        return
    payload = {
        "message": message,
        "workspace_root": evidence.workspace_root,
        **fields,
    }
    print("[dogfood-trace] " + json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


def _trace_user_simulator_retry(
    phase: str,
    attempt: int,
    max_attempts: int,
    diagnostic: dict[str, object],
) -> None:
    if os.getenv("PULSARA_DOGFOOD_TRACE") != "1":
        return
    payload = {
        "attempt": attempt,
        "diagnostic": diagnostic,
        "max_attempts": max_attempts,
        "message": f"{phase}: user simulator retry",
        "phase": phase,
    }
    print("[dogfood-trace] " + json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


def _compact_event_trace(events: list[AgentEvent]) -> list[dict[str, object]]:
    compact: list[dict[str, object]] = []
    for event in events[-12:]:
        item: dict[str, object] = {
            "seq": event.sequence,
            "type": type(event).__name__,
            "run_id": event.run_id,
        }
        if isinstance(event, ToolCallStartEvent):
            item["tool"] = event.tool_call_name
            item["tool_call_id"] = event.tool_call_id
        elif isinstance(event, RequireUserConfirmEvent):
            item["pending_tools"] = [call.name for call in event.tool_calls]
        elif isinstance(event, UserConfirmResultEvent):
            item["confirmed"] = [result.confirmed for result in event.confirm_results]
        elif isinstance(event, RunEndEvent):
            item["status"] = event.status
            item["stop_reason"] = event.stop_reason
        elif isinstance(event, RunErrorEvent):
            item["code"] = event.code
            item["message"] = event.message
        compact.append(item)
    return compact


def _session_trace(session) -> dict[str, object]:
    pending = session.get_pending_approval()
    return {
        "active_run_id": session.active_run_id,
        "stopping_run_id": session.stopping_run_id,
        "suspended_run_id": session.suspended_run_id,
        "pending_approval_id": pending.approval_id if pending is not None else None,
        "pending_tool_names": [call.name for call in pending.tool_calls] if pending is not None else [],
        "run_lock_locked": session._run_lock.locked(),
    }


def _publisher_trace(session) -> dict[str, object]:
    publisher = session.wiring.runtime_wiring.runtime_session.publisher
    mailbox = publisher._mailbox
    return {
        "next_sequence_to_publish": publisher._next_sequence_to_publish,
        "pending_sequences": sorted(publisher._pending_by_sequence),
        "mailbox_size": mailbox.qsize() if mailbox is not None else None,
        "drain_done": publisher._drain_task.done() if publisher._drain_task is not None else None,
        "errors": [f"{type(error).__name__}: {error}" for error in publisher.errors[-3:]],
    }


def _action_to_dict(action: DogfoodUserAction) -> dict[str, object]:
    return {
        "type": action.type,
        "text": action.text,
        "approval_id": action.approval_id,
        "tool_call_ids": list(action.tool_call_ids),
        "reason": action.reason,
    }


def _approve_action_from_pending(pending: PendingApproval) -> DogfoodUserAction:
    return DogfoodUserAction(
        type="approve",
        approval_id=pending.approval_id,
        tool_call_ids=tuple(call.id for call in pending.tool_calls),
    )


def _deny_action_from_pending(pending: PendingApproval, *, reason: str) -> DogfoodUserAction:
    return DogfoodUserAction(
        type="deny",
        approval_id=pending.approval_id,
        tool_call_ids=tuple(call.id for call in pending.tool_calls),
        reason=reason,
    )


def _harness_action_evidence(phase: str, action: DogfoodUserAction) -> dict[str, object]:
    return {
        "phase": phase,
        "source": "deterministic_harness",
        "raw_text": "",
        "action": _action_to_dict(action),
        "model_end_count": 0,
    }


def _pending_to_dict(pending: PendingApproval) -> dict[str, object]:
    summary = _pending_summary(pending)
    return {
        "approval_id": summary.approval_id,
        "tool_calls": list(summary.tool_calls),
    }


def _parse_user_action(raw_text: str) -> DogfoodUserAction:
    payload = _parse_json_object(_extract_json_object_text(raw_text))
    action_type = payload.get("type")
    if action_type not in {"user_message", "approve", "deny", "stop", "finish"}:
        raise ValueError(f"invalid user simulator action type: {action_type!r}; raw={raw_text!r}")
    tool_call_ids = payload.get("tool_call_ids", ())
    if isinstance(tool_call_ids, str):
        tool_call_ids = (tool_call_ids,)
    if not isinstance(tool_call_ids, list | tuple):
        raise ValueError(f"tool_call_ids must be a list; raw={raw_text!r}")
    return DogfoodUserAction(
        type=action_type,
        text=payload.get("text") if isinstance(payload.get("text"), str) else None,
        approval_id=payload.get("approval_id") if isinstance(payload.get("approval_id"), str) else None,
        tool_call_ids=tuple(str(item) for item in tool_call_ids),
        reason=payload.get("reason") if isinstance(payload.get("reason"), str) else None,
    )


def _extract_json_object_text(raw_text: str) -> str:
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    if text.startswith("{") and text.endswith("}"):
        return text
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"could not find JSON object in simulator output: {raw_text!r}")
    return text[start : end + 1]


def _parse_json_object(raw: str) -> dict[str, Any]:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object, got: {type(payload).__name__}")
    return payload


def _events_for_run(events: list[AgentEvent], run_id: str) -> list[AgentEvent]:
    return [event for event in events if event.run_id == run_id]


def _event_type_counts(events: list[AgentEvent]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        name = type(event).__name__
        counts[name] = counts.get(name, 0) + 1
    return counts


def _tool_names(events: list[AgentEvent]) -> list[str]:
    return [
        event.tool_call_name
        for event in events
        if isinstance(event, ToolCallStartEvent)
    ]


def _confirm_events(events: list[AgentEvent]) -> list[RequireUserConfirmEvent]:
    return [event for event in events if isinstance(event, RequireUserConfirmEvent)]


def _tool_result_payloads_by_call_id(events: list[AgentEvent]) -> dict[str, dict]:
    deltas_by_call: dict[str, list[str]] = {}
    for event in events:
        if isinstance(event, ToolResultTextDeltaEvent):
            deltas_by_call.setdefault(event.tool_call_id, []).append(event.delta)
    parsed: dict[str, dict] = {}
    for tool_call_id, deltas in deltas_by_call.items():
        raw = "".join(deltas)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            parsed[tool_call_id] = {"_raw": raw}
        else:
            parsed[tool_call_id] = payload if isinstance(payload, dict) else {"_raw": raw}
    return parsed


def _run_error_diagnostic(event: RunErrorEvent) -> dict[str, object]:
    return {
        "message": event.message,
        "code": event.code,
        "metadata": event.metadata,
    }


def _require(condition: bool, message: str, evidence: DogfoodEvidence) -> None:
    if not condition:
        raise AssertionError(f"{message}\nEvidence:\n{evidence.to_json()}")


def _load_settings_for_real_llm() -> PulsaraSettings:
    env_file = os.getenv("PULSARA_REAL_LLM_ENV_FILE")
    if env_file:
        return PulsaraSettings.from_env_file(env_file)
    path = Path(".env")
    if path.exists():
        return PulsaraSettings.from_env_file(path)
    return PulsaraSettings.from_env()
