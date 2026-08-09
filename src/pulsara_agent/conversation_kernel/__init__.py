"""Stage 2 canonical relational conversation kernel.

This package is the only production owner of the post-hard-cut conversation,
effect, work, and selective occurrence authority.  It deliberately does not
import the legacy EventLog or any presentation/projection subsystem.
"""

from pulsara_agent.conversation_kernel.vocabulary import (
    APPEND_GUARDS,
    COMMITTED_EVENT_DESCRIPTORS,
    LIVE_EVENT_TYPES,
    SUBJECT_SLOTS,
)

__all__ = [
    "APPEND_GUARDS",
    "COMMITTED_EVENT_DESCRIPTORS",
    "LIVE_EVENT_TYPES",
    "SUBJECT_SLOTS",
]
