"""Stable model-resolution and execution contract errors."""

from __future__ import annotations


class ModelContractError(RuntimeError):
    reason_code = "model_contract_error"


class ModelLimitsConfigurationError(ModelContractError):
    reason_code = "model_limits_invalid"


class ModelBudgetConfigurationError(ModelContractError):
    reason_code = "provider_budget_key_conflict"


class ModelInputBudgetUnavailable(ModelContractError):
    reason_code = "model_input_budget_non_positive"


class ModelTransportUnavailable(ModelContractError):
    reason_code = "model_transport_unavailable"


class ModelTransportBindingMismatch(ModelContractError):
    reason_code = "model_transport_binding_mismatch"


class ModelOptionUnsupported(ModelContractError):
    reason_code = "model_option_unsupported"


class ModelTargetBindingMismatch(ModelContractError):
    reason_code = "model_target_binding_mismatch"


class ModelTargetCapabilityMismatch(ModelContractError):
    reason_code = "model_target_capability_mismatch"


class ModelInputBudgetExceeded(ModelContractError):
    reason_code = "model_input_budget_exceeded"


class ModelInputEstimateMismatch(ModelContractError):
    reason_code = "model_input_estimate_mismatch"


class ModelContextIdentityMismatch(ModelContractError):
    reason_code = "model_context_identity_mismatch"


class LLMTransportContractError(ModelContractError):
    reason_code = "llm_transport_contract_error"

    def __init__(self, message: str, *, reason_code: str | None = None) -> None:
        super().__init__(message)
        if reason_code is not None:
            self.reason_code = reason_code


class CompactionSummarizerInputBudgetExceeded(ModelContractError):
    reason_code = "compaction_summarizer_input_budget_exceeded"


class CompactionTargetUnreachable(ModelContractError):
    reason_code = "compaction_target_unreachable"
