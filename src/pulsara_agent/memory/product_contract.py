"""Product language for memory tool descriptions and parameters."""

from __future__ import annotations


MEMORY_KIND_PRODUCT_DEFINITIONS = (
    (
        "USER_PROFILE",
        "who the user is, likes, tends to do, aims for or may prefer",
    ),
    (
        "RESPONSE_PREFERENCE",
        "a soft default for answer style, explanation or formatting",
    ),
    (
        "FACT",
        "attributed declarative context, including explicit retention of uncertain information",
    ),
    (
        "DECISION",
        "a source-supported choice, inclination, plan, goal, commitment or adopted high-level method",
    ),
)

MEMORY_CONTEXT_PRODUCT_GUIDE = (
    "GLOBAL is available across conversations sharing this memory store; "
    "CURRENT_PROJECT only within this project. Either scope accepts any kind. "
    "Availability does not broaden the claim: keep its actual subject, date and "
    "conditions in statement."
)

MEMORY_COHESIVE_UNIT_GUIDE = (
    "One self-contained idea that can be read and deleted as a whole; separate unrelated claims."
)

MEMORY_RETRIEVAL_AUTHORING_GUIDE = (
    "Preserve the subject, attribution, project, time, conditions, quantities, "
    "negations and uncertainty that matter. Distinguish suggestions, tentative plans, "
    "adopted choices and completed actions. Resolve pronouns and relative dates only "
    "when unambiguous. Use natural prose without template labels or invented details."
)


def memory_kind_product_guide() -> str:
    return " ".join(
        f"{name}: {meaning}." for name, meaning in MEMORY_KIND_PRODUCT_DEFINITIONS
    )
