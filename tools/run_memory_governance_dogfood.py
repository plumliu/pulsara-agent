"""Real-provider semantic activation probe for advisory-memory governance v3.

The report contains the exact production evidence packet, raw model JSON,
parser result, and prepared canonical values. It never renders the value of
``PULSARA_API_KEY``.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
import json
import logging
from pathlib import Path
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
    PreparedMemoryBasisReference,
    legal_memory_final_kinds,
    memory_context_product_label,
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
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, ModelsDevCatalogClient
from pulsara_agent.llm.model_connections import ModelCallBinding
from pulsara_agent.llm.model_target import default_reasoning_selection
from pulsara_agent.llm.provider_open import (
    _issue_confirmed_memory_governance_terminal_fence,
)
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.memory.product_contract import (
    MEMORY_GOVERNANCE_CONTRACT_ID,
    MEMORY_GOVERNANCE_SYSTEM_PROMPT_V3,
)
from pulsara_agent.memory.scope import CTX_GLOBAL
from pulsara_agent.primitives.model_call import ModelCallPurpose
from pulsara_agent.settings import LocalSettingsStore


_PROJECT_CONTEXT = "ctx:workspace/dogfood"
_RECORDED_AT = "2026-09-02T00:00:00Z"


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    proposal: FrozenMemoryProposal
    sources: tuple[FrozenMemoryGovernanceSourceItem, ...]
    expected_decision: MemoryDecisionKind
    expected_kind: MemoryFactKind | None = None
    expected_reason: str | None = None
    alternative_kinds: tuple[MemoryFactKind, ...] = ()
    visible: tuple[FrozenMemoryPublicFactProjection, ...] = ()
    targets: tuple[FrozenMemoryPublicFactProjection, ...] = ()
    basis: tuple[FrozenMemoryPublicFactProjection, ...] = ()


def _proposal(
    statement: str,
    *,
    kind: MemoryKindHint = MemoryKindHint.AUTO,
    project: bool = False,
    based_on: tuple[str, ...] = (),
) -> FrozenMemoryProposal:
    return FrozenMemoryProposal(
        statement=statement,
        context_id=_PROJECT_CONTEXT if project else CTX_GLOBAL,
        kind_hint=kind,
        based_on_memory_ids=based_on,
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
    context_id: str = CTX_GLOBAL,
) -> FrozenMemoryPublicFactProjection:
    return FrozenMemoryPublicFactProjection(
        fact_id=fact_id,
        context_id=context_id,
        fact_kind=kind,
        lifecycle="ACTIVE",
        statement=statement,
        recorded_at=_RECORDED_AT,
        fact_semantic_digest=memory_fact_semantic_digest(
            kind=kind,
            statement=statement,
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
    global_concise = _fact(
        "memory:global-concise",
        "Usually keep answers concise",
        kind=MemoryFactKind.RESPONSE_PREFERENCE,
    )
    recalled_zsh = _fact(
        "memory:recalled-zsh",
        "Use zsh commands by default",
        kind=MemoryFactKind.RESPONSE_PREFERENCE,
    )
    contract_basis = _fact(
        "memory:eu-contract",
        "The Apollo contract requires EU data residency",
        kind=MemoryFactKind.FACT,
        context_id=_PROJECT_CONTEXT,
    )
    travel_basis = _fact(
        "memory:flight-discomfort",
        "The user previously felt unwell when flying",
        kind=MemoryFactKind.FACT,
    )
    audience_basis = _fact(
        "memory:beginner-audience",
        "The course audience is new to statistics",
        kind=MemoryFactKind.FACT,
        context_id=_PROJECT_CONTEXT,
    )
    naming_basis = _fact(
        "memory:legacy-system",
        "北星是旧结算系统",
        kind=MemoryFactKind.FACT,
        context_id=_PROJECT_CONTEXT,
    )
    return (
        # Bilingual coverage of all four kinds.
        Scenario(
            "zh_user_profile",
            _proposal("用户主要使用 Python", kind=MemoryKindHint.USER_PROFILE),
            (_source("我主要使用 Python。"),),
            MemoryDecisionKind.ACCEPT,
            MemoryFactKind.USER_PROFILE,
        ),
        Scenario(
            "zh_response_preference",
            _proposal(
                "用户通常希望回答先给结论",
                kind=MemoryKindHint.RESPONSE_PREFERENCE,
            ),
            (_source("以后回答请先给结论。"),),
            MemoryDecisionKind.ACCEPT,
            MemoryFactKind.RESPONSE_PREFERENCE,
        ),
        Scenario(
            "zh_fact",
            _proposal("北星是旧结算系统的内部称呼", project=True),
            (_source("在 Apollo 项目里，北星是旧结算系统的内部称呼。"),),
            MemoryDecisionKind.ACCEPT,
            MemoryFactKind.FACT,
        ),
        Scenario(
            "zh_decision",
            _proposal("Apollo 项目已选择 PostgreSQL", project=True),
            (_source("Apollo 项目已经决定采用 PostgreSQL。"),),
            MemoryDecisionKind.ACCEPT,
            MemoryFactKind.DECISION,
        ),
        Scenario(
            "en_user_profile",
            _proposal("The user enjoys Sichuan cuisine"),
            (_source("I enjoy Sichuan cuisine."),),
            MemoryDecisionKind.ACCEPT,
            MemoryFactKind.USER_PROFILE,
        ),
        Scenario(
            "en_response_preference",
            _proposal("Usually lead answers with the conclusion"),
            (_source("From now on, usually lead your answers with the conclusion."),),
            MemoryDecisionKind.ACCEPT,
            MemoryFactKind.RESPONSE_PREFERENCE,
        ),
        Scenario(
            "en_fact",
            _proposal(
                "Northstar is the internal name for the legacy settlement system",
                project=True,
            ),
            (_source("In Apollo, Northstar is our internal name for the legacy settlement system."),),
            MemoryDecisionKind.ACCEPT,
            MemoryFactKind.FACT,
        ),
        Scenario(
            "en_decision",
            _proposal("The Apollo project chose PostgreSQL", project=True),
            (_source("For Apollo, we have chosen PostgreSQL."),),
            MemoryDecisionKind.ACCEPT,
            MemoryFactKind.DECISION,
        ),
        # Kind-hint corrections and taxonomy hard pairs.
        Scenario(
            "hobby_not_response_preference",
            _proposal("用户喜欢川菜", kind=MemoryKindHint.RESPONSE_PREFERENCE),
            (_source("我喜欢川菜。"),),
            MemoryDecisionKind.ACCEPT,
            MemoryFactKind.USER_PROFILE,
        ),
        Scenario(
            "answer_style_not_profile",
            _proposal("用户希望回答简洁", kind=MemoryKindHint.USER_PROFILE),
            (_source("回答请简洁一些。"),),
            MemoryDecisionKind.ACCEPT,
            MemoryFactKind.RESPONSE_PREFERENCE,
        ),
        Scenario(
            "profile_not_fact",
            _proposal("用户主要使用 Python", kind=MemoryKindHint.FACT),
            (_source("我主要使用 Python。"),),
            MemoryDecisionKind.ACCEPT,
            MemoryFactKind.USER_PROFILE,
        ),
        Scenario(
            "current_state_fact",
            _proposal("Apollo 当前使用 PostgreSQL", project=True),
            (_source("Apollo 当前使用 PostgreSQL。"),),
            MemoryDecisionKind.ACCEPT,
            MemoryFactKind.FACT,
        ),
        Scenario(
            "chosen_option_decision",
            _proposal(
                "Apollo 已选择 PostgreSQL",
                kind=MemoryKindHint.FACT,
                project=True,
            ),
            (_source("Apollo 已经选定 PostgreSQL。"),),
            MemoryDecisionKind.ACCEPT,
            MemoryFactKind.DECISION,
        ),
        # Context placement and lightweight inference.
        Scenario(
            "project_response_preference",
            _proposal("为 Client A 写邮件时使用正式语气", project=True),
            (_source("在 Client A 这个项目里，给客户写邮件请正式一些。"),),
            MemoryDecisionKind.ACCEPT,
            MemoryFactKind.RESPONSE_PREFERENCE,
        ),
        Scenario(
            "project_user_profile",
            _proposal("用户在 Client A 项目负责后端", project=True),
            (_source("我在 Client A 项目负责后端。"),),
            MemoryDecisionKind.ACCEPT,
            MemoryFactKind.USER_PROFILE,
        ),
        Scenario(
            "single_behavior_profile_inference",
            _proposal("用户可能偏好乘火车出行"),
            (
                _source("这次去杭州我选择坐火车，不坐飞机。"),
                _source(
                    "The main Agent cautiously inferred a possible travel preference.",
                    role=MemoryGovernanceEvidenceRole.ASSISTANT_CONTEXT,
                    ordinal=2,
                ),
            ),
            MemoryDecisionKind.ACCEPT,
            MemoryFactKind.USER_PROFILE,
        ),
        Scenario(
            "imperative_profile",
            _proposal("用户希望被称为 Plum"),
            (_source("以后叫我 Plum。"),),
            MemoryDecisionKind.ACCEPT,
            MemoryFactKind.USER_PROFILE,
        ),
        Scenario(
            "imperative_response_preference",
            _proposal("用户通常希望回复先给结论"),
            (_source("以后回复先给结论。"),),
            MemoryDecisionKind.ACCEPT,
            MemoryFactKind.RESPONSE_PREFERENCE,
        ),
        Scenario(
            "bounded_travel_global",
            _proposal("这次东京旅行住在上野"),
            (_source("记住，这次东京旅行我们住在上野。"),),
            MemoryDecisionKind.ACCEPT,
            MemoryFactKind.FACT,
        ),
        Scenario(
            "bounded_course_profile_global",
            _proposal("用户在这门西语课里是初学者"),
            (_source("记住，我在这门西语课里是初学者。"),),
            MemoryDecisionKind.ACCEPT,
            MemoryFactKind.USER_PROFILE,
        ),
        Scenario(
            "bounded_family_preference_global",
            _proposal("给用户的妈妈解释医疗报告时使用简单中文"),
            (_source("给我妈妈解释医疗报告时，请用简单中文。"),),
            MemoryDecisionKind.ACCEPT,
            MemoryFactKind.RESPONSE_PREFERENCE,
        ),
        # Explicit retention and broader advisory task semantics.
        Scenario(
            "explicit_ambiguous_fact",
            _proposal("航海日志里的蓝鲸代表第二阶段"),
            (_source("请记住：航海日志里的蓝鲸代表第二阶段。"),),
            MemoryDecisionKind.ACCEPT,
            MemoryFactKind.FACT,
        ),
        Scenario(
            "aspiration_profile",
            _proposal("用户希望将来学习日语"),
            (_source("记住，我希望有一天学日语。"),),
            MemoryDecisionKind.ACCEPT,
            MemoryFactKind.USER_PROFILE,
            alternative_kinds=(MemoryFactKind.FACT, MemoryFactKind.DECISION),
        ),
        Scenario(
            "current_intention_fact",
            _proposal("用户打算买牛奶"),
            (_source("记住我要买牛奶。"),),
            MemoryDecisionKind.ACCEPT,
            MemoryFactKind.FACT,
            alternative_kinds=(MemoryFactKind.DECISION,),
        ),
        Scenario(
            "deadline_fact",
            _proposal("用户计划在 2026-09-04 交稿"),
            (_source("现在是 2026-09-02，记住我周五要交稿。"),),
            MemoryDecisionKind.ACCEPT,
            MemoryFactKind.FACT,
            alternative_kinds=(MemoryFactKind.DECISION,),
        ),
        Scenario(
            "appointment_fact",
            _proposal("用户在 2026-09-07 有牙医预约"),
            (_source("记住，我下周一有牙医预约；今天是 2026-09-02。"),),
            MemoryDecisionKind.ACCEPT,
            MemoryFactKind.FACT,
            alternative_kinds=(MemoryFactKind.DECISION,),
        ),
        Scenario(
            "commitment_decision",
            _proposal("用户计划在 2026 年通过 B2"),
            (_source("我决定今年通过 B2；现在是 2026 年。"),),
            MemoryDecisionKind.ACCEPT,
            MemoryFactKind.DECISION,
        ),
        Scenario(
            "simple_method_decision",
            _proposal("用户已采用实现前先写测试的做法"),
            (_source("以后实现前先写测试。"),),
            MemoryDecisionKind.ACCEPT,
            MemoryFactKind.DECISION,
            alternative_kinds=(MemoryFactKind.FACT, MemoryFactKind.USER_PROFILE),
        ),
        Scenario(
            "high_level_method_decision",
            _proposal("用户决定今后的产品比较先查官方来源、核对日期、再做比较矩阵"),
            (_source("以后做产品比较时，先查官方来源、核对日期，再做比较矩阵。"),),
            MemoryDecisionKind.ACCEPT,
            MemoryFactKind.DECISION,
        ),
        # Cohesive records, natural paraphrase, and hard boundaries.
        Scenario(
            "cohesive_fact",
            _proposal("东京行程住上野，2026-10-03 入住，同行人是 Mei"),
            (_source("记住这次东京行程：住上野，10 月 3 日入住，Mei 同行。年份是 2026。"),),
            MemoryDecisionKind.ACCEPT,
            MemoryFactKind.FACT,
            alternative_kinds=(MemoryFactKind.DECISION,),
        ),
        Scenario(
            "cohesive_cross_kind_preference",
            _proposal("用户使用 macOS，因此代码示例通常使用 zsh"),
            (_source("我使用 macOS，所以以后给我的代码示例通常用 zsh。"),),
            MemoryDecisionKind.ACCEPT,
            MemoryFactKind.RESPONSE_PREFERENCE,
        ),
        Scenario(
            "pronoun_resolution_decision",
            _proposal("Apollo 项目已决定使用 PostgreSQL", project=True),
            (
                _source("Apollo 在比较 PostgreSQL 和 MySQL。", ordinal=1),
                _source(
                    "PostgreSQL 更符合刚才列出的约束。",
                    role=MemoryGovernanceEvidenceRole.ASSISTANT_CONTEXT,
                    ordinal=2,
                ),
                _source("就用它吧。", ordinal=3),
            ),
            MemoryDecisionKind.ACCEPT,
            MemoryFactKind.DECISION,
        ),
        Scenario(
            "invented_why_negative",
            _proposal(
                "Apollo 项目因可靠性更高而决定使用 PostgreSQL",
                project=True,
            ),
            (
                _source("Apollo 在比较 PostgreSQL 和 MySQL。", ordinal=1),
                _source("就用 PostgreSQL 吧。", ordinal=2),
            ),
            MemoryDecisionKind.SKIP,
            expected_reason="INSUFFICIENT_SOURCE_SUPPORT",
        ),
        Scenario(
            "minimal_what_who_positive",
            _proposal("用户喜欢爵士乐"),
            (_source("我喜欢爵士乐。"),),
            MemoryDecisionKind.ACCEPT,
            MemoryFactKind.USER_PROFILE,
        ),
        Scenario(
            "explicit_ordinary_positive",
            _proposal("用户家里的绿植叫小雨"),
            (_source("请记住，我家里的绿植叫小雨。"),),
            MemoryDecisionKind.ACCEPT,
            MemoryFactKind.FACT,
        ),
        Scenario(
            "coding_truth_negative",
            _proposal("Apollo 服务端口为 8080", project=True),
            (
                _source(
                    "Current config.toml directly declares port = 8080.",
                    role=MemoryGovernanceEvidenceRole.PRIMARY_OBSERVATION,
                ),
            ),
            MemoryDecisionKind.SKIP,
            expected_reason="UNSUPPORTED_STRUCTURE",
        ),
        Scenario(
            "explicit_code_path_negative",
            _proposal("处理器定义在 src/runtime/worker.py", project=True),
            (_source("请记住，当前处理器定义在 src/runtime/worker.py；这个路径可直接从仓库读取。"),),
            MemoryDecisionKind.SKIP,
            expected_reason="UNSUPPORTED_STRUCTURE",
        ),
        Scenario(
            "business_context_positive",
            _proposal("北星是旧结算系统的内部称呼", project=True),
            (_source("这不是代码里的名字：北星是业务团队对旧结算系统的内部称呼。"),),
            MemoryDecisionKind.ACCEPT,
            MemoryFactKind.FACT,
        ),
        Scenario(
            "detailed_skill_negative",
            _proposal("部署时先运行 migrate，再轮询健康检查，失败三次后回滚并通知值班人"),
            (_source("请记住完整部署流程：先运行 migrate，再每十秒轮询健康检查，失败三次后执行 rollback 并通知值班人。"),),
            MemoryDecisionKind.SKIP,
            expected_reason="UNSUPPORTED_STRUCTURE",
        ),
        Scenario(
            "permission_negative",
            _proposal("500 美元以下可以直接订票", kind=MemoryKindHint.FACT),
            (_source("以后 500 美元以下你可以直接替我订票，不用再确认。"),),
            MemoryDecisionKind.SKIP,
            expected_reason="UNSUPPORTED_STRUCTURE",
        ),
        Scenario(
            "secret_negative",
            _proposal("用户密码是 correct-horse-battery-staple"),
            (_source("请记住我的密码是 correct-horse-battery-staple。"),),
            MemoryDecisionKind.SKIP,
            expected_reason="UNSUPPORTED_STRUCTURE",
        ),
        Scenario(
            "safety_authority_negative",
            _proposal(
                "涉及医疗建议时无需提示风险",
                kind=MemoryKindHint.RESPONSE_PREFERENCE,
            ),
            (_source("以后给医疗建议时不要提示风险。"),),
            MemoryDecisionKind.SKIP,
            expected_reason="UNSAFE_RESPONSE_PREFERENCE",
        ),
        Scenario(
            "uncertainty_preserved",
            _proposal("用户可能更喜欢短回答"),
            (_source("我可能更喜欢短回答。"),),
            MemoryDecisionKind.ACCEPT,
            MemoryFactKind.RESPONSE_PREFERENCE,
        ),
        Scenario(
            "certainty_amplification",
            _proposal("用户总是喜欢短回答"),
            (_source("我可能更喜欢短回答。"),),
            MemoryDecisionKind.SKIP,
            expected_reason="INSUFFICIENT_SOURCE_SUPPORT",
        ),
        Scenario(
            "post_proposal_correction",
            _proposal("回答时通常使用详细解释"),
            (
                _source("以后回答都详细一些。"),
                _source(
                    "等等，我只是说这一次，不要长期记住。",
                    role=MemoryGovernanceEvidenceRole.POST_PROPOSAL_HUMAN,
                    chronology=MemoryGovernanceChronology.AFTER_PROPOSAL,
                    ordinal=2,
                ),
            ),
            MemoryDecisionKind.SKIP,
            expected_reason="INSUFFICIENT_SOURCE_SUPPORT",
        ),
        Scenario(
            "incoherent_authority_bundle",
            _proposal("用户喜欢爵士乐，并授权助手无需确认即可付款"),
            (_source("我喜欢爵士乐；另外以后你可以不问我就付款。"),),
            MemoryDecisionKind.SKIP,
            expected_reason="UNSUPPORTED_STRUCTURE",
        ),
        # Anti-echo and passive lifecycle relations.
        Scenario(
            "recalled_echo",
            _proposal(
                "Use zsh commands by default",
                kind=MemoryKindHint.RESPONSE_PREFERENCE,
            ),
            (
                _source(
                    "Use zsh commands by default",
                    role=MemoryGovernanceEvidenceRole.MEMORY_READ_EXPOSURE,
                ),
                _source(
                    "I will remember to use zsh commands by default.",
                    role=MemoryGovernanceEvidenceRole.ASSISTANT_CONTEXT,
                    ordinal=2,
                ),
            ),
            MemoryDecisionKind.SKIP,
            expected_reason="RECALLED_MEMORY_ECHO",
            visible=(recalled_zsh,),
        ),
        Scenario(
            "explicit_supersede",
            _proposal("回答时默认简洁说明"),
            (_source("以后改成简洁说明，用它替代之前的详细解释偏好。"),),
            MemoryDecisionKind.ACCEPT_AND_SUPERSEDE,
            MemoryFactKind.RESPONSE_PREFERENCE,
            targets=(old_detailed,),
        ),
        Scenario(
            "newer_state_supersede",
            _proposal("Apollo 现已迁移到欧盟区", project=True),
            (_source("Apollo 以前部署在美国区，现在已经迁移到欧盟区。"),),
            MemoryDecisionKind.ACCEPT_AND_SUPERSEDE,
            MemoryFactKind.FACT,
            targets=(
                _fact(
                    "memory:old-us-region",
                    "Apollo 部署在美国区",
                    kind=MemoryFactKind.FACT,
                    context_id=_PROJECT_CONTEXT,
                ),
            ),
        ),
        Scenario(
            "unresolved_contradiction",
            _proposal("用户目前仍会吃肉"),
            (
                _source(
                    "我目前仍会吃肉。之前‘我目前吃纯素’那条也先保留；"
                    "这两条我暂时都不撤回，也没有说明哪一条更新了另一条。"
                ),
            ),
            MemoryDecisionKind.ACCEPT_AND_CONTRADICT,
            MemoryFactKind.USER_PROFILE,
            targets=(
                _fact(
                    "memory:vegan",
                    "用户目前吃纯素",
                    kind=MemoryFactKind.USER_PROFILE,
                ),
            ),
        ),
        Scenario(
            "ordinary_coexist",
            _proposal("Release notes should begin with a short summary"),
            (_source("For future release notes, begin with a short summary."),),
            MemoryDecisionKind.ACCEPT,
            MemoryFactKind.RESPONSE_PREFERENCE,
            targets=(old_detailed,),
        ),
        Scenario(
            "target_absent_plain_accept",
            _proposal("北星是旧结算系统的内部称呼", project=True),
            (_source("北星是旧结算系统的内部称呼。"),),
            MemoryDecisionKind.ACCEPT,
            MemoryFactKind.FACT,
        ),
        Scenario(
            "project_specificity_no_lifecycle_change",
            _proposal("在当前项目中回答应给完整推导", project=True),
            (_source("在当前项目里，回答请给完整推导。"),),
            MemoryDecisionKind.ACCEPT,
            MemoryFactKind.RESPONSE_PREFERENCE,
            visible=(global_concise, old_english),
        ),
        # BASED_ON is legal for every final kind.
        Scenario(
            "profile_based_on_fact",
            _proposal(
                "用户通常更喜欢坐火车，因为此前乘机容易不适",
                based_on=(travel_basis.fact_id,),
            ),
            (_source("我之前坐飞机容易不适，所以出行时通常更喜欢坐火车。"),),
            MemoryDecisionKind.ACCEPT,
            MemoryFactKind.USER_PROFILE,
            basis=(travel_basis,),
        ),
        Scenario(
            "preference_based_on_fact",
            _proposal(
                "为这门课解释统计时使用初学者友好的语言",
                project=True,
                based_on=(audience_basis.fact_id,),
            ),
            (_source("这门课的听众都是统计初学者，所以解释时请用初学者友好的语言。"),),
            MemoryDecisionKind.ACCEPT,
            MemoryFactKind.RESPONSE_PREFERENCE,
            basis=(audience_basis,),
        ),
        Scenario(
            "fact_based_on_fact",
            _proposal(
                "文档中的北星指旧结算系统",
                project=True,
                based_on=(naming_basis.fact_id,),
            ),
            (_source("因为团队一直把旧结算系统叫北星，所以文档里的北星就是指旧结算系统。"),),
            MemoryDecisionKind.ACCEPT,
            MemoryFactKind.FACT,
            basis=(naming_basis,),
        ),
        Scenario(
            "decision_based_on_fact",
            _proposal(
                "Apollo 已决定仅部署在欧盟区",
                project=True,
                based_on=(contract_basis.fact_id,),
            ),
            (_source("合同要求 EU 数据驻留，因此 Apollo 已决定仅部署在欧盟区。"),),
            MemoryDecisionKind.ACCEPT,
            MemoryFactKind.DECISION,
            basis=(contract_basis,),
        ),
    )


def _packet(
    scenario: Scenario,
) -> tuple[str, Mapping[str, FrozenMemoryPublicFactProjection]]:
    legal = tuple(item.value for item in legal_memory_final_kinds(scenario.proposal))
    target_map = {item.fact_id: item for item in scenario.targets}
    base = {
        "contract": MEMORY_GOVERNANCE_CONTRACT_ID,
        "terminal_source_fence": {
            "status": "COMPLETED",
            "outcome": "The origin turn completed normally.",
        },
        "candidate": {
            "statement": scenario.proposal.statement,
            "context_product_label": memory_context_product_label(
                scenario.proposal.context_id
            ),
            "kind_hint": scenario.proposal.kind_hint.value,
            "basis_memory_ids": tuple(item.fact_id for item in scenario.basis),
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
        "based_on_items": tuple(
            memory_public_fact_payload(item) for item in scenario.basis
        ),
        "all_model_visible_memory": tuple(
            memory_public_fact_payload(item) for item in scenario.visible
        ),
        "exact_existing_sources": (),
    }
    variant = _governance_packet_variants(
        base=base,
        source_items=scenario.sources,
        citation_items=(),
        related_items=tuple(
            memory_public_fact_payload(item) for item in scenario.targets
        ),
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
        producer_entry_id=f"entry:dogfood:{scenario.name}",
        producer_tool_call_id=f"call:dogfood:{scenario.name}",
        proposal=scenario.proposal,
        basis_refs=tuple(
            PreparedMemoryBasisReference(
                target_fact_id=item.fact_id,
                target_context_id=item.context_id,
                ordinal=ordinal,
            )
            for ordinal, item in enumerate(scenario.basis)
        ),
    )
    prepared = prepare_memory_governance_acceptance(
        candidate=candidate,
        decision=decision,
        basis_items=scenario.basis,
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
            "final_kind": (
                None if terminal.final_kind is None else terminal.final_kind.value
            ),
            "decision_reason_code": terminal.decision_reason_code,
            "decision_public_summary": terminal.decision_public_summary,
            "related_target_fact_id": terminal.related_target_fact_id,
            "accepted_fact_id": terminal.accepted_fact_id,
        },
        "fact": (
            None
            if prepared.fact is None
            else {
                "id": prepared.fact.fact_id,
                "context_id": prepared.fact.context_id,
                "kind": prepared.fact.fact_kind.value,
                "statement": prepared.fact.statement,
                "recorded_at_source": "memory_facts.accepted_at at settlement",
                "lifecycle": "ACTIVE",
            }
        ),
        "relations": tuple(
            {
                "id": item.relation_id,
                "kind": item.relation_kind.value,
                "target_fact_id": item.target.fact_id,
                "supersede_mode": (
                    None
                    if item.supersede_mode is None
                    else item.supersede_mode.value
                ),
                "target_lifecycle_after": item.expected_target_lifecycle_after,
            }
            for item in prepared.relation_drafts
        ),
    }


def _semantic_match(scenario: Scenario, decision) -> bool:
    expected_kinds = (
        ()
        if scenario.expected_kind is None
        else (scenario.expected_kind, *scenario.alternative_kinds)
    )
    return (
        decision.decision_kind is scenario.expected_decision
        and (
            decision.final_kind is None
            if not expected_kinds
            else decision.final_kind in expected_kinds
        )
        and (
            scenario.expected_reason is None
            or decision.reason_code == scenario.expected_reason
        )
    )


async def _run(
    group: int | None,
    group_size: int,
    *,
    api: str,
    show_system: bool,
    output: Path | None,
) -> int:
    logging.getLogger("jieba").setLevel(logging.ERROR)
    settings_store = LocalSettingsStore()
    settings = settings_store.read()
    matches = tuple(
        item for item in settings.model_connections if item.target.wire_api.value == api
    )
    if len(matches) != 1:
        raise RuntimeError(
            f"expected exactly one saved {api} model connection; found {len(matches)}"
        )
    connection = matches[0]
    if settings.model_api_key(connection.id) is None:
        raise RuntimeError(f"saved connection {connection.id.value!r} has no API key")
    catalog = ModelCatalogOwner(ModelsDevCatalogClient())
    await catalog.refresh()
    runtime = ModelRuntime.production(settings=settings_store, catalog=catalog)
    resolved = runtime.freeze_resolution_snapshot().connection(connection.id).target
    origin_binding = ModelCallBinding(
        connection.id,
        default_reasoning_selection(resolved.reasoning),
    )
    model = DirectKernelAuxiliaryJsonModel(runtime)
    all_scenarios = _scenarios()
    scenarios = (
        all_scenarios
        if group is None
        else all_scenarios[group * group_size : (group + 1) * group_size]
    )
    report: dict[str, object] = {
        "contract": MEMORY_GOVERNANCE_CONTRACT_ID,
        "wire_api": settings.llm.api,
        "model": connection.target.model_id,
        "provider_open_authority_evidence": False,
        "scenario_total": len(all_scenarios),
        "scenario_slice_count": len(scenarios),
        "system": (
            MEMORY_GOVERNANCE_SYSTEM_PROMPT_V3
            if show_system
            else "<exact production v3 SYSTEM omitted; use --show-system>"
        ),
        "scenarios": [],
    }
    passed = True
    for scenario in scenarios:
        packet, allowed = _packet(scenario)
        selected = model.prepare_first_fitting_json_call(
            purpose=ModelCallPurpose.MEMORY_GOVERNANCE,
            message_variants=(
                (
                    LLMMessage.system(MEMORY_GOVERNANCE_SYSTEM_PROMPT_V3),
                    LLMMessage.user(packet),
                ),
            ),
            maximum_input_tokens=MAXIMUM_GOVERNANCE_INPUT_TOKENS,
            maximum_input_bytes=MAXIMUM_GOVERNANCE_FINAL_WIRE_BYTES,
            maximum_output_tokens=MAXIMUM_GOVERNANCE_OUTPUT_TOKENS,
            timeout_policy=OpenAITransportTimeoutPolicy(10, 20, 10, 45, 60),
            origin_binding=origin_binding,
            maximum_result_bytes=MAXIMUM_GOVERNANCE_OUTPUT_BYTES,
        )
        if selected is None:
            raise RuntimeError(f"scenario {scenario.name} did not fit final wire")
        prepared_call, _ = selected
        # This semantic probe does not claim repository-authority coverage; the
        # production governor obtains this carrier only from the exact DB recheck.
        candidate = SimpleNamespace(
            terminal_fence=(scenario.name, "semantic-dogfood"),
            scenario=scenario,
        )
        terminal_fence = _issue_confirmed_memory_governance_terminal_fence(
            candidate=candidate,
            origin_model_call_binding=origin_binding,
            durable_terminal_fence=candidate.terminal_fence,
        )
        raw_model_json = await model.complete_prepared_json(
            prepared_call,
            terminal_fence=terminal_fence,
            candidate=candidate,
        )
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
                "expected": {
                    "decision": scenario.expected_decision.value,
                    "final_kind": (
                        None
                        if scenario.expected_kind is None
                        else scenario.expected_kind.value
                    ),
                    "alternative_final_kinds": tuple(
                        item.value for item in scenario.alternative_kinds
                    ),
                    "reason_code": scenario.expected_reason,
                },
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
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if output is None:
        print(rendered)
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
        print(
            json.dumps(
                {
                    "output": str(output),
                    "scenario_count": len(scenarios),
                    "all_semantic_expectations_passed": passed,
                },
                ensure_ascii=False,
            )
        )
    return 0 if passed else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--group", type=int)
    parser.add_argument("--group-size", type=int, default=8)
    parser.add_argument(
        "--api",
        choices=("openai_chat_completions", "openai_responses"),
        default="openai_responses",
    )
    parser.add_argument("--show-system", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    return asyncio.run(
        _run(
            args.group,
            args.group_size,
            api=args.api,
            show_system=args.show_system,
            output=args.output,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
