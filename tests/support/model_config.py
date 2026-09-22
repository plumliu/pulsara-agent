"""Production-shaped model-runtime helpers for Kernel tests."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from time import monotonic

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
    ReasoningWireProfile,
)
from pulsara_agent.llm.model_target import RouteWireRegistry
from pulsara_agent.llm.normalized_transport import NormalizedLLMTransportRegistry
from pulsara_agent.llm.provider import RouteWireProfile
from pulsara_agent.llm.provider_replay import provider_replay_contract_fingerprint
from pulsara_agent.llm.provider_replay import (
    ProviderReplayTargetCompatibilityFact,
    build_provider_replay_target_compatibility,
)
from pulsara_agent.llm.retry import LLMRetryConfig
from pulsara_agent.llm.route_wires import production_route_wire_registry
from pulsara_agent.llm.runtime import BorrowedProviderTransport, ModelRuntime
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
    _test_call_overrides: dict[str, object] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _test_default_connection_id: ModelConnectionId | None = field(
        default=None,
        init=False,
        repr=False,
    )

    def register_test_call_override(self, call) -> None:
        self._test_call_overrides[call.resolved_model_call_id] = call

    def borrow_transport(self, purpose_permit):
        call = self._test_call_overrides.get(
            purpose_permit.resolved_model_call_id,
            None,
        )
        if call is None:
            return ModelRuntime.borrow_transport(self, purpose_permit)
        target = purpose_permit.call_target
        if (
            call.fact.purpose is not target.purpose
            or call.binding != target.model_call_binding
            or call.target.fact != target.target_bundle.target_fact
        ):
            raise RuntimeError("test provider-call override does not exact-join")
        purpose_permit._consume_once()
        return BorrowedProviderTransport(call, credential_owner=None)

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
        ReasoningWireProfile.CATALOG_STANDARD,
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
    runtime = TestModelRuntime(  # type: ignore[arg-type] - explicit immutable test store
        settings=settings,
        catalog=catalog,
        route_wires=route_wires,
        retry=retry,
    )
    runtime._test_default_connection_id = connection_id
    return runtime


def include_test_runtime_connections(
    destination: TestModelRuntime,
    *sources: TestModelRuntime,
) -> TestModelRuntime:
    """Keep immutable predecessor connections available after a test model switch."""

    destination_settings = destination.settings.read()
    connections = {item.id: item for item in destination_settings.model_connections}
    api_keys = {
        item.connection_id: item for item in destination_settings.model_api_keys
    }
    destination_snapshot = destination.catalog.snapshot
    if destination_snapshot is None:
        raise AssertionError("destination test catalog is unavailable")
    entries = dict(destination_snapshot.entries)
    route_entries: dict[str, dict[ModelCatalogEntryKey, ModelCatalogEntry]] = {}
    route_names: dict[str, str] = {}
    for route in destination_snapshot.routes:
        route_names[route.route_id] = route.display_name
        route_entries.setdefault(route.route_id, {}).update(
            (entry.key, entry) for entry in route.entries
        )
    for source in sources:
        settings = source.settings.read()
        connections.update((item.id, item) for item in settings.model_connections)
        api_keys.update((item.connection_id, item) for item in settings.model_api_keys)
        snapshot = source.catalog.snapshot
        if snapshot is None:
            raise AssertionError("source test catalog is unavailable")
        entries.update(snapshot.entries)
        for route in snapshot.routes:
            route_names.setdefault(route.route_id, route.display_name)
            route_entries.setdefault(route.route_id, {}).update(
                (entry.key, entry) for entry in route.entries
            )
    destination.settings.value = replace(
        destination_settings,
        model_connections=tuple(connections.values()),
        model_api_keys=tuple(api_keys.values()),
    )
    destination.catalog._snapshot = ModelCatalogSnapshot(  # noqa: SLF001
        entries=entries,
        routes=tuple(
            ModelCatalogRoute(
                route_id=route_id,
                display_name=route_names[route_id],
                entries=tuple(values.values()),
            )
            for route_id, values in route_entries.items()
        ),
    )
    return destination


def test_model_binding(runtime: ModelRuntime) -> ModelCallBinding:
    connections = runtime.settings.read().model_connections
    default_id = getattr(runtime, "_test_default_connection_id", None)
    if default_id is None:
        if len(connections) != 1:
            raise AssertionError("test runtime lacks one default connection")
        default_id = connections[0].id
    return ModelCallBinding(default_id, None)


def build_test_provider_replay_target(
    *,
    wire_api: str = "openai_chat_completions",
    endpoint: str = "1",
    model_id: str = "test-model",
    transport_binding_id: str | None = None,
) -> ProviderReplayTargetCompatibilityFact:
    """Build replay compatibility through the hard-cut resolved target fact."""

    runtime = test_model_runtime(
        wire_api=wire_api,
        base_url=f"https://replay-{endpoint}.example.invalid/v1",
        model_id=model_id,
    )
    target = runtime.resolve_target(
        test_model_binding(runtime),
        timeout_policy=OpenAITransportTimeoutPolicy(
            connect_seconds=1,
            write_seconds=1,
            pool_seconds=1,
            read_idle_seconds=1,
            total_seconds=2,
        ),
    )
    fact = target.fact
    if transport_binding_id is not None:
        fact = fact.model_copy(update={"transport_binding_id": transport_binding_id})
    return build_provider_replay_target_compatibility(target_fact=fact)


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
    admission = object.__new__(PreparedRootProviderInputAdmission)
    object.__setattr__(admission, "candidate", candidate)
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
    lease = repository.acquire_host_writer(intent=kwargs.pop("intent", "NEW"), **kwargs)
    bind_test_session(repository, lease)
    return lease


test_model_limits.__test__ = False
test_model_runtime.__test__ = False
include_test_runtime_connections.__test__ = False
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
    "include_test_runtime_connections",
    "start_test_root_turn",
    "test_model_binding",
    "test_model_resolution_snapshot",
    "test_model_limits",
    "test_model_runtime",
]
