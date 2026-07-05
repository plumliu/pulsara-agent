"""Provider-neutral context compiler primitives.

The context compiler is a projection layer: it turns existing runtime facts
into model-visible context and an inspectable report. It must not decide
subsystem truth such as memory validity, tool permission, or capability
existence.
"""

from pulsara_agent.runtime.context_engine.compiler import (
    ContextCompileInputs,
    compile_context,
)
from pulsara_agent.runtime.context_engine.lifecycle import ContextLifecycleCoordinator
from pulsara_agent.runtime.context_engine.types import (
    CompiledContext,
    CompiledContextSection,
    CompiledToolSpecUnit,
    ContextBudgetClass,
    ContextBudgetExceeded,
    ContextBudgetReport,
    ContextChannel,
    ContextCompileRequest,
    ContextDiagnostic,
    ContextLifecycleDecisionDiagnostic,
    ContextLifecycleStatus,
    ContextRenderMode,
    ContextSection,
    ContextStability,
)

__all__ = [
    "CompiledContext",
    "CompiledContextSection",
    "CompiledToolSpecUnit",
    "ContextBudgetClass",
    "ContextBudgetExceeded",
    "ContextBudgetReport",
    "ContextChannel",
    "ContextCompileInputs",
    "ContextCompileRequest",
    "ContextLifecycleCoordinator",
    "ContextDiagnostic",
    "ContextLifecycleDecisionDiagnostic",
    "ContextLifecycleStatus",
    "ContextRenderMode",
    "ContextSection",
    "ContextStability",
    "compile_context",
]
