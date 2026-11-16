"""A known physical outcome is accepted once, without rerunning business effects."""
import asyncio
from datetime import datetime, timezone
from io import BytesIO
from time import monotonic
from types import SimpleNamespace

from PIL import Image
import pytest

from pulsara_agent.conversation_kernel.content_parts import ResultPart
from pulsara_agent.conversation_kernel.tool_contracts import KernelToolResult
from pulsara_agent.conversation_kernel.tool_execution import ToolBatchExecutor, _code_result
from pulsara_agent.conversation_kernel.tool_outputs import PostgresToolOutputReadPort, ToolOutputProcessor
from pulsara_agent.identity import random_id
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.ports.tool_execution import (ToolBusinessValue, thaw_tool_json_object, tool_json_values_equal)
from pulsara_agent.ports.tool_output import (AcceptedToolResultProjectionError, ToolResultSaveError, ToolOutputUnavailableReason)
from pulsara_agent.primitives.tool_observation import ToolObservationOrigin
from tests.test_hardcut_tool_result_storage import prepare
from tests.test_hardcut_entry_parts import read

pytestmark = pytest.mark.postgres


@pytest.fixture
def repository(stage2_migrated_postgres_database):
    from pulsara_agent.conversation_kernel.repository import ConversationKernelRepository
    from tests.support.postgres import verified_postgres_provider
    return ConversationKernelRepository(verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn))


class InlineIO:
    async def run(self, operation, *args, **kwargs):
        return operation(*args, **kwargs)


class LiveBus:
    def __init__(self):
        self.events, self.settlements = [], []

    def offer_nowait(self, **event):
        self.events.append(event)

    def offer_settlement_nowait(self, **event):
        self.settlements.append(event)


def executor(repository, lease):
    value = object.__new__(ToolBatchExecutor)
    value._repository, value._writer_lease = repository, lease
    value._io, value._live_bus = InlineIO(), LiveBus()
    value._tool_output_processor = ToolOutputProcessor()
    value._canonical_deadline = lambda: monotonic() + 60
    # Settlement may only commit an already executed operation, never invoke it.
    value._tools = SimpleNamespace()
    return value


def settle(execution, candidate, result, *, tool_name="read_file"):
    return execution._settle_known_tool_result(
        session_id=candidate.session_id, turn_id=candidate.turn_id,
        assistant_entry_id=candidate.assistant_entry_id, tool_name=tool_name,
        tool_call_id=candidate.tool_call_id, invocation_arguments={"target": {"kind": "PATH", "path": "saved.txt"}},
        call_ordinal=0, scope_kind=ModelInputScopeKind.ROOT,
        result_id=candidate.result_id, result_entry_id=candidate.result_entry_id,
        attempt_id=candidate.attempt_id, workspace_id=candidate.workspace_id,
        result=result, observed_at=datetime.now(timezone.utc), observation_origin=ToolObservationOrigin.BUILTIN,
        live_sink=None, tool_result_block_id=random_id("block"), live_attribution={})


def read_value(repository, candidate):
    return PostgresToolOutputReadPort(repository.connection_provider,
        session_id=candidate.session_id, workspace_id=candidate.workspace_id).read_result(
            {"kind": "SESSION_ENTRY", "entry_id": candidate.result_entry_id}).value


def test_typed_settlement_saves_complete_value_and_confirms_lost_ack_without_reexecution(repository, monkeypatch):
    lease, candidate = prepare(repository)
    execution = executor(repository, lease)
    calls = []
    accept = repository.accept_tool_result
    def lose_ack(*args, **kwargs):
        calls.append(kwargs["candidate"])
        accept(*args, **kwargs)
        raise TimeoutError("ACK lost after commit")
    monkeypatch.setattr(repository, "accept_tool_result", lose_ack)
    captured = []
    token = _code_result.set(captured)
    try:
        result = KernelToolResult("APPLICATION_ERROR", ToolBusinessValue(
            {"meta": {"exit_code": 23}, "structuredContent": None},
            (ResultPart("text", "physical effect already happened\r\n"), ResultPart("object", {"all": [1, None]}))),
            result_facts={"canonical_path": "/actual", "file_mtime_ns": 7})
        outcome = asyncio.run(settle(execution, candidate, result))
    finally:
        _code_result.reset(token)
    assert len(calls) == 1
    assert outcome.settlement.result_state == "APPLICATION_ERROR"
    value = read_value(repository, candidate)
    assert value["meta"] == {"status": "ERROR", "exit_code": 23}
    assert value["content"][0]["value"] == "physical effect already happened\r\n"
    assert len(value["content"]) == 2 and captured[0][0].content == result.content
    assert len(execution._live_bus.settlements) == 1
    with read(repository) as connection:
        saved = connection.execute("SELECT result_facts FROM pulsara_v3.tool_results WHERE id=%s", (candidate.result_id,)).fetchone()
        assert saved["result_facts"] == {"canonical_path": "/actual", "file_mtime_ns": 7}


def test_json_shaped_text_is_preserved_as_declared_text(repository):
    lease, candidate = prepare(repository)
    text = '{"content":"business text","next":null}\r\n'
    result = KernelToolResult("SUCCESS", ToolBusinessValue(content=(ResultPart("text",text),)))
    asyncio.run(settle(executor(repository,lease),candidate,result))
    value = read_value(repository,candidate)
    assert value["content"] == ({"type":"text","value":text},)
    assert value["meta"] == {"status":"SUCCESS"}


def test_save_failure_produces_no_accepted_target_or_code_value(repository, monkeypatch):
    from pulsara_agent.conversation_kernel._repository import tools
    lease, candidate = prepare(repository)
    execution = executor(repository, lease)
    def fail(*args, **kwargs):
        raise RuntimeError("result storage failed after known effect")
    monkeypatch.setattr(tools, "write_content_parts", fail)
    captured = []
    token = _code_result.set(captured)
    try:
        with pytest.raises(ToolResultSaveError, match="保存失败") as failure:
            asyncio.run(settle(execution, candidate, KernelToolResult("SUCCESS", ToolBusinessValue(content=(ResultPart("text","effect already happened"),)))))
        assert failure.value.reason is ToolOutputUnavailableReason.BODY_PUBLICATION_FAILED
        assert failure.value.result_state == "SUCCESS"
        assert failure.value.known_value["content"] == ({"type": "text", "value": "effect already happened"},)
        assert isinstance(failure.value.cause, RuntimeError)
        assert "不应重做" in str(failure.value)
    finally:
        _code_result.reset(token)
    assert captured == [] and execution._live_bus.settlements == []
    with pytest.raises(KeyError):
        read_value(repository, candidate)


def test_frozen_image_outcome_is_published_then_accepted_as_ordered_content(repository):
    lease, candidate = prepare(repository)
    stream = BytesIO()
    Image.new("RGB", (7, 5), (11, 23, 41)).save(stream, format="PNG")
    from tests.support.images import bound_test_image
    image = bound_test_image("image/png", stream.getvalue(), 7, 5)
    content = ToolBusinessValue(content=(ResultPart("text", "image failure context\n"),
        ResultPart("image", {"kind":"IMAGE_REF","image_ref":image.image_ref}),
        ResultPart("text", "following explanation")))
    execution = executor(repository, lease)
    outcome = asyncio.run(settle(execution, candidate,
        KernelToolResult("APPLICATION_ERROR", content, pending_images=(image,)), tool_name="view_image"))
    value = read_value(repository, candidate)
    assert value["meta"] == {"status": "ERROR"}
    assert [part["type"] for part in value["content"]] == ["text", "image", "text"]
    assert value["content"][0]["value"] == "image failure context\n"
    assert outcome.settlement.canonical_content is not None


def test_confirmation_failure_retains_known_value_and_cause_without_readable_target(repository, monkeypatch):
    lease, candidate = prepare(repository)
    execution = executor(repository, lease)
    accepted = repository.accept_tool_result
    calls = []
    def lose_ack(*args, **kwargs):
        calls.append(kwargs["candidate"])
        accepted(*args, **kwargs)
        raise TimeoutError("actual write committed, ACK lost")
    confirmation_error = ConnectionError("confirmation connection unavailable")
    def cannot_confirm(*args, **kwargs):
        raise confirmation_error
    monkeypatch.setattr(repository, "accept_tool_result", lose_ack)
    monkeypatch.setattr(repository, "confirm_tool_result_winner", cannot_confirm)
    value = ToolBusinessValue({"meta": {"exit_code": 23}}, (ResultPart("text", "actual obtained outcome"),))
    captured = []
    token = _code_result.set(captured)
    try:
        with pytest.raises(ToolResultSaveError, match="保存状态未确认") as failure:
            asyncio.run(settle(execution, candidate, KernelToolResult("APPLICATION_ERROR", value)))
    finally:
        _code_result.reset(token)
    error = failure.value
    assert error.reason is ToolOutputUnavailableReason.BODY_PUBLICATION_UNCONFIRMED
    assert error.result_state == "APPLICATION_ERROR"
    assert error.known_value["meta"] == {"status": "ERROR", "exit_code": 23}
    assert error.known_value["content"][0]["value"] == "actual obtained outcome"
    assert error.cause is confirmation_error and error.__cause__ is confirmation_error
    assert len(calls) == 1 and captured == [] and execution._live_bus.settlements == []
    # The write did happen. The error still makes no claim of a readable result;
    # a separate ordinary read can inspect that fact after connectivity recovers.
    assert read_value(repository, candidate) == error.known_value


@pytest.mark.parametrize("state", ["SUCCESS", "APPLICATION_ERROR", "CANCELLED"])
def test_text_code_units_are_saved_before_display_and_kept_in_complete_value(repository, monkeypatch, state):
    from pulsara_agent.conversation_kernel import native_tool_delivery

    lease, candidate = prepare(repository, state)
    execution = executor(repository, lease)
    original_project = native_tool_delivery.project_native_tool_page
    text = "actual\x00\r\n中文😀\ud800\udfff"
    projected_after_save = []

    def project(**arguments):
        projected_after_save.append(read_value(repository, candidate))
        return original_project(**arguments)

    monkeypatch.setattr(native_tool_delivery, "project_native_tool_page", project)
    outcome = asyncio.run(settle(execution, candidate,
        KernelToolResult(state, ToolBusinessValue(content=(ResultPart("text", text),)))))
    assert projected_after_save[0]["content"][0]["value"] == text
    assert read_value(repository, candidate)["content"][0]["value"] == text
    assert outcome.settlement.result_state == state
    projected = text.encode("utf-16-le",errors="surrogatepass").decode("utf-16-le",errors="surrogatepass")
    assert projected in outcome.settlement.public_projection.canonical_body
    assert len(execution._live_bus.settlements) == 1


@pytest.mark.parametrize("number", [-0.0, 1.0, 1e20, 1e30])
def test_original_utf16_and_number_candidate_confirms_real_commit_after_lost_ack(repository, monkeypatch, number):
    lease, candidate = prepare(repository)
    execution = executor(repository, lease)
    original_accept = repository.accept_tool_result
    calls = []

    def lose_ack(*args, **kwargs):
        calls.append(kwargs["candidate"])
        original_accept(*args, **kwargs)
        raise TimeoutError("ACK lost after commit")

    monkeypatch.setattr(repository, "accept_tool_result", lose_ack)
    nested = {"\ud83d\ude00": ["\ud83d\ude00", "\ud800", "\x00", True, 1, 1.0, None], "\ue000": "中文"}
    result = KernelToolResult("SUCCESS", ToolBusinessValue(
        {"structuredContent": nested, "number": number}, (ResultPart("object", nested),)),
        result_facts={"number": number})
    outcome = asyncio.run(settle(execution, candidate, result))
    saved = read_value(repository, candidate)
    assert tool_json_values_equal(saved["structuredContent"], nested)
    assert tool_json_values_equal(saved["content"][0]["value"], nested)
    assert len(calls) == 1 and outcome.settlement.result_id == candidate.result_id
    assert repository.confirm_tool_result_winner(lease.guard, candidate=calls[0],
        deadline_monotonic=monotonic()+30) is not None


def test_projection_failure_preserves_accepted_value_identity_and_live_commit(repository, monkeypatch):
    from pulsara_agent.conversation_kernel import native_tool_delivery
    from tests.test_hardcut_source_consumers import invoke_code_mode_failure

    lease, candidate = prepare(repository)
    execution = executor(repository, lease)
    original_accept = repository.accept_tool_result
    accept_calls = []
    def accept(*args, **kwargs):
        accept_calls.append(kwargs["candidate"])
        return original_accept(*args, **kwargs)
    monkeypatch.setattr(repository, "accept_tool_result", accept)
    cause = RuntimeError("injected display failure")
    def cannot_project(_page, **_kwargs):
        assert read_value(repository, candidate)["content"][0]["value"] == "physical write complete"
        raise cause
    monkeypatch.setattr(native_tool_delivery, "render_native_tool_page", cannot_project)
    delivered = []
    result = KernelToolResult("SUCCESS", ToolBusinessValue(
        {"structuredContent": None}, (ResultPart("text", "physical write complete"),)),
        delivery_succeeded=lambda: delivered.append(True))
    with pytest.raises(AcceptedToolResultProjectionError, match="完整结果已保存") as caught:
        asyncio.run(settle(execution, candidate, result))
    error = caught.value
    assert error.cause is cause and error.__cause__ is cause
    assert error.result_id == candidate.result_id and error.result_entry_id == candidate.result_entry_id
    assert error.result_state == "SUCCESS" and error.known_value == read_value(repository, candidate)
    assert len(accept_calls) == 1 and len(execution._live_bus.settlements) == 1
    assert execution._live_bus.settlements[0]["committed_entry_id"] == candidate.result_entry_id
    assert "injected display failure" in execution._live_bus.events[-1]["payload"].final_text
    code_result = invoke_code_mode_failure(monkeypatch, error)
    assert code_result.value == thaw_tool_json_object(error.known_value)
    assert code_result.result_id == candidate.result_id and code_result.result_entry_id == candidate.result_entry_id
    assert code_result.has_result
    assert code_result.error_code == "RESULT_PROJECTION_FAILED" and code_result.state == "SUCCESS"
    assert code_result.delivery_succeeded is None and delivered == []


def test_preparation_failure_retains_known_value_without_accepted_identity(repository, monkeypatch):
    lease, candidate = prepare(repository)
    execution = executor(repository, lease)
    cause = RuntimeError("injected candidate preparation failure")
    def fail(**_):
        raise cause
    monkeypatch.setattr(execution._tool_output_processor, "prepare", fail)
    result = KernelToolResult("SUCCESS", ToolBusinessValue(content=(ResultPart("text", "effect completed"),)))
    with pytest.raises(ToolResultSaveError) as caught:
        asyncio.run(settle(execution, candidate, result))
    assert caught.value.known_value["content"][0]["value"] == "effect completed"
    assert caught.value.result_state == "SUCCESS" and caught.value.cause is cause
    assert caught.value.reason is ToolOutputUnavailableReason.BODY_PUBLICATION_FAILED
    assert execution._live_bus.events == [] and execution._live_bus.settlements == []
    with pytest.raises(KeyError):
        read_value(repository, candidate)


def test_projection_failure_does_not_discard_committed_process_local_effect(repository, monkeypatch):
    from pulsara_agent.conversation_kernel import native_tool_delivery
    from pulsara_agent.conversation_kernel.tool_contracts import (
        ProcessLocalEffectSettlementDisposition, ProcessLocalEffectSettlementToken,
    )
    from tests.support.model_config import frozen_test_prompt
    from tests.support.round3 import ScriptedKernelModel
    from tests.test_round5_long_horizon_postgres import _lease, _runner, _tool_stream, _SettlementTokenTool

    class Tool(_SettlementTokenTool):
        async def invoke(self, **_kwargs):
            self.invocations += 1
            return KernelToolResult("SUCCESS", ToolBusinessValue(content=(ResultPart("text", "committed effect"),)),
                process_local_settlement=ProcessLocalEffectSettlementToken(random_id("settlement"), self))

    session_id, workspace_id, lease = _lease(repository)
    tool = Tool()
    def cannot_project(_page, **_kwargs):
        raise RuntimeError("display failed after effect commit")
    monkeypatch.setattr(native_tool_delivery, "render_native_tool_page", cannot_project)
    model = ScriptedKernelModel([_tool_stream(1)])
    with pytest.raises(AcceptedToolResultProjectionError) as caught:
        asyncio.run(_runner(repository, lease, model, tool).run_turn(frozen_test_prompt("run once")))
    assert tool.invocations == 1
    assert tool.settlements == [ProcessLocalEffectSettlementDisposition.COMMITTED]
    value = PostgresToolOutputReadPort(repository.connection_provider,
        session_id=session_id, workspace_id=workspace_id).read_result(
            {"kind": "SESSION_ENTRY", "entry_id": caught.value.result_entry_id}).value
    assert value == caught.value.known_value
