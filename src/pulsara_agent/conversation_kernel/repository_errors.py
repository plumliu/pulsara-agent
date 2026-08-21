"""Stable public repository errors without an implementation-package import."""

from __future__ import annotations


class ConversationKernelConflict(RuntimeError):
    """A stable identity already names a different semantic fact."""


# Preserve the long-standing public/pickle identity owned by the repository
# facade while allowing neutral readers and blob helpers to avoid importing the
# private ``_repository`` implementation package (or the facade's SQL owners).
ConversationKernelConflict.__module__ = (
    "pulsara_agent.conversation_kernel.repository"
)


__all__ = ["ConversationKernelConflict"]
