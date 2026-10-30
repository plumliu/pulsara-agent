"""Single provider-error sanitization contract shared by all stream paths."""

from __future__ import annotations

import re
from collections.abc import Callable

from pulsara_agent.primitives.model_call import (
    ProviderErrorSanitizationContractFact,
    ProviderModelStreamErrorCode,
    ProviderRetrySummaryFact,
    ProviderSanitizedErrorFact,
    provider_sanitized_error_identity_fingerprint,
    sha256_fingerprint,
)


_URL_RE = re.compile(r"https?://[^\s\"'<>]+")
_AUTHORIZATION_RE = re.compile(
    r"(?i)(\b(?:authorization|proxy-authorization)\s*:\s*(?:Bearer|Basic)\s+)[A-Za-z0-9._~+/=-]+"
)


def _contract() -> ProviderErrorSanitizationContractFact:
    payload = {
        "contract_id": "pulsara.provider-error-sanitizer",
        "contract_version": "v3",
        "stable_code_mapping_fingerprint": sha256_fingerprint(
            "provider-error-code-map:v3",
            {
                "codes": tuple(item.value for item in ProviderModelStreamErrorCode),
                "transport_contract_hint": "transport_* -> transport_protocol_error",
                "provider_transport_error": "provider_connection_error",
            },
        ),
        "sensitive_key_policy_fingerprint": sha256_fingerprint(
            "provider-error-sensitive-keys:v2",
            (
                "execution-captured-exact-values",
                "authorization-header-bearer-basic",
                "url-userinfo",
            ),
        ),
        "secret_pattern_policy_fingerprint": sha256_fingerprint(
            "provider-error-secret-patterns:v2", _AUTHORIZATION_RE.pattern
        ),
        "url_redaction_policy_fingerprint": sha256_fingerprint(
            "provider-error-url-policy:v2",
            "remove-userinfo;preserve-path-query-fragment",
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
    # URL authority userinfo is an authentication carrier. Preserve all other
    # URL evidence, including ordinary query parameters and fragments.
    return re.sub(r"^(https?://)[^/?#\s]*@", r"\1", match.group(0))


def sanitize_provider_failure(
    *,
    message: object,
    code_hint: str | None = None,
    retry_summary: ProviderRetrySummaryFact | None = None,
    scrub_text: Callable[[str], str] | None = None,
) -> ProviderSanitizedErrorFact:
    """Map one provider failure to a bounded event-safe fact."""

    try:
        raw = str(message)
        if scrub_text is not None:
            raw = scrub_text(raw)
        text = _URL_RE.sub(_redact_url, raw)
        text = _AUTHORIZATION_RE.sub(r"\1[redacted]", text)
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
        elif hint == "provider_transport_error":
            stable_code = ProviderModelStreamErrorCode.PROVIDER_CONNECTION_ERROR
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
        return ProviderSanitizedErrorFact(
            **payload,
            error_fingerprint=provider_sanitized_error_identity_fingerprint(
                provisional
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
        return ProviderSanitizedErrorFact(
            **payload,
            error_fingerprint=provider_sanitized_error_identity_fingerprint(
                provisional
            ),
        )


__all__ = [
    "DEFAULT_PROVIDER_ERROR_SANITIZATION_CONTRACT",
    "sanitize_provider_failure",
]
