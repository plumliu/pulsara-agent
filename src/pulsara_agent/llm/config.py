"""LLM configuration with pro/flash model slots."""

from __future__ import annotations

import os
from dataclasses import dataclass

from pulsara_agent.llm.models import ModelProfile, ModelRole


DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"


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
    api: str = "openai_responses"
    provider: str = "openai"

    @classmethod
    def from_env(cls, prefix: str = "PULSARA") -> "LLMConfig":
        return cls(
            api_key=_required_env(f"{prefix}_API_KEY"),
            base_url=os.getenv(f"{prefix}_BASE_URL", DEFAULT_OPENAI_BASE_URL).strip(),
            pro_model=_required_env(f"{prefix}_PRO_MODEL"),
            flash_model=_required_env(f"{prefix}_FLASH_MODEL"),
        )

    def model_for(self, role: ModelRole) -> ModelProfile:
        model_name = self.pro_model if role is ModelRole.PRO else self.flash_model
        return ModelProfile(
            id=model_name,
            role=role,
            api=self.api,
            provider=self.provider,
            base_url=self.base_url,
        )


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value
