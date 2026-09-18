"""Process-local owner joining saved connections, catalog and wire adapters."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock

from pulsara_agent.llm.adapters.openai.chat_completions import (
    OpenAIChatCompletionsTransport,
)
from pulsara_agent.llm.adapters.openai.client import OpenAITransportTimeoutPolicy
from pulsara_agent.llm.adapters.openai.responses import OpenAIResponsesTransport
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, SelectableModelCatalog
from pulsara_agent.llm.model_connections import (
    ModelCallBinding,
    ModelConnectionConfig,
    ModelConnectionId,
)
from pulsara_agent.llm.frozen_target import (
    FrozenEpochModelTargetBundle,
    _freeze_provider_projection_strategy,
    _freeze_token_estimator_strategy,
)
from pulsara_agent.llm.model_target import (
    FrozenModelResolutionSnapshot,
    ResolvedModelConnection,
    RouteWireRegistry,
    resolve_model_target_contract,
)
from pulsara_agent.llm.normalized_transport import (
    NormalizedLLMTransport,
    NormalizedLLMTransportRegistry,
)
from pulsara_agent.llm.resolution import (
    ResolvedModelTarget,
    resolve_model_target,
    with_call_output_cap,
)
from pulsara_agent.llm.resolution import ResolvedModelCall, resolve_model_call
from pulsara_agent.llm.provider_open import (
    AuxiliaryModelProviderOpenPermit,
    CompactionSummaryProviderOpenPermit,
    ConnectionProbeProviderOpenPermit,
    EpochAgentLoopProviderOpenPermit,
    ProviderOpenPurposePermit,
)
from pulsara_agent.llm.retry import LLMRetryConfig
from pulsara_agent.llm.route_wires import production_route_wire_registry
from pulsara_agent.primitives.model_call import ModelCallPurpose
from pulsara_agent.settings import LocalSettingsStore


class ModelRuntimeUnavailable(RuntimeError):
    pass


class BorrowedProviderTransport:
    """One-operation live borrow created only after a purpose permit is consumed."""

    __slots__ = ("call", "_credential_owner", "_closed", "_lock")

    def __init__(self, call: ResolvedModelCall, *, credential_owner: object | None) -> None:
        self.call = call
        self._credential_owner = credential_owner
        self._closed = False
        self._lock = Lock()

    def open_stream(self, *, context):
        with self._lock:
            if self._closed:
                raise RuntimeError("provider transport borrow is closed")
        return self.call.target.transport.open_stream(call=self.call, context=context)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            owner = self._credential_owner
            self._credential_owner = None
        if owner is not None:
            owner.close()


@dataclass(slots=True)
class ModelRuntime:
    settings: LocalSettingsStore
    catalog: ModelCatalogOwner
    route_wires: RouteWireRegistry
    retry: LLMRetryConfig = LLMRetryConfig()

    @classmethod
    def production(
        cls,
        *,
        settings: LocalSettingsStore,
        catalog: ModelCatalogOwner,
    ) -> "ModelRuntime":
        return cls(settings, catalog, production_route_wire_registry())

    def connection(self, binding: ModelCallBinding) -> ModelConnectionConfig:
        try:
            settings = self.settings.read()
        except Exception as exc:
            raise ModelRuntimeUnavailable("local model settings are unavailable") from exc
        connection = settings.connection(binding.connection_id)
        if connection is None:
            raise ModelRuntimeUnavailable("model connection metadata is unavailable")
        return connection

    def selectable_catalog(self) -> SelectableModelCatalog:
        catalog = self.catalog.selectable()
        if catalog is None:
            raise ModelRuntimeUnavailable("model catalog snapshot is unavailable")
        return catalog

    def freeze_resolution_snapshot(self) -> FrozenModelResolutionSnapshot:
        """Freeze all non-secret facts needed inside one canonical admission."""

        try:
            settings = self.settings.read()
        except Exception as exc:
            raise ModelRuntimeUnavailable("local model settings are unavailable") from exc
        catalog = self.catalog.selectable()
        resolved: dict[ModelConnectionId, ResolvedModelConnection] = {}
        unavailable: dict[ModelConnectionId, str] = {}
        for connection in settings.model_connections:
            try:
                target = resolve_model_target_contract(
                    catalog=catalog,
                    connection=connection,
                    route_wires=self.route_wires,
                )
            except (KeyError, ValueError) as exc:
                unavailable[connection.id] = str(exc)
            else:
                resolved[connection.id] = ResolvedModelConnection(connection, target)
        return FrozenModelResolutionSnapshot(resolved, unavailable)

    def resolve_target(
        self,
        binding: ModelCallBinding,
        *,
        timeout_policy: OpenAITransportTimeoutPolicy,
    ) -> ResolvedModelTarget:
        registry = self.transport_registry(timeout_policy)
        return resolve_model_target(
            connection=self.connection(binding),
            binding=binding,
            catalog=self.catalog.selectable(),
            route_wires=self.route_wires,
            registry=registry,
        )

    def resolve_frozen_target_bundle(
        self,
        bundle: FrozenEpochModelTargetBundle,
        *,
        binding: ModelCallBinding,
        timeout_policy: OpenAITransportTimeoutPolicy,
    ) -> ResolvedModelTarget:
        """Borrow current physical resolution only when all frozen facts match."""

        connection = self.connection(binding)
        if (
            connection.id != bundle.connection.connection_id
            or connection.authentication
            is not bundle.connection.authentication_mode
        ):
            raise ModelRuntimeUnavailable(
                "current model connection differs from the installed epoch contract"
            )
        target = resolve_model_target(
            connection=connection,
            binding=binding,
            catalog=self.catalog.selectable(),
            route_wires=self.route_wires,
            registry=self.transport_registry(timeout_policy),
        )
        if (
            target.fact != bundle.target_fact
            or target.contract.reasoning != bundle.reasoning_contract
            or _freeze_provider_projection_strategy(target)
            != bundle.projection_strategy
            or _freeze_token_estimator_strategy(target) != bundle.estimator
        ):
            raise ModelRuntimeUnavailable(
                "current runtime cannot reproduce the installed epoch target"
            )
        return target

    def borrow_transport(
        self, purpose_permit: ProviderOpenPurposePermit
    ) -> BorrowedProviderTransport:
        """Consume exactly one closed purpose permit and reborrow its target."""

        if isinstance(purpose_permit, EpochAgentLoopProviderOpenPermit):
            expected = ModelCallPurpose.AGENT_MODEL_LOOP
            credential_owner = None
        elif isinstance(purpose_permit, CompactionSummaryProviderOpenPermit):
            expected = ModelCallPurpose.CONTEXT_COMPACTION_SUMMARY
            credential_owner = None
        elif isinstance(purpose_permit, AuxiliaryModelProviderOpenPermit):
            expected = ModelCallPurpose.MEMORY_GOVERNANCE
            credential_owner = None
        elif isinstance(purpose_permit, ConnectionProbeProviderOpenPermit):
            expected = ModelCallPurpose.CONNECTION_PROBE
            credential_owner = purpose_permit.credential_owner
        else:  # pragma: no cover - the public union is closed
            raise TypeError("provider-open purpose permit union is not closed")
        call_target = purpose_permit.call_target
        if call_target.purpose is not expected:
            raise ModelRuntimeUnavailable("provider-open permit purpose drifted")
        purpose_permit._consume_once()
        try:
            if isinstance(purpose_permit, ConnectionProbeProviderOpenPermit):
                connection = purpose_permit.credential_owner.connection
                settings = purpose_permit.credential_owner
                target = resolve_model_target(
                    connection=connection,
                    binding=call_target.model_call_binding,
                    catalog=self.catalog.selectable(),
                    route_wires=self.route_wires,
                    registry=self._transport_registry_for_settings(
                        settings, purpose_permit.timeout_policy
                    ),
                )
                bundle = call_target.target_bundle
                if (
                    target.fact != bundle.target_fact
                    or target.contract.reasoning != bundle.reasoning_contract
                    or _freeze_provider_projection_strategy(target)
                    != bundle.projection_strategy
                    or _freeze_token_estimator_strategy(target) != bundle.estimator
                ):
                    raise ModelRuntimeUnavailable(
                        "draft probe target changed before provider open"
                    )
            else:
                target = self.resolve_frozen_target_bundle(
                    call_target.target_bundle,
                    binding=call_target.model_call_binding,
                    timeout_policy=purpose_permit.timeout_policy,
                )
            target = with_call_output_cap(
                target,
                call_target.input_budget.effective_output_tokens,
            )
            if (
                target.context_budget.input_budget_tokens
                < call_target.input_budget.effective_input_budget_tokens
            ):
                raise ModelRuntimeUnavailable(
                    "current runtime cannot reproduce the frozen call budget"
                )
            call = resolve_model_call(
                target=target,
                binding=call_target.model_call_binding,
                purpose=call_target.purpose,
                resolved_model_call_id=purpose_permit.resolved_model_call_id,
            )
            return BorrowedProviderTransport(
                call,
                credential_owner=credential_owner,
            )
        except BaseException:
            if credential_owner is not None:
                credential_owner.close()
            raise

    def transport_registry(
        self, timeout_policy: OpenAITransportTimeoutPolicy
    ) -> NormalizedLLMTransportRegistry:
        return self._transport_registry_for_settings(self.settings, timeout_policy)

    def _transport_registry_for_settings(
        self,
        settings,
        timeout_policy: OpenAITransportTimeoutPolicy,
    ) -> NormalizedLLMTransportRegistry:
        registry = NormalizedLLMTransportRegistry()
        registry.register(
            NormalizedLLMTransport(
                OpenAIResponsesTransport(
                    settings=settings,
                    timeout_policy=timeout_policy,
                    retry_config=self.retry,
                )
            )
        )
        registry.register(
            NormalizedLLMTransport(
                OpenAIChatCompletionsTransport(
                    settings=settings,
                    timeout_policy=timeout_policy,
                    retry_config=self.retry,
                )
            )
        )
        return registry


__all__ = ["BorrowedProviderTransport", "ModelRuntime", "ModelRuntimeUnavailable"]
