"""Product language shared by the ROOT memory prompt and tools."""

from __future__ import annotations


MEMORY_KIND_PRODUCT_DEFINITIONS = (
    (
        "USER_PROFILE",
        "who the user is, likes, tends to do, is working toward, or may prefer; "
        "it may be a source-faithful lightweight inference and does not grant authority",
    ),
    (
        "RESPONSE_PREFERENCE",
        "a soft default for how the Agent should usually answer, explain, format, or "
        "express itself; it changes presentation, not permissions or execution policy",
    ),
    (
        "FACT",
        "attributed declarative context worth recalling, including safe ambiguous "
        "explicit retention; it is advisory rather than a source of truth or executor",
    ),
    (
        "DECISION",
        "a source-supported choice, inclination, plan, goal, commitment, or adopted "
        "high-level method; it does not execute, track, or guarantee completion",
    ),
)

MEMORY_CONTEXT_PRODUCT_GUIDE = (
    "GLOBAL makes the item available in conversations that share this memory store; "
    "it does not make a project-only claim universally true. CURRENT_PROJECT makes it "
    "available only in this project. Put the real subject, date, and conditions in the "
    "statement. Any kind may use either scope when the content warrants it."
)

MEMORY_COHESIVE_UNIT_GUIDE = (
    "Save one idea that makes sense to read and delete as a whole. It may have several "
    "clauses when they naturally belong together; save unrelated claims separately."
)

MEMORY_RETRIEVAL_AUTHORING_GUIDE = (
    "Write a self-contained statement faithful to what was said or observed. Name the "
    "subject and relevant project, time, conditions, quantities, negations, and "
    "uncertainty when they matter. Do not use template labels or invent details."
)


def memory_kind_product_guide() -> str:
    return " ".join(
        f"{name}: {meaning}." for name, meaning in MEMORY_KIND_PRODUCT_DEFINITIONS
    )
