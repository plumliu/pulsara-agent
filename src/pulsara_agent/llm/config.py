"""LLM configuration with pro/flash model slots."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from pulsara_agent.llm.models import ModelProfile, ModelRole
from pulsara_agent.llm.provider import (
    ModelIdentityPolicy,
    ProviderProfile,
    ThinkingProfile,
    ThinkingReplayPolicy,
)
from pulsara_agent.llm.retry import LLMRetryConfig, retry_config_from_env
from pulsara_agent.primitives.model_call import ModelContextLimits


DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_OPENAI_API = "openai_responses"
OPENAI_CHAT_COMPLETIONS_API = "openai_chat_completions"
DEFAULT_CHAT_THINKING_OMIT_PARAMS = (
    "temperature",
    "top_p",
    "presence_penalty",
    "frequency_penalty",
)

DEFAULT_MODEL_CONTEXT_LIMITS = ModelContextLimits(
    total_context_tokens=256_000,
    max_input_tokens=256_000,
    max_output_tokens=128_000,
    default_output_tokens=8_192,
    input_safety_margin_tokens=8_192,
)

PROVIDER_EXTENSION_ALLOWLIST_BY_API: dict[str, dict[str, frozenset[str]]] = {
    DEFAULT_OPENAI_API: {
        "request_defaults": frozenset({"service_tier"}),
        "request_extra_body": frozenset({"thinking"}),
    },
    OPENAI_CHAT_COMPLETIONS_API: {
        "request_defaults": frozenset({"service_tier"}),
        "request_extra_body": frozenset({"thinking"}),
    },
}
OUTPUT_BUDGET_KEYS = frozenset(
    {"max_completion_tokens", "max_output_tokens", "max_tokens"}
)


@dataclass(frozen=True, slots=True)
class ModelSlotConfig:
    model_id: str
    limits: ModelContextLimits

    def __post_init__(self) -> None:
        if not self.model_id.strip():
            raise ValueError("model_id is required")


@dataclass(frozen=True, slots=True)
class LLMConfig:
    """User-facing model configuration.

    Users provide one credential set and two fully-specified model slots. The
    runtime selects by role: pro for main reasoning, flash for side/cheap work.
    """

    api_key: str
    base_url: str
    pro: ModelSlotConfig
    flash: ModelSlotConfig
    api: str = DEFAULT_OPENAI_API
    provider: str = "custom"
    provider_profile: ProviderProfile | None = None
    retry: LLMRetryConfig = LLMRetryConfig()
    openai_sdk_max_retries: int | None = None

    def __post_init__(self) -> None:
        if not self.api_key:
            raise ValueError("api_key is required")
        if not self.base_url.strip():
            raise ValueError("base_url is required")
        profile = (
            self.provider_profile
            or ProviderProfile(id=self.provider, wire_api=self.api)
        ).copy_for_api(self.api)
        _validate_provider_payload_ownership(api=self.api, provider_profile=profile)
        object.__setattr__(self, "provider_profile", profile)

    @property
    def pro_model(self) -> str:
        """Display-only compatibility alias derived from the canonical slot."""

        return self.pro.model_id

    @property
    def flash_model(self) -> str:
        """Display-only compatibility alias derived from the canonical slot."""

        return self.flash.model_id

    @classmethod
    def from_env(cls, prefix: str = "PULSARA") -> "LLMConfig":
        api = (
            os.getenv(f"{prefix}_API", DEFAULT_OPENAI_API).strip() or DEFAULT_OPENAI_API
        )
        provider = os.getenv(f"{prefix}_PROVIDER", "custom").strip() or "custom"
        provider_profile = _provider_profile_from_env(
            prefix=prefix, api=api, provider=provider
        )
        return cls(
            api_key=_required_env(f"{prefix}_API_KEY"),
            base_url=os.getenv(f"{prefix}_BASE_URL", DEFAULT_OPENAI_BASE_URL).strip(),
            pro=_model_slot_from_env(prefix=prefix, role="PRO"),
            flash=_model_slot_from_env(prefix=prefix, role="FLASH"),
            api=api,
            provider=provider,
            provider_profile=provider_profile,
            retry=retry_config_from_env(prefix=prefix),
            openai_sdk_max_retries=_optional_int_env(
                f"{prefix}_OPENAI_SDK_MAX_RETRIES"
            ),
        )

    def model_for(self, role: ModelRole) -> ModelProfile:
        slot = self.pro if role is ModelRole.PRO else self.flash
        provider_profile = self.provider_profile or ProviderProfile(
            id=self.provider, wire_api=self.api
        )
        return ModelProfile(
            id=slot.model_id,
            role=role,
            api=self.api,
            provider=self.provider,
            base_url=self.base_url,
            provider_profile=provider_profile,
            supports_tools=provider_profile.supports_tools,
            supports_reasoning=provider_profile.supports_reasoning,
        )

    def slot_for(self, role: ModelRole) -> ModelSlotConfig:
        return self.pro if role is ModelRole.PRO else self.flash


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _model_slot_from_env(*, prefix: str, role: str) -> ModelSlotConfig:
    field_prefix = f"{prefix}_{role}"
    return ModelSlotConfig(
        model_id=_required_env(f"{field_prefix}_MODEL"),
        limits=ModelContextLimits(
            total_context_tokens=_int_env(
                f"{field_prefix}_TOTAL_CONTEXT_TOKENS",
                DEFAULT_MODEL_CONTEXT_LIMITS.total_context_tokens,
            ),
            max_input_tokens=_int_env(
                f"{field_prefix}_MAX_INPUT_TOKENS",
                DEFAULT_MODEL_CONTEXT_LIMITS.max_input_tokens,
            ),
            max_output_tokens=_int_env(
                f"{field_prefix}_MAX_OUTPUT_TOKENS",
                DEFAULT_MODEL_CONTEXT_LIMITS.max_output_tokens,
            ),
            default_output_tokens=_int_env(
                f"{field_prefix}_DEFAULT_OUTPUT_TOKENS",
                DEFAULT_MODEL_CONTEXT_LIMITS.default_output_tokens,
            ),
            input_safety_margin_tokens=_int_env(
                f"{field_prefix}_INPUT_SAFETY_MARGIN_TOKENS",
                DEFAULT_MODEL_CONTEXT_LIMITS.input_safety_margin_tokens,
            ),
        ),
    )


def _validate_provider_payload_ownership(
    *,
    api: str,
    provider_profile: ProviderProfile,
) -> None:
    # ``api`` is the canonical wire-contract identity used by model_for(),
    # target resolution, adapter lookup, and request-shape fingerprinting.
    # Unknown APIs have no extension policy by default.  In particular, they
    # must not borrow an allowlist from a pre-canonical ProviderProfile.
    policy_api = api
    allowlist = PROVIDER_EXTENSION_ALLOWLIST_BY_API.get(policy_api, {})
    for source_name, value in (
        ("request_defaults", provider_profile.request_defaults),
        ("request_extra_body", provider_profile.request_extra_body),
    ):
        allowed = allowlist.get(source_name, frozenset())
        unsupported = sorted(str(key) for key in value if str(key) not in allowed)
        if unsupported:
            raise ValueError(
                f"provider {source_name} contains unsupported extension keys for "
                f"{policy_api}: " + ", ".join(unsupported)
            )
    omitted = sorted(
        OUTPUT_BUDGET_KEYS.intersection(provider_profile.omit_params_when_thinking)
    )
    if omitted:
        raise ValueError(
            "thinking omission policy contains reserved output budget keys: "
            + ", ".join(omitted)
        )


def _provider_profile_from_env(
    *, prefix: str, api: str, provider: str
) -> ProviderProfile:
    request_defaults = _json_object_env(f"{prefix}_REQUEST_DEFAULTS_JSON")
    request_extra_body = _json_object_env(f"{prefix}_EXTRA_BODY_JSON")
    thinking_extra_body = _thinking_extra_body_from_env(prefix)
    is_chat_completions = api == OPENAI_CHAT_COMPLETIONS_API
    if is_chat_completions and not request_extra_body and not thinking_extra_body:
        thinking_extra_body = {"type": "enabled"}
    thinking = _thinking_profile_from_env(
        prefix,
        enabled_default=is_chat_completions or bool(thinking_extra_body),
        replay_default=(
            ThinkingReplayPolicy.WHEN_TOOL_CALLS
            if is_chat_completions
            else ThinkingReplayPolicy.NEVER
        ),
    )
    if thinking_extra_body and "thinking" not in request_extra_body:
        request_extra_body = {**request_extra_body, "thinking": thinking_extra_body}
    return ProviderProfile(
        id=provider,
        wire_api=api,
        request_defaults=request_defaults,
        request_extra_body=request_extra_body,
        omit_params_when_thinking=_csv_env(
            f"{prefix}_OMIT_PARAMS_WHEN_THINKING",
            default=DEFAULT_CHAT_THINKING_OMIT_PARAMS if is_chat_completions else (),
        ),
        supports_tools=_bool_env(f"{prefix}_SUPPORTS_TOOLS", default=True),
        supports_reasoning=_bool_env(f"{prefix}_SUPPORTS_REASONING", default=True),
        model_identity_policy=ModelIdentityPolicy(
            os.getenv(
                f"{prefix}_MODEL_IDENTITY_POLICY",
                ModelIdentityPolicy.ACCEPT_REPORTED.value,
            ).strip()
            or ModelIdentityPolicy.ACCEPT_REPORTED.value
        ),
        thinking=thinking,
    )


def _thinking_profile_from_env(
    prefix: str,
    *,
    enabled_default: bool,
    replay_default: ThinkingReplayPolicy,
) -> ThinkingProfile:
    replay_policy = _enum_env(
        f"{prefix}_THINKING_REPLAY_POLICY",
        ThinkingReplayPolicy,
        default=replay_default,
    )
    return ThinkingProfile(
        enabled=_bool_env(f"{prefix}_THINKING_ENABLED", default=enabled_default),
        delta_fields=_csv_env(
            f"{prefix}_THINKING_DELTA_FIELDS", default=("reasoning_content",)
        ),
        message_field=os.getenv(
            f"{prefix}_THINKING_MESSAGE_FIELD", "reasoning_content"
        ).strip()
        or None,
        replay_policy=replay_policy,
    )


def _thinking_extra_body_from_env(prefix: str) -> dict[str, Any]:
    thinking: dict[str, Any] = {}
    thinking_type = os.getenv(f"{prefix}_THINKING_TYPE", "").strip()
    if thinking_type:
        thinking["type"] = thinking_type
    thinking_keep = os.getenv(f"{prefix}_THINKING_KEEP", "").strip()
    if thinking_keep:
        thinking["keep"] = thinking_keep
    clear_thinking = os.getenv(f"{prefix}_THINKING_CLEAR_THINKING")
    if clear_thinking is not None and clear_thinking.strip():
        thinking["clear_thinking"] = _parse_bool(clear_thinking)
    return thinking


def _json_object_env(name: str) -> dict[str, Any]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object")
    return value


def _csv_env(name: str, *, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _bool_env(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return _parse_bool(raw)


def _optional_int_env(name: str) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    return int(raw)


def _parse_bool(raw: str) -> bool:
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on", "enabled"}:
        return True
    if value in {"0", "false", "no", "off", "disabled"}:
        return False
    raise ValueError(f"Invalid boolean value: {raw}")


def _enum_env(
    name: str,
    enum_type: type[ThinkingReplayPolicy],
    *,
    default: ThinkingReplayPolicy,
) -> ThinkingReplayPolicy:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return enum_type(raw)
