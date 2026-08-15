"""Pure, sealed Cheap Hint Reflection contracts.

The matcher and handoff in this module intentionally have no repository,
provider, transport, or task authority.  They only decide whether one
successfully completed ROOT turn is worth offering to the Host-local advisory
memory owner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import re
import unicodedata
from typing import Sequence

from pulsara_agent.conversation_kernel.memory.contracts import (
    PreparedMemoryCandidateAcceptance,
    canonical_json_bytes,
)
from pulsara_agent.model_input.contracts import (
    CanonicalInputOriginKind,
    CanonicalModelInputSnapshot,
    FrozenProviderInputItemKind,
    ModelInputScopeKind,
)
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.run_permission import (
    FrozenRunPermissionSnapshot,
    RunPermissionOverlay,
)


MAXIMUM_HINT_ENTRIES = 8
MAXIMUM_HINTS = 16
MAXIMUM_HINT_EXCERPT_CODEPOINTS = 2_048
MAXIMUM_REFLECTION_ENTRY_BYTES = 16 * 1024
MAXIMUM_REFLECTION_ADJACENT_ASSISTANT_BYTES = 8 * 1024


# This is the sealed high-value subset adapted from the hard-cut-before
# reflection engine at 5b7ad9f7.  Broad negative/imperative words are excluded;
# order is not semantic and matching is longest-first.
_SEALED_SIGNALS = (
    "从现在开始",
    "我特别讨厌",
    "我真的不喜欢",
    "我真的讨厌",
    "我极其讨厌",
    "不要忘记",
    "从今以后",
    "我比较喜欢",
    "我喜欢",
    "我不是这个意思",
    "make sure you",
    "just so you know",
    "going forward",
    "from now on",
    "from then on",
    "in the future",
    "make sure to",
    "stop doing",
    "stop saying",
    "don't forget",
    "keep in mind",
    "for the record",
    "like i said",
    "what i meant was",
    "i really dislike",
    "i don't like",
    "i never",
    "我更喜欢",
    "我不喜欢",
    "我通常",
    "我常常",
    "我一般",
    "我习惯",
    "我总",
    "我讨厌",
    "以后都",
    "不要再",
    "不是这个意思",
    "我的意思是",
    "千万不要",
    "千万别",
    "别忘了",
    "记下来",
    "你要记住",
    "你得记住",
    "you always",
    "i told you",
    "i usually",
    "i always",
    "i prefer",
    "i'd rather",
    "i hate",
    "i dislike",
    "i like",
    "my favorite",
    "next time",
    "take note",
    "that's not what i meant",
    "别再",
    "我更偏好",
    "我更偏爱",
    "我更爱",
    "我们决定",
    "已经决定",
    "决定采用",
    "remember",
)

_ENGLISH_WRITE_OPT_OUT = re.compile(
    r"(?:^|[.!?;:]\s*|(?:and|but)\s+)"
    r"(?:please\s+)?(?:don't|do\s+not)\s+(?:"
    r"(?:remember|save|store|retain)\s+(?:"
    r"(?:this|that)\s+(?:message|entry|detail|content)|"
    r"what\s+i\s+just\s+said|(?:this|that|it)(?=$|[.!?;,])"
    r")|"
    r"(?:add|put|write)\s+(?:this|that|it|(?:this|that)\s+"
    r"(?:message|entry|detail|content))\s+(?:to|in|into)\s+"
    r"(?:your\s+)?(?:long-term\s+)?memor(?:y|ies)"
    r")"
)
_CHINESE_WRITE_OPT_OUT = re.compile(
    r"(?:^|[。！？；]\s*)(?:请)?(?:不要|别|不用)(?:"
    r"(?:记住|保存|记录)(?:这条消息|这个内容|这件事|我刚才说的|"
    r"本条消息|本次内容|(?:这条|这个)(?=$|[。！？；，]))|"
    r"(?:把|将)?(?:这条消息|这个内容|这件事|我刚才说的|本次内容)"
    r"(?:写入|加入|存入)(?:长期)?记忆"
    r")"
)
_ENGLISH_TURN_MEMORY_USE_OPT_OUT = re.compile(
    r"(?:"
    r"(?:for\s+this\s+(?:turn|answer)|this\s+time)[,:]?\s+"
    r"(?:please\s+)?(?:don't|do\s+not)\s+"
    r"(?:use|consult|read|access|reference|refer\s+to|retrieve\s+from)\s+"
    r"(?:my\s+|your\s+|the\s+|saved\s+|stored\s+|previous\s+|long-term\s+)?"
    r"memor(?:y|ies)(?![a-z0-9_]|\s+(?:mapping|map|allocation|management|"
    r"address|buffer|layout)\b)|"
    r"(?:^|[.!?;:]\s*)(?:please\s+)?(?:don't|do\s+not)\s+"
    r"(?:use|consult|read|access|reference|refer\s+to|retrieve\s+from)\s+"
    r"(?:saved|stored|previous|long-term|your)\s+memor(?:y|ies)"
    r"(?![a-z0-9_]|\s+(?:mapping|map|allocation|management|address|buffer|"
    r"layout)\b)"
    r"(?:\s+for\s+this\s+(?:turn|answer))?|"
    r"(?:^|[.!?;:]\s*)(?:please\s+)?(?:don't|do\s+not)\s+use\s+"
    r"memor(?:y|ies)\s+for\s+this\s+(?:turn|answer)|"
    r"(?:^|[.!?;:]\s*)answer(?:\s+this)?\s+(?:question\s+)?without\s+"
    r"(?:using|consulting|reading|accessing|referencing)\s+"
    r"(?:saved\s+|stored\s+|previous\s+|long-term\s+)?memor(?:y|ies)"
    r"(?![a-z0-9_]|\s+(?:mapping|map|allocation|management|address|buffer|"
    r"layout)\b)"
    r")"
)
_CHINESE_TURN_MEMORY_USE_OPT_OUT = re.compile(
    r"(?:"
    r"(?:本轮|这轮|这次|本次|当前回答)[，,:：]?\s*(?:请)?"
    r"(?:不|不要|别|不用|无需|请勿)(?:使用|读取|查询|参考|调用|检索)"
    r"(?:已有|历史|长期|已保存的?)?(?:记忆|偏好|信息)|"
    r"(?:^|[。！？；]\s*)(?:请)?(?:不要用|别用|不用|无需使用|请勿使用)"
    r"(?:已有|历史|长期|已保存的?)?记忆(?:来)?回答"
    r")"
)


def normalize_reflection_text(value: str) -> str:
    """Return the only normalized coordinate space used by these matchers."""

    return " ".join(unicodedata.normalize("NFC", value).casefold().split())


@dataclass(frozen=True, slots=True)
class MemoryWriteOptOut:
    contract_id: str = "pulsara.memory-write-opt-out.v2"

    def excludes(self, text: str) -> bool:
        normalized = normalize_reflection_text(text)
        # Positive reminders are explicit counterexamples.  They must remain
        # eligible even though the historical signal set also contains broad
        # negative words such as "don't" and "不要".
        if any(
            marker in normalized
            for marker in ("don't forget", "do not forget", "不要忘记", "别忘了")
        ):
            return False
        return bool(
            _ENGLISH_WRITE_OPT_OUT.search(normalized)
            or _CHINESE_WRITE_OPT_OUT.search(normalized)
        )


@dataclass(frozen=True, slots=True)
class TurnMemoryUseOptOut:
    contract_id: str = "pulsara.turn-memory-use-opt-out.v1"

    def excludes(self, text: str) -> bool:
        normalized = normalize_reflection_text(text)
        return bool(
            _ENGLISH_TURN_MEMORY_USE_OPT_OUT.search(normalized)
            or _CHINESE_TURN_MEMORY_USE_OPT_OUT.search(normalized)
        )


@dataclass(frozen=True, slots=True)
class CheapMemoryHint:
    signal_code: str
    normalized_excerpt: str = field(repr=False)
    excerpt_digest: str

    def __post_init__(self) -> None:
        if not self.signal_code.startswith("hint:"):
            raise ValueError("cheap memory hint code is invalid")
        if not 1 <= len(self.normalized_excerpt) <= MAXIMUM_HINT_EXCERPT_CODEPOINTS:
            raise ValueError("cheap memory hint excerpt exceeds its bound")
        expected = _digest(
            "pulsara:cheap-memory-hint-excerpt:v1", self.normalized_excerpt
        )
        if self.excerpt_digest != expected:
            raise ValueError("cheap memory hint excerpt digest mismatch")


@dataclass(frozen=True, slots=True)
class CheapMemoryHintSetV1:
    contract_id: str = "pulsara.cheap-memory-hint-set.v1"
    signals: tuple[str, ...] = _SEALED_SIGNALS

    def match(self, text: str, *, maximum_hints: int = MAXIMUM_HINTS) -> tuple[CheapMemoryHint, ...]:
        if not 1 <= maximum_hints <= MAXIMUM_HINTS:
            raise ValueError("cheap memory hint maximum is invalid")
        normalized = normalize_reflection_text(text)
        matches: list[tuple[int, int, str]] = []
        for signal in self.signals:
            needle = normalize_reflection_text(signal)
            start = 0
            while True:
                position = normalized.find(needle, start)
                if position < 0:
                    break
                matches.append((position, position + len(needle), needle))
                start = position + max(1, len(needle))
        # Longest overlap wins.  The selected set is then restored to canonical
        # text order; no normalized offset becomes durable identity.
        matches.sort(key=lambda value: (-(value[1] - value[0]), value[0], value[2]))
        selected: list[tuple[int, int, str]] = []
        for candidate in matches:
            if any(
                candidate[0] < item[1] and item[0] < candidate[1]
                for item in selected
            ):
                continue
            selected.append(candidate)
            if len(selected) >= maximum_hints:
                break
        selected.sort(key=lambda value: (value[0], value[1], value[2]))
        result: list[CheapMemoryHint] = []
        for start, end, signal in selected:
            excerpt_start = max(0, start - MAXIMUM_HINT_EXCERPT_CODEPOINTS // 2)
            excerpt_end = min(
                len(normalized), excerpt_start + MAXIMUM_HINT_EXCERPT_CODEPOINTS
            )
            if excerpt_end - excerpt_start < MAXIMUM_HINT_EXCERPT_CODEPOINTS:
                excerpt_start = max(0, excerpt_end - MAXIMUM_HINT_EXCERPT_CODEPOINTS)
            excerpt = normalized[excerpt_start:excerpt_end]
            result.append(
                CheapMemoryHint(
                    signal_code="hint:"
                    + sha256(signal.encode("utf-8")).hexdigest()[:20],
                    normalized_excerpt=excerpt,
                    excerpt_digest=_digest(
                        "pulsara:cheap-memory-hint-excerpt:v1", excerpt
                    ),
                )
            )
        return tuple(result)


@dataclass(frozen=True, slots=True)
class CheapHintEligibleEntry:
    entry_id: str
    entry_sequence: int
    public_text: str = field(repr=False)
    adjacent_assistant_text: str = field(repr=False)
    hints: tuple[CheapMemoryHint, ...]

    def __post_init__(self) -> None:
        if not self.entry_id or self.entry_sequence < 1 or not self.hints:
            raise ValueError("cheap hint eligible entry is invalid")
        if len(self.public_text.encode("utf-8")) > MAXIMUM_REFLECTION_ENTRY_BYTES:
            raise ValueError("cheap hint public entry exceeds its projection bound")
        if (
            len(self.adjacent_assistant_text.encode("utf-8"))
            > MAXIMUM_REFLECTION_ADJACENT_ASSISTANT_BYTES
        ):
            raise ValueError("cheap hint assistant projection exceeds its bound")


@dataclass(frozen=True, slots=True)
class PreparedCheapHintReflectionHandoff:
    session_id: str
    workspace_id: str
    memory_domain_id: str
    workspace_scope_id: str | None
    turn_id: str
    permission_snapshot_fingerprint: str
    provider_trust_domain_identity: str
    eligible_entries: tuple[CheapHintEligibleEntry, ...] = field(repr=False)
    final_assistant_text: str = field(repr=False)
    handoff_fingerprint: str

    def __post_init__(self) -> None:
        if not all(
            (
                self.session_id,
                self.workspace_id,
                self.memory_domain_id,
                self.turn_id,
                self.permission_snapshot_fingerprint,
                self.provider_trust_domain_identity,
                self.handoff_fingerprint,
            )
        ):
            raise ValueError("cheap hint handoff identity is incomplete")
        if self.workspace_scope_id is not None and not self.workspace_scope_id.startswith(
            "ctx:workspace/"
        ):
            raise ValueError("cheap hint handoff workspace scope is invalid")
        if not 1 <= len(self.eligible_entries) <= MAXIMUM_HINT_ENTRIES:
            raise ValueError("cheap hint handoff entry count is invalid")
        if sum(len(item.hints) for item in self.eligible_entries) > MAXIMUM_HINTS:
            raise ValueError("cheap hint handoff exceeds its hint bound")
        if (
            len(self.final_assistant_text.encode("utf-8"))
            > MAXIMUM_REFLECTION_ADJACENT_ASSISTANT_BYTES
        ):
            raise ValueError("cheap hint final assistant projection exceeds its bound")
        if self.handoff_fingerprint != cheap_hint_handoff_fingerprint(self):
            raise ValueError("cheap hint handoff fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class PreparedCheapHintReflectionCandidateBatch:
    handoff_fingerprint: str
    model_output_digest: str
    candidates: tuple[PreparedMemoryCandidateAcceptance, ...]
    batch_fingerprint: str

    def __post_init__(self) -> None:
        if not self.handoff_fingerprint or not self.model_output_digest.startswith(
            "sha256:"
        ):
            raise ValueError("reflection candidate batch identity is invalid")
        if len(self.candidates) > 4:
            raise ValueError("reflection candidate batch exceeds its bound")
        if self.batch_fingerprint != _digest(
            "pulsara:cheap-hint-reflection-candidate-batch:v1",
            {
                "handoff": self.handoff_fingerprint,
                "model_output": self.model_output_digest,
                "candidates": tuple(
                    (item.candidate_id, item.candidate_acceptance_digest)
                    for item in self.candidates
                ),
            },
        ):
            raise ValueError("reflection candidate batch fingerprint mismatch")


def prepare_cheap_hint_reflection_handoff(
    *,
    canonical: CanonicalModelInputSnapshot,
    permission: FrozenRunPermissionSnapshot,
    workspace_id: str,
    memory_domain_id: str,
    workspace_scope_id: str | None,
    provider_trust_domain_identity: str,
    remember_requested: bool,
    matcher: CheapMemoryHintSetV1 | None = None,
    write_opt_out: MemoryWriteOptOut | None = None,
    turn_use_opt_out: TurnMemoryUseOptOut | None = None,
) -> PreparedCheapHintReflectionHandoff | None:
    """Freeze a successful ROOT turn's optional reflection handoff."""

    identity = canonical.identity
    if identity.conversation_scope_kind is not ModelInputScopeKind.ROOT:
        return None
    if remember_requested:
        return None
    if (
        permission.overlay is not RunPermissionOverlay.NONE
        or permission.effective_mode is PermissionMode.READ_ONLY
    ):
        return None
    hint_set = matcher or CheapMemoryHintSetV1()
    write_opt_out_gate = write_opt_out or MemoryWriteOptOut()
    turn_use_opt_out_gate = turn_use_opt_out or TurnMemoryUseOptOut()
    ordered = tuple(
        item
        for item in canonical.items
        if item.source_turn_id == identity.turn_id
    )
    if any(
        item.item_kind is FrozenProviderInputItemKind.USER
        and item.input_origin
        in {
            CanonicalInputOriginKind.HUMAN_MESSAGE,
            CanonicalInputOriginKind.HUMAN_STEER,
        }
        and turn_use_opt_out_gate.excludes(item.text)
        for item in ordered
    ):
        return None
    final_assistant = ""
    for item in ordered:
        if item.item_kind in {
            FrozenProviderInputItemKind.ASSISTANT,
            FrozenProviderInputItemKind.ASSISTANT_TOOL_REQUEST,
        }:
            final_assistant = _bounded_head_tail(
                item.text, MAXIMUM_REFLECTION_ADJACENT_ASSISTANT_BYTES
            )
    eligible: list[CheapHintEligibleEntry] = []
    remaining_hints = MAXIMUM_HINTS
    preceding_assistant = ""
    for item in ordered:
        if item.item_kind in {
            FrozenProviderInputItemKind.ASSISTANT,
            FrozenProviderInputItemKind.ASSISTANT_TOOL_REQUEST,
        }:
            preceding_assistant = _bounded_head_tail(
                item.text, MAXIMUM_REFLECTION_ADJACENT_ASSISTANT_BYTES
            )
            continue
        if (
            item.item_kind is not FrozenProviderInputItemKind.USER
            or item.input_origin
            not in {
                CanonicalInputOriginKind.HUMAN_MESSAGE,
                CanonicalInputOriginKind.HUMAN_STEER,
            }
            or item.source_entry_id is None
            or item.source_entry_sequence is None
            or write_opt_out_gate.excludes(item.text)
        ):
            continue
        hints = hint_set.match(item.text, maximum_hints=max(1, remaining_hints))
        if not hints:
            continue
        eligible.append(
            CheapHintEligibleEntry(
                entry_id=item.source_entry_id,
                entry_sequence=item.source_entry_sequence,
                public_text=_bounded_head_tail(
                    item.text, MAXIMUM_REFLECTION_ENTRY_BYTES
                ),
                adjacent_assistant_text=preceding_assistant,
                hints=hints,
            )
        )
        remaining_hints -= len(hints)
        if len(eligible) >= MAXIMUM_HINT_ENTRIES or remaining_hints == 0:
            break
    if not eligible:
        return None
    values: dict[str, object] = {
        "session_id": identity.session_id,
        "workspace_id": workspace_id,
        "memory_domain_id": memory_domain_id,
        "workspace_scope_id": workspace_scope_id,
        "turn_id": identity.turn_id,
        "permission_snapshot_fingerprint": permission.snapshot_fingerprint,
        "provider_trust_domain_identity": provider_trust_domain_identity,
        "eligible_entries": tuple(eligible),
        "final_assistant_text": final_assistant,
    }
    provisional = object.__new__(PreparedCheapHintReflectionHandoff)
    for name, value in values.items():
        object.__setattr__(provisional, name, value)
    object.__setattr__(provisional, "handoff_fingerprint", "")
    return PreparedCheapHintReflectionHandoff(
        **values,
        handoff_fingerprint=cheap_hint_handoff_fingerprint(provisional),
    )


def cheap_hint_handoff_fingerprint(
    handoff: PreparedCheapHintReflectionHandoff,
) -> str:
    return _digest(
        "pulsara:cheap-hint-reflection-handoff:v1",
        {
            "session_id": handoff.session_id,
            "workspace_id": handoff.workspace_id,
            "memory_domain_id": handoff.memory_domain_id,
            "workspace_scope_id": handoff.workspace_scope_id,
            "turn_id": handoff.turn_id,
            "permission": handoff.permission_snapshot_fingerprint,
            "provider_trust_domain": handoff.provider_trust_domain_identity,
            "entries": tuple(
                {
                    "id": entry.entry_id,
                    "sequence": entry.entry_sequence,
                    "text_digest": _digest(
                        "pulsara:cheap-hint-entry-text:v1", entry.public_text
                    ),
                    "assistant_digest": _digest(
                        "pulsara:cheap-hint-adjacent-assistant:v1",
                        entry.adjacent_assistant_text,
                    ),
                    "hints": tuple(
                        (hint.signal_code, hint.excerpt_digest)
                        for hint in entry.hints
                    ),
                }
                for entry in handoff.eligible_entries
            ),
            "final_assistant_digest": _digest(
                "pulsara:cheap-hint-final-assistant:v1",
                handoff.final_assistant_text,
            ),
        },
    )


def _bounded_head_tail(value: str, maximum_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return value
    marker = b"\n...[bounded projection omitted]...\n"
    budget = maximum_bytes - len(marker)
    head = _utf8_prefix(encoded, budget // 2)
    tail = _utf8_suffix(encoded, budget - len(head))
    return (head + marker + tail).decode("utf-8")


def _utf8_prefix(value: bytes, limit: int) -> bytes:
    result = value[: max(0, limit)]
    while result:
        try:
            result.decode("utf-8")
            return result
        except UnicodeDecodeError:
            result = result[:-1]
    return b""


def _utf8_suffix(value: bytes, limit: int) -> bytes:
    result = value[-max(0, limit) :] if limit else b""
    while result:
        try:
            result.decode("utf-8")
            return result
        except UnicodeDecodeError:
            result = result[1:]
    return b""


def _digest(namespace: str, value: object) -> str:
    return "sha256:" + sha256(
        namespace.encode("utf-8") + b"\0" + canonical_json_bytes(value)
    ).hexdigest()


def reflection_batch_fingerprint(
    *,
    handoff_fingerprint: str,
    model_output_digest: str,
    candidates: Sequence[PreparedMemoryCandidateAcceptance],
) -> str:
    return _digest(
        "pulsara:cheap-hint-reflection-candidate-batch:v1",
        {
            "handoff": handoff_fingerprint,
            "model_output": model_output_digest,
            "candidates": tuple(
                (item.candidate_id, item.candidate_acceptance_digest)
                for item in candidates
            ),
        },
    )


__all__ = [
    "CheapHintEligibleEntry",
    "CheapMemoryHint",
    "CheapMemoryHintSetV1",
    "MemoryWriteOptOut",
    "PreparedCheapHintReflectionCandidateBatch",
    "PreparedCheapHintReflectionHandoff",
    "TurnMemoryUseOptOut",
    "cheap_hint_handoff_fingerprint",
    "normalize_reflection_text",
    "prepare_cheap_hint_reflection_handoff",
    "reflection_batch_fingerprint",
]
