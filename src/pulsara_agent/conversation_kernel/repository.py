"""Compatibility facade for the canonical conversation repository."""

# Re-export compatibility is the entire purpose of this module.
# ruff: noqa: F401

from __future__ import annotations

from datetime import datetime
from typing import Mapping

from pulsara_agent.conversation_kernel.contracts import (
    BlobContent,
    CanonicalContent,
    CommittedEventDraft,
    HostWriterGuard,
    InlineContent,
    PromptDeliveryMode,
)
from pulsara_agent.conversation_kernel.prompt_content import FrozenCanonicalPrompt
from pulsara_agent.ports.artifact import (
    ToolOutputArtifactDisposition,
    ToolOutputArtifactUnavailabilityReason,
    ToolResultDisplayKind,
)
from pulsara_agent.ports.tool_execution import (
    FrozenToolJsonDict,
    ToolOutputSourceCoverage,
    ToolOutputSourceCoverageReason,
)
from pulsara_agent.primitives.context import FrozenJsonObjectFact
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.plan_workflow import (
    ExtractedPlanDraft,
    PlanDraftDecision,
    PlanHandoffKind,
    PlanInteractionBinding,
    PlanInteractionKind,
    PlanQuestionAnswerKind,
    PlanQuestionContent,
    PlanWorkflowStatus,
)
from pulsara_agent.primitives.run_permission import FrozenRunPermissionSnapshot
from pulsara_agent.primitives.tool_observation import ToolObservationOrigin
from pulsara_agent.model_input.contracts import PreparedProviderInputCut
from pulsara_agent.conversation_kernel.memory.writes import PreparedMemoryMutation

from ._repository.contracts import (
    AcceptedCapabilityDecision,
    AcceptedEntry,
    AcceptedMemoryToolResult,
    AcceptedSubagentCompletion,
    AcceptedInteractionDecision,
    AcceptedPlanResolution,
    AcceptedPlanToolBatch,
    AcceptedPlanWorkflowCommand,
    AcceptedRootTurnAdmission,
    AcceptedToolAttempt,
    AssistantBlock,
    AssistantDataBlock,
    AssistantTextBlock,
    AssistantToolCallBlock,
    ConversationKernelConflict,
    NoToolResultSideBranch,
    PlanContinuationDisposition,
    PlanContinuationInspection,
    PlanDraftIdentityConflict,
    PlanQuestionAnswer,
    PlanToolBatchDisposition,
    PlanToolControlKind,
    PreparedAutomaticSubagentCompletion,
    PreparedMemoryMutationSideBranch,
    PreparedPlanBatchCall,
    PreparedPlanToolBatch,
    PreparedRootTurnIntent,
    PreparedSubagentTurnAdmission,
    PreparedToolRemoteIdentityPublication,
    PreparedToolResultAcceptance,
    PromptIngressRejected,
    StaleHostWriter,
    SubagentCompletionDisposition,
    ToolRemoteIdentityConfirmationKind,
    ToolResultSideBranch,
    ToolResultSideBranchKind,
    TurnAdmissionConfirmation,
    TurnAdmissionConfirmationKind,
    build_prepared_root_turn_intent,
    build_prepared_subagent_turn_admission,
    build_prepared_tool_remote_identity_publication,
    build_prepared_tool_result_acceptance,
    plan_draft_review_semantic_candidate,
    plan_exit_semantic_fingerprint,
    plan_question_resolution_semantic_fingerprint,
)
from ._repository.authority import _AuthorityOperations
from ._repository.conversation import _ConversationOperations
from ._repository.completions import _SubagentCompletionOperations
from ._repository.kernel import _RepositoryKernel
from ._repository.matching import _MatchingOperations
from ._repository.memory import _MemoryOperations
from ._repository.plans import _PlanOperations
from ._repository.prompts import _PromptOperations
from ._repository.subagents import _SubagentOperations
from ._repository.tools import _ToolOperations
from ._repository.fork import _ForkOperations


class ConversationKernelRepository(
    _ForkOperations,
    _MatchingOperations,
    _AuthorityOperations,
    _ConversationOperations,
    _ToolOperations,
    _PlanOperations,
    _PromptOperations,
    _SubagentOperations,
    _SubagentCompletionOperations,
    _MemoryOperations,
    _RepositoryKernel,
):
    """Single storage owner for canonical conversation-kernel facts."""


_FACADE_OWNED_SYMBOLS = (
    "AcceptedCapabilityDecision",
    "AcceptedEntry",
    "AcceptedSubagentCompletion",
    "AcceptedInteractionDecision",
    "AcceptedPlanResolution",
    "AcceptedPlanToolBatch",
    "AcceptedPlanWorkflowCommand",
    "AcceptedToolAttempt",
    "AcceptedRootTurnAdmission",
    "AssistantDataBlock",
    "AssistantTextBlock",
    "AssistantToolCallBlock",
    "ConversationKernelConflict",
    "NoToolResultSideBranch",
    "PreparedAutomaticSubagentCompletion",
    "PlanContinuationDisposition",
    "PlanContinuationInspection",
    "PlanDraftIdentityConflict",
    "PlanQuestionAnswer",
    "PlanToolBatchDisposition",
    "PlanToolControlKind",
    "PreparedMemoryMutationSideBranch",
    "PreparedPlanBatchCall",
    "PreparedPlanToolBatch",
    "PreparedRootTurnIntent",
    "PreparedSubagentTurnAdmission",
    "PreparedToolRemoteIdentityPublication",
    "PreparedToolResultAcceptance",
    "PromptIngressRejected",
    "StaleHostWriter",
    "SubagentCompletionDisposition",
    "ToolRemoteIdentityConfirmationKind",
    "ToolResultSideBranchKind",
    "TurnAdmissionConfirmation",
    "TurnAdmissionConfirmationKind",
    "build_prepared_root_turn_intent",
    "build_prepared_subagent_turn_admission",
    "build_prepared_tool_remote_identity_publication",
    "build_prepared_tool_result_acceptance",
    "plan_draft_review_semantic_candidate",
    "plan_exit_semantic_fingerprint",
    "plan_question_resolution_semantic_fingerprint",
)
for _symbol_name in _FACADE_OWNED_SYMBOLS:
    _symbol = globals()[_symbol_name]
    _symbol.__module__ = __name__
    _symbol.__qualname__ = _symbol_name
del _symbol, _symbol_name


__all__ = [
    "AcceptedCapabilityDecision",
    "AcceptedEntry",
    "AcceptedInteractionDecision",
    "AcceptedSubagentCompletion",
    "AcceptedToolAttempt",
    "AcceptedRootTurnAdmission",
    "AssistantBlock",
    "AssistantDataBlock",
    "AssistantTextBlock",
    "AssistantToolCallBlock",
    "ConversationKernelConflict",
    "ConversationKernelRepository",
    "AcceptedMemoryToolResult",
    "NoToolResultSideBranch",
    "PreparedAutomaticSubagentCompletion",
    "PreparedMemoryMutationSideBranch",
    "PreparedToolResultAcceptance",
    "PlanDraftIdentityConflict",
    "PlanContinuationDisposition",
    "PreparedRootTurnIntent",
    "PreparedSubagentTurnAdmission",
    "PreparedToolRemoteIdentityPublication",
    "StaleHostWriter",
    "SubagentCompletionDisposition",
    "ToolResultSideBranch",
    "ToolResultSideBranchKind",
    "ToolRemoteIdentityConfirmationKind",
    "TurnAdmissionConfirmation",
    "TurnAdmissionConfirmationKind",
    "build_prepared_root_turn_intent",
    "build_prepared_subagent_turn_admission",
    "build_prepared_tool_remote_identity_publication",
    "build_prepared_tool_result_acceptance",
]
