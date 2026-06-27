from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from pulsara_agent.event import (
    AgentEvent,
    ModelCallEndEvent,
    PlanExitRequestedEvent,
    PlanExitResolvedEvent,
    PlanModeEnteredEvent,
    PlanModeExitedEvent,
    PlanQuestionAnsweredEvent,
    PlanQuestionAskedEvent,
    RunEndEvent,
    RunErrorEvent,
    ToolCallDeltaEvent,
    ToolCallStartEvent,
    ToolResultTextDeltaEvent,
)
from pulsara_agent.host import HostCore, HostWorkspaceInput
from pulsara_agent.llm import ModelRole
from pulsara_agent.llm.request import LLMOptions
from pulsara_agent.runtime import PlanExitResolution, PlanQuestionResolution
from pulsara_agent.runtime.permission import PermissionMode, preset_to_policy
from pulsara_agent.settings import PulsaraSettings


pytestmark = pytest.mark.real_llm

MQ_WORKSPACE_ROOT = Path("/Users/plumliu/Desktop/python_workspace/pulsara_mq_test")
TEST_COMMAND = "uv run pytest tests/test_visibility_timeout.py -q"
PLAN_SENTINEL = "PULSARA_PLAN_QUEUE_DOGFOOD_OK"
PLAN_REASON = "visibility timeout dogfood"
PLAN_QUESTION_ANSWER = (
    "Use lazy/passive sweep during take, do not create per-job timers or background workers, "
    "and require a receipt/token when completing a processing job."
)


@dataclass(slots=True)
class PlanDogfoodEvidence:
    workspace_root: str
    policy: dict[str, object]
    registry_names: list[str] = field(default_factory=list)
    turns: list[dict[str, object]] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {
                "workspace_root": self.workspace_root,
                "policy": self.policy,
                "registry_names": self.registry_names,
                "turns": self.turns,
                "artifacts": self.artifacts,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )


def test_real_plan_mode_job_queue_long_dogfood(monkeypatch: pytest.MonkeyPatch) -> None:
    if os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_REAL_LLM=1 to call the configured real LLM provider.")
    if os.getenv("PULSARA_RUN_DOGFOOD_LLM") != "1":
        pytest.skip("Set PULSARA_RUN_DOGFOOD_LLM=1 to run dogfood real LLM tests.")
    if os.getenv("PULSARA_RUN_DOGFOOD_PLAN_LONG") != "1":
        pytest.skip("Set PULSARA_RUN_DOGFOOD_PLAN_LONG=1 to run the long Plan workflow dogfood.")

    result = asyncio.run(
        asyncio.wait_for(
            _run_real_plan_mode_job_queue_long_dogfood(monkeypatch),
            timeout=900,
        )
    )

    assert result["ok"], f"{result.get('error', 'plan dogfood failed')}\n{result['evidence']}"


async def _run_real_plan_mode_job_queue_long_dogfood(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, object]:
    _prepare_mq_workspace(MQ_WORKSPACE_ROOT)
    temp_root = Path(tempfile.mkdtemp(prefix="pulsara-plan-dogfood-"))
    temp_home = temp_root / "home"
    pulsara_home = temp_root / "pulsara-home"
    temp_home.mkdir(parents=True)
    pulsara_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(temp_home))
    monkeypatch.setenv("PULSARA_HOME", str(pulsara_home))

    policy = preset_to_policy(PermissionMode.BYPASS_PERMISSIONS)
    evidence = PlanDogfoodEvidence(
        workspace_root=str(MQ_WORKSPACE_ROOT),
        policy=policy.to_dict(),
    )
    settings = _load_settings_for_real_llm()
    core = HostCore(settings=settings, durable=False, use_workspace_supervisor=False)
    session = None
    failed = False
    try:
        session = await core.open_session(
            HostWorkspaceInput(
                workspace_kind="project",
                workspace_root=MQ_WORKSPACE_ROOT,
                memory_domain_id=f"plan_dogfood_queue_{uuid4().hex[:12]}",
            ),
            host_session_id=f"host:plan-dogfood-queue:{uuid4().hex[:12]}",
            conversation_id=f"conversation:plan-dogfood-queue:{uuid4().hex[:12]}",
            model_role=ModelRole.FLASH,
            options=LLMOptions(temperature=0, max_output_tokens=4096),
            memory_reflection=False,
            system_prompt=_PLAN_DOGFOOD_SYSTEM_PROMPT,
            permission_policy=policy,
        )
        evidence.registry_names = sorted(session.wiring.agent_runtime.tool_executor.registry.names())
        _require({"read_file", "edit_file", "write_file", "terminal", "ask_plan_question", "exit_plan"}.issubset(
            set(evidence.registry_names)
        ), "plan dogfood expected core tools in registry", evidence)

        session.enter_plan(reason=PLAN_REASON)
        _require(session.current_permission_mode is PermissionMode.READ_ONLY, "enter_plan did not switch read-only", evidence)
        first = await _await_with_plan_trace(
            session,
            session.run_turn(_PLAN_DOGFOOD_USER_REQUEST),
            evidence,
            "round1_plan_request",
        )
        last_result = first
        question_count = 0
        exit_count = 0
        for step in range(1, 5):
            pending = session.get_pending_interaction()
            if pending is None:
                break
            if pending.kind == "question":
                question_count += 1
                _require(question_count == 1, "plan dogfood expected exactly one plan question", evidence)
                _record_pending_interaction(evidence, f"round{step}_question_pending", pending)
                last_result = await _await_with_plan_trace(
                    session,
                    session.resolve_plan_interaction(
                        PlanQuestionResolution(
                            interaction_id=pending.interaction_id,
                            answer_text=PLAN_QUESTION_ANSWER,
                            selected_option=None,
                        )
                    ),
                    evidence,
                    f"round{step}_question_answered",
                )
                continue
            if pending.kind == "exit":
                exit_count += 1
                _require(exit_count == 1, "plan dogfood expected exactly one exit_plan request", evidence)
                _record_pending_interaction(evidence, f"round{step}_exit_pending", pending)
                _validate_exit_plan_text(pending.plan_text, evidence)
                last_result = await _await_with_plan_trace(
                    session,
                    session.resolve_plan_interaction(
                        PlanExitResolution(
                            interaction_id=pending.interaction_id,
                            decision="approve",
                            user_feedback=(
                                "Approved. Implement the minimal lazy-sweep receipt-token design and run the exact test command."
                            ),
                        )
                    ),
                    evidence,
                    f"round{step}_exit_approved",
                )
                continue
            raise AssertionError(f"unexpected plan interaction kind: {pending.kind}")

        _require(session.get_pending_interaction() is None, "plan dogfood left pending interaction", evidence)
        _require(question_count == 1, "plan dogfood did not ask exactly one plan question", evidence)
        _require(exit_count == 1, "plan dogfood did not request exit_plan exactly once", evidence)

        events = session.replay_events()
        _collect_artifacts(MQ_WORKSPACE_ROOT, evidence)
        _assert_plan_trajectory(events, session, last_result.final_text.strip(), evidence)
        return {"ok": True, "evidence": evidence.to_json()}
    except Exception as exc:  # pragma: no cover - diagnostics for real-provider dogfood.
        failed = True
        _collect_artifacts(MQ_WORKSPACE_ROOT, evidence)
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
        if not failed and os.getenv("PULSARA_DOGFOOD_RESET_WORKSPACE_ON_SUCCESS") == "1":
            _prepare_mq_workspace(MQ_WORKSPACE_ROOT)


async def _await_with_plan_trace(session, awaitable, evidence: PlanDogfoodEvidence, round_name: str):
    task = asyncio.create_task(awaitable)
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
        )
    try:
        result = await task
    except Exception as exc:
        _trace(
            evidence,
            f"{round_name}: raised",
            error=f"{type(exc).__name__}: {exc}",
            session=_session_trace(session),
        )
        raise
    run_events = _events_for_run(session.replay_events(), result.state.run_id)
    evidence.turns.append(
        {
            "round": round_name,
            "status": result.status.value,
            "stop_reason": result.stop_reason,
            "final_text": result.final_text.strip(),
            "tool_names": _tool_names(run_events),
            "errors": [_run_error_diagnostic(event) for event in run_events if isinstance(event, RunErrorEvent)],
            "event_type_counts": _event_type_counts(run_events),
            "tool_results": _tool_result_payloads_by_call_id(run_events),
            "session": _session_trace(session),
        }
    )
    _trace(
        evidence,
        f"{round_name}: completed",
        status=result.status.value,
        stop_reason=result.stop_reason,
        session=_session_trace(session),
    )
    return result


def _prepare_mq_workspace(workspace_root: Path) -> None:
    if not workspace_root.exists():
        raise AssertionError(f"workspace does not exist: {workspace_root}")
    pyproject = workspace_root / "pyproject.toml"
    if not pyproject.exists():
        raise AssertionError(f"workspace is missing pyproject.toml: {workspace_root}")
    if not (workspace_root / ".venv").exists():
        raise AssertionError(f"workspace is missing .venv: {workspace_root}")
    project = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    dependencies = _dependency_names(project.get("project", {}).get("dependencies", []))
    dev_dependencies = _dependency_names(project.get("dependency-groups", {}).get("dev", []))
    missing = {"fastapi", "uvicorn"} - dependencies
    if missing:
        raise AssertionError(f"workspace dependencies missing: {sorted(missing)}")
    if "pytest" not in dev_dependencies:
        raise AssertionError("workspace dev dependencies missing pytest; run `uv add --dev pytest` in pulsara_mq_test")

    (workspace_root / "tests").mkdir(parents=True, exist_ok=True)
    (workspace_root / "README.md").write_text(_FIXTURE_README, encoding="utf-8")
    (workspace_root / "main.py").write_text(_FIXTURE_MAIN, encoding="utf-8")
    (workspace_root / "tests" / "test_visibility_timeout.py").write_text(
        _FIXTURE_VISIBILITY_TEST,
        encoding="utf-8",
    )


def _dependency_names(dependencies: object) -> set[str]:
    names: set[str] = set()
    if not isinstance(dependencies, list):
        return names
    for item in dependencies:
        if not isinstance(item, str):
            continue
        name = re.split(r"[\[<>=!~; ]", item.strip(), maxsplit=1)[0].lower()
        if name:
            names.add(name)
    return names


def _validate_exit_plan_text(plan_text: str, evidence: PlanDogfoodEvidence) -> None:
    text = plan_text.lower()
    _require(any(term in text for term in ("lazy", "passive", "sweep", "惰性", "扫描")), "exit_plan omitted lazy/passive sweep", evidence)
    _require(any(term in text for term in ("receipt", "token", "凭证", "令牌")), "exit_plan omitted receipt/token", evidence)
    _require(
        any(term in text for term in ("locked", "deadline", "expires", "expire", "过期", "超时")),
        "exit_plan omitted locked/deadline timestamp",
        evidence,
    )
    _require("main.py" in text, "exit_plan omitted main.py", evidence)
    _require(TEST_COMMAND in plan_text, "exit_plan omitted exact test command", evidence)


def _assert_plan_trajectory(events: list[AgentEvent], session, final_text: str, evidence: PlanDogfoodEvidence) -> None:
    entered = [event for event in events if isinstance(event, PlanModeEnteredEvent)]
    exited = [event for event in events if isinstance(event, PlanModeExitedEvent)]
    questions = [event for event in events if isinstance(event, PlanQuestionAskedEvent)]
    answers = [event for event in events if isinstance(event, PlanQuestionAnsweredEvent)]
    exit_requests = [event for event in events if isinstance(event, PlanExitRequestedEvent)]
    exit_resolutions = [event for event in events if isinstance(event, PlanExitResolvedEvent)]
    _require(entered, "missing PlanModeEnteredEvent", evidence)
    _require(entered[0].source == "user", "PlanModeEnteredEvent source was not user", evidence)
    _require(
        entered[0].previous_permission_mode == PermissionMode.BYPASS_PERMISSIONS.value,
        "PlanModeEnteredEvent did not capture bypass previous mode",
        evidence,
    )
    _require(len(questions) == 1, "expected exactly one PlanQuestionAskedEvent", evidence)
    _require(len(answers) == 1, "expected exactly one PlanQuestionAnsweredEvent", evidence)
    _require(len(exit_requests) == 1, "expected exactly one PlanExitRequestedEvent", evidence)
    _require(
        any(event.decision == "approve" for event in exit_resolutions),
        "missing approved PlanExitResolvedEvent",
        evidence,
    )
    _require(
        len([event for event in exited if event.source == "approved_exit_plan"]) == 1,
        "missing approved PlanModeExitedEvent",
        evidence,
    )
    exit_sequence = min(event.sequence or 0 for event in exited if event.source == "approved_exit_plan")
    calls = _tool_calls(events)
    side_effecting = {"write_file", "edit_file", "terminal", "terminal_process"}
    pre_exit_side_effects = [
        call
        for call in calls
        if call["sequence"] < exit_sequence and call["name"] in side_effecting
    ]
    _require(not pre_exit_side_effects, f"side-effecting tools ran before plan exit: {pre_exit_side_effects}", evidence)
    post_exit_calls = [call for call in calls if call["sequence"] > exit_sequence]
    _require(
        any(call["name"] in {"write_file", "edit_file"} for call in post_exit_calls),
        "missing post-approval code modification tool",
        evidence,
    )
    terminal_commands = [
        call["arguments"].get("command")
        for call in post_exit_calls
        if call["name"] == "terminal" and isinstance(call["arguments"], dict)
    ]
    _require(TEST_COMMAND in terminal_commands, f"missing exact terminal test command: {terminal_commands}", evidence)
    _require(session.plan_state.active is False, "plan state remained active after approval", evidence)
    _require(
        session.current_permission_mode is PermissionMode.BYPASS_PERMISSIONS,
        "permission mode did not restore to bypass",
        evidence,
    )
    _require(PLAN_SENTINEL in final_text, "final answer omitted plan dogfood sentinel", evidence)
    _assert_fixture_static_shape(MQ_WORKSPACE_ROOT, evidence)


def _assert_fixture_static_shape(workspace_root: Path, evidence: PlanDogfoodEvidence) -> None:
    source = (workspace_root / "main.py").read_text(encoding="utf-8")
    forbidden = ("threading.Timer", "asyncio.create_task", "time.sleep(", "BackgroundTasks", "setTimeout")
    offenders = [token for token in forbidden if token in source]
    _require(not offenders, f"implementation used forbidden active timer pattern: {offenders}", evidence)
    _require(any(token in source.lower() for token in ("receipt", "token")), "implementation omitted receipt/token", evidence)
    _require(
        any(token in source.lower() for token in ("locked", "deadline", "expire", "timeout")),
        "implementation omitted lock/deadline timestamp",
        evidence,
    )


def _tool_calls(events: list[AgentEvent]) -> list[dict[str, object]]:
    deltas_by_id: dict[str, list[str]] = {}
    calls: list[dict[str, object]] = []
    for event in events:
        if isinstance(event, ToolCallDeltaEvent):
            deltas_by_id.setdefault(event.tool_call_id, []).append(event.delta)
        elif isinstance(event, ToolCallStartEvent):
            calls.append(
                {
                    "id": event.tool_call_id,
                    "name": event.tool_call_name,
                    "sequence": event.sequence or 0,
                    "arguments": {},
                }
            )
    for call in calls:
        raw = "".join(deltas_by_id.get(str(call["id"]), ()))
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {"_raw": raw}
        call["arguments"] = parsed if isinstance(parsed, dict) else {"_raw": raw}
    return calls


def _record_pending_interaction(evidence: PlanDogfoodEvidence, round_name: str, pending) -> None:
    evidence.turns.append(
        {
            "round": round_name,
            "pending": pending.to_dict(),
        }
    )


def _collect_artifacts(workspace_root: Path, evidence: PlanDogfoodEvidence) -> None:
    for relative in ("README.md", "main.py", "tests/test_visibility_timeout.py"):
        path = workspace_root / relative
        if path.exists():
            text = path.read_text(encoding="utf-8")
            evidence.artifacts[relative] = text[:8000]


def _compact_event_trace(events: list[AgentEvent]) -> list[dict[str, object]]:
    compact: list[dict[str, object]] = []
    for event in events[-20:]:
        item: dict[str, object] = {
            "seq": event.sequence,
            "type": type(event).__name__,
            "run_id": event.run_id,
        }
        if isinstance(event, ToolCallStartEvent):
            item["tool"] = event.tool_call_name
            item["tool_call_id"] = event.tool_call_id
        elif isinstance(event, PlanQuestionAskedEvent):
            item["question"] = event.question
        elif isinstance(event, PlanExitRequestedEvent):
            item["summary"] = event.summary
        elif isinstance(event, RunEndEvent):
            item["status"] = event.status
            item["stop_reason"] = event.stop_reason
        elif isinstance(event, RunErrorEvent):
            item["code"] = event.code
            item["message"] = event.message
        compact.append(item)
    return compact


def _session_trace(session) -> dict[str, object]:
    pending = session.get_pending_interaction()
    return {
        "summary": session.summary(),
        "permission_mode": (
            session.current_permission_mode.value if session.current_permission_mode is not None else None
        ),
        "plan": session.plan_state.to_dict(),
        "pending_interaction": pending.to_dict() if pending is not None else None,
        "event_count": len(session.replay_events()),
    }


def _event_type_counts(events: list[AgentEvent]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        name = type(event).__name__
        counts[name] = counts.get(name, 0) + 1
    return counts


def _tool_names(events: list[AgentEvent]) -> list[str]:
    return [event.tool_call_name for event in events if isinstance(event, ToolCallStartEvent)]


def _events_for_run(events: list[AgentEvent], run_id: str) -> list[AgentEvent]:
    return [event for event in events if event.run_id == run_id]


def _tool_result_payloads_by_call_id(events: list[AgentEvent]) -> dict[str, dict[str, object]]:
    deltas_by_call: dict[str, list[str]] = {}
    for event in events:
        if isinstance(event, ToolResultTextDeltaEvent):
            deltas_by_call.setdefault(event.tool_call_id, []).append(event.delta)
    parsed: dict[str, dict[str, object]] = {}
    for tool_call_id, deltas in deltas_by_call.items():
        raw = "".join(deltas)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            parsed[tool_call_id] = {"_raw": raw[:2000]}
        else:
            parsed[tool_call_id] = payload if isinstance(payload, dict) else {"_raw": raw[:2000]}
    return parsed


def _run_error_diagnostic(event: RunErrorEvent) -> dict[str, object]:
    return {
        "message": event.message,
        "code": event.code,
        "metadata": event.metadata,
    }


def _trace(evidence: PlanDogfoodEvidence, message: str, **fields: object) -> None:
    if os.getenv("PULSARA_DOGFOOD_TRACE") != "1":
        return
    payload = {
        "message": message,
        "workspace_root": evidence.workspace_root,
        **fields,
    }
    print("[plan-dogfood-trace] " + json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


def _require(condition: bool, message: str, evidence: PlanDogfoodEvidence) -> None:
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


_PLAN_DOGFOOD_USER_REQUEST = """
请在 Plan workflow 中先理解这个 FastAPI 内存任务队列项目，然后为它实现任务超时重试（Visibility Timeout）。

要求：
- 先只读探索 README.md、main.py、tests/test_visibility_timeout.py。
- 在计划阶段先问我一个关键设计问题。
- 我确认后，用 exit_plan 提交计划，得到批准后再改代码。
- 获批后实现 lazy/passive timeout recovery + receipt/token，运行 `uv run pytest tests/test_visibility_timeout.py -q`。
- 测试通过后，用一句话总结并包含 PULSARA_PLAN_QUEUE_DOGFOOD_OK。
""".strip()


_PLAN_DOGFOOD_SYSTEM_PROMPT = f"""
You are Pulsara running a long real-LLM dogfood for Plan workflow.
The host has already entered Plan workflow before the user request.

Validation rules:
1. During Plan workflow, inspect README.md, main.py, and tests/test_visibility_timeout.py with read-only tools before proposing the implementation.
2. During Plan workflow, do not call write_file, edit_file, terminal, or terminal_process.
3. During Plan workflow, call ask_plan_question exactly once. Ask about the timeout/retry design tradeoff. Good questions mention lazy sweep/passive recovery and receipt/token handling.
4. After the user answers, call exit_plan exactly once. The plan must mention:
   - adding locked_until, visibility_deadline, expires_at, or an equivalent lock/deadline timestamp;
   - adding a receipt handle / worker token returned by take and required by complete;
   - lazy/passive timeout recovery instead of per-job timers;
   - editing main.py;
   - running the exact command: {TEST_COMMAND}
5. After exit_plan is approved, implement the minimal fix. Do not refactor unrelated API shape.
6. Run the exact command: {TEST_COMMAND}
7. If tests fail, inspect the failure, fix main.py, and rerun the exact same command.
8. When tests pass, answer with a short final response containing exactly this sentinel somewhere: {PLAN_SENTINEL}

Do not inspect secrets. Do not modify files outside this workspace.
""".strip()


_FIXTURE_README = """
# Pulsara MQ Test

This is a deliberately small FastAPI in-memory job queue. It behaves like a tiny
message broker or task scheduler, similar to a minimal Redis List or RabbitMQ
queue, but all data lives in process memory.

Current API:

- `POST /jobs`: producer submits a job. New jobs start as `pending`.
- `GET /jobs/take`: consumer takes one `pending` job. The job becomes `processing`.
- `POST /jobs/{id}/complete`: consumer marks a job as `completed`.

Current job shape:

- `id`: unique identifier.
- `payload`: arbitrary JSON-like task data.
- `status`: one of `pending`, `processing`, `completed`.

Missing feature:

Add Visibility Timeout. If a consumer takes a job but does not complete it
within 30 seconds, the job should become available for another consumer again.
Avoid one timer per job; prefer a passive/lazy sweep when taking jobs. Prevent
stale consumers from completing a job after it has timed out and been reissued.
""".lstrip()


_FIXTURE_MAIN = '''
from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException


app = FastAPI(title="Pulsara MQ Test")
jobs: list[dict[str, Any]] = []


def create_job(payload: dict[str, Any]) -> dict[str, Any]:
    job = {
        "id": uuid4().hex,
        "payload": payload,
        "status": "pending",
    }
    jobs.append(job)
    return dict(job)


def take_job() -> dict[str, Any] | None:
    for job in jobs:
        if job["status"] == "pending":
            job["status"] = "processing"
            return dict(job)
    return None


def complete_job(job_id: str) -> dict[str, Any]:
    for job in jobs:
        if job["id"] == job_id:
            if job["status"] != "processing":
                raise HTTPException(status_code=409, detail="job is not processing")
            job["status"] = "completed"
            return dict(job)
    raise HTTPException(status_code=404, detail="job not found")


@app.post("/jobs")
def post_job(body: dict[str, Any]) -> dict[str, Any]:
    payload = body.get("payload", body)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be an object")
    return create_job(payload)


@app.get("/jobs/take")
def get_job() -> dict[str, Any] | None:
    return take_job()


@app.post("/jobs/{job_id}/complete")
def post_complete(job_id: str) -> dict[str, Any]:
    return complete_job(job_id)
'''.lstrip()


_FIXTURE_VISIBILITY_TEST = '''
from __future__ import annotations

from pathlib import Path

import pytest

import main


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def setup_function() -> None:
    main.jobs.clear()


def install_fake_clock() -> FakeClock:
    assert hasattr(main, "set_clock"), "Visibility timeout needs injectable set_clock(clock_callable)."
    clock = FakeClock()
    main.set_clock(clock)
    return clock


def test_fastapi_routes_still_exist() -> None:
    paths = {route.path for route in main.app.routes}
    assert "/jobs" in paths
    assert "/jobs/take" in paths
    assert "/jobs/{job_id}/complete" in paths


def test_visibility_timeout_requeues_processing_job_lazily_and_rejects_stale_receipt() -> None:
    clock = install_fake_clock()
    created = main.create_job({"kind": "email", "to": "test@example.com"})

    first = main.take_job()
    assert first is not None
    assert first["id"] == created["id"]
    assert first["status"] == "processing"
    receipt_a = first.get("receipt")
    assert isinstance(receipt_a, str) and receipt_a
    assert main.jobs[0]["status"] == "processing"

    # Before the 30-second visibility timeout, the job must stay invisible.
    clock.advance(29)
    assert main.take_job() is None

    # After timeout, take_job should lazily make the processing job available
    # and issue a new receipt for the new consumer.
    clock.advance(2)
    second = main.take_job()
    assert second is not None
    assert second["id"] == created["id"]
    assert second["status"] == "processing"
    receipt_b = second.get("receipt")
    assert isinstance(receipt_b, str) and receipt_b
    assert receipt_b != receipt_a

    # The stale consumer must not be able to complete a job it no longer owns.
    with pytest.raises(Exception):
        main.complete_job(created["id"], receipt=receipt_a)

    completed = main.complete_job(created["id"], receipt=receipt_b)
    assert completed["status"] == "completed"
    assert main.jobs[0]["status"] == "completed"
    assert main.take_job() is None


def test_no_per_job_timer_or_background_worker_pattern() -> None:
    source = Path(main.__file__).read_text(encoding="utf-8")
    forbidden = [
        "threading.Timer",
        "asyncio.create_task",
        "BackgroundTasks",
        "time.sleep(",
        "setTimeout",
    ]
    assert [token for token in forbidden if token in source] == []
'''.lstrip()
