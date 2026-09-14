"""Fresh-database production-composition dogfood for the Stage 2 cut."""

from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path
from time import monotonic
from typing import AsyncIterator

from PIL import Image
import psycopg
import pytest

from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.conversation_kernel.compaction.contracts import (
    ResolvedCompactionHeadroomBounds,
    ResolvedCompactionPolicy,
)
from pulsara_agent.conversation_kernel.direct_model import KernelModelExecutionRequest
from pulsara_agent.conversation_kernel.extensions import (
    ExtensionDelivery,
    ExtensionPlane,
    ExtensionProjectionProfile,
    ExtensionRegistrationRequest,
)
from pulsara_agent.conversation_kernel.vocabulary import CommittedEventType
from pulsara_agent.workspace_identity import HostWorkspaceInput
from pulsara_agent.ports.live_agent_event import (
    TextDeltaPayload,
    TextEndPayload,
    TextStartPayload,
    live_digest,
)
from pulsara_agent.llm.input import (
    LLMImagePart,
    LLMTextPart,
    MessageRole,
    PromptContent,
    PromptImagePart,
)
from pulsara_agent.llm.estimator import PulsaraHeuristicTokenEstimatorV2
from pulsara_agent.model_input.contracts import CapabilityActivationSubjectKind
from pulsara_agent.primitives.context import context_fingerprint
from tests.support.model_config import test_model_binding, test_model_runtime
from tests.support.round3 import CallbackScriptedKernelModel


pytestmark = pytest.mark.postgres


class _DogfoodModelPort:
    def __init__(self, **_: object) -> None:
        self._delegate = CallbackScriptedKernelModel(self._stream)

    def prepare_call(self, request):
        return self._delegate.prepare_call(request)

    def prepare_target(self, request):
        return self._delegate.prepare_target(request)

    def freeze_native_tool_eligibility(self, **kwargs):
        return self._delegate.freeze_native_tool_eligibility(**kwargs)

    def materialize_native_tool_projection_set(self, **kwargs):
        return self._delegate.materialize_native_tool_projection_set(**kwargs)

    def bind_tool_surface(self, **kwargs):
        return self._delegate.bind_tool_surface(**kwargs)

    def plan_wire_input(self, **kwargs):
        return self._delegate.plan_wire_input(**kwargs)

    def freeze_wire_measurement(self, **kwargs):
        return self._delegate.freeze_wire_measurement(**kwargs)

    def replay_target_for_resolved_call(self, call):
        return self._delegate.replay_target_for_resolved_call(call)

    def preflight_execution(self, request, **kwargs):
        return self._delegate.preflight_execution(request, **kwargs)

    async def _stream(
        self, _request: KernelModelExecutionRequest
    ) -> AsyncIterator[object]:
        text = "STAGE2_DOGFOOD_OK"
        yield TextStartPayload("text:dogfood")
        yield TextDeltaPayload("text:dogfood", text)
        yield TextEndPayload(
            "text:dogfood",
            text,
            len(text.encode("utf-8")),
            live_digest(text),
        )


class _SteerModelPort:
    def __init__(self, **_: object) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self._delegate = CallbackScriptedKernelModel(self._stream)
        self.requests = self._delegate.requests

    def prepare_call(self, request):
        return self._delegate.prepare_call(request)

    def prepare_target(self, request):
        return self._delegate.prepare_target(request)

    def freeze_native_tool_eligibility(self, **kwargs):
        return self._delegate.freeze_native_tool_eligibility(**kwargs)

    def materialize_native_tool_projection_set(self, **kwargs):
        return self._delegate.materialize_native_tool_projection_set(**kwargs)

    def bind_tool_surface(self, **kwargs):
        return self._delegate.bind_tool_surface(**kwargs)

    def plan_wire_input(self, **kwargs):
        return self._delegate.plan_wire_input(**kwargs)

    def freeze_wire_measurement(self, **kwargs):
        return self._delegate.freeze_wire_measurement(**kwargs)

    def replay_target_for_resolved_call(self, call):
        return self._delegate.replay_target_for_resolved_call(call)

    def preflight_execution(self, request, **kwargs):
        return self._delegate.preflight_execution(request, **kwargs)

    async def _stream(
        self, request: KernelModelExecutionRequest
    ) -> AsyncIterator[object]:
        call_index = len(self.requests)
        if call_index == 1:
            self.started.set()
            await self.release.wait()
        text = "BEFORE_STEER" if call_index == 1 else "AFTER_STEER"
        block = f"text:steer:{call_index}"
        yield TextStartPayload(block)
        yield TextDeltaPayload(block, text)
        yield TextEndPayload(
            block,
            text,
            len(text.encode("utf-8")),
            live_digest(text),
        )


class _K2ImageCompileProbe(_DogfoodModelPort):
    def __init__(self) -> None:
        super().__init__()
        self.semantic_input = None

    def freeze_wire_measurement(self, **kwargs):
        self.semantic_input = kwargs["semantic_input"]
        return super().freeze_wire_measurement(**kwargs)


class _K2SteerImageCompileProbe(_SteerModelPort):
    def __init__(self) -> None:
        super().__init__()
        self.semantic_input = None

    def freeze_wire_measurement(self, **kwargs):
        semantic_input = kwargs["semantic_input"]
        if any(
            isinstance(part, LLMImagePart)
            for message in semantic_input.messages
            for part in message.content
        ):
            self.semantic_input = semantic_input
        return super().freeze_wire_measurement(**kwargs)


def test_stage2_public_host_fresh_open_run_and_canonical_rehydrate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage2_migrated_postgres_database,
) -> None:
    import pulsara_agent.conversation_kernel.host as kernel_host

    monkeypatch.setattr(kernel_host, "DirectKernelModelPort", _DogfoodModelPort)
    monkeypatch.setattr(kernel_host.LocalMcpManagementService, "load_configs", lambda *_args, **_kwargs: ())
    model_runtime = test_model_runtime(
        api_key="sk-fixture-secret",
        base_url="https://example.invalid/v1",
        model_id="test-pro",
        wire_api="openai_chat_completions",
        postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
    )
    workspace = HostWorkspaceInput(
        workspace_kind="project",
        workspace_root=tmp_path,
    )

    async def scenario() -> None:
        core = KernelHostCore.production(
            model_runtime=model_runtime,
            authenticated_first_party_extension_ids=frozenset({"extension:dogfood"}),
        )
        first = await core.open_session(workspace)
        await first.update_model_call_binding(test_model_binding(model_runtime))
        committed_deliveries: list[ExtensionDelivery] = []
        turn_completed = asyncio.Event()

        async def committed_callback(delivery: ExtensionDelivery) -> None:
            committed_deliveries.append(delivery)
            if delivery.event_type == CommittedEventType.TURN_COMPLETED.value:
                turn_completed.set()

        await first.register_extension(
            ExtensionRegistrationRequest(
                principal=first.authenticate_extension_principal(
                    extension_principal_id="extension:dogfood",
                ),
                handler_id="handler:committed-dogfood",
                manifest_digest="sha256:" + "1" * 64,
                plane=ExtensionPlane.POST_COMMIT,
                session_id=first.session_id,
                turn_id=None,
                event_types=frozenset(
                    {
                        CommittedEventType.USER_MESSAGE_ACCEPTED.value,
                        CommittedEventType.ASSISTANT_MESSAGE_ACCEPTED.value,
                        CommittedEventType.TURN_COMPLETED.value,
                    }
                ),
                projection_major=1,
                projection_profile=ExtensionProjectionProfile.REDACTED,
                capability_set=frozenset(),
                lease_seconds=30,
                maximum_queue_events=8,
                maximum_queue_bytes=32 * 1024,
                callback_deadline_seconds=1,
                callback=committed_callback,
            )
        )
        session_id = first.session_id
        first_generation = first.writer_generation
        result = await first.run_turn(PromptContent.text("fresh database dogfood"))
        assert result.final_text == "STAGE2_DOGFOOD_OK"
        await asyncio.wait_for(turn_completed.wait(), timeout=2)
        assert [item.event_type for item in committed_deliveries] == [
            CommittedEventType.USER_MESSAGE_ACCEPTED.value,
            CommittedEventType.ASSISTANT_MESSAGE_ACCEPTED.value,
            CommittedEventType.TURN_COMPLETED.value,
        ]
        assert [item.source_revision for item in committed_deliveries] == [1, 2, 3]
        await core.close_session(
            first.host_session_id,
            close_conversation=False,
        )

        resumed = await core.resume_session(session_id, workspace_input=workspace)
        assert resumed.writer_generation == first_generation + 1
        page = await asyncio.to_thread(
            resumed.query.page_entries,
            session_id=session_id,
            deadline_monotonic=monotonic() + 10.0,
        )
        assert [entry["entry_kind"] for entry in page.entries] == [
            "USER_MESSAGE",
            "ASSISTANT_MESSAGE",
        ]
        assert page.through_entry_sequence == 2
        assert page.through_event_sequence == 3
        await core.close_session(
            resumed.host_session_id,
            close_conversation=True,
        )
        await core.shutdown()

    asyncio.run(scenario())

    # The clean universe does not create any legacy public authority.
    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as connection:
        for relation in (
            "sessions",
            "agent_events",
            "prompt_queue_items",
            "durable_projection_jobs",
            "memory_nodes",
        ):
            assert (
                connection.execute(
                    "SELECT pg_catalog.to_regclass(%s)",
                    (f"public.{relation}",),
                ).fetchone()[0]
                is None
            ), relation


@pytest.mark.parametrize("mixed", (False, True))
def test_k2_public_host_validates_image_input_and_reaches_existing_compiler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage2_migrated_postgres_database,
    mixed: bool,
) -> None:
    import pulsara_agent.conversation_kernel.host as kernel_host
    import pulsara_agent.conversation_kernel.provider_dispatch as provider_dispatch

    payload = BytesIO()
    Image.new("RGB", (7, 5), (11, 23, 41)).save(payload, "PNG")
    image_bytes = payload.getvalue()
    prompt = (
        PromptContent(
            (
                LLMTextPart("before"),
                PromptImagePart(image_bytes, "image/png"),
                LLMTextPart("after"),
            )
        )
        if mixed
        else PromptContent((PromptImagePart(image_bytes, "image/png"),))
    )
    expected_content = (
        (
            LLMTextPart("before"),
            LLMImagePart("image/png", image_bytes, 7, 5),
            LLMTextPart("after"),
        )
        if mixed
        else (LLMImagePart("image/png", image_bytes, 7, 5),)
    )
    model = _K2ImageCompileProbe()
    compiled_messages = []
    activation_projections = []
    original_estimate_message = PulsaraHeuristicTokenEstimatorV2.estimate_message
    original_activation = provider_dispatch._activation_subject_for_anchor

    def estimate_message(estimator, message):
        if any(isinstance(part, LLMImagePart) for part in message.content):
            compiled_messages.append(message)
        return original_estimate_message(estimator, message)

    def activation_subject(canonical_input, anchor):
        result = original_activation(canonical_input, anchor)
        activation_projections.append(result)
        return result

    monkeypatch.setattr(
        PulsaraHeuristicTokenEstimatorV2,
        "estimate_message",
        estimate_message,
    )
    monkeypatch.setattr(
        provider_dispatch,
        "_activation_subject_for_anchor",
        activation_subject,
    )
    monkeypatch.setattr(kernel_host, "DirectKernelModelPort", lambda **_: model)
    monkeypatch.setattr(
        kernel_host.LocalMcpManagementService,
        "load_configs",
        lambda *_args, **_kwargs: (),
    )
    runtime = test_model_runtime(
        model_id="test-pro",
        wire_api="openai_chat_completions",
        postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
    )
    session_id = ""

    async def scenario() -> None:
        nonlocal session_id
        core = KernelHostCore.production(model_runtime=runtime)
        session = await core.open_session(
            HostWorkspaceInput(workspace_kind="project", workspace_root=tmp_path)
        )
        session_id = session.session_id
        try:
            await session.update_model_call_binding(test_model_binding(runtime))
            result = await session.run_turn(
                prompt,
                command_id=f"command:k2-image:{mixed}",
            )
            assert result.final_text == "STAGE2_DOGFOOD_OK"
        finally:
            await core.close_session(
                session.host_session_id,
                close_conversation=False,
            )
            await core.shutdown()

    asyncio.run(scenario())

    assert model.semantic_input is not None
    assert compiled_messages
    assert all(message.content == expected_content for message in compiled_messages)
    assert (
        CapabilityActivationSubjectKind.ROOT_HUMAN_PROMPT,
        "before\nafter" if mixed else "",
    ) in activation_projections
    images = tuple(
        part
        for message in model.semantic_input.messages
        for part in message.content
        if isinstance(part, LLMImagePart)
    )
    assert images == (LLMImagePart("image/png", image_bytes, 7, 5),)
    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as connection:
        ref = connection.execute(
            """SELECT r.ref_ordinal, b.body, b.media_type, b.codec
               FROM pulsara_v3.canonical_image_refs r
               JOIN pulsara_v3.blobs b ON b.id=r.blob_id
               WHERE r.session_id=%s AND r.transcript_entry_id IS NOT NULL""",
            (session_id,),
        ).fetchone()
    assert ref is not None
    assert (ref[0], bytes(ref[1]), ref[2], ref[3]) == (
        0,
        image_bytes,
        "image/png",
        "binary",
    )


def test_k2_public_host_reports_image_validation_reason_before_hook_or_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage2_migrated_postgres_database,
) -> None:
    import pulsara_agent.conversation_kernel.host as kernel_host

    payload = BytesIO()
    Image.new("RGB", (7, 5), (11, 23, 41)).save(payload, "PNG")
    hook_calls = 0

    async def unexpected_hook(*_args, **_kwargs):
        nonlocal hook_calls
        hook_calls += 1
        raise AssertionError("invalid prompt reached UserPromptSubmit Hook")

    monkeypatch.setattr(
        kernel_host.KernelHostSession,
        "_dispatch_user_prompt_hook",
        unexpected_hook,
    )
    monkeypatch.setattr(kernel_host, "DirectKernelModelPort", _DogfoodModelPort)
    monkeypatch.setattr(
        kernel_host.LocalMcpManagementService,
        "load_configs",
        lambda *_args, **_kwargs: (),
    )
    runtime = test_model_runtime(
        model_id="test-pro",
        wire_api="openai_chat_completions",
        postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
    )

    async def scenario() -> tuple[str, object]:
        core = KernelHostCore.production(model_runtime=runtime)
        session = await core.open_session(
            HostWorkspaceInput(workspace_kind="project", workspace_root=tmp_path)
        )
        try:
            outcome = await session.submit_prompt(
                command_id="command:k2-invalid-image",
                content=PromptContent(
                    (PromptImagePart(payload.getvalue(), "image/jpeg"),)
                ),
            )
            return session.session_id, outcome
        finally:
            await core.close_session(
                session.host_session_id,
                close_conversation=False,
            )
            await core.shutdown()

    session_id, outcome = asyncio.run(scenario())

    assert hook_calls == 0
    assert outcome.status == "REJECTED"
    assert outcome.public_code == "INVALID_PROMPT"
    assert outcome.public_message == (
        "PromptImageValidationError: "
        "declared image MIME does not match the decoded format"
    )
    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as connection:
        persisted = connection.execute(
            """SELECT
                   (SELECT count(*) FROM pulsara_v3.session_commands
                    WHERE session_id=%s AND command_id=%s),
                   (SELECT count(*) FROM pulsara_v3.prompt_queue_items
                    WHERE session_id=%s)""",
            (
                session_id,
                "command:k2-invalid-image",
                session_id,
            ),
        ).fetchone()
    assert persisted == (0, 0)


def test_k2_root_install_prepares_ordered_image_gap_without_a_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage2_migrated_postgres_database,
) -> None:
    import pulsara_agent.conversation_kernel.host as kernel_host
    import pulsara_agent.conversation_kernel.provider_dispatch as provider_dispatch

    payload = BytesIO()
    Image.new("RGB", (7, 5), (11, 23, 41)).save(payload, "PNG")
    image_bytes = payload.getvalue()
    model = _DogfoodModelPort()
    prepared_parent_subjects = []
    original_parent_subject = (
        provider_dispatch._prepare_subagent_parent_context_call_subject
    )

    def capture_parent_subject(*, dispatch, compiled_input):
        subject = original_parent_subject(
            dispatch=dispatch,
            compiled_input=compiled_input,
        )
        prepared_parent_subjects.append(subject)
        return subject

    monkeypatch.setattr(
        provider_dispatch,
        "_prepare_subagent_parent_context_call_subject",
        capture_parent_subject,
    )
    monkeypatch.setattr(kernel_host, "DirectKernelModelPort", lambda **_: model)
    monkeypatch.setattr(
        kernel_host.LocalMcpManagementService,
        "load_configs",
        lambda *_args, **_kwargs: (),
    )
    runtime = test_model_runtime(
        model_id="test-pro",
        wire_api="openai_chat_completions",
        postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
    )

    async def scenario() -> None:
        core = KernelHostCore.production(model_runtime=runtime)
        session = await core.open_session(
            HostWorkspaceInput(workspace_kind="project", workspace_root=tmp_path)
        )
        try:
            await session.update_model_call_binding(test_model_binding(runtime))
            result = await session.run_turn(
                PromptContent(
                    (
                        LLMTextPart("before"),
                        PromptImagePart(image_bytes, "image/png"),
                        LLMTextPart("after"),
                    )
                ),
                command_id="command:k2-root-parent-context",
            )
            assert result.final_text == "STAGE2_DOGFOOD_OK"
        finally:
            await core.close_session(
                session.host_session_id,
                close_conversation=True,
            )
            await core.shutdown()

    asyncio.run(scenario())
    assert len(prepared_parent_subjects) == 1
    assert len(prepared_parent_subjects[0].ordered_eligible_units) == 1
    assert prepared_parent_subjects[0].ordered_eligible_units[
        0
    ].ordered_public_items == (
        "USER: before[image part 1 omitted from parent context; "
        "visual content unavailable]after",
    )


def test_k2_public_host_consumes_typed_image_steer_at_provider_safe_point(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage2_migrated_postgres_database,
) -> None:
    import pulsara_agent.conversation_kernel.host as kernel_host

    payload = BytesIO()
    Image.new("RGB", (7, 5), (11, 23, 41)).save(payload, "PNG")
    image_bytes = payload.getvalue()
    expected = (
        LLMTextPart("redirect"),
        LLMImagePart("image/png", image_bytes, 7, 5),
    )
    model = _K2SteerImageCompileProbe()
    monkeypatch.setattr(kernel_host, "DirectKernelModelPort", lambda **_: model)
    monkeypatch.setattr(
        kernel_host.LocalMcpManagementService,
        "load_configs",
        lambda *_args, **_kwargs: (),
    )
    runtime = test_model_runtime(
        model_id="test-pro",
        wire_api="openai_chat_completions",
        postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
    )
    session_id = ""
    turn_id = ""

    async def scenario() -> None:
        nonlocal session_id, turn_id
        core = KernelHostCore.production(model_runtime=runtime)
        session = await core.open_session(
            HostWorkspaceInput(workspace_kind="project", workspace_root=tmp_path)
        )
        session_id = session.session_id
        try:
            await session.update_model_call_binding(test_model_binding(runtime))
            running = asyncio.create_task(
                session.run_turn(
                    PromptContent.text("initial"),
                    command_id="command:k2-image-steer-root",
                )
            )
            await asyncio.wait_for(model.started.wait(), timeout=2)
            assert session._active_turn_id is not None  # noqa: SLF001
            turn_id = session._active_turn_id  # noqa: SLF001
            outcome = await session.steer_active_turn(
                command_id="command:k2-image-steer",
                content=PromptContent(
                    (
                        LLMTextPart("redirect"),
                        PromptImagePart(image_bytes, "image/png"),
                    )
                ),
                target_turn_id=turn_id,
            )
            assert outcome.status == "PENDING"
            model.release.set()
            result = await asyncio.wait_for(running, timeout=5)
            assert result.final_text == "AFTER_STEER"
        finally:
            await core.close_session(
                session.host_session_id,
                close_conversation=False,
            )
            await core.shutdown()

    asyncio.run(scenario())

    assert model.semantic_input is not None
    assert any(
        message.role is MessageRole.USER and message.content == expected
        for message in model.semantic_input.messages
    )
    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as connection:
        ref = connection.execute(
            """SELECT e.entry_kind, r.ref_ordinal, b.body
               FROM pulsara_v3.transcript_entries e
               JOIN pulsara_v3.canonical_image_refs r
                 ON r.session_id=e.session_id AND r.transcript_entry_id=e.id
               JOIN pulsara_v3.blobs b ON b.id=r.blob_id
               WHERE e.session_id=%s AND e.turn_id=%s AND e.entry_kind='USER_STEER'""",
            (session_id, turn_id),
        ).fetchone()
    assert ref is not None
    assert (ref[0], ref[1], bytes(ref[2])) == ("USER_STEER", 0, image_bytes)


def test_k3_queued_root_resource_failure_rejects_before_consumption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage2_migrated_postgres_database,
) -> None:
    import pulsara_agent.conversation_kernel.host as kernel_host
    import pulsara_agent.conversation_kernel.provider_dispatch as provider_dispatch

    model = _SteerModelPort()
    monkeypatch.setattr(kernel_host, "DirectKernelModelPort", lambda **_: model)
    monkeypatch.setattr(
        kernel_host.LocalMcpManagementService,
        "load_configs",
        lambda *_args, **_kwargs: (),
    )
    runtime = test_model_runtime(
        model_id="test-pro",
        wire_api="openai_chat_completions",
        input_modalities=("text", "image"),
        postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
    )
    image_output = BytesIO()
    Image.effect_noise((256, 256), 100).convert("RGB").save(image_output, "PNG")
    image_bytes = image_output.getvalue()
    maximum_canonical_expanded_bytes = (4 << 20) + 1_024
    values = {
        "maximum_canonical_items": 4_096,
        "maximum_canonical_expanded_bytes": maximum_canonical_expanded_bytes,
        "maximum_epoch_logical_bytes": 64 << 20,
        "reserved_canonical_items": 296,
        "reserved_canonical_expanded_bytes": 4 << 20,
        "reserved_epoch_logical_bytes": 4 << 20,
    }
    constrained_bounds = ResolvedCompactionHeadroomBounds(
        **values,
        resolved_hard_bound_set_fingerprint=context_fingerprint(
            "pulsara.compaction-resource-headroom.v3-expanded-content",
            {"role": "minimum_service_headroom", **values},
        ),
    )
    default_bounds = provider_dispatch.resolved_compaction_headroom_bounds()
    active_bounds = [default_bounds]
    monkeypatch.setattr(
        provider_dispatch,
        "resolved_compaction_headroom_bounds",
        lambda: active_bounds[0],
    )

    async def scenario() -> tuple[str, str]:
        core = KernelHostCore.production(model_runtime=runtime)
        session = await core.open_session(
            HostWorkspaceInput(workspace_kind="project", workspace_root=tmp_path)
        )
        try:
            await session.update_model_call_binding(test_model_binding(runtime))
            running = asyncio.create_task(
                session.run_turn(
                    PromptContent.text("occupy the ROOT slot"),
                    command_id="command:k3-queue-origin",
                )
            )
            await asyncio.wait_for(model.started.wait(), timeout=2)
            queued = await session.submit_prompt(
                command_id="command:k3-queue-image",
                content=PromptContent(
                    (PromptImagePart(image_bytes, "image/png"),)
                ),
            )
            assert queued.status == "PENDING"
            assert queued.prompt_delivery is not None
            queue_item_id = queued.prompt_delivery.queue_item_id

            # This exact multipart exceeds C even as the only user input after G.
            # Compaction is explicitly unavailable, so delivery must settle the
            # existing queue owner rather than publish a USER entry first.
            active_bounds[0] = constrained_bounds
            session._compaction.policy = ResolvedCompactionPolicy(  # noqa: SLF001
                enabled=False,
                automatic_enabled=False,
                manual_enabled=False,
            )
            model.release.set()
            assert (await asyncio.wait_for(running, timeout=5)).final_text == "BEFORE_STEER"

            deadline = monotonic() + 5
            while True:
                outcome = await session.query_command("command:k3-queue-image")
                assert outcome is not None
                if outcome.status != "PENDING":
                    break
                assert monotonic() < deadline
                await asyncio.sleep(0.01)
            assert outcome.status == "REJECTED"
            assert (
                outcome.public_code
                == "PROVIDER_INPUT_RESOURCE_EXHAUSTED_BEFORE_DELIVERY"
            )
            return session.session_id, queue_item_id
        finally:
            await core.close_session(
                session.host_session_id,
                close_conversation=False,
            )
            await core.shutdown()

    session_id, queue_item_id = asyncio.run(scenario())

    assert len(model.requests) == 1
    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as connection:
        queue = connection.execute(
            "SELECT status, terminal_reason, consumed_entry_id "
            "FROM pulsara_v3.prompt_queue_items WHERE session_id=%s AND id=%s",
            (session_id, queue_item_id),
        ).fetchone()
        counts = connection.execute(
            "SELECT "
            "(SELECT count(*) FROM pulsara_v3.turns WHERE session_id=%s), "
            "(SELECT count(*) FROM pulsara_v3.transcript_entries WHERE session_id=%s), "
            "(SELECT count(*) FROM pulsara_v3.canonical_image_refs "
            " WHERE session_id=%s AND queue_item_id=%s)",
            (session_id, session_id, session_id, queue_item_id),
        ).fetchone()
    assert queue == (
        "REJECTED",
        "PROVIDER_INPUT_RESOURCE_EXHAUSTED_BEFORE_DELIVERY",
        None,
    )
    assert counts == (1, 2, 1)


def _exercise_host_steer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage2_migrated_postgres_database,
    redirect_queued: bool,
) -> None:
    import pulsara_agent.conversation_kernel.host as kernel_host

    model = _SteerModelPort()
    monkeypatch.setattr(kernel_host, "DirectKernelModelPort", lambda **_: model)
    monkeypatch.setattr(kernel_host.LocalMcpManagementService, "load_configs", lambda *_args, **_kwargs: ())
    model_runtime = test_model_runtime(
        api_key="sk-fixture-secret",
        base_url="https://example.invalid/v1",
        model_id="test-pro",
        wire_api="openai_chat_completions",
        postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
    )

    async def scenario() -> None:
        core = KernelHostCore.production(model_runtime=model_runtime)
        session = await core.open_session(
            HostWorkspaceInput(
                workspace_kind="project",
                workspace_root=tmp_path,
            )
        )
        await session.update_model_call_binding(test_model_binding(model_runtime))
        running = asyncio.create_task(
            session.run_turn(PromptContent.text("initial"), command_id="command:steer-root")
        )
        await asyncio.wait_for(model.started.wait(), timeout=2)
        assert session._active_turn_id is not None  # noqa: SLF001
        if redirect_queued:
            queued = await session.submit_prompt(
                command_id="command:queued-source", content=PromptContent.text("new direction"),
            )
            assert queued.prompt_delivery is not None
            outcome = await session.steer_queued_prompt(
                command_id="command:steer",
                source_queue_item_id=queued.prompt_delivery.queue_item_id,
                target_turn_id=session._active_turn_id,
            )
            assert outcome.public_code == "PROMPT_STEER_QUEUED"
        else:
            outcome = await session.steer_active_turn(
                command_id="command:steer",
                content=PromptContent.text("new direction"),
                target_turn_id=session._active_turn_id,  # noqa: SLF001
            )
        assert outcome.status == "PENDING"
        model.release.set()
        result = await asyncio.wait_for(running, timeout=5)
        assert result.final_text == "AFTER_STEER"
        assert len(model.requests) == 2
        second = model.requests[1]
        first_input = model.requests[0].compiled_input
        assert second.compiled_input.system_prompt == first_input.system_prompt
        assert second.compiled_input.tools == first_input.tools
        assert second.compiled_input.messages[:len(first_input.messages)] == first_input.messages
        assert any(
            item.role is MessageRole.USER
            and item.content == (LLMTextPart("new direction"),)
            for item in second.compiled_input.messages  # type: ignore[attr-defined]
        )
        rows = await asyncio.to_thread(
            session.repository.rehydrate_session,
            session_id=session.session_id,
            deadline_monotonic=monotonic() + 5,
        )
        assert [row["entry_kind"] for row in rows] == [
            "USER_MESSAGE",
            "ASSISTANT_MESSAGE",
            "USER_STEER",
            "ASSISTANT_MESSAGE",
        ]
        assert {row["turn_id"] for row in rows} == {result.turn_id}
        await core.close_session(
            session.host_session_id,
            close_conversation=True,
        )
        await core.shutdown()

    asyncio.run(scenario())


def test_stage2_host_consumes_exact_active_turn_steer_at_provider_safe_point(tmp_path, monkeypatch, stage2_migrated_postgres_database):
    _exercise_host_steer(tmp_path, monkeypatch, stage2_migrated_postgres_database, False)


def test_k3_root_output_seal_linearizes_and_cancels_late_steer_waiters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage2_migrated_postgres_database,
) -> None:
    import pulsara_agent.conversation_kernel.host as kernel_host

    model = _SteerModelPort()
    monkeypatch.setattr(kernel_host, "DirectKernelModelPort", lambda **_: model)
    monkeypatch.setattr(
        kernel_host.LocalMcpManagementService,
        "load_configs",
        lambda *_args, **_kwargs: (),
    )
    model_runtime = test_model_runtime(
        model_id="test-pro",
        wire_api="openai_chat_completions",
        postgres_dsn=stage2_migrated_postgres_database.runtime_dsn,
    )

    async def scenario() -> None:
        core = KernelHostCore.production(model_runtime=model_runtime)
        session = await core.open_session(
            HostWorkspaceInput(workspace_kind="project", workspace_root=tmp_path)
        )
        try:
            await session.update_model_call_binding(test_model_binding(model_runtime))
            settlement_started = asyncio.Event()
            release_settlement = asyncio.Event()
            settle = session._assistant_settlements.settle  # noqa: SLF001

            async def gated_settle(candidate):
                settlement_started.set()
                await release_settlement.wait()
                return await settle(candidate)

            monkeypatch.setattr(
                session._assistant_settlements,  # noqa: SLF001
                "settle",
                gated_settle,
            )
            running = asyncio.create_task(
                session.run_turn(
                    PromptContent.text("initial"),
                    command_id="command:k3-output-seal-root",
                )
            )
            await asyncio.wait_for(model.started.wait(), timeout=2)
            turn_id = session._active_turn_id  # noqa: SLF001
            assert turn_id is not None
            model.release.set()
            await asyncio.wait_for(settlement_started.wait(), timeout=2)

            cancelled = asyncio.create_task(
                session.steer_active_turn(
                    command_id="command:k3-output-seal-cancelled",
                    content=PromptContent.text("cancel while sealed"),
                    target_turn_id=turn_id,
                )
            )
            waiting = asyncio.create_task(
                session.steer_active_turn(
                    command_id="command:k3-output-seal-waiting",
                    content=PromptContent.text("wait while sealed"),
                    target_turn_id=turn_id,
                )
            )
            await asyncio.sleep(0)
            assert not cancelled.done()
            assert not waiting.done()
            cancelled.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled

            release_settlement.set()
            result = await asyncio.wait_for(running, timeout=5)
            late = await asyncio.wait_for(waiting, timeout=5)
            assert result.final_text == "BEFORE_STEER"
            assert len(model.requests) == 1
            assert late.status == "REJECTED"
            assert late.public_code == "STEER_TARGET_STALE"
            assert await session.query_command(
                "command:k3-output-seal-cancelled"
            ) is None
        finally:
            await core.close_session(
                session.host_session_id,
                close_conversation=True,
            )
            await core.shutdown()

    asyncio.run(scenario())


def test_pr04_host_redirect_consumes_same_turn_steer_with_prefix_continuity(tmp_path, monkeypatch, stage2_migrated_postgres_database):
    _exercise_host_steer(tmp_path, monkeypatch, stage2_migrated_postgres_database, True)
