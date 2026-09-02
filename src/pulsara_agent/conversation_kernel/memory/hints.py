"""Pure, sealed lexical gates for advisory-memory use and write guidance."""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata


MEMORY_WRITE_HINT_BODY = (
    "The following human input may contain durable, reusable information. Apply the "
    "memory contract independently. If appropriate, use remember before the final "
    "reply; otherwise continue normally. This hint does not itself require storage."
)


_SEALED_WRITE_HINT_SIGNALS = (
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
    r"layout)\b)(?:\s+for\s+this\s+(?:turn|answer))?|"
    r"(?:^|[.!?;:]\s*)(?:please\s+)?(?:don't|do\s+not)\s+use\s+"
    r"memor(?:y|ies)\s+for\s+this\s+(?:turn|answer)|"
    r"(?:^|[.!?;:]\s*)answer(?:\s+this)?\s+(?:question\s+)?without\s+"
    r"(?:using|consulting|reading|accessing|referencing)\s+"
    r"(?:saved\s+|stored\s+|previous\s+|long-term\s+)?memor(?:y|ies)"
    r"(?![a-z0-9_]|\s+(?:mapping|map|allocation|management|address|buffer|"
    r"layout)\b))"
)
_CHINESE_TURN_MEMORY_USE_OPT_OUT = re.compile(
    r"(?:"
    r"(?:本轮|这轮|这次|本次|当前回答)[，,:：]?\s*(?:请)?"
    r"(?:不|不要|别|不用|无需|请勿)(?:使用|读取|查询|参考|调用|检索)"
    r"(?:已有|历史|长期|已保存的?)?(?:记忆|偏好|信息)|"
    r"(?:^|[。！？；]\s*)(?:请)?(?:不要用|别用|不用|无需使用|请勿使用)"
    r"(?:已有|历史|长期|已保存的?)?记忆(?:来)?回答)"
)


def normalize_memory_trigger_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).casefold().split())


@dataclass(frozen=True, slots=True)
class MemoryWriteOptOut:
    contract_id: str = "pulsara.memory-write-opt-out.v2"

    def excludes(self, text: str) -> bool:
        normalized = normalize_memory_trigger_text(text)
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
        normalized = normalize_memory_trigger_text(text)
        return bool(
            _ENGLISH_TURN_MEMORY_USE_OPT_OUT.search(normalized)
            or _CHINESE_TURN_MEMORY_USE_OPT_OUT.search(normalized)
        )


@dataclass(frozen=True, slots=True)
class CheapMemoryWriteHintMatcher:
    """A fallible local attention gate with no candidate or source authority."""

    contract_id: str = "pulsara.cheap-memory-write-hint.v2"
    signals: tuple[str, ...] = _SEALED_WRITE_HINT_SIGNALS

    def matches(self, text: str) -> bool:
        normalized = normalize_memory_trigger_text(text)
        return any(
            normalize_memory_trigger_text(signal) in normalized
            for signal in self.signals
        )


__all__ = [
    "CheapMemoryWriteHintMatcher",
    "MEMORY_WRITE_HINT_BODY",
    "MemoryWriteOptOut",
    "TurnMemoryUseOptOut",
    "normalize_memory_trigger_text",
]
