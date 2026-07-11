"""Resolve immutable model targets and per-execution model calls."""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from pulsara_agent.llm.errors import (
    ModelInputBudgetUnavailable,
    ModelOptionUnsupported,
    ModelTargetBindingMismatch,
    ModelTransportUnavailable,
)
from pulsara_agent.llm.estimator import PulsaraHeuristicTokenEstimatorV1, TokenEstimator
from pulsara_agent.llm.models import ModelProfile, ModelRole
from pulsara_agent.llm.request import LLMOptions
from pulsara_agent.primitives.model_call import (
    ModelCallPurpose,
    ModelContextLimits,
    ModelContextMode,
    ResolvedModelCallFact,
    ResolvedModelContextBudgetFact,
    ResolvedModelOptionsFact,
    ResolvedModelTargetFact,
    resolved_model_options_fingerprint,
    resolved_model_target_fingerprint,
    sha256_fingerprint,
)

if TYPE_CHECKING:
    from pulsara_agent.llm.config import LLMConfig
    from pulsara_agent.llm.registry import LLMTransportRegistry
    from pulsara_agent.llm.transport import LLMTransport


_SENSITIVE_EXACT_KEYS = frozenset(
    {
        "authorization",
        "proxy_authorization",
        "api_key",
        "apikey",
        "x_api_key",
        "access_token",
        "refresh_token",
        "auth_token",
        "bearer_token",
        "password",
        "passwd",
        "client_secret",
        "secret",
        "credential",
        "credentials",
        "cookie",
        "set_cookie",
        "session_token",
    }
)
_SENSITIVE_SUFFIXES = (
    "_access_token",
    "_refresh_token",
    "_auth_token",
    "_bearer_token",
    "_password",
    "_passwd",
    "_client_secret",
    "_credential",
    "_credentials",
)
_PERCENT_ESCAPE_RE = re.compile(r"%([0-9a-fA-F]{2})")
_INVALID_PERCENT_ESCAPE_RE = re.compile(r"%(?![0-9a-fA-F]{2})")


@dataclass(frozen=True, slots=True)
class ResolvedModelTarget:
    model_profile: ModelProfile
    transport: LLMTransport
    effective_options: LLMOptions
    limits: ModelContextLimits
    context_budget: ResolvedModelContextBudgetFact
    token_estimator: TokenEstimator
    fact: ResolvedModelTargetFact


@dataclass(frozen=True, slots=True)
class ResolvedModelCall:
    target: ResolvedModelTarget
    fact: ResolvedModelCallFact

    @property
    def resolved_model_call_id(self) -> str:
        return self.fact.resolved_model_call_id


def _normalize_key(key: str) -> str:
    return (
        unicodedata.normalize("NFKC", key)
        .strip()
        .lower()
        .replace("-", "_")
        .replace(" ", "_")
    )


def redact_provider_request_shape(value: object, *, context: str = "root") -> object:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"non-finite provider request value at {context}")
        return value
    if isinstance(value, (list, tuple)):
        return [
            redact_provider_request_shape(item, context=f"{context}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str):
                raise ValueError(f"provider request key at {context} must be a string")
            key = _normalize_key(raw_key)
            if key in normalized:
                raise ValueError(
                    f"normalized provider request key collision at {context}: {key}"
                )
            if key == "cookies":
                if isinstance(raw_value, Mapping):
                    cookie_names: dict[str, str] = {}
                    for raw_cookie_name in raw_value:
                        if not isinstance(raw_cookie_name, str):
                            raise ValueError(
                                f"cookie name at {context}.cookies must be a string"
                            )
                        cookie_name = _normalize_key(raw_cookie_name)
                        if cookie_name in cookie_names:
                            raise ValueError(
                                f"normalized cookie name collision at {context}: {cookie_name}"
                            )
                        cookie_names[cookie_name] = "<redacted:secret>"
                    normalized[key] = {
                        cookie_name: cookie_names[cookie_name]
                        for cookie_name in sorted(cookie_names)
                    }
                else:
                    normalized[key] = "<redacted:secret>"
                continue
            sensitive = key in _SENSITIVE_EXACT_KEYS or key.endswith(
                _SENSITIVE_SUFFIXES
            )
            normalized[key] = (
                "<redacted:secret>"
                if sensitive
                else redact_provider_request_shape(
                    raw_value, context=f"{context}.{key}"
                )
            )
        return {key: normalized[key] for key in sorted(normalized)}
    raise ValueError(
        f"unsupported provider request value at {context}: {type(value).__name__}"
    )


def canonicalize_endpoint(base_url: str) -> tuple[str, str]:
    parsed = urlsplit(base_url)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError("model endpoint scheme must be http or https")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("model endpoint cannot contain userinfo")
    if parsed.query or parsed.fragment:
        raise ValueError("model endpoint cannot contain query or fragment")
    if not parsed.hostname:
        raise ValueError("model endpoint hostname is required")
    hostname = parsed.hostname.encode("idna").decode("ascii").lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("model endpoint port is invalid") from exc
    if port == (80 if scheme == "http" else 443):
        port = None
    rendered_hostname = f"[{hostname}]" if ":" in hostname else hostname
    authority = rendered_hostname if port is None else f"{rendered_hostname}:{port}"
    path = parsed.path or "/"
    if _INVALID_PERCENT_ESCAPE_RE.search(path):
        raise ValueError("model endpoint path contains an invalid percent escape")
    path = _PERCENT_ESCAPE_RE.sub(lambda match: f"%{match.group(1).upper()}", path)
    decoded_segments = unquote(path).split("/")
    if any(segment in {".", ".."} for segment in decoded_segments):
        raise ValueError("model endpoint path cannot contain dot segments")
    if not path.startswith("/"):
        path = f"/{path}"
    if path != "/":
        path = path.rstrip("/")
    endpoint_origin = f"{scheme}://{authority}"
    endpoint_fingerprint = sha256_fingerprint(
        "model-endpoint:v1",
        {"scheme": scheme, "host": hostname, "port": port, "path": path},
    )
    return endpoint_origin, endpoint_fingerprint


def resolve_model_target(
    *,
    config: LLMConfig,
    registry: LLMTransportRegistry,
    role: ModelRole,
    requested_options: LLMOptions | None,
) -> ResolvedModelTarget:
    slot = config.slot_for(role)
    model = config.model_for(role)
    profile = model.provider_profile
    requested = requested_options or LLMOptions()
    effective_output = slot.limits.default_output_tokens
    omitted = (
        set(profile.omit_params_when_thinking) if profile.thinking.enabled else set()
    )
    unsupported: list[str] = []
    if requested.reasoning_effort is not None and (
        not profile.supports_reasoning
        or "reasoning" in omitted
        or "reasoning_effort" in omitted
    ):
        unsupported.append("reasoning_effort")
    if unsupported:
        raise ModelOptionUnsupported(
            "provider cannot send explicitly requested options: "
            + ", ".join(unsupported)
        )
    effective_options = LLMOptions(
        reasoning_effort=requested.reasoning_effort,
    )
    options_fingerprint = resolved_model_options_fingerprint(
        reasoning_effort=effective_options.reasoning_effort,
    )
    options_fact = ResolvedModelOptionsFact(
        reasoning_effort=effective_options.reasoning_effort,
        options_fingerprint=options_fingerprint,
    )
    pre_margin = min(
        slot.limits.max_input_tokens,
        slot.limits.total_context_tokens - effective_output,
    )
    input_budget = pre_margin - slot.limits.input_safety_margin_tokens
    if input_budget < 1:
        raise ModelInputBudgetUnavailable("resolved model input budget is non-positive")
    budget = ResolvedModelContextBudgetFact(
        effective_output_tokens=effective_output,
        pre_margin_input_tokens=pre_margin,
        safety_margin_tokens=slot.limits.input_safety_margin_tokens,
        input_budget_tokens=input_budget,
    )
    try:
        transport = registry.get(model.api)
    except KeyError as exc:
        raise ModelTransportUnavailable(str(exc)) from exc
    binding_id = getattr(transport, "binding_id", "")
    contract_version = getattr(transport, "contract_version", "")
    if not binding_id or not contract_version:
        raise ModelTransportUnavailable("transport binding identity is incomplete")
    endpoint_origin, endpoint_fingerprint = canonicalize_endpoint(config.base_url)
    provider_shape = redact_provider_request_shape(
        {
            "request_defaults": profile.request_defaults,
            "request_extra_body": profile.request_extra_body,
            "omit_params_when_thinking": profile.omit_params_when_thinking,
            "thinking": {
                "enabled": profile.thinking.enabled,
                "delta_fields": profile.thinking.delta_fields,
                "message_field": profile.thinking.message_field,
                "replay_policy": profile.thinking.replay_policy.value,
            },
            "merge_policy": "validated_extensions_then_pulsara_fields:v2",
        },
        context="provider_profile",
    )
    provider_shape_fingerprint = sha256_fingerprint(
        "provider-request-shape:v1", provider_shape
    )
    estimator = PulsaraHeuristicTokenEstimatorV1()
    payload: dict[str, Any] = {
        "contract_version": "resolved-model-target:v2",
        "model_id": model.id,
        "model_role": role.value,
        "provider": model.provider,
        "api": model.api,
        "endpoint_origin": endpoint_origin,
        "endpoint_fingerprint": endpoint_fingerprint,
        "provider_profile_id": profile.id,
        "provider_request_shape_fingerprint": provider_shape_fingerprint,
        "transport_binding_id": binding_id,
        "transport_contract_version": contract_version,
        "model_identity_policy": profile.model_identity_policy.value,
        "supports_tools": model.supports_tools,
        "supports_reasoning": model.supports_reasoning,
        "limits": slot.limits.model_dump(mode="json"),
        "effective_options": options_fact.model_dump(mode="json"),
        "context_budget": budget.model_dump(mode="json"),
        "token_estimator": estimator.fact.model_dump(mode="json"),
    }
    fact = ResolvedModelTargetFact(
        target_fingerprint=resolved_model_target_fingerprint(payload),
        **payload,
    )
    return ResolvedModelTarget(
        model_profile=model,
        transport=transport,
        effective_options=effective_options,
        limits=slot.limits,
        context_budget=budget,
        token_estimator=estimator,
        fact=fact,
    )


def resolve_model_call(
    *,
    target: ResolvedModelTarget,
    purpose: ModelCallPurpose,
) -> ResolvedModelCall:
    mode = (
        ModelContextMode.COMPILED
        if purpose is ModelCallPurpose.AGENT_MODEL_LOOP
        else ModelContextMode.DIRECT
    )
    fact = ResolvedModelCallFact(
        resolved_model_call_id=f"model_call:{uuid4().hex}",
        purpose=purpose,
        context_mode=mode,
        target=target.fact,
    )
    return ResolvedModelCall(target=target, fact=fact)


def rebind_model_target(
    *,
    config: LLMConfig,
    registry: LLMTransportRegistry,
    fact: ResolvedModelTargetFact,
) -> ResolvedModelTarget:
    options = fact.effective_options
    target = resolve_model_target(
        config=config,
        registry=registry,
        role=ModelRole(fact.model_role),
        requested_options=LLMOptions(
            reasoning_effort=options.reasoning_effort,
        ),
    )
    if target.fact.target_fingerprint != fact.target_fingerprint:
        raise ModelTargetBindingMismatch(
            "current runtime cannot reproduce the persisted model target"
        )
    return target
