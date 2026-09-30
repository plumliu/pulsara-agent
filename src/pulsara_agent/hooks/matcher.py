"""Frozen RE2-compatible Hook matcher and closed Tool aliases."""

from __future__ import annotations

from dataclasses import dataclass

import re2

from pulsara_agent.hooks.contracts import FrozenHookDefinition, HookEventType


@dataclass(frozen=True, slots=True)
class FrozenHookMatcherSubject:
    canonical_subject: str
    aliases: tuple[str, ...]

    @property
    def candidates(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((self.canonical_subject, *self.aliases)))


def compile_matcher(pattern: str):
    """Compile the sealed case-sensitive RE2 search dialect."""

    options = re2.Options()
    options.case_sensitive = True
    options.longest_match = False
    options.log_errors = False
    options.max_mem = 8 * 1024 * 1024
    try:
        return re2.compile(pattern, options=options)
    except re2.error as exc:
        raise ValueError("matcher is not valid RE2 syntax") from exc


def definition_matches(
    definition: FrozenHookDefinition,
    subject: FrozenHookMatcherSubject,
) -> bool:
    fact = definition.matcher
    if fact.matches_all or fact.ignored_by_profile:
        return True
    compiled = compile_matcher(fact.pattern)
    return any(
        compiled.search(candidate) is not None for candidate in subject.candidates
    )


def event_matcher_subject(
    event_type: HookEventType,
    *,
    source: str | None = None,
    reason: str | None = None,
    trigger: str | None = None,
    agent_type: str | None = None,
    tool: FrozenHookMatcherSubject | None = None,
) -> FrozenHookMatcherSubject:
    if event_type in {
        HookEventType.USER_PROMPT_SUBMIT_EVENT,
        HookEventType.STOP_EVENT,
    }:
        return FrozenHookMatcherSubject("*", ())
    if event_type is HookEventType.SESSION_START_EVENT and source is not None:
        return FrozenHookMatcherSubject(source, ())
    if event_type is HookEventType.SESSION_END_EVENT and reason is not None:
        return FrozenHookMatcherSubject(reason, ())
    if (
        event_type
        in {
            HookEventType.PRE_COMPACT_EVENT,
            HookEventType.POST_COMPACT_EVENT,
        }
        and trigger is not None
    ):
        return FrozenHookMatcherSubject(trigger, ())
    if (
        event_type
        in {
            HookEventType.SUBAGENT_START_EVENT,
            HookEventType.SUBAGENT_STOP_EVENT,
        }
        and agent_type is not None
    ):
        return FrozenHookMatcherSubject(agent_type, ())
    if (
        event_type
        in {
            HookEventType.PRE_TOOL_USE_EVENT,
            HookEventType.PERMISSION_REQUEST_EVENT,
            HookEventType.POST_TOOL_USE_EVENT,
        }
        and tool is not None
    ):
        return tool
    raise ValueError("event matcher subject is incomplete")


def tool_matcher_subject(
    pulsara_name: str, *, resolved_remote_identity: str | None = None
) -> FrozenHookMatcherSubject:
    if resolved_remote_identity is not None:
        return FrozenHookMatcherSubject(resolved_remote_identity, ())
    aliases = {
        "terminal": ("Bash",),
        "read_file": ("Read",),
        "edit_file": ("apply_patch", "Edit"),
        "write_file": ("apply_patch", "Write"),
        "spawn_agent": ("Agent",),
        "search_content": ("Grep",),
        "find_files": ("Glob",),
    }
    return FrozenHookMatcherSubject(pulsara_name, aliases.get(pulsara_name, ()))


__all__ = [
    "FrozenHookMatcherSubject",
    "compile_matcher",
    "definition_matches",
    "event_matcher_subject",
    "tool_matcher_subject",
]
