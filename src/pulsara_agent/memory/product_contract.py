"""Product language for memory tool descriptions and parameters."""

from __future__ import annotations


MEMORY_KIND_PRODUCT_DEFINITIONS = (
    (
        "USER_PROFILE",
        "identity, interests, habits, goals or possible preferences",
    ),
    (
        "RESPONSE_PREFERENCE",
        "soft defaults for answer style, explanation or formatting",
    ),
    (
        "FACT",
        "attributed declarative context, including uncertain information",
    ),
    (
        "DECISION",
        "source-supported choices, inclinations, plans, goals, commitments or adopted high-level methods",
    ),
)

MEMORY_CONTEXT_PRODUCT_GUIDE = (
    "GLOBAL: conversations sharing this memory store. CURRENT_PROJECT: this "
    "project only. Both accept any kind. Keep the claim's actual subject, date "
    "and conditions in statement."
)

MEMORY_COHESIVE_UNIT_GUIDE = (
    "One self-contained idea, readable and deletable as a whole; split unrelated claims."
)

MEMORY_RETRIEVAL_AUTHORING_GUIDE = (
    "Keep relevant subject, attribution, project, time, conditions, quantities, "
    "negation and uncertainty. Distinguish suggestions, tentative plans, adopted "
    "choices and completed actions. Resolve pronouns/relative dates only when "
    "unambiguous. Use natural prose without template labels or invented details."
)


def memory_kind_product_guide() -> str:
    return " ".join(
        f"{name}: {meaning}." for name, meaning in MEMORY_KIND_PRODUCT_DEFINITIONS
    )
