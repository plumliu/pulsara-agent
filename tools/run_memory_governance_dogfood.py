"""Real-provider semantic activation probe for advisory-memory governance v2.

The report deliberately includes the exact production SYSTEM contract, evidence
packet, model JSON, parser result, and prepared canonical row values.  It never
prints the value of ``PULSARA_API_KEY``.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import json
import logging
import os
from typing import Mapping

from pulsara_agent.conversation_kernel.auxiliary_model import (
    DirectKernelAuxiliaryJsonModel,
)
from pulsara_agent.conversation_kernel.memory.contracts import (
    MAXIMUM_GOVERNANCE_FINAL_WIRE_BYTES,
    MAXIMUM_GOVERNANCE_INPUT_TOKENS,
    MAXIMUM_GOVERNANCE_OUTPUT_BYTES,
    MAXIMUM_GOVERNANCE_OUTPUT_TOKENS,
    FrozenMemoryGovernanceSourceBlock,
    FrozenMemoryGovernanceSourceItem,
    FrozenMemoryProposal,
    FrozenMemoryPublicFactProjection,
    MemoryDecisionKind,
    MemoryFactKind,
    MemoryGovernanceChronology,
    MemoryGovernanceEvidenceRole,
    MemoryGovernanceSourceBlockKind,
    MemoryKindHint,
    MemoryProducerKind,
    MemoryScopeKind,
    legal_memory_final_kinds,
    memory_fact_semantic_digest,
    memory_public_fact_payload,
    prepare_memory_candidate,
    prepare_memory_governance_acceptance,
)
from pulsara_agent.conversation_kernel.memory.governor import (
    _governance_packet_variants,
    _parse_governance_decision,
    _selected_governance_relation_targets,
)
from pulsara_agent.llm.adapters.openai.client import OpenAITransportTimeoutPolicy
from pulsara_agent.llm.input import LLMMessage
from pulsara_agent.memory.product_contract import (
    MEMORY_GOVERNANCE_CONTRACT_ID,
    MEMORY_GOVERNANCE_SYSTEM_PROMPT_V2,
)
from pulsara_agent.memory.scope import CTX_USER
from pulsara_agent.primitives.model_call import ModelCallPurpose
from pulsara_agent.process_api_key_boundary import ProcessApiKeyBoundary
from pulsara_agent.settings import PulsaraSettings, load_env_file


_WORKSPACE_SCOPE = "ctx:workspace/dogfood"


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    proposal: FrozenMemoryProposal
    sources: tuple[FrozenMemoryGovernanceSourceItem, ...]
    expected_decision: MemoryDecisionKind
    expected_kind: MemoryFactKind | None = None
    expected_reason: str | None = None
    visible: tuple[FrozenMemoryPublicFactProjection, ...] = ()
    targets: tuple[FrozenMemoryPublicFactProjection, ...] = ()
    exact_existing: tuple[FrozenMemoryPublicFactProjection, ...] = ()


def _proposal(
    statement: str,
    *,
    kind: MemoryKindHint = MemoryKindHint.AUTO,
    workspace: bool = False,
    applies_when: str | None = None,
) -> FrozenMemoryProposal:
    return FrozenMemoryProposal(
        statement=statement,
        scope_kind=(MemoryScopeKind.WORKSPACE if workspace else MemoryScopeKind.USER),
        scope_id=(_WORKSPACE_SCOPE if workspace else CTX_USER),
        kind_hint=kind,
        applies_when=applies_when,
    )


def _source(
    text: str,
    *,
    role: MemoryGovernanceEvidenceRole = MemoryGovernanceEvidenceRole.HUMAN_ASSERTION,
    chronology: MemoryGovernanceChronology = MemoryGovernanceChronology.BEFORE_PROPOSAL,
    ordinal: int = 1,
) -> FrozenMemoryGovernanceSourceItem:
    human = role in {
        MemoryGovernanceEvidenceRole.HUMAN_ASSERTION,
        MemoryGovernanceEvidenceRole.POST_PROPOSAL_HUMAN,
    }
    return FrozenMemoryGovernanceSourceItem(
        source_entry_id=f"dogfood-source:{ordinal}",
        chronology=chronology,
        source_product_label=(
            "候选提出后的用户原话"
            if role is MemoryGovernanceEvidenceRole.POST_PROPOSAL_HUMAN
            else "用户原话"
            if human
            else "非用户上下文"
        ),
        evidence_role=role,
        public_kind="用户消息" if human else "运行时上下文",
        blocks=(
            FrozenMemoryGovernanceSourceBlock(
                MemoryGovernanceSourceBlockKind.TEXT,
                text,
            ),
        ),
        anchor=human,
    )


def _fact(
    fact_id: str,
    statement: str,
    *,
    kind: MemoryFactKind,
    workspace: bool = False,
    applies_when: str | None = None,
) -> FrozenMemoryPublicFactProjection:
    scope_kind = MemoryScopeKind.WORKSPACE if workspace else MemoryScopeKind.USER
    scope_id = _WORKSPACE_SCOPE if workspace else CTX_USER
    return FrozenMemoryPublicFactProjection(
        fact_id=fact_id,
        scope_kind=scope_kind,
        scope_id=scope_id,
        fact_kind=kind,
        lifecycle="ACTIVE",
        statement=statement,
        applies_when=applies_when,
        fact_semantic_digest=memory_fact_semantic_digest(
            kind=kind,
            statement=statement,
            applies_when=applies_when,
            do_not_apply_when=(),
        ),
    )


def _scenarios() -> tuple[Scenario, ...]:
    old_detailed = _fact(
        "memory:old-detailed",
        "回答时默认给出详细解释",
        kind=MemoryFactKind.RESPONSE_PREFERENCE,
    )
    old_english = _fact(
        "memory:old-english",
        "Default to English replies",
        kind=MemoryFactKind.RESPONSE_PREFERENCE,
    )
    old_taxonomy = _fact(
        "memory:old-taxonomy",
        "Please answer concisely",
        kind=MemoryFactKind.USER_PROFILE,
    )
    related_rule = _fact(
        "memory:related-rule",
        "Back up the production database before schema changes",
        kind=MemoryFactKind.ACTION_RULE,
        workspace=True,
        applies_when="Before a production schema change",
    )
    recalled = _fact(
        "memory:recalled-zsh",
        "Use zsh commands by default",
        kind=MemoryFactKind.RESPONSE_PREFERENCE,
    )
    local_rule = _fact(
        "memory:production-backup-rule",
        "Create a backup before production schema changes",
        kind=MemoryFactKind.ACTION_RULE,
        workspace=True,
        applies_when="Before a production schema change",
    )
    return (
        Scenario("zh_fact", _proposal("本项目生产数据库是 PostgreSQL", kind=MemoryKindHint.FACT, workspace=True), (_source("本项目生产数据库是 PostgreSQL。"),), MemoryDecisionKind.ACCEPT, MemoryFactKind.FACT),
        Scenario("zh_user_profile", _proposal("用户使用 macOS", kind=MemoryKindHint.USER_PROFILE), (_source("我使用 macOS。"),), MemoryDecisionKind.ACCEPT, MemoryFactKind.USER_PROFILE),
        Scenario("zh_response_preference", _proposal("回答时通常先给结论", kind=MemoryKindHint.RESPONSE_PREFERENCE), (_source("以后回答请先给结论。"),), MemoryDecisionKind.ACCEPT, MemoryFactKind.RESPONSE_PREFERENCE),
        Scenario("zh_action_rule", _proposal("先备份生产数据库", kind=MemoryKindHint.ACTION_RULE, workspace=True, applies_when="执行生产数据库 schema 变更前"), (_source("以后执行生产数据库 schema 变更前，先备份生产数据库。"),), MemoryDecisionKind.ACCEPT, MemoryFactKind.ACTION_RULE),
        Scenario("zh_decision", _proposal("本项目已决定采用事件溯源方案", kind=MemoryKindHint.DECISION, workspace=True), (_source("本项目已经决定采用事件溯源方案。"),), MemoryDecisionKind.ACCEPT, MemoryFactKind.DECISION),
        Scenario("en_fact", _proposal("The project production database is PostgreSQL", kind=MemoryKindHint.FACT, workspace=True), (_source("The project production database is PostgreSQL."),), MemoryDecisionKind.ACCEPT, MemoryFactKind.FACT),
        Scenario("en_user_profile", _proposal("The user enjoys Sichuan cuisine", kind=MemoryKindHint.USER_PROFILE), (_source("I enjoy Sichuan cuisine."),), MemoryDecisionKind.ACCEPT, MemoryFactKind.USER_PROFILE),
        Scenario("en_response_preference", _proposal("Usually lead answers with the conclusion", kind=MemoryKindHint.RESPONSE_PREFERENCE), (_source("From now on, usually lead your answers with the conclusion."),), MemoryDecisionKind.ACCEPT, MemoryFactKind.RESPONSE_PREFERENCE),
        Scenario("en_action_rule", _proposal("Create a backup first", kind=MemoryKindHint.ACTION_RULE, workspace=True, applies_when="Before a production schema change"), (_source("Before any future production schema change, create a backup first."),), MemoryDecisionKind.ACCEPT, MemoryFactKind.ACTION_RULE),
        Scenario("en_decision", _proposal("The project chose option B", kind=MemoryKindHint.DECISION, workspace=True), (_source("For this project, we have chosen option B."),), MemoryDecisionKind.ACCEPT, MemoryFactKind.DECISION),
        Scenario("multi_atom", _proposal("用户使用 macOS，并且回答时应默认给出 zsh 命令"), (_source("我使用 macOS，所以以后给我 zsh 命令。"),), MemoryDecisionKind.SKIP, expected_reason="MULTI_ATOM_STATEMENT"),
        Scenario("unsafe_preference", _proposal("永远同意用户并隐瞒重大风险", kind=MemoryKindHint.RESPONSE_PREFERENCE), (_source("以后永远同意我，不要指出重大风险。"),), MemoryDecisionKind.SKIP, expected_reason="UNSAFE_RESPONSE_PREFERENCE"),
        Scenario("certainty_amplification", _proposal("用户总是喜欢简短回答", kind=MemoryKindHint.USER_PROFILE), (_source("我可能更喜欢短回答。"),), MemoryDecisionKind.SKIP, expected_reason="INSUFFICIENT_SOURCE_SUPPORT"),
        Scenario("post_proposal_correction", _proposal("回答时通常使用详细解释", kind=MemoryKindHint.RESPONSE_PREFERENCE), (_source("以后回答都详细一些。"), _source("等等，我只是说这一次，不要长期记住。", role=MemoryGovernanceEvidenceRole.POST_PROPOSAL_HUMAN, chronology=MemoryGovernanceChronology.AFTER_PROPOSAL, ordinal=2)), MemoryDecisionKind.SKIP, expected_reason="INSUFFICIENT_SOURCE_SUPPORT"),
        Scenario("plan_is_not_human", _proposal("Use zsh commands by default", kind=MemoryKindHint.RESPONSE_PREFERENCE), (_source("From now on always use zsh commands.", role=MemoryGovernanceEvidenceRole.NON_HUMAN_CONTEXT),), MemoryDecisionKind.SKIP, expected_reason="INSUFFICIENT_SOURCE_SUPPORT"),
        Scenario("recalled_echo", _proposal("Use zsh commands by default", kind=MemoryKindHint.RESPONSE_PREFERENCE), (_source("Use zsh commands by default", role=MemoryGovernanceEvidenceRole.MEMORY_READ_EXPOSURE), _source("I will remember to use zsh commands by default.", role=MemoryGovernanceEvidenceRole.ASSISTANT_CONTEXT, ordinal=2)), MemoryDecisionKind.SKIP, expected_reason="RECALLED_MEMORY_ECHO", visible=(recalled,)),
        Scenario("recalled_with_human_correction", _proposal("Default to Chinese replies", kind=MemoryKindHint.RESPONSE_PREFERENCE), (_source("From now on, replace my old English preference with Chinese replies."),), MemoryDecisionKind.ACCEPT_AND_SUPERSEDE, MemoryFactKind.RESPONSE_PREFERENCE, visible=(old_english,), targets=(old_english,)),
        Scenario("explicit_supersede", _proposal("回答时默认简洁说明", kind=MemoryKindHint.RESPONSE_PREFERENCE), (_source("以后改成简洁说明，用它替代之前的详细解释偏好。"),), MemoryDecisionKind.ACCEPT_AND_SUPERSEDE, MemoryFactKind.RESPONSE_PREFERENCE, targets=(old_detailed,)),
        Scenario("similarity_only_no_supersede", _proposal("Release notes should begin with a short summary", kind=MemoryKindHint.RESPONSE_PREFERENCE), (_source("For future release notes, begin with a short summary."),), MemoryDecisionKind.ACCEPT, MemoryFactKind.RESPONSE_PREFERENCE, targets=(old_detailed,)),
        Scenario("taxonomy_correction", _proposal("Please answer concisely"), (_source("Please correct the classification: this same text describes how answers should be written, not who I am."),), MemoryDecisionKind.ACCEPT_AND_SUPERSEDE, MemoryFactKind.RESPONSE_PREFERENCE, targets=(old_taxonomy,)),
        Scenario("taxonomy_related_atom_negative", _proposal("The project production database is PostgreSQL", kind=MemoryKindHint.FACT, workspace=True), (_source("The project production database is PostgreSQL; this is a project fact."),), MemoryDecisionKind.ACCEPT, MemoryFactKind.FACT, targets=(related_rule,)),
        Scenario("contradiction", _proposal("Default to Chinese replies", kind=MemoryKindHint.RESPONSE_PREFERENCE), (_source("I prefer Chinese replies."),), MemoryDecisionKind.ACCEPT_AND_CONTRADICT, MemoryFactKind.RESPONSE_PREFERENCE, targets=(old_english,)),
        Scenario("different_conditions_coexist", _proposal("Skip backups for local development schema changes", kind=MemoryKindHint.ACTION_RULE, workspace=True, applies_when="Before a local development schema change"), (_source("For future local development schema changes, skip the backup step."),), MemoryDecisionKind.ACCEPT, MemoryFactKind.ACTION_RULE, targets=(local_rule,)),
    )


def _packet(scenario: Scenario) -> tuple[str, Mapping[str, FrozenMemoryPublicFactProjection]]:
    legal = tuple(item.value for item in legal_memory_final_kinds(scenario.proposal))
    target_map = {item.fact_id: item for item in scenario.targets}
    base = {
        "contract": MEMORY_GOVERNANCE_CONTRACT_ID,
        "terminal_source_fence": {"status": "COMPLETED", "outcome": "The origin turn completed normally."},
        "producer": "The main model proposed this while replying.",
        "candidate": {
            "statement": scenario.proposal.statement,
            "scope_kind": scenario.proposal.scope_kind.value,
            "kind_hint": scenario.proposal.kind_hint.value,
            "applies_when": scenario.proposal.applies_when,
            "do_not_apply_when": scenario.proposal.do_not_apply_when,
            "basis_memory_ids": (),
            "visible_memory_disposition": "COMPLETE",
            "legal_final_kinds": legal,
        },
        "source_coverage": {
            "causal_context_complete": True,
            "origin_turn_human_source_complete": True,
            "post_proposal_human_source_complete": True,
            "omitted_causal_items": 0,
            "omitted_assistant_or_tool_items": 0,
            "omitted_post_proposal_human_items": 0,
            "relation_authority": True,
        },
        "based_on_items": (),
        "all_model_visible_memory": tuple(memory_public_fact_payload(item) for item in scenario.visible),
        "exact_existing_sources": tuple(memory_public_fact_payload(item) for item in scenario.exact_existing),
    }
    variant = _governance_packet_variants(
        base=base,
        source_items=scenario.sources,
        citation_items=(),
        related_items=tuple(memory_public_fact_payload(item) for item in scenario.targets),
        targets=target_map,
        legal_final_kinds=legal,
    )[0]
    return variant.packet, variant.allowed_targets


def _final_rows(scenario: Scenario, decision) -> Mapping[str, object]:
    candidate = prepare_memory_candidate(
        candidate_id=f"candidate:dogfood:{scenario.name}",
        memory_domain_id="u_dogfood",
        origin_workspace_id="workspace:dogfood",
        origin_session_id="session:dogfood",
        producer_kind=MemoryProducerKind.MAIN_AGENT_REMEMBER,
        producer_entry_id=f"entry:dogfood:{scenario.name}",
        producer_tool_call_id=f"call:dogfood:{scenario.name}",
        proposal=scenario.proposal,
    )
    prepared = prepare_memory_governance_acceptance(
        candidate=candidate,
        decision=decision,
        relation_targets=_selected_governance_relation_targets(
            decision,
            {item.fact_id: item for item in scenario.targets},
        ),
    )
    terminal = prepared.terminal_draft
    return {
        "candidate_terminal": {
            "status": terminal.status.value,
            "decision_kind": terminal.decision_kind.value,
            "final_kind": None if terminal.final_kind is None else terminal.final_kind.value,
            "decision_reason_code": terminal.decision_reason_code,
            "decision_public_summary": terminal.decision_public_summary,
            "related_target_fact_id": terminal.related_target_fact_id,
            "accepted_fact_id": terminal.accepted_fact_id,
        },
        "fact": None if prepared.fact is None else {
            "id": prepared.fact.fact_id,
            "scope_kind": prepared.fact.scope_kind.value,
            "scope_id": prepared.fact.scope_id,
            "kind": prepared.fact.fact_kind.value,
            "statement": prepared.fact.statement,
            "applies_when": prepared.fact.applies_when,
            "lifecycle": "ACTIVE",
        },
        "relations": tuple(
            {
                "id": item.relation_id,
                "kind": item.relation_kind.value,
                "target_fact_id": item.target.fact_id,
                "supersede_mode": None if item.supersede_mode is None else item.supersede_mode.value,
                "target_lifecycle_after": item.expected_target_lifecycle_after,
            }
            for item in prepared.relation_drafts
        ),
    }


def _semantic_match(scenario: Scenario, decision) -> bool:
    return (
        decision.decision_kind is scenario.expected_decision
        and decision.final_kind is scenario.expected_kind
        and decision.reason_code == scenario.expected_reason
    )


async def _run(group: int, group_size: int, *, show_system: bool) -> int:
    logging.getLogger("jieba").setLevel(logging.ERROR)
    load_env_file(".env", override=False)
    os.environ["PULSARA_API"] = "openai_responses"
    settings = PulsaraSettings.from_env()
    if not settings.llm.api_key:
        raise RuntimeError("PULSARA_API_KEY is unavailable")
    model = DirectKernelAuxiliaryJsonModel(
        settings.llm,
        api_key_boundary=ProcessApiKeyBoundary(),
    )
    scenarios = _scenarios()[group * group_size : (group + 1) * group_size]
    report: dict[str, object] = {
        "wire_api": settings.llm.api,
        "model": settings.llm.flash_model,
        "system": MEMORY_GOVERNANCE_SYSTEM_PROMPT_V2 if show_system else "<same exact production SYSTEM as group 0>",
        "scenarios": [],
    }
    passed = True
    for scenario in scenarios:
        packet, allowed = _packet(scenario)
        selected = model.prepare_first_fitting_json_call(
            purpose=ModelCallPurpose.MEMORY_GOVERNANCE,
            message_variants=((LLMMessage.system(MEMORY_GOVERNANCE_SYSTEM_PROMPT_V2), LLMMessage.user(packet)),),
            maximum_input_tokens=MAXIMUM_GOVERNANCE_INPUT_TOKENS,
            maximum_input_bytes=MAXIMUM_GOVERNANCE_FINAL_WIRE_BYTES,
            maximum_output_tokens=MAXIMUM_GOVERNANCE_OUTPUT_TOKENS,
            timeout_policy=OpenAITransportTimeoutPolicy(10, 20, 10, 45, 60),
            maximum_result_bytes=MAXIMUM_GOVERNANCE_OUTPUT_BYTES,
        )
        if selected is None:
            raise RuntimeError(f"scenario {scenario.name} did not fit final wire")
        prepared_call, _ = selected
        raw_model_json = await model.complete_prepared_json(prepared_call)
        try:
            parsed = _parse_governance_decision(
                raw_model_json,
                allowed,
                legal_final_kinds=legal_memory_final_kinds(scenario.proposal),
            )
            parse_error = None
            match = _semantic_match(scenario, parsed)
            final_rows = _final_rows(scenario, parsed)
            parse_outcome = {
                "decision": parsed.decision_kind.value,
                "final_kind": (
                    None if parsed.final_kind is None else parsed.final_kind.value
                ),
                "reason_code": parsed.reason_code,
                "semantic_match": match,
            }
        except Exception as exc:
            parsed = None
            parse_error = f"{type(exc).__name__}: {exc}"
            match = False
            final_rows = None
            parse_outcome = {
                "decision": None,
                "final_kind": None,
                "reason_code": None,
                "semantic_match": False,
            }
        passed = passed and match
        report["scenarios"].append(
            {
                "name": scenario.name,
                "evidence_packet": json.loads(packet),
                "final_wire_utf8_bytes": prepared_call.final_wire_utf8_bytes,
                "estimated_input_tokens": prepared_call.estimated_input_tokens,
                "raw_model_json": raw_model_json,
                "parse_error": parse_error,
                "parse_outcome": parse_outcome,
                "prepared_final_rows": final_rows,
            }
        )
    report["all_semantic_expectations_passed"] = passed
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if passed else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--group", type=int, required=True)
    parser.add_argument("--group-size", type=int, default=4)
    parser.add_argument("--show-system", action="store_true")
    args = parser.parse_args()
    return asyncio.run(_run(args.group, args.group_size, show_system=args.show_system))


if __name__ == "__main__":
    raise SystemExit(main())
