"""LLM configuration with pro/flash model slots."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from pulsara_agent.llm.models import ModelProfile, ModelRole
from pulsara_agent.llm.provider import ProviderProfile, ThinkingProfile, ThinkingReplayPolicy


DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_OPENAI_API = "openai_responses"
OPENAI_CHAT_COMPLETIONS_API = "openai_chat_completions"
DEFAULT_CHAT_THINKING_OMIT_PARAMS = (
    "temperature",
    "top_p",
    "presence_penalty",
    "frequency_penalty",
)


@dataclass(frozen=True, slots=True)
class LLMConfig:
    """User-facing model configuration.

    Users provide one credential set and two model names. The rest of Pulsara
    selects by role: pro for main reasoning, flash for side/cheap work.
    """

    api_key: str
    base_url: str
    pro_model: str
    flash_model: str
    api: str = DEFAULT_OPENAI_API
    provider: str = "custom"
    provider_profile: ProviderProfile | None = None

    @classmethod
    def from_env(cls, prefix: str = "PULSARA") -> "LLMConfig":
        api = os.getenv(f"{prefix}_API", DEFAULT_OPENAI_API).strip() or DEFAULT_OPENAI_API
        provider = os.getenv(f"{prefix}_PROVIDER", "custom").strip() or "custom"
        provider_profile = _provider_profile_from_env(prefix=prefix, api=api, provider=provider)
        return cls(
            api_key=_required_env(f"{prefix}_API_KEY"),
            base_url=os.getenv(f"{prefix}_BASE_URL", DEFAULT_OPENAI_BASE_URL).strip(),
            pro_model=_required_env(f"{prefix}_PRO_MODEL"),
            flash_model=_required_env(f"{prefix}_FLASH_MODEL"),
            api=api,
            provider=provider,
            provider_profile=provider_profile,
        )

    def model_for(self, role: ModelRole) -> ModelProfile:
        model_name = self.pro_model if role is ModelRole.PRO else self.flash_model
        provider_profile = self.provider_profile or ProviderProfile(id=self.provider, wire_api=self.api)
        return ModelProfile(
            id=model_name,
            role=role,
            api=self.api,
            provider=self.provider,
            base_url=self.base_url,
            provider_profile=provider_profile.copy_for_api(self.api),
            supports_tools=provider_profile.supports_tools,
            supports_reasoning=provider_profile.supports_reasoning,
        )


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def _provider_profile_from_env(*, prefix: str, api: str, provider: str) -> ProviderProfile:
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
        delta_fields=_csv_env(f"{prefix}_THINKING_DELTA_FIELDS", default=("reasoning_content",)),
        message_field=os.getenv(f"{prefix}_THINKING_MESSAGE_FIELD", "reasoning_content").strip()
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
