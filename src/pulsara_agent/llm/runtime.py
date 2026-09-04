"""Process-local owner joining saved connections, catalog and wire adapters."""

from __future__ import annotations

from dataclasses import dataclass

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
from pulsara_agent.llm.resolution import ResolvedModelTarget, resolve_model_target
from pulsara_agent.llm.retry import LLMRetryConfig
from pulsara_agent.llm.route_wires import production_route_wire_registry
from pulsara_agent.settings import LocalSettingsStore


class ModelRuntimeUnavailable(RuntimeError):
    pass


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

    def transport_registry(
        self, timeout_policy: OpenAITransportTimeoutPolicy
    ) -> NormalizedLLMTransportRegistry:
        registry = NormalizedLLMTransportRegistry()
        registry.register(
            NormalizedLLMTransport(
                OpenAIResponsesTransport(
                    settings=self.settings,
                    timeout_policy=timeout_policy,
                    retry_config=self.retry,
                )
            )
        )
        registry.register(
            NormalizedLLMTransport(
                OpenAIChatCompletionsTransport(
                    settings=self.settings,
                    timeout_policy=timeout_policy,
                    retry_config=self.retry,
                )
            )
        )
        return registry


__all__ = ["ModelRuntime", "ModelRuntimeUnavailable"]
