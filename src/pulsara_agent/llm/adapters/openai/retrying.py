"""Shared retry helpers for OpenAI-compatible transports."""

from __future__ import annotations

import logging
import asyncio
import json
import math
import os
import re
import base64
import traceback
from contextvars import ContextVar
from collections.abc import Callable
from datetime import datetime, timezone
from dataclasses import dataclass
from urllib.parse import parse_qsl, quote, unquote, urlsplit
from urllib.request import getproxies
from contextlib import asynccontextmanager

from pulsara_agent.llm.retry import LLMRetryConfig, LLMRetryDecision, RetryAttemptTrace
from pulsara_agent.llm.provider_sanitization import (
    DEFAULT_PROVIDER_ERROR_SANITIZATION_CONTRACT,
)
from pulsara_agent.process_credential_boundary import (
    ProcessCredentialScrubSet,
    PROCESS_CREDENTIAL_REPLACEMENT,
)
from pulsara_agent.primitives.model_call import (
    ModelCallPurpose,
    ProviderRetryAttemptSummaryFact,
    ProviderRetrySummaryFact,
)


LOGGER = logging.getLogger(__name__)


_DIAGNOSTIC_SCRUBBER: ContextVar[Callable[[str], str] | None] = ContextVar(
    "provider-diagnostic-scrubber",
    default=None,
)


class _ProviderDiagnosticLogFilter(logging.Filter):
    """Keep dependency log metadata while scrubbing this call's exact values.

    httpx2 logs the entire request URL at INFO; OpenAI logs exception chains
    and response URLs at DEBUG. Neither SDK offers a per-client log scrubber.
    Existing MCP filters suppress protocol records and cannot preserve these
    provider diagnostics. Context belongs only to one awaited SDK operation,
    never to a generator yield or a global collection of credential values.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        scrub = _DIAGNOSTIC_SCRUBBER.get()
        if scrub is not None:
            text = record.getMessage()
            if record.exc_info:
                text += "\n" + "".join(traceback.format_exception(*record.exc_info))
                record.exc_info = None
                record.exc_text = None
            record.msg = scrub(text)
            record.args = ()
        return True


for _logger_name in (
    "httpx2",
    "openai._base_client",
    "httpcore2.connection",
    "httpcore2.http11",
    "httpcore2.http2",
    "httpcore2.proxy",
    "httpcore2.socks",
):
    logging.getLogger(_logger_name).addFilter(_ProviderDiagnosticLogFilter())


async def provider_diagnostic_await(operation, scrub):
    token = _DIAGNOSTIC_SCRUBBER.set(scrub.scrub_text)
    try:
        return await operation
    finally:
        _DIAGNOSTIC_SCRUBBER.reset(token)


def sdk_max_retries_for_transport(
    *, retry_config: LLMRetryConfig, purpose: ModelCallPurpose
) -> int | None:
    if purpose in {
        ModelCallPurpose.AGENT_MODEL_LOOP,
        ModelCallPurpose.CONNECTION_PROBE,
        ModelCallPurpose.SESSION_TITLE,
        ModelCallPurpose.CONVERSATION_SUGGESTION,
    }:
        return 0
    # Preserve the existing non-agent policy, including its explicitly disabled
    # adapter configuration. Only the ordinary agent loop changes this round.
    return 0 if retry_config.enabled else None


def make_retry_trace(
    *,
    exc: BaseException,
    decision: LLMRetryDecision,
    attempt: int,
    max_attempts: int,
    delay_seconds: float | None,
) -> RetryAttemptTrace:
    return RetryAttemptTrace(
        attempt=attempt,
        max_attempts=max_attempts,
        error_type=type(exc).__name__,
        error_message=str(exc),
        reason=decision.reason,
        delay_seconds=delay_seconds,
        status_code=decision.status_code,
        retry_after_seconds=decision.retry_after_seconds,
        retry_after_exceeded=decision.retry_after_exceeded,
        provider_code=decision.provider_code,
    )


def build_provider_retry_summary(
    *,
    config: LLMRetryConfig,
    traces: list[RetryAttemptTrace],
    final_decision: LLMRetryDecision | None,
    final_attempt: int,
    has_semantic_output: bool,
    exhausted: bool,
    skipped_reason: str | None,
) -> ProviderRetrySummaryFact:
    if final_decision is None:
        raise ValueError("provider retry summary requires a final decision")
    attempts = []
    for trace in traces:
        payload = {
            "attempt": trace.attempt,
            "max_attempts": trace.max_attempts,
            "reason": trace.reason[:96],
            "status_code": trace.status_code,
            "delay_millis": _seconds_to_millis(trace.delay_seconds),
            "retry_after_millis": _seconds_to_millis(trace.retry_after_seconds),
            "retry_after_exceeded": trace.retry_after_exceeded,
        }
        attempts.append(ProviderRetryAttemptSummaryFact(**payload))
    payload = {
        "enabled": config.enabled,
        "final_attempt": final_attempt,
        "max_attempts": config.attempts if config.enabled else 1,
        "retry_count": len(attempts),
        "exhausted": exhausted,
        "has_semantic_output": has_semantic_output,
        "skipped_reason": skipped_reason[:96] if skipped_reason else None,
        "final_reason": final_decision.reason[:96],
        "final_status_code": final_decision.status_code,
        "retry_after_exceeded": final_decision.retry_after_exceeded,
        "attempts": tuple(attempts),
    }
    return ProviderRetrySummaryFact(**payload)


def provider_failure_code_hint(decision: LLMRetryDecision) -> str:
    if decision.status_code == 401:
        return "provider_authentication_401"
    if decision.status_code == 403:
        return "provider_permission_403"
    if decision.status_code == 429 or decision.provider_code in {
        "rate_limit_exceeded",
        "rate_limit_error",
    }:
        return "provider_rate_limit_429"
    if (
        decision.status_code == 408
        or decision.provider_code == "request_timeout"
        or "timeout" in decision.reason.casefold()
    ):
        return "provider_timeout"
    if decision.reason == "transient_provider_error" or (
        decision.status_code is not None and decision.status_code >= 500
    ):
        return "provider_overloaded"
    if decision.status_code is not None and 400 <= decision.status_code < 500:
        return "provider_invalid_request"
    if decision.reason in {
        "transport_error",
        "physical_close_unconfirmed",
        "completed_stream_close_failed",
    }:
        return "provider_transport_error"
    return "provider_unknown_error"


def _seconds_to_millis(value: float | None) -> int | None:
    if value is None or not math.isfinite(value) or value < 0 or value > 600:
        return None
    return round(value * 1_000)


def _safe_host(base_url: str | None) -> str | None:
    if not base_url:
        return None
    parsed = urlsplit(base_url)
    return parsed.hostname


class ProviderFailureResponseError(Exception):
    """A structured failure SSE terminal, before normalized consumer settlement."""

    def __init__(self, body):
        self.body = body
        super().__init__(json.dumps(body, ensure_ascii=False, default=str))


class PhysicalStreamCloseError(Exception):
    def __init__(self, primary: BaseException | None, close_error: BaseException):
        self.primary = primary
        self.close_error = close_error
        super().__init__(
            f"connection cleanup failed: {type(close_error).__name__}: {close_error}; "
            + (
                f"original {type(primary).__name__}: {primary}"
                if primary is not None
                else "provider result received"
            )
        )


@dataclass(slots=True)
class _SDKStreamClosure:
    closing: bool = False
    closed: bool = False


async def close_sdk_resource(resource, *, scrub, primary=None, closure=None):
    """Join the SDK's supported close operation before settling any result."""
    closure = closure if closure is not None else _SDKStreamClosure()
    closure.closing = True
    closer = getattr(resource, "close", None) or getattr(resource, "aclose", None)
    if closer is None:
        raise PhysicalStreamCloseError(
            primary, RuntimeError("physical close unavailable")
        ) from primary
    task = asyncio.create_task(
        provider_diagnostic_await(closer(), scrub), name="provider-sdk-close"
    )
    cancelled = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancelled = exc
        except BaseException:
            break
    try:
        task.result()
    except BaseException as close_error:
        if isinstance(primary, asyncio.CancelledError) or cancelled is not None:
            raise (
                primary if isinstance(primary, asyncio.CancelledError) else cancelled
            ) from close_error
        raise PhysicalStreamCloseError(primary, close_error) from primary
    closure.closed = True
    if cancelled is not None:
        raise cancelled


@asynccontextmanager
async def closed_sdk_stream(stream, *, scrub):
    """SDK owns close; join it before replacement, including cancellation races."""
    primary = None
    closure = _SDKStreamClosure()
    try:
        yield closure
    except BaseException as exc:
        primary = exc
        raise
    finally:
        await close_sdk_resource(stream, scrub=scrub, primary=primary, closure=closure)


def _hex_equivalent_pattern(value: str, *, percent: bool) -> bytes:
    """Vary only encoded hex digits; ordinary credential letters stay exact."""
    expression = r"%([0-9a-fA-F]{2})" if percent else r"u([0-9a-fA-F]{4})"
    result = []
    offset = 0
    for match in re.finditer(expression, value):
        start, end = match.span(1)
        result.append(re.escape(os.fsencode(value[offset:start])))
        for digit in value[start:end]:
            result.append(
                ("[" + digit.lower() + digit.upper() + "]").encode()
                if digit.isalpha()
                else digit.encode()
            )
        offset = end
    result.append(re.escape(os.fsencode(value[offset:])))
    return b"".join(result)


def _uri_secret_pattern(value: str, *, form_query: bool = False) -> bytes:
    # URI encoders may independently escape unreserved bytes too. Keep both
    # spellings for each exact UTF-8 byte without folding ordinary letter case.
    parts = []
    for byte in os.fsencode(value):
        alternatives = [
            re.escape(bytes((byte,))),
            _hex_equivalent_pattern("%%%02X" % byte, percent=True),
        ]
        if form_query and byte == 32:
            alternatives.append(rb"\+")
        if byte == 39:
            # A legal URI apostrophe may be escaped by the SDK body repr.
            alternatives.append(rb"\\'")
        parts.append(b"(?:" + b"|".join(alternatives) + b")")
    return b"".join(parts)


_AUTHENTICATION_URL = re.compile(r"https?://[^\s\"<>]+", re.IGNORECASE)


def _authentication_query_key(key: str) -> bool:
    return key.casefold() in {
        "api_key",
        "apikey",
        "x-api-key",
        "key",
        "access_token",
        "refresh_token",
        "token",
        "auth_token",
        "id_token",
        "client_secret",
        "password",
        "secret",
        "authorization",
        "credential",
        "credentials",
        "signature",
        "sig",
        "x-amz-credential",
        "x-amz-signature",
        "x-amz-security-token",
    }


def _python_string_body(value: str, delimiter: str) -> str:
    # SDK status errors embed their structured body using repr. Keep the two
    # possible quote delimiters without losing the secret's exact character case.
    return "".join(
        "\\" + char
        if char in {"\\", delimiter}
        else char
        if char in {"'", '"'}
        else repr(char)[1:-1]
        for char in value
    )


def _json_secret_pattern(value: str, encode: Callable[[str], str]) -> bytes:
    parts = []
    for char in value:
        rendered = json.dumps(char, ensure_ascii=False)[1:-1]
        unicode_form = (
            json.dumps(char, ensure_ascii=True)[1:-1]
            if ord(char) > 127
            else "\\u%04x" % ord(char)
        )
        # JSON permits an independent choice at each slash/unicode escape.
        # Construct a linear regex, rather than enumerating encoded combinations.
        choices = [(rendered, rendered.startswith("\\u")), (unicode_form, True)]
        if char == "/":
            choices.append(("\\/", False))
        alternatives = []
        for spelling, unicode_escape in choices:
            transformed = encode(spelling)
            pattern = (
                _hex_equivalent_pattern(transformed, percent=False)
                if unicode_escape or (ord(char) > 127 and "u" in transformed)
                else re.escape(os.fsencode(transformed))
            )
            if pattern not in alternatives:
                alternatives.append(pattern)
        parts.append(b"(?:" + b"|".join(alternatives) + b")")
    return b"".join(parts)


class _EncodedCredentialScrubSet(ProcessCredentialScrubSet):
    """The existing exact owner plus equivalent encodings of its captured values."""

    __slots__ = ("_encoded_patterns", "_form_query_patterns")

    def __init__(self, exact: ProcessCredentialScrubSet, captured: tuple[bytes, ...]):
        super().__init__()
        for value in exact.values:
            self.observe(value)
        patterns = []
        encoders = (
            lambda value: value,
            lambda value: json.dumps(value, ensure_ascii=False)[1:-1],
            lambda value: json.dumps(value, ensure_ascii=True)[1:-1],
            lambda value: _python_string_body(value, "'"),
            lambda value: _python_string_body(value, '"'),
        )
        for raw in captured:
            value = os.fsdecode(raw)
            patterns.append(re.compile(_uri_secret_pattern(value)))
            patterns.extend(
                re.compile(_json_secret_pattern(value, encode)) for encode in encoders
            )
        self._encoded_patterns = tuple(patterns)
        self._form_query_patterns = tuple(
            re.compile(_uri_secret_pattern(os.fsdecode(raw), form_query=True))
            for raw in captured
            if b" " in raw
        )

    def contains(self, raw: bytes | str) -> bool:
        encoded = os.fsencode(raw) if isinstance(raw, str) else raw
        return self.scrub_bytes(encoded) != encoded

    def _scrub_authentication_url(
        self, match: re.Match[str], replacement: bytes
    ) -> str:
        raw = match.group(0)
        try:
            query = urlsplit(raw).query
        except ValueError:
            return raw
        if not query:
            return raw
        segments = []
        for segment in query.split("&"):
            pair = parse_qsl(segment, keep_blank_values=True)
            raw_key, separator, value = segment.partition("=")
            if separator and len(pair) == 1 and _authentication_query_key(pair[0][0]):
                encoded = os.fsencode(value)
                for pattern in self._form_query_patterns:
                    quote_boundary = encoded.find(b"'")
                    # A captured credential can itself contain this legal URI
                    # character. Do not treat unrelated text after a closing
                    # single quote as another authentication value.
                    encoded = pattern.sub(
                        lambda match: (
                            replacement
                            if quote_boundary < 0 or match.start() <= quote_boundary
                            else match.group(0)
                        ),
                        encoded,
                    )

                value = os.fsdecode(encoded)
            segments.append(raw_key + separator + value)
        start = raw.index("?") + 1
        return raw[:start] + "&".join(segments) + raw[start + len(query) :]

    def scrub_bytes(self, raw: bytes) -> bytes:
        replacement = super().scrub_bytes(PROCESS_CREDENTIAL_REPLACEMENT)
        value = raw
        if self._form_query_patterns:
            # parse_qsl defines '+' as space only in query fields. Keep that
            # equivalence inside recognized authentication URL values, never
            # in ordinary prose, arithmetic, fragments or unrelated fields.
            value = os.fsencode(
                _AUTHENTICATION_URL.sub(
                    lambda match: self._scrub_authentication_url(match, replacement),
                    os.fsdecode(value),
                )
            )
        for pattern in self._encoded_patterns:
            value = pattern.sub(lambda _match: replacement, value)
        return super().scrub_bytes(value)


def capture_retry_scrubber(settings, credential_boundary, endpoint):
    """Capture actual credentials once, then reuse across all attempts."""
    scrub = settings.credential_scrub_set()
    for value in credential_boundary.capture_scrub_set().values:
        scrub.observe(value)
    return expand_provider_diagnostic_scrubber(scrub, endpoint)


def expand_provider_diagnostic_scrubber(scrub, endpoint):
    # httpx owns environment proxy selection. Capture only authentication from
    # those URLs, never use it as a model-credential fallback.
    for url in (endpoint, *getproxies().values()):
        if not isinstance(url, str):
            continue
        try:
            parsed = urlsplit(url or "")
            for value in (parsed.username, parsed.password):
                if value:
                    scrub.observe(value)
                    scrub.observe(unquote(value))
            if parsed.username is not None and parsed.password is not None:
                scrub.observe(
                    base64.b64encode(
                        f"{unquote(parsed.username)}:{unquote(parsed.password)}".encode()
                    ).decode()
                )
            for key, value in parse_qsl(parsed.query):
                if _authentication_query_key(key):
                    scrub.observe(value)
        except ValueError:
            pass
    captured = scrub.values
    for raw in captured:
        value = os.fsdecode(raw)
        variants = (
            value,
            quote(value, safe=""),
            re.sub(
                r"%[0-9A-F]{2}",
                lambda match: match.group().lower(),
                quote(value, safe=""),
            ),
            json.dumps(value, ensure_ascii=False)[1:-1],
            json.dumps(value, ensure_ascii=True)[1:-1],
        )
        for variant in variants:
            scrub.observe(variant)
            # SDK status messages embed the structured error using Python repr;
            # nested JSON argument/error strings cross that formatting boundary.
            scrub.observe(repr(variant)[1:-1])
            scrub.observe(json.dumps(variant, ensure_ascii=False)[1:-1])
            scrub.observe(json.dumps(variant, ensure_ascii=True)[1:-1])
    return _EncodedCredentialScrubSet(scrub, captured)


def replay_is_safe(*, api, payload) -> bool:
    """Prove the effective SDK body has only content/function generation.

    The inspected SDK extra_body merge is shallow with extension precedence.
    Unknown extensions are deliberately not granted replay authority.
    """
    if api not in {"openai_chat_completions", "openai_responses"}:
        return False
    effective = {key: value for key, value in payload.items() if key != "extra_body"}
    extra = payload.get("extra_body", {})
    if not isinstance(extra, dict):
        return False
    effective.update(extra)
    common = {
        "model",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "temperature",
        "top_p",
        "max_tokens",
        "max_output_tokens",
        "max_completion_tokens",
        "reasoning",
        "reasoning_effort",
        "thinking",
        "enable_thinking",
        "service_tier",
        "metadata",
        "user",
        "safety_identifier",
        "prompt_cache_key",
        "prompt_cache_retention",
        "seed",
        "stop",
        "frequency_penalty",
        "presence_penalty",
        "logit_bias",
        "logprobs",
        "top_logprobs",
        "verbosity",
        "timeout",
    }
    specific = (
        {"messages", "n", "stream_options", "response_format", "store"}
        if api == "openai_chat_completions"
        else {
            "input",
            "instructions",
            "store",
            "include",
            "text",
            "truncation",
            "background",
            "previous_response_id",
            "conversation",
        }
    )
    if set(effective) - common - specific:
        return False
    if (
        effective.get("background") not in (None, False)
        or effective.get("previous_response_id") is not None
        or effective.get("conversation") is not None
    ):
        return False
    tools = effective.get("tools", [])
    if not isinstance(tools, (list, tuple)) or any(
        not isinstance(tool, dict) or tool.get("type") != "function" for tool in tools
    ):
        return False
    for tool in tools:
        if api == "openai_chat_completions":
            function = tool.get("function")
            if set(tool) - {"type", "function"} or not isinstance(function, dict):
                return False
            if set(function) - {"name", "description", "parameters", "strict"}:
                return False
        elif set(tool) - {"type", "name", "description", "parameters", "strict"}:
            return False
    if effective.get("store") not in (None, False):
        return False
    for key, allowed in (
        ("reasoning", {"enabled", "effort", "summary"}),
        ("thinking", {"type"}),
    ):
        value = effective.get(key)
        if value is not None and (not isinstance(value, dict) or set(value) - allowed):
            return False
    choice = effective.get("tool_choice")
    if isinstance(choice, dict):
        allowed = (
            {"type", "function"}
            if api == "openai_chat_completions"
            else {"type", "name"}
        )
        if choice.get("type") != "function" or set(choice) - allowed:
            return False
    elif choice is not None and (
        not isinstance(choice, str) or choice not in {"auto", "none", "required"}
    ):
        return False
    return True


def exception_diagnostics(exc):
    result = []
    seen = set()
    pending = [exc]
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        body = getattr(item, "body", None)
        response = getattr(item, "response", None)
        result.append(
            {
                "type": type(item).__name__,
                "message": str(item),
                "body": body,
                "request_id": getattr(item, "request_id", None),
                "status": getattr(item, "status_code", None)
                or getattr(response, "status_code", None),
            }
        )
        if item.__context__ is not None:
            pending.append(item.__context__)
        if item.__cause__ is not None:
            pending.append(item.__cause__)
        if isinstance(item, PhysicalStreamCloseError):
            pending.append(item.close_error)
    return result


def provider_failure_message(exc: BaseException) -> str:
    """Keep the concrete cause alongside the provider wrapper in public detail."""
    messages = []
    for item in exception_diagnostics(exc):
        message = f"{item['type']}: {item['message']}"
        if message not in messages:
            messages.append(message)
    return "; ".join(messages)


def log_provider_decision(
    *,
    api,
    model,
    scrub,
    config,
    attempt,
    max_attempts,
    phase,
    exc,
    decision,
    skipped_reason,
    semantic,
    terminal,
    closed,
    delay=None,
    outcome="failed",
    close_scope=None,
    client_close_confirmed=None,
    original_classification=None,
):
    payload = {
        "api": api,
        "provider": model.route_id,
        "model": model.id,
        "host": _safe_host(model.base_url),
        "attempt": attempt,
        "max_attempts": max_attempts,
        "retry_count": attempt - 1,
        "phase": phase,
        "exception_chain": exception_diagnostics(exc) if exc else [],
        "classification": decision.reason if decision else None,
        "decision": outcome,
        "skipped_reason": skipped_reason,
        "has_semantic_output": semantic,
        "terminal_observed": terminal,
        "stream_closed": closed,
        "close_scope": close_scope,
        "client_close_confirmed": client_close_confirmed,
        "original_classification": original_classification,
        "retry_after_seconds": decision.retry_after_seconds if decision else None,
        "status_code": decision.status_code if decision else None,
        "provider_code": decision.provider_code if decision else None,
        "base_delay_seconds": min(
            config.base_delay_seconds * (2 ** (attempt - 1)), config.max_delay_seconds
        )
        if delay is not None
        else None,
        "delay_seconds": delay,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    # Bound diagnostics at their existing public-message size per value; mark
    # truncation explicitly and perform final-text exact credential scrub.
    for item in payload["exception_chain"]:
        for key in ("message", "body"):
            value = scrub.scrub_text(str(item[key])) if item[key] is not None else None
            item[key] = value
            limit = DEFAULT_PROVIDER_ERROR_SANITIZATION_CONTRACT.max_message_chars
            if value is not None and len(value) > limit:
                item[key] = value[:limit]
                item[key + "_truncated"] = True
    LOGGER.warning(
        "%s",
        scrub.scrub_text(
            "Provider request " + json.dumps(payload, ensure_ascii=False, default=str)
        ),
    )


def log_invalid_tool_arguments(*, scrub, attempt, arguments, terminal_chunk, exc):
    try:
        diagnostic = scrub.scrub_json(
            {
                "arguments": arguments,
                "terminal_chunk": terminal_chunk,
                "parse_error": str(exc.__cause__ or exc),
            }
        )
        limit = DEFAULT_PROVIDER_ERROR_SANITIZATION_CONTRACT.max_message_chars
        for key, value in diagnostic["arguments"].items():
            if len(value) > limit:
                diagnostic["arguments"][key] = value[:limit]
                diagnostic["arguments_truncated"] = True
        terminal_text = json.dumps(diagnostic["terminal_chunk"], ensure_ascii=False)
        if len(terminal_text) > limit:
            diagnostic["terminal_chunk"] = {
                "prefix": terminal_text[:limit],
                "truncated": True,
            }
        text = "Invalid tool arguments attempt=%s diagnostic=%s" % (
            attempt,
            json.dumps(diagnostic, ensure_ascii=False),
        )
    except Exception as diagnostic_error:
        # Diagnostics have no authority to alter a recovery decision.
        text = f"Invalid tool arguments attempt={attempt} diagnostic_unavailable={type(diagnostic_error).__name__}: {diagnostic_error}"
    LOGGER.warning("%s", scrub.scrub_text(text))


def retry_reason(decision):
    from pulsara_agent.ports.provider_stream import ProviderRetryReason

    if decision.reason == "tool_arguments_invalid":
        return ProviderRetryReason.TOOL_ARGUMENT_REGENERATION
    if decision.reason in {"transport_error", "transport_timeout"}:
        return ProviderRetryReason.CONNECTION_RECOVERY
    return ProviderRetryReason.PROVIDER_RECOVERY


def observe_retry(context, value):
    if context.retry_observer is not None:
        context.retry_observer(value)
