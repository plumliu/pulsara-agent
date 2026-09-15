"""Production-shaped model-runtime helpers for Kernel tests."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from time import monotonic
from types import SimpleNamespace
from typing import cast

from pulsara_agent.llm.adapters.openai.client import OpenAITransportTimeoutPolicy

from pulsara_agent.llm.model_catalog import (
    ModelCatalogEntry,
    ModelCatalogEntryKey,
    ModelCatalogOwner,
    ModelCatalogRoute,
    ModelCatalogSnapshot,
    ModelHardLimits,
    ModelTargetKey,
    ModelsDevCatalogClient,
    ReasoningControlContract,
    ReasoningProviderDefault,
    RouteWireDialect,
    WireApi,
)
from pulsara_agent.llm.model_connections import (
    ModelCallBinding,
    ModelConnectionConfig,
    ModelConnectionId,
)
from pulsara_agent.llm.model_target import RouteWireRegistry
from pulsara_agent.llm.normalized_transport import NormalizedLLMTransportRegistry
from pulsara_agent.llm.provider import RouteWireProfile
from pulsara_agent.llm.provider_replay import provider_replay_contract_fingerprint
from pulsara_agent.llm.retry import LLMRetryConfig
from pulsara_agent.llm.route_wires import production_route_wire_registry
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.primitives.model_call import ModelContextLimits
from pulsara_agent.conversation_kernel.contracts import PromptDeliveryMode
from pulsara_agent.conversation_kernel.prompt_content import (
    FrozenCanonicalPrompt,
    freeze_canonical_prompt,
)
from pulsara_agent.conversation_kernel.repository import (
    build_prepared_root_turn_intent,
)
from pulsara_agent.conversation_kernel.steer import build_prompt_ingress_command
from pulsara_agent.conversation_kernel.steer import PreparedRootProviderInputAdmission
from pulsara_agent.llm.input import FrozenPromptContent
from pulsara_agent.settings import LocalModelApiKey, LocalPostgresConfig, LocalSettings


@dataclass(slots=True)
class StaticTestSettingsStore:
    """Database-independent immutable settings source for component tests."""

    value: LocalSettings

    def read(self) -> LocalSettings:
        return self.value


@dataclass(slots=True)
class TestModelRuntime(ModelRuntime):
    """Keep test transports stable so recorded chunks can be injected exactly."""

    _test_registries: dict[str, NormalizedLLMTransportRegistry] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    def transport_registry(
        self, timeout_policy: OpenAITransportTimeoutPolicy
    ) -> NormalizedLLMTransportRegistry:
        key = timeout_policy.policy_fingerprint
        registry = self._test_registries.get(key)
        if registry is None:
            registry = ModelRuntime.transport_registry(self, timeout_policy)
            self._test_registries[key] = registry
        return registry


def test_model_limits(
    *,
    total_context_tokens: int = 256_000,
    max_input_tokens: int = 256_000,
    max_output_tokens: int = 8_192,
    default_output_tokens: int = 8_192,
    input_safety_margin_tokens: int = 8_192,
) -> ModelContextLimits:
    return ModelContextLimits(
        total_context_tokens=total_context_tokens,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        default_output_tokens=default_output_tokens,
        input_safety_margin_tokens=input_safety_margin_tokens,
    )


def test_model_runtime(
    *,
    api_key: str = "sk-fixture-secret",
    base_url: str = "https://example.invalid/v1",
    model_id: str = "test-model",
    wire_api: str = "openai_responses",
    route_wire_profile: RouteWireProfile | None = None,
    retry: LLMRetryConfig = LLMRetryConfig(),
    limits: ModelContextLimits | None = None,
    postgres_dsn: str | None = None,
    connection_id: ModelConnectionId | None = None,
    tool_call: bool | None = True,
    reasoning: ReasoningControlContract | None = None,
    input_modalities: tuple[str, ...] | None = None,
    output_modalities: tuple[str, ...] | None = None,
) -> TestModelRuntime:
    """Build one exact catalog-backed connection with one typed credential."""

    parsed_wire_api = WireApi(wire_api)
    resolved_limits = limits or test_model_limits()
    hard_limits = ModelHardLimits(
        resolved_limits.total_context_tokens,
        resolved_limits.max_input_tokens,
        resolved_limits.max_output_tokens,
    )
    key = ModelCatalogEntryKey("test", model_id)
    entry = ModelCatalogEntry(
        key=key,
        route_name="Test Route",
        display_name=model_id,
        endpoint=base_url,
        wire_dialect=RouteWireDialect.OPENAI_COMPATIBLE,
        total_context_tokens=resolved_limits.total_context_tokens,
        limits=hard_limits,
        reasoning=reasoning or ReasoningProviderDefault(),
        tool_call=tool_call,
        wire_shape_hint=(
            "completions"
            if parsed_wire_api is WireApi.OPENAI_CHAT_COMPLETIONS
            else "responses"
        ),
        input_modalities=input_modalities,
        output_modalities=output_modalities,
    )
    snapshot = ModelCatalogSnapshot(
        entries={key: entry},
        routes=(ModelCatalogRoute("test", "Test Route", (entry,)),),
    )
    catalog = ModelCatalogOwner(ModelsDevCatalogClient())
    catalog._snapshot = snapshot  # noqa: SLF001 - explicit immutable test fixture

    connection_id = connection_id or ModelConnectionId("model-connection:" + "0" * 32)
    connection = ModelConnectionConfig(
        connection_id,
        ModelTargetKey("test", parsed_wire_api, model_id),
        base_url,
    )
    settings = StaticTestSettingsStore(
        LocalSettings(
            postgres=(
                LocalPostgresConfig(postgres_dsn) if postgres_dsn is not None else None
            ),
            model_connections=(connection,),
            model_api_keys=(LocalModelApiKey(connection_id, api_key),),
        )
    )

    base_contract = production_route_wire_registry().contract_for(
        entry, parsed_wire_api
    )
    profile = route_wire_profile or replace(
        base_contract.profile,
        id=f"test-route-wire:{parsed_wire_api.value}",
    )
    if profile.wire_api != parsed_wire_api.value:
        raise ValueError("test route/wire profile uses another wire API")
    route_wires = RouteWireRegistry()
    route_wires.register_dialect(
        RouteWireDialect.OPENAI_COMPATIBLE,
        parsed_wire_api,
        replace(
            base_contract,
            model_identity_policy=profile.model_identity_policy,
            assistant_replay_contract=provider_replay_contract_fingerprint(
                profile.assistant_replay_codec_kind
            ),
            profile=profile,
        ),
    )
    return TestModelRuntime(  # type: ignore[arg-type] - explicit immutable test store
        settings=settings,
        catalog=catalog,
        route_wires=route_wires,
        retry=retry,
    )


def test_model_binding(runtime: ModelRuntime) -> ModelCallBinding:
    connections = runtime.settings.read().model_connections
    if len(connections) != 1:
        raise AssertionError("test runtime must have exactly one connection")
    return ModelCallBinding(connections[0].id, None)


def test_model_resolution_snapshot():
    """Freeze the production-shaped test target used by direct admissions."""

    return test_model_runtime().freeze_resolution_snapshot()


def frozen_test_prompt(text: str) -> FrozenCanonicalPrompt:
    """Build the exact internal Runner input used by text-only tests."""

    if not isinstance(text, str):
        raise TypeError("test prompt text must be a string")
    return freeze_canonical_prompt(FrozenPromptContent.text(text))


def enqueue_test_prompt(
    repository,
    guard,
    *,
    command_id,
    queue_item_id,
    client_submission_id,
    delivery_mode,
    target_turn_id,
    permission_snapshot_id,
    requested_permission_mode,
    model_call_binding,
    content,
    occurred_at,
    actor_id,
    deadline_monotonic,
    _expected_permission_snapshot=None,
):
    """Exercise the hard-cut typed prompt-ingress admission in tests."""

    if not isinstance(content, FrozenPromptContent):
        raise TypeError("test prompt ingress requires FrozenPromptContent")
    canonical_prompt = freeze_canonical_prompt(content)

    if delivery_mode is PromptDeliveryMode.NEW_TURN and model_call_binding is not None:
        repository.update_session_model_call_binding(
            guard,
            binding=model_call_binding,
            deadline_monotonic=deadline_monotonic,
        )
    if (
        delivery_mode is PromptDeliveryMode.NEW_TURN
        and _expected_permission_snapshot is None
    ):
        _expected_permission_snapshot = repository.prepare_root_permission_snapshot(
            guard,
            snapshot_id=permission_snapshot_id,
            requested_mode=requested_permission_mode,
            deadline_monotonic=deadline_monotonic,
        )
    candidate = build_prompt_ingress_command(
        session_id=guard.session_id,
        command_id=command_id,
        queue_item_id=queue_item_id,
        client_submission_id=client_submission_id,
        delivery_mode=delivery_mode,
        target_turn_id=target_turn_id,
        permission_snapshot_id=permission_snapshot_id,
        requested_permission_mode=requested_permission_mode,
        canonical_prompt=canonical_prompt,
    )
    accepted = repository.enqueue_prompt(
        guard,
        candidate=candidate,
        model_resolution_snapshot=(
            test_model_resolution_snapshot()
            if delivery_mode is PromptDeliveryMode.NEW_TURN
            else None
        ),
        occurred_at=occurred_at,
        actor_id=actor_id,
        deadline_monotonic=deadline_monotonic,
        _expected_permission_snapshot=_expected_permission_snapshot,
    )
    return accepted.queue_sequence


def start_test_root_turn(
    repository,
    guard,
    *,
    command_id,
    turn_id,
    entry_id,
    context_binding_revision_id,
    permission_snapshot_id,
    requested_permission_mode,
    model_call_binding,
    content,
    occurred_at,
    actor_kind="human",
    actor_id="user",
    deadline_monotonic,
):
    """Admit a direct ROOT turn through the canonical binding-under-lock path."""

    if not isinstance(content, FrozenPromptContent):
        raise TypeError("test ROOT ingress requires FrozenPromptContent")
    canonical_prompt = freeze_canonical_prompt(content)

    repository.update_session_model_call_binding(
        guard,
        binding=model_call_binding,
        deadline_monotonic=deadline_monotonic,
    )
    intent = build_prepared_root_turn_intent(
        session_id=guard.session_id,
        command_id=command_id,
        turn_id=turn_id,
        entry_id=entry_id,
        context_binding_revision_id=context_binding_revision_id,
        permission_snapshot_id=permission_snapshot_id,
        requested_permission_mode=requested_permission_mode,
        canonical_prompt=canonical_prompt,
        occurred_at=occurred_at,
        actor_kind=actor_kind,
        actor_id=actor_id,
    )
    resolution_snapshot = test_model_resolution_snapshot()
    candidate = repository.prepare_root_provider_input_candidate(
        guard,
        intent=intent,
        model_resolution_snapshot=resolution_snapshot,
        deadline_monotonic=deadline_monotonic,
    )
    # This helper seeds repository fixtures whose subject is downstream durable
    # behavior.  Production ROOT admission is exercised through Runner/Host tests;
    # here the exact candidate still crosses the hard-cut writer precondition.
    admission = cast(
        PreparedRootProviderInputAdmission,
        SimpleNamespace(candidate=candidate),
    )
    return repository.accept_root_turn_intent(
        guard,
        intent=intent,
        provider_input_admission=admission,
        model_resolution_snapshot=resolution_snapshot,
        deadline_monotonic=deadline_monotonic,
    ).accepted


def bind_test_session(repository, lease, runtime: ModelRuntime | None = None):
    """Install the one explicit test connection as the session composer choice."""

    binding = test_model_binding(runtime or test_model_runtime())
    repository.update_session_model_call_binding(
        lease.guard,
        binding=binding,
        deadline_monotonic=monotonic() + 30,
    )
    return binding


def acquire_bound_test_writer(repository, **kwargs):
    lease = repository.acquire_host_writer(**kwargs)
    bind_test_session(repository, lease)
    return lease


test_model_limits.__test__ = False
test_model_runtime.__test__ = False
test_model_binding.__test__ = False
test_model_resolution_snapshot.__test__ = False
enqueue_test_prompt.__test__ = False
start_test_root_turn.__test__ = False
bind_test_session.__test__ = False
acquire_bound_test_writer.__test__ = False


__all__ = [
    "StaticTestSettingsStore",
    "TestModelRuntime",
    "acquire_bound_test_writer",
    "bind_test_session",
    "enqueue_test_prompt",
    "start_test_root_turn",
    "test_model_binding",
    "test_model_resolution_snapshot",
    "test_model_limits",
    "test_model_runtime",
]
