"""Pulsara context and scope ontology."""

from __future__ import annotations

from typing import Any

from pulsara_agent.jsonld import Namespace


CONTEXT_NS = Namespace("https://pulsara.dev/context#")

SCOPE = CONTEXT_NS.term("Scope")
USER_SCOPE = CONTEXT_NS.term("UserScope")
AGENT_SCOPE = CONTEXT_NS.term("AgentScope")
SESSION_SCOPE = CONTEXT_NS.term("SessionScope")
TASK_SCOPE = CONTEXT_NS.term("TaskScope")
DOMAIN_SCOPE = CONTEXT_NS.term("DomainScope")
WORKSPACE_SCOPE = CONTEXT_NS.term("WorkspaceScope")
ARTIFACT_SCOPE = CONTEXT_NS.term("ArtifactScope")
SKILL_SCOPE = CONTEXT_NS.term("SkillScope")
TEAM_SCOPE = CONTEXT_NS.term("TeamScope")

SCOPE_KIND = CONTEXT_NS.term("scopeKind")
SCOPE_LABEL = CONTEXT_NS.term("scopeLabel")
SCOPE_KEY = CONTEXT_NS.term("scopeKey")
PARENT_SCOPE = CONTEXT_NS.term("parentScope")
CONTAINS = CONTEXT_NS.term("contains")
ACTIVE_IN = CONTEXT_NS.term("activeIn")
WORKSPACE_ROOT = CONTEXT_NS.term("workspaceRoot")
GIT_REMOTE = CONTEXT_NS.term("gitRemote")
DOMAIN_SLUG = CONTEXT_NS.term("domainSlug")

CONTEXT: dict[str, Any] = {
    "ctx": CONTEXT_NS.base,
    SCOPE.name: SCOPE.value,
    USER_SCOPE.name: USER_SCOPE.value,
    AGENT_SCOPE.name: AGENT_SCOPE.value,
    SESSION_SCOPE.name: SESSION_SCOPE.value,
    TASK_SCOPE.name: TASK_SCOPE.value,
    DOMAIN_SCOPE.name: DOMAIN_SCOPE.value,
    WORKSPACE_SCOPE.name: WORKSPACE_SCOPE.value,
    ARTIFACT_SCOPE.name: ARTIFACT_SCOPE.value,
    SKILL_SCOPE.name: SKILL_SCOPE.value,
    TEAM_SCOPE.name: TEAM_SCOPE.value,
    SCOPE_KIND.name: SCOPE_KIND.value,
    SCOPE_LABEL.name: SCOPE_LABEL.value,
    SCOPE_KEY.name: SCOPE_KEY.value,
    PARENT_SCOPE.name: {"@id": PARENT_SCOPE.value, "@type": "@id"},
    CONTAINS.name: {"@id": CONTAINS.value, "@type": "@id"},
    ACTIVE_IN.name: {"@id": ACTIVE_IN.value, "@type": "@id"},
    WORKSPACE_ROOT.name: WORKSPACE_ROOT.value,
    GIT_REMOTE.name: GIT_REMOTE.value,
    DOMAIN_SLUG.name: DOMAIN_SLUG.value,
}
