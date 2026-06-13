"""Pulsara capability ontology for skills, tools, plugins, and policies."""

from __future__ import annotations

from typing import Any

from pulsara_agent.jsonld import Namespace, Term


CAPABILITY = Namespace("https://pulsara.dev/capability#")

SKILL = CAPABILITY.term("Skill")
TOOL = CAPABILITY.term("Tool")
PLUGIN = CAPABILITY.term("Plugin")
POLICY = CAPABILITY.term("Policy")


def _term(local_name: str) -> Term:
    """Build a property term whose compact name is a ``cap:`` CURIE.

    Type terms use ``Namespace.term()`` (bare name, registered in ``CONTEXT``);
    property terms use this helper (CURIE name, NOT in ``CONTEXT``, expanded via
    the ``cap`` prefix). See ``runtime._term`` for the full rationale.
    """
    return Term(name=f"cap:{local_name}", iri=CAPABILITY.iri(local_name))


VERSION = _term("version")
VERSION_OF = _term("versionOf")
SUPERSEDES = _term("supersedes")
PROVIDES_TOOL = _term("providesTool")
PROVIDES_SKILL = _term("providesSkill")
REQUIRES = _term("requires")
HAS_INPUT_SCHEMA = _term("hasInputSchema")
HAS_OUTPUT_SCHEMA = _term("hasOutputSchema")
ALLOWED_IN_SCOPE = _term("allowedInScope")
BLOCKED_IN_SCOPE = _term("blockedInScope")
SOURCE_DATA_URI = _term("sourceDataURI")

CONTEXT: dict[str, Any] = {
    "cap": CAPABILITY.base,
    "skill": "https://pulsara.dev/skill/",
    "tool": "https://pulsara.dev/tool/",
    "plugin": "https://pulsara.dev/plugin/",
    "policy": "https://pulsara.dev/policy/",
    SKILL.name: SKILL.value,
    TOOL.name: TOOL.value,
    PLUGIN.name: PLUGIN.value,
    POLICY.name: POLICY.value,
}
