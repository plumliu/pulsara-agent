"""Stable public repository errors without an implementation-package import."""

from __future__ import annotations


class ConversationKernelConflict(RuntimeError):
    """A stable identity already names a different semantic fact."""


class PreparedCompletionSuffixStale(ConversationKernelConflict):
    """A speculative ROOT completion batch lost its exact canonical cut."""


class SessionDeletionBusy(RuntimeError):
    """Deletion has no authority over the current canonical writer."""


class SessionWriterConflict(SessionDeletionBusy):
    """The requested retirement does not own the current session writer."""


# Preserve the long-standing public/pickle identity owned by the repository
# facade while allowing neutral readers and blob helpers to avoid importing the
# private ``_repository`` implementation package (or the facade's SQL owners).
ConversationKernelConflict.__module__ = (
    "pulsara_agent.conversation_kernel.repository"
)


__all__ = ["ConversationKernelConflict", "PreparedCompletionSuffixStale", "SessionDeletionBusy", "SessionWriterConflict"]
