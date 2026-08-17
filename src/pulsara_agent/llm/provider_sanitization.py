"""Single provider-error sanitization contract shared by all stream paths."""

from __future__ import annotations

import re
from urllib.parse import SplitResult, urlsplit, urlunsplit

from pulsara_agent.primitives.model_call import (
    ProviderErrorSanitizationContractFact,
    ProviderModelStreamErrorCode,
    ProviderRetrySummaryFact,
    ProviderSanitizedErrorFact,
    sha256_fingerprint,
)


_URL_RE = re.compile(r"https?://[^\s\"'<>]+")
_SECRET_RE = re.compile(
    r"(?i)(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+|"
    r"\b(?:authorization|proxy[-_ ]?authorization|api[-_ ]?key|x[-_ ]?api[-_ ]?key|"
    r"access[-_ ]?token|refresh[-_ ]?token|password|passwd|secret|cookie|set[-_ ]?cookie)"
    r"\s*[:=]\s*(?:(?:Bearer|Basic)\s+)?[^\s,;}]+"
)


def _contract() -> ProviderErrorSanitizationContractFact:
    payload = {
        "contract_id": "pulsara.provider-error-sanitizer",
        "contract_version": "v2",
        "stable_code_mapping_fingerprint": sha256_fingerprint(
            "provider-error-code-map:v2",
            {
                "codes": tuple(item.value for item in ProviderModelStreamErrorCode),
                "transport_contract_hint": "transport_* -> transport_protocol_error",
            },
        ),
        "sensitive_key_policy_fingerprint": sha256_fingerprint(
            "provider-error-sensitive-keys:v1",
            (
                "authorization",
                "proxyauthorization",
                "apikey",
                "xapikey",
                "accesstoken",
                "refreshtoken",
                "password",
                "passwd",
                "secret",
                "cookie",
                "setcookie",
            ),
        ),
        "secret_pattern_policy_fingerprint": sha256_fingerprint(
            "provider-error-secret-patterns:v1", _SECRET_RE.pattern
        ),
        "url_redaction_policy_fingerprint": sha256_fingerprint(
            "provider-error-url-policy:v1", "remove-userinfo-query-fragment"
        ),
        "diagnostic_attribute_allowlist_fingerprint": sha256_fingerprint(
            "provider-error-diagnostic-allowlist:v1", ()
        ),
        "max_message_chars": 512,
        "max_diagnostic_count": 8,
        "max_diagnostic_attribute_chars": 128,
    }
    provisional = ProviderErrorSanitizationContractFact.model_construct(
        **payload, contract_fingerprint="pending"
    )
    canonical = provisional.model_dump(mode="json", exclude={"contract_fingerprint"})
    return ProviderErrorSanitizationContractFact(
        **canonical,
        contract_fingerprint=sha256_fingerprint(
            "provider-error-sanitization-contract:v1", canonical
        ),
    )


DEFAULT_PROVIDER_ERROR_SANITIZATION_CONTRACT = _contract()


def _redact_url(match: re.Match[str]) -> str:
    raw = match.group(0)
    trailing = ""
    while raw and raw[-1] in ".,;:)":
        trailing = raw[-1] + trailing
        raw = raw[:-1]
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return "[redacted-url]" + trailing
    host = parsed.hostname or ""
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urlunsplit(SplitResult(parsed.scheme, host, parsed.path, "", "")) + trailing


def sanitize_provider_failure(
    *,
    message: object,
    code_hint: str | None = None,
    retry_summary: ProviderRetrySummaryFact | None = None,
) -> ProviderSanitizedErrorFact:
    """Map one provider failure to a bounded event-safe fact."""

    try:
        raw = str(message)
        text = _URL_RE.sub(_redact_url, raw)
        text = _SECRET_RE.sub("[redacted]", text)
        truncated = (
            len(text) > DEFAULT_PROVIDER_ERROR_SANITIZATION_CONTRACT.max_message_chars
        )
        text = text[: DEFAULT_PROVIDER_ERROR_SANITIZATION_CONTRACT.max_message_chars]
        hint = (code_hint or "").casefold()
        if hint == "transport_source_item_limit_exceeded":
            stable_code = (
                ProviderModelStreamErrorCode.TRANSPORT_SOURCE_ITEM_LIMIT_EXCEEDED
            )
        elif hint == "transport_source_payload_limit_exceeded":
            stable_code = (
                ProviderModelStreamErrorCode.TRANSPORT_SOURCE_PAYLOAD_LIMIT_EXCEEDED
            )
        elif hint.startswith("transport_"):
            stable_code = ProviderModelStreamErrorCode.TRANSPORT_PROTOCOL_ERROR
        elif "auth" in hint or "401" in hint:
            stable_code = ProviderModelStreamErrorCode.AUTHENTICATION_FAILED
        elif "permission" in hint or "403" in hint:
            stable_code = ProviderModelStreamErrorCode.PERMISSION_DENIED
        elif "rate" in hint or "429" in hint:
            stable_code = ProviderModelStreamErrorCode.RATE_LIMITED
        elif "timeout" in hint:
            stable_code = ProviderModelStreamErrorCode.PROVIDER_TIMEOUT
        elif "overload" in hint:
            stable_code = ProviderModelStreamErrorCode.PROVIDER_OVERLOADED
        elif "invalid" in hint or "400" in hint:
            stable_code = ProviderModelStreamErrorCode.INVALID_REQUEST
        else:
            stable_code = ProviderModelStreamErrorCode.UNKNOWN_PROVIDER_ERROR
        payload = {
            "code": stable_code,
            "message": text or "Provider model stream failed.",
            "diagnostics": (),
            "redaction_count": 0,
            "truncated": truncated,
            "sanitization_contract": DEFAULT_PROVIDER_ERROR_SANITIZATION_CONTRACT,
            "retry_summary": retry_summary,
        }
        provisional = ProviderSanitizedErrorFact.model_construct(
            **payload, error_fingerprint="pending"
        )
        canonical = provisional.model_dump(mode="json", exclude={"error_fingerprint"})
        return ProviderSanitizedErrorFact(
            **canonical,
            error_fingerprint=sha256_fingerprint(
                "provider-sanitized-error:v2", canonical
            ),
        )
    except BaseException:
        payload = {
            "code": ProviderModelStreamErrorCode.TRANSPORT_PROTOCOL_ERROR,
            "message": "Provider error sanitization failed.",
            "diagnostics": (),
            "redaction_count": 0,
            "truncated": False,
            "sanitization_contract": DEFAULT_PROVIDER_ERROR_SANITIZATION_CONTRACT,
            "retry_summary": retry_summary,
        }
        provisional = ProviderSanitizedErrorFact.model_construct(
            **payload, error_fingerprint="pending"
        )
        canonical = provisional.model_dump(mode="json", exclude={"error_fingerprint"})
        return ProviderSanitizedErrorFact(
            **canonical,
            error_fingerprint=sha256_fingerprint(
                "provider-sanitized-error:v2", canonical
            ),
        )


__all__ = [
    "DEFAULT_PROVIDER_ERROR_SANITIZATION_CONTRACT",
    "sanitize_provider_failure",
]
