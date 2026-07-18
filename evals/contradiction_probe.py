"""Real-LLM probe for non-explicit memory contradiction judgment.

This is deliberately not production governance. It measures the core v2
assumption before adding a real ``contradict_and_submit`` decision type.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pulsara_agent.event import EventContext, RunErrorEvent, TextBlockSegmentEvent
from pulsara_agent.llm import LLMMessage, ModelRole, build_llm_runtime
from pulsara_agent.llm.request import LLMContext, LLMOptions
from pulsara_agent.settings import PulsaraSettings


SYSTEM_PROMPT = """
You are probing a future Pulsara memory-governance rule.

Classify how governance should handle a pending durable-memory candidate when it
is shown with one or more related existing canonical memories.

Allowed labels:
- skip: do not write the new candidate; it is duplicate, weak, not durable, or task-local.
- coexist: write the new candidate, but do not link it to the old memory.
- supersede: write the new candidate and retire the old memory. Use only when
  the user explicitly says they changed/replaced the old preference.
- contradict: write the new candidate and link a non-destructive contradiction
  edge to the old memory. Use when the old and new ACTIVE Preferences are same
  scope, same subject, can not both be true as durable user preferences, and
  there is no explicit replacement instruction.

Safety rules:
- A false supersede is very bad because it destroys a valid memory.
- A false contradiction is less bad because both memories stay ACTIVE, but it is
  still noise. Use contradict only for a clear same-subject conflict.
- Do not contradict exact duplicates.
- Do not contradict different scopes.
- This v2 probe supports only one contradiction target. If more than one
  existing memory would need a contradiction edge, choose coexist instead of
  returning multiple target ids.
- Do not turn temporary mood, story context, or one-off task details into durable memory.
- If subject match is uncertain, choose coexist rather than contradict.

Output JSON only:
{
  "label": "skip|coexist|supersede|contradict",
  "confidence": "low|medium|high",
  "target_memory_ids": ["..."],  // existing canonical memory ids selected for supersede/contradict
  "reason": "short reason",
  "safety_notes": ["..."]
}
""".strip()


@dataclass(frozen=True, slots=True)
class ProbeCase:
    case_id: str
    expected: tuple[str, ...]
    user_utterance: str
    candidate: dict[str, Any]
    note: str
    old_memory: dict[str, Any] | None = None
    related_existing_memories: tuple[dict[str, Any], ...] = ()
    expected_target_ids: tuple[str, ...] = ()


def probe_cases() -> list[ProbeCase]:
    old_egg = {
        "memory_id": "preference:likes-egg-tarts",
        "memory_type": "Preference",
        "statement": "The user likes egg tarts.",
        "scope": "ctx:user",
        "status": "active",
        "is_exact_duplicate": False,
    }
    old_concise = {
        "memory_id": "preference:concise-summaries",
        "memory_type": "Preference",
        "statement": "The user prefers concise final summaries.",
        "scope": "ctx:user",
        "status": "active",
        "is_exact_duplicate": False,
    }
    old_brief_status = {
        "memory_id": "preference:brief-status-updates",
        "memory_type": "Preference",
        "statement": "The user prefers brief status updates.",
        "scope": "ctx:user",
        "status": "active",
        "is_exact_duplicate": False,
    }
    old_light_roast = {
        "memory_id": "preference:light-roast-coffee",
        "memory_type": "Preference",
        "statement": "The user prefers light roast coffee.",
        "scope": "ctx:user",
        "status": "active",
        "is_exact_duplicate": False,
    }
    distractors = (
        _memory("preference:likes-cheesecake", "The user likes cheesecake."),
        _memory("preference:hates-durian", "The user hates durian."),
        _memory("preference:prefers-tea", "The user prefers oolong tea."),
        _memory(
            "preference:concise-summaries", "The user prefers concise final summaries."
        ),
        _memory(
            "preference:uses-uv", "The user prefers uv for Python project commands."
        ),
        _memory("preference:dark-theme", "The user prefers dark theme in tools."),
        _memory("preference:likes-egg-tarts", "The user likes egg tarts."),
        _memory(
            "preference:morning-work", "The user prefers deep work in the morning."
        ),
        _memory("preference:markdown", "The user prefers Markdown output for notes."),
        _memory(
            "preference:avoid-emojis",
            "The user prefers no emojis in engineering summaries.",
        ),
    )
    summary_distractors = (
        _memory("preference:likes-egg-tarts", "The user likes egg tarts."),
        _memory(
            "preference:verbose-code-comments",
            "The user prefers explanatory code comments.",
        ),
        _memory(
            "preference:table-summaries",
            "The user likes table summaries for comparisons.",
        ),
        _memory(
            "preference:concise-summaries", "The user prefers concise final summaries."
        ),
        _memory("preference:dark-theme", "The user prefers dark theme in tools."),
        _memory("preference:python-uv", "The user prefers uv for Python commands."),
        _memory(
            "preference:morning-work", "The user prefers deep work in the morning."
        ),
        _memory(
            "preference:no-emoji",
            "The user prefers no emojis in engineering summaries.",
        ),
        _memory(
            "preference:tests-first", "The user prefers tests before broad refactors."
        ),
        _memory("preference:markdown", "The user prefers Markdown output for notes."),
    )
    pytest_distractors = (
        _memory(
            "preference:repo-a-pytest",
            "The user prefers pytest in this project.",
            scope="ctx:workspace/repo_a",
        ),
        _memory(
            "preference:repo-b-ruff",
            "The user prefers ruff in this project.",
            scope="ctx:workspace/repo_b",
        ),
        _memory(
            "preference:repo-b-pytest",
            "The user prefers pytest in this project.",
            scope="ctx:workspace/repo_b",
        ),
        _memory(
            "preference:repo-b-uv",
            "The user prefers uv in this project.",
            scope="ctx:workspace/repo_b",
        ),
        _memory(
            "preference:user-pytest",
            "The user usually prefers pytest.",
            scope="ctx:user",
        ),
        _memory(
            "preference:repo-a-uv",
            "The user prefers uv in this project.",
            scope="ctx:workspace/repo_a",
        ),
        _memory(
            "preference:repo-b-mypy",
            "The user prefers mypy in this project.",
            scope="ctx:workspace/repo_b",
        ),
        _memory(
            "preference:repo-c-pytest",
            "The user prefers pytest in this project.",
            scope="ctx:workspace/repo_c",
        ),
        _memory(
            "preference:repo-b-vitest",
            "The user prefers vitest in frontend tests.",
            scope="ctx:workspace/repo_b",
        ),
        _memory(
            "preference:repo-a-ruff",
            "The user prefers ruff in this project.",
            scope="ctx:workspace/repo_a",
        ),
    )
    return [
        ProbeCase(
            case_id="explicit_replacement_zh",
            expected=("supersede",),
            user_utterance="我改了，以后不要记我喜欢蛋挞了。请记住：我最讨厌蛋挞。",
            old_memory=old_egg,
            candidate=_pref("The user hates egg tarts.", "ctx:user"),
            note="Explicit replacement should retire the old preference, not merely contradict.",
        ),
        ProbeCase(
            case_id="explicit_replacement_en",
            expected=("supersede",),
            user_utterance="Actually change my preference: stop remembering that I like egg tarts; I hate egg tarts now.",
            old_memory=old_egg,
            candidate=_pref("The user hates egg tarts.", "ctx:user"),
            note="English explicit replacement.",
        ),
        ProbeCase(
            case_id="from_now_on_not_like",
            expected=("supersede",),
            user_utterance="From now on, assume I hate egg tarts, not that I like them.",
            old_memory=old_egg,
            candidate=_pref("The user hates egg tarts.", "ctx:user"),
            note="Explicit future override without the word replace.",
        ),
        ProbeCase(
            case_id="non_explicit_conflict_zh",
            expected=("contradict",),
            user_utterance="请记住：我最讨厌的就是蛋挞。",
            old_memory=old_egg,
            candidate=_pref("The user hates egg tarts.", "ctx:user"),
            note="Strong same-subject conflict, but no explicit replacement.",
        ),
        ProbeCase(
            case_id="non_explicit_conflict_en",
            expected=("contradict",),
            user_utterance="Please remember this: I absolutely hate egg tarts.",
            old_memory=old_egg,
            candidate=_pref("The user hates egg tarts.", "ctx:user"),
            note="English non-explicit same-subject conflict.",
        ),
        ProbeCase(
            case_id="summary_preference_conflict",
            expected=("contradict",),
            user_utterance="Please remember: I hate concise final summaries.",
            old_memory=old_concise,
            candidate=_pref("The user hates concise final summaries.", "ctx:user"),
            note="Non-explicit conflict on a non-food preference.",
        ),
        ProbeCase(
            case_id="exact_duplicate",
            expected=("skip",),
            user_utterance="Please remember: I like egg tarts.",
            old_memory=old_egg | {"is_exact_duplicate": True},
            candidate=_pref("The user likes egg tarts.", "ctx:user"),
            note="Statement-equivalent duplicate should skip.",
        ),
        ProbeCase(
            case_id="temporary_mood",
            expected=("skip",),
            user_utterance="Today I do not feel like eating egg tarts.",
            old_memory=old_egg,
            candidate=_pref("The user does not want egg tarts today.", "ctx:user"),
            note="Temporary mood should not become durable memory.",
        ),
        ProbeCase(
            case_id="story_context",
            expected=("skip",),
            user_utterance="For this story scene, my character hates egg tarts.",
            old_memory=old_egg,
            candidate=_pref("The user's story character hates egg tarts.", "ctx:user"),
            note="Fiction/task context should not become user preference.",
        ),
        ProbeCase(
            case_id="narrower_variant",
            expected=("coexist",),
            user_utterance="Please remember: I hate overly sweet egg tarts.",
            old_memory=old_egg,
            candidate=_pref("The user hates overly sweet egg tarts.", "ctx:user"),
            note="Can like egg tarts generally while hating an overly sweet subtype.",
        ),
        ProbeCase(
            case_id="different_subject",
            expected=("coexist",),
            user_utterance="Please remember: I hate durian.",
            old_memory=old_egg,
            candidate=_pref("The user hates durian.", "ctx:user"),
            note="Different subject should coexist if durable.",
        ),
        ProbeCase(
            case_id="scope_mismatch",
            expected=("coexist",),
            user_utterance="In repo_b, remember that I hate pytest for this project.",
            old_memory={
                "memory_id": "preference:repo-a-pytest",
                "memory_type": "Preference",
                "statement": "The user prefers pytest in this project.",
                "scope": "ctx:workspace/repo_a",
                "status": "active",
                "is_exact_duplicate": False,
            },
            candidate=_pref(
                "The user hates pytest in this project.", "ctx:workspace/repo_b"
            ),
            note="Different scopes must not be contradicted.",
        ),
        ProbeCase(
            case_id="ambiguous_dark_roast_for_cold_brew",
            expected=("coexist",),
            user_utterance="Please remember: I prefer dark roast coffee for cold brew.",
            old_memory=old_light_roast,
            candidate=_pref(
                "The user prefers dark roast coffee for cold brew.", "ctx:user"
            ),
            note="A context-specific coffee preference can coexist with a general light-roast preference.",
        ),
        ProbeCase(
            case_id="ambiguous_detailed_design_reviews",
            expected=("coexist",),
            user_utterance="Please remember: I prefer detailed notes for design reviews.",
            old_memory=old_concise,
            candidate=_pref(
                "The user prefers detailed notes for design reviews.", "ctx:user"
            ),
            note="Detailed design-review notes can coexist with concise final summaries.",
        ),
        ProbeCase(
            case_id="temporary_health_related_avoidance",
            expected=("skip",),
            user_utterance="While my throat hurts this week, I am avoiding egg tarts.",
            old_memory=old_egg,
            candidate=_pref(
                "The user is avoiding egg tarts while their throat hurts this week.",
                "ctx:user",
            ),
            note="Temporary health context should not become durable preference or contradiction.",
        ),
        ProbeCase(
            case_id="buried_counterpart_egg_tart",
            expected=("contradict",),
            user_utterance="Please remember this: I absolutely hate egg tarts.",
            related_existing_memories=distractors,
            expected_target_ids=("preference:likes-egg-tarts",),
            candidate=_pref("The user hates egg tarts.", "ctx:user"),
            note="Correct contradiction target is buried among unrelated same-scope preferences.",
        ),
        ProbeCase(
            case_id="buried_counterpart_summary_replacement",
            expected=("supersede",),
            user_utterance="Actually change my preference: stop using concise final summaries; I want exhaustive final summaries now.",
            related_existing_memories=summary_distractors,
            expected_target_ids=("preference:concise-summaries",),
            candidate=_pref("The user prefers exhaustive final summaries.", "ctx:user"),
            note="Explicit replacement target is buried among same-scope preferences.",
        ),
        ProbeCase(
            case_id="buried_counterpart_scope_select_repo_b",
            expected=("contradict",),
            user_utterance="In repo_b, please remember: I hate pytest in this project.",
            related_existing_memories=pytest_distractors,
            expected_target_ids=("preference:repo-b-pytest",),
            candidate=_pref(
                "The user hates pytest in this project.", "ctx:workspace/repo_b"
            ),
            note="Must select same-scope repo_b pytest target, not repo_a/user pytest distractors.",
        ),
        ProbeCase(
            case_id="multi_counterpart_food_preferences",
            expected=("coexist",),
            user_utterance="Please remember: I hate egg tarts and cheesecake.",
            related_existing_memories=(
                old_egg,
                _memory("preference:likes-cheesecake", "The user likes cheesecake."),
                _memory("preference:likes-durian", "The user likes durian."),
            ),
            candidate=_pref("The user hates egg tarts and cheesecake.", "ctx:user"),
            note="Two clear contradiction targets; v2 single-target policy should force coexist.",
        ),
        ProbeCase(
            case_id="multi_counterpart_summary_preferences",
            expected=("coexist",),
            user_utterance="Please remember: I hate concise final summaries and brief status updates.",
            related_existing_memories=(old_concise, old_brief_status, old_egg),
            candidate=_pref(
                "The user hates concise final summaries and brief status updates.",
                "ctx:user",
            ),
            note="Two clear same-scope summary/update targets; v2 should not return multiple targets.",
        ),
    ]


def _pref(statement: str, scope: str) -> dict[str, Any]:
    return {
        "kind": "Preference",
        "statement": statement,
        "scope": scope,
        "source_authority": "explicit_user_instruction",
        "verification_status": "user_confirmed",
    }


def _memory(
    memory_id: str,
    statement: str,
    *,
    scope: str = "ctx:user",
    memory_type: str = "Preference",
    status: str = "active",
    exact_duplicate: bool = False,
) -> dict[str, Any]:
    return {
        "memory_id": memory_id,
        "memory_type": memory_type,
        "statement": statement,
        "scope": scope,
        "status": status,
        "is_exact_duplicate": exact_duplicate,
    }


async def run_probe(
    *,
    output: Path | None = None,
    limit: int | None = None,
    case_ids: set[str] | None = None,
) -> dict[str, Any]:
    settings = _load_settings()
    runtime = build_llm_runtime(settings.llm)
    cases = probe_cases()
    if case_ids is not None:
        cases = [case for case in cases if case.case_id in case_ids]
    cases = cases[: limit or None]
    trajectories = []
    for index, case in enumerate(cases, start=1):
        trajectories.append(await _run_case(runtime, case, index=index))
    summary = _summarize(trajectories)
    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "model_role": ModelRole.FLASH.value,
        "case_count": len(trajectories),
        "summary": summary,
        "trajectories": trajectories,
    }
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return report


async def _run_case(runtime, case: ProbeCase, *, index: int) -> dict[str, Any]:
    event_context = EventContext(
        run_id=f"run:contradiction-probe/{case.case_id}/{uuid4().hex}",
        turn_id=f"turn:contradiction-probe/{index:03d}",
        reply_id=f"reply:contradiction-probe/{case.case_id}/{uuid4().hex}",
    )
    payload = {
        "case_id": case.case_id,
        "note": case.note,
        "user_utterance": case.user_utterance,
        "new_candidate": case.candidate,
        "related_existing_memories": list(_related_memories(case)),
        "expected_for_probe": list(case.expected),
        "expected_target_ids_for_probe": list(case.expected_target_ids),
    }
    text_parts: list[str] = []
    errors: list[str] = []
    async for event in runtime.stream(
        role=ModelRole.FLASH,
        context=LLMContext(
            system_prompt=SYSTEM_PROMPT,
            messages=(
                LLMMessage.user(
                    "Classify this case. Output JSON only:\n"
                    + json.dumps(payload, ensure_ascii=False, indent=2)
                ),
            ),
        ),
        event_context=event_context,
        options=LLMOptions(),
    ):
        if isinstance(event, TextBlockSegmentEvent):
            text_parts.append(event.text)
        elif isinstance(event, RunErrorEvent):
            errors.append(event.message)
    raw_text = "".join(text_parts).strip()
    parsed, parse_error = _parse_json_object(raw_text)
    label = parsed.get("label") if isinstance(parsed, dict) else None
    expected_match = label in case.expected
    target_ids = _target_ids(
        parsed.get("target_memory_ids") if isinstance(parsed, dict) else None
    )
    target_match = _target_match(target_ids, case.expected_target_ids)
    return {
        "case_id": case.case_id,
        "expected": list(case.expected),
        "expected_target_ids": list(case.expected_target_ids),
        "label": label,
        "expected_match": expected_match,
        "confidence": parsed.get("confidence") if isinstance(parsed, dict) else None,
        "target_memory_ids": target_ids,
        "target_match": target_match,
        "reason": parsed.get("reason") if isinstance(parsed, dict) else None,
        "safety_notes": parsed.get("safety_notes")
        if isinstance(parsed, dict)
        else None,
        "parse_error": parse_error,
        "errors": errors,
        "raw_text": raw_text,
        "input": payload,
    }


def _related_memories(case: ProbeCase) -> tuple[dict[str, Any], ...]:
    if case.related_existing_memories:
        return case.related_existing_memories
    if case.old_memory is None:
        return ()
    return (case.old_memory,)


def _target_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str)]


def _target_match(actual: list[str], expected: tuple[str, ...]) -> bool:
    if not expected:
        return True
    return set(actual) == set(expected)


def _parse_json_object(text: str) -> tuple[dict[str, Any], str | None]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end < start:
        return {}, "no_json_object"
    try:
        payload = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError as exc:
        return {}, f"json_decode_error:{exc}"
    if not isinstance(payload, dict):
        return {}, "json_not_object"
    return payload, None


def _summarize(trajectories: list[dict[str, Any]]) -> dict[str, Any]:
    by_label: dict[str, int] = {}
    mismatches = []
    target_mismatches = []
    parse_errors = []
    for trajectory in trajectories:
        label = str(trajectory.get("label") or "missing")
        by_label[label] = by_label.get(label, 0) + 1
        if trajectory.get("parse_error"):
            parse_errors.append(trajectory["case_id"])
        if not trajectory.get("expected_match"):
            mismatches.append(
                {
                    "case_id": trajectory["case_id"],
                    "expected": trajectory["expected"],
                    "actual": trajectory.get("label"),
                    "reason": trajectory.get("reason"),
                }
            )
        if not trajectory.get("target_match"):
            target_mismatches.append(
                {
                    "case_id": trajectory["case_id"],
                    "expected_target_ids": trajectory["expected_target_ids"],
                    "actual_target_ids": trajectory.get("target_memory_ids"),
                    "label": trajectory.get("label"),
                    "reason": trajectory.get("reason"),
                }
            )
    return {
        "expected_match_count": sum(
            1 for item in trajectories if item.get("expected_match")
        ),
        "mismatch_count": len(mismatches),
        "target_match_count": sum(
            1 for item in trajectories if item.get("target_match")
        ),
        "target_mismatch_count": len(target_mismatches),
        "parse_error_count": len(parse_errors),
        "labels": by_label,
        "mismatches": mismatches,
        "target_mismatches": target_mismatches,
        "parse_errors": parse_errors,
    }


def _load_settings() -> PulsaraSettings:
    env_file = Path(".env")
    if env_file.exists():
        return PulsaraSettings.from_env_file(env_file)
    return PulsaraSettings.from_env()


def _default_output_path() -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return Path("/tmp") / f"pulsara_contradiction_probe_{stamp}.json"


async def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=_default_output_path())
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--case-id",
        action="append",
        default=None,
        help="Run only this case id. May be passed multiple times.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run without PULSARA_RUN_REAL_LLM=1.",
    )
    args = parser.parse_args()
    if not args.force and os.getenv("PULSARA_RUN_REAL_LLM") != "1":
        raise SystemExit(
            "Set PULSARA_RUN_REAL_LLM=1 or pass --force to spend real LLM calls."
        )
    report = await run_probe(
        output=args.output,
        limit=args.limit,
        case_ids=set(args.case_id) if args.case_id else None,
    )
    print(
        json.dumps(
            {"output": str(args.output), **report["summary"]},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(_main())
