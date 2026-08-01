"""Closed sanitizer registry for compaction-memory evidence and diagnostics."""

from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha256

from pulsara_agent.primitives.compaction import (
    COMPACTION_MEMORY_EVIDENCE_SANITIZER_CONTRACT_FINGERPRINT,
    CompactionMemoryEvidenceRedactionAuditFact,
)
from pulsara_agent.primitives.frozen import build_frozen_fact


SANITIZER_CONTRACT_FINGERPRINT = (
    COMPACTION_MEMORY_EVIDENCE_SANITIZER_CONTRACT_FINGERPRINT
)


@dataclass(frozen=True, slots=True)
class SanitizedCompactionEvidence:
    text: str
    text_sha256: str
    text_utf8_bytes: int
    audits: tuple[CompactionMemoryEvidenceRedactionAuditFact, ...]


@dataclass(frozen=True, slots=True)
class _Rule:
    rule_id: str
    version: str
    pattern: re.Pattern[str]


_RULES = (
    _Rule(
        "pem-private-key",
        "1",
        re.compile(
            r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----[\s\S]*?"
            r"-----END(?: [A-Z0-9]+)? PRIVATE KEY-----",
            re.IGNORECASE,
        ),
    ),
    _Rule(
        "bearer-token",
        "1",
        re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
    ),
    _Rule(
        "openai-style-token",
        "1",
        re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b", re.IGNORECASE),
    ),
    _Rule(
        "credential-assignment",
        "1",
        re.compile(
            r"(?i)\b(?:api[_-]?key|password|passwd|token|secret|authorization)"
            r"\s*[:=]\s*[^\s,;]{4,}"
        ),
    ),
    _Rule(
        "dsn-password",
        "1",
        re.compile(r"(?i)(?:postgres(?:ql)?|mysql)://[^\s:@/]+:[^\s@/]+@[^\s]+"),
    ),
    _Rule(
        "cloud-access-key",
        "1",
        re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    ),
    _Rule(
        "opaque-high-entropy-run",
        "1",
        re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9+/=_-]{48,}(?![A-Za-z0-9])"),
    ),
)


def sanitize_compaction_evidence(text: str) -> SanitizedCompactionEvidence:
    """Return full sanitized text and attribution-only replacement audits."""

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    matches: list[tuple[int, int, int, _Rule]] = []
    for priority, rule in enumerate(_RULES):
        matches.extend(
            (match.start(), match.end(), priority, rule)
            for match in rule.pattern.finditer(normalized)
        )
    matches.sort(key=lambda item: (item[0], -(item[1] - item[0]), item[2]))

    parts: list[str] = []
    audits: list[CompactionMemoryEvidenceRedactionAuditFact] = []
    source_cursor = 0
    sanitized_chars = 0
    for source_start, source_end, _priority, rule in matches:
        if source_start < source_cursor:
            continue
        prefix = normalized[source_cursor:source_start]
        replacement = f"[REDACTED:{rule.rule_id}]"
        parts.extend((prefix, replacement))
        sanitized_start = sanitized_chars + len(prefix)
        sanitized_end = sanitized_start + len(replacement)
        ordinal = len(audits)
        audits.append(
            build_frozen_fact(
                CompactionMemoryEvidenceRedactionAuditFact,
                schema_version="compaction_memory_evidence_redaction_audit.v1",
                redaction_ordinal=ordinal,
                sanitizer_rule_id=rule.rule_id,
                sanitizer_rule_version=rule.version,
                replacement_text=replacement,
                sanitized_start_char=sanitized_start,
                sanitized_end_char=sanitized_end,
            )
        )
        sanitized_chars = sanitized_end
        source_cursor = source_end

    parts.append(normalized[source_cursor:])
    sanitized = "".join(parts)

    encoded = sanitized.encode("utf-8")
    return SanitizedCompactionEvidence(
        text=sanitized,
        text_sha256=sha256(encoded).hexdigest(),
        text_utf8_bytes=len(encoded),
        audits=tuple(audits),
    )


def contains_compaction_secret(text: str) -> bool:
    return sanitize_compaction_evidence(text).text != text.replace(
        "\r\n", "\n"
    ).replace("\r", "\n")


__all__ = [
    "SANITIZER_CONTRACT_FINGERPRINT",
    "SanitizedCompactionEvidence",
    "contains_compaction_secret",
    "sanitize_compaction_evidence",
]
