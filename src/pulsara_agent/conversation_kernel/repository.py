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
    JobAttemptClaimGuard,
    JobSafetyClass,
    PromptDeliveryMode,
)
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

from ._repository.contracts import (
    AcceptedCapabilityDecision,
    AcceptedEntry,
    AcceptedInteractionDecision,
    AcceptedJobAttempt,
    AcceptedJobSettlement,
    AcceptedPlanResolution,
    AcceptedPlanToolBatch,
    AcceptedPlanWorkflowCommand,
    AcceptedToolAttempt,
    AssistantBlock,
    AssistantDataBlock,
    AssistantTextBlock,
    AssistantToolCallBlock,
    ConversationKernelConflict,
    JobAttemptTerminalized,
    JobCancellationRequested,
    MemoryVectorFactSource,
    MemoryVectorSource,
    NoToolResultSideBranch,
    PlanContinuationDisposition,
    PlanContinuationInspection,
    PlanDraftIdentityConflict,
    PlanQuestionAnswer,
    PlanToolBatchDisposition,
    PlanToolControlKind,
    PreparedMemoryProposalSideBranch,
    PreparedPlanBatchCall,
    PreparedPlanToolBatch,
    PreparedRootTurnAdmission,
    PreparedSubagentTurnAdmission,
    PreparedToolRemoteIdentityPublication,
    PreparedToolResultAcceptance,
    PromptIngressRejected,
    StaleHostWriter,
    StaleJobClaim,
    ToolRemoteIdentityConfirmationKind,
    ToolResultSideBranch,
    ToolResultSideBranchKind,
    TurnAdmissionConfirmation,
    TurnAdmissionConfirmationKind,
    build_prepared_root_turn_admission,
    build_prepared_subagent_turn_admission,
    build_prepared_tool_remote_identity_publication,
    build_prepared_tool_result_acceptance,
    plan_draft_review_semantic_candidate,
    plan_exit_semantic_fingerprint,
    plan_question_resolution_semantic_fingerprint,
)
from ._repository.authority import _AuthorityOperations
from ._repository.conversation import _ConversationOperations
from ._repository.external_results import _ExternalResultOperations
from ._repository.jobs import _JobOperations
from ._repository.kernel import _RepositoryKernel
from ._repository.matching import _MatchingOperations
from ._repository.memory import _MemoryOperations
from ._repository.plans import _PlanOperations
from ._repository.prompts import _PromptOperations
from ._repository.subagents import _SubagentOperations
from ._repository.tools import _ToolOperations


class ConversationKernelRepository(
    _MatchingOperations,
    _AuthorityOperations,
    _ConversationOperations,
    _ToolOperations,
    _PlanOperations,
    _PromptOperations,
    _SubagentOperations,
    _ExternalResultOperations,
    _JobOperations,
    _MemoryOperations,
    _RepositoryKernel,
):
    """Single storage owner for canonical conversation-kernel facts."""


_FACADE_OWNED_SYMBOLS = (
    "AcceptedCapabilityDecision",
    "AcceptedEntry",
    "AcceptedInteractionDecision",
    "AcceptedJobAttempt",
    "AcceptedJobSettlement",
    "AcceptedPlanResolution",
    "AcceptedPlanToolBatch",
    "AcceptedPlanWorkflowCommand",
    "AcceptedToolAttempt",
    "AssistantDataBlock",
    "AssistantTextBlock",
    "AssistantToolCallBlock",
    "ConversationKernelConflict",
    "JobAttemptTerminalized",
    "JobCancellationRequested",
    "MemoryVectorFactSource",
    "MemoryVectorSource",
    "NoToolResultSideBranch",
    "PlanContinuationDisposition",
    "PlanContinuationInspection",
    "PlanDraftIdentityConflict",
    "PlanQuestionAnswer",
    "PlanToolBatchDisposition",
    "PlanToolControlKind",
    "PreparedMemoryProposalSideBranch",
    "PreparedPlanBatchCall",
    "PreparedPlanToolBatch",
    "PreparedRootTurnAdmission",
    "PreparedSubagentTurnAdmission",
    "PreparedToolRemoteIdentityPublication",
    "PreparedToolResultAcceptance",
    "PromptIngressRejected",
    "StaleHostWriter",
    "StaleJobClaim",
    "ToolRemoteIdentityConfirmationKind",
    "ToolResultSideBranchKind",
    "TurnAdmissionConfirmation",
    "TurnAdmissionConfirmationKind",
    "build_prepared_root_turn_admission",
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
    "AcceptedJobAttempt",
    "AcceptedJobSettlement",
    "AcceptedToolAttempt",
    "AssistantBlock",
    "AssistantDataBlock",
    "AssistantTextBlock",
    "AssistantToolCallBlock",
    "ConversationKernelConflict",
    "ConversationKernelRepository",
    "JobAttemptTerminalized",
    "MemoryVectorFactSource",
    "MemoryVectorSource",
    "NoToolResultSideBranch",
    "PreparedMemoryProposalSideBranch",
    "PreparedToolResultAcceptance",
    "PlanDraftIdentityConflict",
    "PlanContinuationDisposition",
    "PreparedRootTurnAdmission",
    "PreparedSubagentTurnAdmission",
    "PreparedToolRemoteIdentityPublication",
    "StaleHostWriter",
    "StaleJobClaim",
    "ToolResultSideBranch",
    "ToolResultSideBranchKind",
    "ToolRemoteIdentityConfirmationKind",
    "TurnAdmissionConfirmation",
    "TurnAdmissionConfirmationKind",
    "build_prepared_root_turn_admission",
    "build_prepared_subagent_turn_admission",
    "build_prepared_tool_remote_identity_publication",
    "build_prepared_tool_result_acceptance",
]
