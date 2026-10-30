"""OpenAI-compatible retry error classification."""

from __future__ import annotations

import re
import math
import ssl
import sys
import errno
import httpx
import httpx2
from collections.abc import Iterable, Mapping
from email.utils import parsedate_to_datetime
from typing import Any

from pulsara_agent.llm.retry import LLMRetryDecision, RetryDecisionKind


_RETRYABLE_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504, 529}
_NON_RETRYABLE_STATUS_CODES = {400, 401, 403, 404, 413}
_TRANSPORT_ERROR_TYPES = (
    ConnectionError,
    TimeoutError,
    httpx.NetworkError,
    httpx.TimeoutException,
    httpx.RemoteProtocolError,
    httpx.ProxyError,
    httpx2.NetworkError,
    httpx2.TimeoutException,
    httpx2.RemoteProtocolError,
    httpx2.ProxyError,
    ssl.SSLWantReadError,
    ssl.SSLWantWriteError,
    ssl.SSLZeroReturnError,
    ssl.SSLEOFError,
)
_RETRYABLE_NETWORK_ERRNOS = frozenset(
    {
        errno.ECONNRESET,
        errno.ECONNABORTED,
        errno.ECONNREFUSED,
        errno.EPIPE,
        errno.ETIMEDOUT,
        errno.ENETRESET,
        errno.ENETUNREACH,
        errno.EHOSTUNREACH,
    }
)
_TRANSIENT_PROVIDER_CODES = {
    "server_error",
    "internal_server_error",
    "internal_error",
    "overloaded_error",
    "rate_limit_exceeded",
    "temporarily_unavailable",
    "request_timeout",
}
_DETERMINISTIC_LOCAL_NAMES = {
    "InvalidURL",
    "UnsupportedProtocol",
    "LocalProtocolError",
}
_DETERMINISTIC_BAD_REQUEST_MARKERS = (
    "unknown parameter",
    "unsupported parameter",
    "invalid request",
    "invalid_request_error",
    "schema validation",
    "tool schema",
    "model not found",
    "does not support",
    "unsupported value",
    "context length",
    "context too long",
    "maximum context",
    "payload too large",
    "authentication",
    "insufficient_quota",
    "billing",
)
_NON_RETRYABLE_CODES = {
    "invalid_request_error",
    "unsupported_parameter",
    "unknown_parameter",
    "invalid_schema",
    "authentication",
    "insufficient_quota",
    "quota_exceeded",
    "insufficient_balance",
    "model_not_found",
    "context_length_exceeded",
}
_RETRY_AFTER_PATTERN = re.compile(
    r"(?:retry|try again|rate limit|too many requests).{0,80}?"
    r"(?P<value>[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*(?P<unit>ms|millisecond|milliseconds|s|sec|secs|second|seconds)?",
    re.IGNORECASE,
)


def classify_llm_error(exc: BaseException) -> LLMRetryDecision:
    """Classify an OpenAI-compatible exception for transport retry."""

    from pulsara_agent.llm.errors import LLMTransportContractError

    if isinstance(exc, LLMTransportContractError):
        return LLMRetryDecision(
            kind=RetryDecisionKind.DO_NOT_RETRY, reason=exc.reason_code
        )
    status_code = _extract_status_code(exc)
    provider_code = _extract_provider_code(exc)
    retry_after_seconds = parse_retry_after_seconds(
        headers=_extract_headers(exc),
        body=_extract_body(exc),
        message=_chain_text(exc),
    )
    text = _chain_text(exc).lower()

    if _is_deterministic_local_error(exc):
        return LLMRetryDecision(
            kind=RetryDecisionKind.DO_NOT_RETRY,
            reason="deterministic_local_configuration",
            status_code=status_code,
            provider_code=provider_code,
        )
    if _is_deterministic_bad_request(text, provider_code=provider_code):
        return LLMRetryDecision(
            kind=RetryDecisionKind.DO_NOT_RETRY,
            reason="deterministic_bad_request",
            status_code=status_code,
            retry_after_seconds=retry_after_seconds,
            provider_code=provider_code,
        )
    if status_code in _NON_RETRYABLE_STATUS_CODES:
        return LLMRetryDecision(
            kind=RetryDecisionKind.DO_NOT_RETRY,
            reason=f"http_{status_code}_non_retryable",
            status_code=status_code,
            retry_after_seconds=retry_after_seconds,
            provider_code=provider_code,
        )
    if status_code in _RETRYABLE_STATUS_CODES:
        return LLMRetryDecision(
            kind=RetryDecisionKind.RETRY,
            reason=f"http_{status_code}_retryable",
            status_code=status_code,
            retry_after_seconds=retry_after_seconds,
            provider_code=provider_code,
        )
    if provider_code in _TRANSIENT_PROVIDER_CODES:
        return LLMRetryDecision(
            kind=RetryDecisionKind.RETRY,
            reason="transient_provider_error",
            status_code=status_code,
            retry_after_seconds=retry_after_seconds,
            provider_code=provider_code,
        )
    if _has_retryable_transport_signal(exc):
        return LLMRetryDecision(
            kind=RetryDecisionKind.RETRY,
            reason=(
                "transport_timeout"
                if any(
                    "Timeout" in type(item).__name__ for item in _exception_chain(exc)
                )
                else "transport_error"
            ),
            status_code=status_code,
            retry_after_seconds=retry_after_seconds,
            provider_code=provider_code,
        )
    return LLMRetryDecision(
        kind=RetryDecisionKind.DO_NOT_RETRY,
        reason="unknown_non_retryable",
        status_code=status_code,
        retry_after_seconds=retry_after_seconds,
        provider_code=provider_code,
    )


def parse_retry_after_seconds(
    *,
    headers: Mapping[str, Any] | None = None,
    body: Any = None,
    message: str | None = None,
) -> float | None:
    """Parse Retry-After hints from headers, body, or provider text."""

    header_value = _header_value(headers, "retry-after-ms")
    parsed = _parse_float_seconds(header_value, multiplier=0.001)
    if parsed is not None:
        return parsed

    header_value = _header_value(headers, "retry-after")
    parsed = _parse_retry_after_header(header_value)
    if parsed is not None:
        return parsed

    parsed = _parse_retry_after_from_body(body)
    if parsed is not None:
        return parsed

    if message:
        match = _RETRY_AFTER_PATTERN.search(message)
        if match:
            unit = (match.group("unit") or "s").lower()
            multiplier = (
                0.001
                if unit.startswith("ms") or unit.startswith("millisecond")
                else 1.0
            )
            return _parse_float_seconds(match.group("value"), multiplier=multiplier)
    return None


def _extract_status_code(exc: BaseException) -> int | None:
    for item in _exception_chain(exc):
        status = getattr(item, "status_code", None)
        if isinstance(status, int):
            return status
        response = getattr(item, "response", None)
        response_status = getattr(response, "status_code", None)
        if isinstance(response_status, int):
            return response_status
    return None


def _extract_headers(exc: BaseException) -> Mapping[str, Any] | None:
    for item in _exception_chain(exc):
        response = getattr(item, "response", None)
        headers = getattr(response, "headers", None)
        if headers is not None:
            return headers
        headers = getattr(item, "headers", None)
        if headers is not None:
            return headers
    return None


def _extract_body(exc: BaseException) -> Any:
    for item in _exception_chain(exc):
        body = getattr(item, "body", None)
        if body is not None:
            return body
        response = getattr(item, "response", None)
        json_method = getattr(response, "json", None)
        if callable(json_method):
            try:
                return json_method()
            except Exception:
                pass
    return None


def _extract_provider_code(exc: BaseException) -> str | None:
    for item in _exception_chain(exc):
        code = getattr(item, "code", None)
        if isinstance(code, str) and code:
            return code
        body_code = _provider_code_from_body(getattr(item, "body", None))
        if body_code:
            return body_code
    return _provider_code_from_body(_extract_body(exc))


def _provider_code_from_body(body: Any) -> str | None:
    if isinstance(body, Mapping):
        for key in ("code", "type"):
            code = body.get(key)
            if isinstance(code, str) and code:
                return code
        error = body.get("error")
        if isinstance(error, Mapping):
            for key in ("code", "type"):
                code = error.get(key)
                if isinstance(code, str) and code:
                    return code
    return None


def _exception_chain(exc: BaseException) -> Iterable[BaseException]:
    seen: set[int] = set()
    pending = [exc]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        if current.__context__ is not None:
            pending.append(current.__context__)
        if current.__cause__ is not None:
            pending.append(current.__cause__)


def _is_deterministic_local_error(exc: BaseException) -> bool:
    for item in _exception_chain(exc):
        if (
            isinstance(item, ssl.SSLCertVerificationError)
            or type(item).__name__ in _DETERMINISTIC_LOCAL_NAMES
        ):
            return True
        text = str(item).casefold()
        if type(item).__name__ == "ProxyError" and any(
            marker in text
            for marker in (
                "unsupported",
                "invalid proxy",
                "407",
                "authentication",
                "credentials",
            )
        ):
            return True
        if (
            "certificate_verify_failed" in text
            or "certificate verify failed" in text
            or "hostname mismatch" in text
        ):
            return True
    return False


def _chain_text(exc: BaseException) -> str:
    parts: list[str] = []
    for item in _exception_chain(exc):
        parts.append(type(item).__name__)
        parts.append(type(item).__module__)
        parts.append(str(item))
        body = getattr(item, "body", None)
        if body is not None:
            parts.append(str(body))
    extracted_body = _extract_body(exc)
    if extracted_body is not None:
        parts.append(str(extracted_body))
    return " ".join(part for part in parts if part)


def _is_deterministic_bad_request(text: str, *, provider_code: str | None) -> bool:
    if provider_code and provider_code.lower() in _NON_RETRYABLE_CODES:
        return True
    return any(marker in text for marker in _DETERMINISTIC_BAD_REQUEST_MARKERS)


def _has_retryable_transport_signal(exc: BaseException) -> bool:
    # The SDK has already been prepared before an SDK exception can exist.
    # Read its public type without importing the SDK on the event-loop thread.
    sdk = sys.modules.get("openai")
    sdk_type = None if sdk is None else getattr(sdk, "APIConnectionError", None)
    return (
        isinstance(exc, _TRANSPORT_ERROR_TYPES)
        or (isinstance(sdk_type, type) and isinstance(exc, sdk_type))
        or (isinstance(exc, OSError) and exc.errno in _RETRYABLE_NETWORK_ERRNOS)
    )


def _header_value(headers: Mapping[str, Any] | None, name: str) -> Any:
    if not headers:
        return None
    lower_name = name.lower()
    for key, value in headers.items():
        if str(key).lower() == lower_name:
            return value
    get = getattr(headers, "get", None)
    if callable(get):
        return get(name) or get(name.lower()) or get(name.title())
    return None


def _parse_float_seconds(value: Any, *, multiplier: float = 1.0) -> float | None:
    if value is None:
        return None
    if isinstance(value, int | float):
        parsed = float(value) * multiplier
        return parsed if math.isfinite(parsed) and parsed >= 0 else None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = float(text) * multiplier
        return parsed if math.isfinite(parsed) and parsed >= 0 else None
    except ValueError:
        return None


def _parse_retry_after_header(value: Any) -> float | None:
    parsed = _parse_float_seconds(value)
    if parsed is not None:
        return parsed
    if value is None:
        return None
    try:
        from datetime import datetime, timezone

        retry_at = parsedate_to_datetime(str(value))
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max((retry_at - datetime.now(timezone.utc)).total_seconds(), 0.0)
    except Exception:
        return None


def _parse_retry_after_from_body(body: Any) -> float | None:
    if isinstance(body, Mapping):
        for key in ("retry_after_ms", "retryAfterMs"):
            parsed = _parse_float_seconds(body.get(key), multiplier=0.001)
            if parsed is not None:
                return parsed
        for key in ("retry_after", "retryAfter"):
            parsed = _parse_float_seconds(body.get(key))
            if parsed is not None:
                return parsed
        for value in body.values():
            parsed = _parse_retry_after_from_body(value)
            if parsed is not None:
                return parsed
    elif isinstance(body, list | tuple):
        for value in body:
            parsed = _parse_retry_after_from_body(value)
            if parsed is not None:
                return parsed
    elif isinstance(body, str):
        return parse_retry_after_seconds(message=body)
    return None
