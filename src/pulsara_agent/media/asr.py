"""One OpenAI multipart protocol; Pulsara owns admission and timestamp truth.

The SDK owns encoding and networking. The public request hook expands only the
already frozen, admitted bytes so the existing credential boundary remains in
force. Unknown remote acceptance is never automatically retried.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field as dataclass_field
import math
from time import monotonic
from typing import Literal

from pulsara_agent.llm.adapters.openai.client import (
    OpenAITransportTimeoutPolicy,
    bounded_json_response_hook,
    build_async_openai_client,
    prepare_openai_sdk,
)
from pulsara_agent.media.asr_config import AsrConnection
from pulsara_agent.process_credential_boundary import (
    ProcessCredentialBoundary,
    ProcessCredentialScrubSet,
)


class AsrError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        remote_result_unknown=False,
        retry_after_seconds: float | None = None,
        retryable_before_send: bool = False,
    ):
        super().__init__(message)
        self.code = code
        self.remote_result_unknown = remote_result_unknown
        self.retry_after_seconds = retry_after_seconds
        self.retryable_before_send = retryable_before_send


@dataclass(frozen=True, slots=True)
class AsrTimestamp:
    start: float
    end: float
    text: str
    speaker: str | None = None


@dataclass(frozen=True, slots=True)
class AsrTranscript:
    text: str
    items: tuple[AsrTimestamp, ...]
    granularity: Literal["word", "segment"] | None
    language: str | None = None
    model: str | None = None
    diagnostics: tuple[str, ...] = ()


@dataclass(slots=True)
class AsrAttemptObservation:
    # Exact request input; partial network byte counts remain unknown.
    audio_bytes: int
    audio_seconds: float
    multipart_bytes: int | None = None
    send_started: bool = False
    body_started: bool = False
    body_completed: bool = False
    response_status: int | None = None
    response_validated: bool = False
    remote_result_unknown: bool = False
    confirmed_not_sent: bool = False
    confirmed_rate_limit: bool = False
    charge_info: dict | None = None


@dataclass(slots=True)
class AsrRequestObservation:
    """Call-local SDK facts, also available while cancellation is settling."""

    attempts: list[AsrAttemptObservation] = dataclass_field(default_factory=list)
    result: AsrTranscript | None = None

    @property
    def remote_result_unknown(self) -> bool:
        return any(
            attempt.remote_result_unknown
            or (
                attempt.send_started
                and not attempt.response_validated
                and (
                    attempt.response_status is None
                    or attempt.response_status < 400
                    or (attempt.response_status == 429 and not attempt.confirmed_rate_limit)
                    or attempt.response_status >= 500
                )
            )
            for attempt in self.attempts
        )

    def public_value(self) -> dict:
        return {
            "attempts": [
                {
                    **asdict(attempt),
                    "uploaded_audio_bytes": attempt.audio_bytes
                    if attempt.body_completed
                    else 0
                    if attempt.confirmed_not_sent
                    else None,
                    "uploaded_audio_seconds": attempt.audio_seconds
                    if attempt.body_completed
                    else 0
                    if attempt.confirmed_not_sent
                    else None,
                    "uploaded_multipart_bytes": attempt.multipart_bytes
                    if attempt.body_completed
                    else 0
                    if attempt.confirmed_not_sent
                    else None,
                }
                for attempt in self.attempts
            ]
        }


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def validate_transcription(value, *, duration: float) -> AsrTranscript:
    if not _number(duration) or duration <= 0:
        raise ValueError("uploaded audio duration must be finite and positive")
    if not isinstance(value, dict) or not isinstance(value.get("text"), str):
        raise AsrError("ASR_RESPONSE_INVALID", "转写响应缺少标准 text 字段。")
    diagnostics = []
    any_invalid = False
    for field, text_field, granularity in (
        ("words", "word", "word"),
        ("segments", "text", "segment"),
    ):
        rows = value.get(field)
        if rows is None and field not in value:
            continue
        try:
            if not isinstance(rows, list):
                raise ValueError("必须为数组")
            result = []
            previous = -1.0
            for row in rows:
                if (
                    not isinstance(row, dict)
                    or not isinstance(row.get(text_field), str)
                    or not row[text_field].strip()
                ):
                    raise ValueError("条目缺少非空原文")
                start, end = row.get("start"), row.get("end")
                if (
                    not _number(start)
                    or not _number(end)
                    or not 0 <= start < end <= duration
                    or start < previous
                ):
                    raise ValueError("条目时间必须为有限秒数、按序且处于上传音频范围内")
                speaker = row.get("speaker")
                if speaker is not None and not isinstance(speaker, str):
                    raise ValueError("speaker 必须为文字")
                result.append(
                    AsrTimestamp(float(start), float(end), row[text_field], speaker)
                )
                previous = start
            if result:
                if not value["text"].strip():
                    raise ValueError("时间条目非空但顶层原文为空")
                return AsrTranscript(
                    value["text"],
                    tuple(result),
                    granularity,
                    value.get("language")
                    if isinstance(value.get("language"), str)
                    else None,
                    value.get("model") if isinstance(value.get("model"), str) else None,
                    tuple(diagnostics),
                )
        except ValueError as exc:
            any_invalid = True
            diagnostics.append(f"{field}: {exc}")
    if value["text"].strip() or any_invalid:
        raise AsrError(
            "ASR_RESPONSE_INVALID",
            "转写响应没有可校验的时间戳。" + "; ".join(diagnostics),
        )
    return AsrTranscript(
        value["text"],
        (),
        None,
        value.get("language") if isinstance(value.get("language"), str) else None,
        value.get("model") if isinstance(value.get("model"), str) else None,
    )


def bounded_multipart_request_hook(*, audio: bytes, maximum_bytes: int):
    """Expand SDK multipart only after length and frozen-input admission."""
    if not isinstance(audio, bytes) or not audio or len(audio) > maximum_bytes:
        raise AsrError("ASR_RESOURCE_BOUNDARY", "音频超过本次上传准入。")

    async def inspect_request(request):
        try:
            raw_length = request.headers.get("content-length")
            if (
                raw_length is None
                or not raw_length.isascii()
                or not raw_length.isdigit()
            ):
                raise AsrError(
                    "ASR_RESOURCE_BOUNDARY", "未知 multipart 长度，未发送音频。"
                )
            length = int(raw_length)
            if not len(audio) <= length <= maximum_bytes:
                raise AsrError(
                    "ASR_RESOURCE_BOUNDARY",
                    "multipart 超过本次内存/上传准入，未发送音频。",
                )
            body = await request.aread()
            if len(body) != length or audio not in body:
                raise AsrError(
                    "ASR_RESOURCE_BOUNDARY",
                    "multipart 与已准入音频不一致，未发送音频。",
                )
        except BaseException:
            await request.stream.aclose()
            raise

    return inspect_request


class AsrClient:
    """User-level owner: unknown service quota admits one request at a time (§7.1)."""

    def __init__(self):
        self._admission = asyncio.Lock()

    async def transcribe_audio(self, config, audio, **kwargs):
        async with self._admission:
            return await self._retry_transcribe_admitted(config, audio, **kwargs)

    async def prepare_and_transcribe(self, config, prepare_audio, **kwargs):
        """Reserve physical quota before producing bounded encoded audio (§7.1)."""
        if config is None:
            raise AsrError("ASR_NOT_CONFIGURED", "请先在设置中配置语音转写服务。")
        async with self._admission:
            audio = await prepare_audio()
            result = await self._retry_transcribe_admitted(
                config,
                audio.data,
                duration=audio.end - audio.start,
                filename=audio.filename,
                mime_type=audio.mime_type,
                **kwargs,
            )
            return audio, result

    async def _retry_transcribe_admitted(
        self, config, audio, *, deadline_monotonic=None, **kwargs
    ):
        from pulsara_agent.llm.retry import LLMRetryConfig, compute_retry_delay

        policy = LLMRetryConfig()
        # The existing delay saturates here; this bounds arithmetic, never the
        # number of logical attempts. Only the caller's watchdog ends retries.
        saturation = max(
            1,
            math.ceil(math.log2(policy.max_delay_seconds / policy.base_delay_seconds))
            + 1,
        )
        attempt = 1
        while True:
            if deadline_monotonic is not None and monotonic() >= deadline_monotonic:
                raise AsrError("ASR_FAILED", "本次处理期限已到，未发送音频。")
            try:
                return await self._transcribe_admitted(
                    config, audio, deadline_monotonic=deadline_monotonic, **kwargs
                )
            except AsrError as exc:
                observation = kwargs.get("observation")
                if observation is not None and observation.attempts:
                    observed = observation.attempts[-1]
                    observed.remote_result_unknown = exc.remote_result_unknown
                    observed.confirmed_not_sent = exc.retryable_before_send or (
                        not observed.send_started
                        and observed.response_status is None
                        and not exc.remote_result_unknown
                    )
                rejected = exc.code == "ASR_RATE_LIMITED" and not exc.remote_result_unknown
                if deadline_monotonic is None or not (exc.retryable_before_send or rejected):
                    raise
                delay = compute_retry_delay(
                    attempt_index=min(attempt, saturation),
                    config=policy,
                    retry_after_seconds=None,
                )
                if exc.retry_after_seconds is not None:
                    delay = max(delay, exc.retry_after_seconds)
                if monotonic() + delay >= deadline_monotonic:
                    raise
                await asyncio.sleep(delay)
                attempt += 1

    async def _transcribe_admitted(
        self,
        config: AsrConnection | None,
        audio: bytes,
        *,
        duration: float,
        maximum_upload_bytes: int,
        maximum_response_bytes: int,
        filename="audio.wav",
        mime_type="audio/wav",
        credential_scrub_set: ProcessCredentialScrubSet | None = None,
        deadline_monotonic: float | None = None,
        observation: AsrRequestObservation | None = None,
    ) -> AsrTranscript:
        if config is None:
            raise AsrError("ASR_NOT_CONFIGURED", "请先在设置中配置语音转写服务。")
        if (
            type(maximum_upload_bytes) is not int
            or type(maximum_response_bytes) is not int
            or maximum_response_bytes <= 0
        ):
            raise ValueError("ASR requires explicit positive physical byte admission")
        if not _number(duration) or duration <= 0:
            raise ValueError("ASR requires actual positive upload duration")
        hook = bounded_multipart_request_hook(
            audio=audio, maximum_bytes=maximum_upload_bytes
        )
        observation = (
            observation if observation is not None else AsrRequestObservation()
        )
        attempt = AsrAttemptObservation(len(audio), duration)
        observation.attempts.append(attempt)
        scrub = ProcessCredentialScrubSet()
        if credential_scrub_set is not None:
            for secret in credential_scrub_set.values:
                scrub.observe(secret)
        scrub.observe(config.api_key)
        state = {"sent": False, "response": False}

        async def trace(name, _info):
            if name.endswith("send_request_headers.started"):
                state["sent"] = True
                attempt.send_started = True
            if name.endswith("send_request_body.started"):
                attempt.body_started = True
            if name.endswith("send_request_body.complete"):
                attempt.body_completed = True

        async def prepare_request(request):
            await hook(request)
            attempt.multipart_bytes = len(request.content)
            if (
                scrub.contains(str(request.url))
                or scrub.contains(request.content)
                or any(
                    scrub.contains(name)
                    or (name.lower() != b"authorization" and scrub.contains(value))
                    for name, value in request.headers.raw
                )
            ):
                raise AsrError("ASR_FAILED", "HTTP 请求包含受保护凭据，未发送音频。")
            request.extensions["trace"] = trace

        inspect_response = bounded_json_response_hook(maximum_response_bytes)

        async def response_hook(response):
            state["response"] = True
            attempt.response_status = response.status_code
            await inspect_response(response)

        # Freeze one config before queueing. Cancellation of a waiter sends nothing.
        await prepare_openai_sdk()
        boundary = ProcessCredentialBoundary(config.api_key)
        client = build_async_openai_client(
            api_key=config.api_key,
            base_url=config.endpoint,
            credential_boundary=boundary,
            max_retries=0,
            timeout_policy=(
                OpenAITransportTimeoutPolicy(10, 60, 10, 120, None)
                if deadline_monotonic is None
                else OpenAITransportTimeoutPolicy(10, 60, 10, 120, None).bounded_by(
                    deadline_monotonic - monotonic()
                )
            ),
            request_hooks=[prepare_request],
            response_hooks=[response_hook],
        )
        try:
            # Public SDK POST accepts the absolute endpoint without adding a path.
            response = await client.post(
                config.endpoint,
                cast_to=dict,
                body={
                    "model": config.model_id,
                    "response_format": "verbose_json",
                    "timestamp_granularities": ["word", "segment"],
                },
                files={"file": (filename, audio, mime_type)},
                options={"headers": {"Content-Type": "multipart/form-data"}},
            )
            result = validate_transcription(
                scrub.scrub_json(response), duration=duration
            )
            attempt.response_validated = True
            observation.result = result
            return result
        except asyncio.CancelledError:
            attempt.confirmed_not_sent = (
                not attempt.send_started and attempt.response_status is None
            )
            raise
        except AsrError as exc:
            attempt.remote_result_unknown = exc.remote_result_unknown
            raise
        except Exception as exc:
            # HTTP status alone never establishes a no-charge retry contract.
            from openai import APIConnectionError, APIStatusError

            detail = scrub.scrub_text(str(exc))
            causes = []
            cause = exc
            while cause is not None and cause not in causes:
                causes.append(cause)
                cause = cause.__cause__
            local_error = next(
                (cause for cause in causes if isinstance(cause, AsrError)), None
            )
            if local_error is not None:
                raise local_error from None
            if state["response"] and not isinstance(exc, APIStatusError):
                raise AsrError("ASR_RESPONSE_INVALID", detail) from None
            if isinstance(exc, APIStatusError):
                status = exc.status_code
                code = (
                    "ASR_AUTH_FAILED"
                    if status in (401, 403)
                    else "ASR_RATE_LIMITED"
                    if status == 429
                    else "ASR_SERVICE_UNAVAILABLE"
                    if status >= 500
                    else "ASR_FAILED"
                )
                # OpenRouter documents failed transcriptions as unbilled. Apply
                # its retry contract only to an explicit rate-limit error from
                # the official endpoint, never to generic compatible services.
                attempt.confirmed_rate_limit = (
                    status == 429
                    and str(exc.response.request.url) == "https://openrouter.ai/api/v1/audio/transcriptions"
                    and isinstance(exc.body, dict)
                    and exc.body.get("code") == 429
                    and isinstance(exc.body.get("message"), str)
                )
                unknown = (status == 429 and not attempt.confirmed_rate_limit) or status >= 500
                from pulsara_agent.llm.adapters.openai.errors import parse_retry_after_seconds

                retry_after = exc.response.headers.get("retry-after")
                retry_after = scrub.scrub_text(retry_after) if retry_after else None
                if retry_after:
                    detail += " Retry-After: " + scrub.scrub_text(retry_after)
                if unknown:
                    detail = "远端是否受理无法确认，可能已经计费；未自动重试。" + detail
                raise AsrError(
                    code,
                    detail,
                    remote_result_unknown=unknown,
                    retry_after_seconds=parse_retry_after_seconds(headers=exc.response.headers),
                ) from None
            if isinstance(exc, APIConnectionError):
                import httpx2

                confirmed_unconnected = any(
                    isinstance(cause, (httpx2.ConnectError, httpx2.ConnectTimeout))
                    for cause in causes
                )
                unknown = state["sent"] or not confirmed_unconnected
                if unknown:
                    detail = "远端处理结果未知，可能已经计费；未自动重试。" + detail
                else:
                    detail = "请求尚未发送。" + detail
                raise AsrError(
                    "ASR_FAILED" if unknown else "ASR_SERVICE_UNAVAILABLE",
                    detail,
                    remote_result_unknown=unknown,
                    retryable_before_send=confirmed_unconnected and not state["sent"],
                ) from None
            raise AsrError(
                "ASR_FAILED", detail, remote_result_unknown=state["sent"]
            ) from None
        finally:
            closing = asyncio.create_task(client.close(), name="close-asr-client")
            cancellation = None
            while not closing.done():
                try:
                    await asyncio.shield(closing)
                except asyncio.CancelledError as exc:
                    cancellation = cancellation or exc
            closing.result()
            if cancellation is not None:
                raise cancellation


# One process-local quota owner shared by transcript requests and explicit tests.
asr_client = AsrClient()
transcribe_audio = asr_client.transcribe_audio
prepare_and_transcribe = asr_client.prepare_and_transcribe
