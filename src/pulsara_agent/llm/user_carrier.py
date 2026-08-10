"""Neutral system-prompt composition after provider-carrier retirement."""

from __future__ import annotations


def compose_provider_root_policy(system_prompt: str | None) -> str | None:
    """Return the canonical Kernel prompt without adding a second protocol."""

    return system_prompt


__all__ = ["compose_provider_root_policy"]
