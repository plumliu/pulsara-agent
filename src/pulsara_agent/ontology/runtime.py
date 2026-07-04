"""Pulsara runtime evidence ontology."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pulsara_agent.jsonld import Namespace, Term


RUNTIME = Namespace("https://pulsara.dev/runtime#")

RUN_TIMELINE = RUNTIME.term("RunTimeline")
TURN = RUNTIME.term("Turn")
TOOL_RESULT = RUNTIME.term("ToolResult")
ARTIFACT = RUNTIME.term("Artifact")
EVIDENCE = RUNTIME.term("Evidence")
EVENT_SPAN = RUNTIME.term("EventSpan")
EVAL_RUN = RUNTIME.term("EvalRun")
JUDGMENT = RUNTIME.term("Judgment")


def _term(local_name: str) -> Term:
    """Build a property term whose compact name is a ``rt:`` CURIE.

    Naming convention across the ontology modules:

    - Type terms use ``Namespace.term()`` so their compact name is the bare
      local name (e.g. ``RunTimeline``) and they are registered in ``CONTEXT``.
    - Property terms use this helper so their compact name is the CURIE form
      (e.g. ``rt:produced``) and are deliberately NOT registered in ``CONTEXT``;
      the graph store expands them through the ``rt`` prefix and compacts them
      back to the same CURIE, keeping round-trips stable.

    ``mem`` and ``ctx`` instead use bare property names registered in
    ``CONTEXT``; both styles round-trip, but new ``rt``/``cap`` properties
    should follow the CURIE style here.
    """
    return Term(name=f"rt:{local_name}", iri=RUNTIME.iri(local_name))


PRODUCED = _term("produced")
PROVIDES = _term("provides")
STORED_AS = _term("storedAs")
STORED_AT = _term("storedAt")
HASH = _term("hash")
ITEM_COUNT = _term("itemCount")
TOOL_NAME = _term("toolName")
INPUT_SUMMARY = _term("inputSummary")
OUTPUT_SUMMARY = _term("outputSummary")
TRUNCATED = _term("truncated")
SOURCE_TYPE = _term("sourceType")
SOURCE_EVENT = _term("sourceEvent")
EVENT_SPAN_PROPERTY = _term("eventSpan")
SOURCE_SESSION = _term("sourceSession")
SOURCE_RUN = _term("sourceRun")
SOURCE_TURN = _term("sourceTurn")
SOURCE_REPLY = _term("sourceReply")
START_SEQUENCE = _term("startSequence")
END_SEQUENCE = _term("endSequence")
OBSERVED_AT = _term("observedAt")
CREATED_FROM = _term("createdFrom")
CREATED_AT = _term("createdAt")
UPDATED_AT = _term("updatedAt")
STATUS = _term("status")
SCOPE = _term("scope")
STATEMENT = _term("statement")
SUMMARY = _term("summary")

CONTEXT: dict[str, Any] = {
    "rt": RUNTIME.base,
    "turn": "https://pulsara.dev/turn/",
    "run-timeline": "https://pulsara.dev/run-timeline/",
    "tool-result": "https://pulsara.dev/tool-result/",
    "artifact": "https://pulsara.dev/artifact/",
    "evidence": "https://pulsara.dev/evidence/",
    "event": "https://pulsara.dev/event/",
    RUN_TIMELINE.name: RUN_TIMELINE.value,
    TURN.name: TURN.value,
    TOOL_RESULT.name: TOOL_RESULT.value,
    ARTIFACT.name: ARTIFACT.value,
    EVIDENCE.name: EVIDENCE.value,
    EVENT_SPAN.name: EVENT_SPAN.value,
    EVAL_RUN.name: EVAL_RUN.value,
    JUDGMENT.name: JUDGMENT.value,
}


class EvidenceSourceType(StrEnum):
    TOOL_RESULT = "tool_result"


class ToolExecutionStatus(StrEnum):
    SUCCESS = "success"
    ERROR = "error"
    CANCELLED = "cancelled"
