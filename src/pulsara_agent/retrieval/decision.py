"""Shared explicit Decision questions, SDK wire adaptation and request ownership."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
import json
import math
from time import monotonic

from pulsara_agent.llm.adapters.openai.client import (
    admit_provider_request,
    openai_auth_request_options,
    prepare_openai_sdk,
)
from pulsara_agent.llm.adapters.openai.retrying import (
    close_sdk_resource,
    expand_provider_diagnostic_scrubber,
    provider_diagnostic_await,
)
from pulsara_agent.retrieval.config import DecisionBackendConfig, RetrievalConnection
from pulsara_agent.retrieval.http import sdk_client


class DecisionError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        input_index: int | None = None,
        limit: int | None = None,
    ):
        self.code = code
        self.input_index = input_index
        self.limit = limit
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class DecisionQuestion:
    name: str
    instructions: str
    true_criteria: str
    false_criteria: str

    def __post_init__(self):
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (
                self.name,
                self.instructions,
                self.true_criteria,
                self.false_criteria,
            )
        ):
            raise ValueError("Decision question fields must be nonempty strings")


@dataclass(frozen=True, slots=True)
class DecisionRequest:
    state: object
    questions: tuple[DecisionQuestion, ...]


@dataclass(frozen=True, slots=True)
class PreparedDecisionBatch:
    connection: RetrievalConnection = field(repr=False)
    payload_bytes: bytes = field(repr=False)
    names: tuple[str, ...]
    input_index: int


def _encode(connection: RetrievalConnection, request: DecisionRequest) -> bytes:
    names = [question.name for question in request.questions]
    if len(set(names)) != len(names):
        raise ValueError("Decision question names must be unique")
    payload = {"model": connection.model_id}
    if connection.shape == "system_one":
        payload.update(
            state=request.state,
            questions={
                question.name: {
                    "type": "noul",
                    "instructions": question.instructions,
                    "criteria": {
                        "true": question.true_criteria,
                        "false": question.false_criteria,
                    },
                }
                for question in request.questions
            },
        )
    else:
        payload.update(
            input=json.dumps(
                request.state,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ),
            questions=[
                {
                    "type": "predicate",
                    "name": question.name,
                    "instructions": question.instructions,
                }
                for question in request.questions
            ],
        )
    # httpx2's supported JSON encoder uses this exact UTF-8 representation.
    # Decisions input is itself JSON text, so its second escaping is counted.
    return json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _probabilities(decoded: object, batch: PreparedDecisionBatch) -> tuple[float, ...]:
    try:
        if not isinstance(decoded, dict):
            raise ValueError("expected a response object")
        answers = decoded["answers"]
        if batch.connection.shape == "system_one":
            if not isinstance(answers, dict) or set(answers) != set(batch.names):
                raise ValueError("answers must cover each requested name exactly once")
            ordered = [answers[name] for name in batch.names]
            expected_type, value_key = "noul", "noul"
        else:
            if not isinstance(answers, list) or any(
                not isinstance(answer, dict) for answer in answers
            ):
                raise ValueError("expected named answers array")
            names = [answer.get("name") for answer in answers]
            if (
                any(not isinstance(name, str) for name in names)
                or len(set(names)) != len(names)
                or set(names) != set(batch.names)
            ):
                raise ValueError("answers must cover each requested name exactly once")
            by_name = dict(zip(names, answers, strict=True))
            ordered = [by_name[name] for name in batch.names]
            expected_type, value_key = "predicate", "probability"
        result = []
        for answer in ordered:
            if not isinstance(answer, dict):
                raise ValueError("expected answer object")
            if answer.get("type") == "refusal":
                raise DecisionError(
                    "DECISION_REFUSED", "Decision provider refused a requested judgment"
                )
            if answer.get("type") != expected_type:
                raise ValueError(f"expected {expected_type} answer")
            value = answer[value_key]
            if (
                type(value) not in (int, float)
                or not 0 <= value <= 1
                or not math.isfinite(value)
            ):
                raise ValueError("probability must be a finite JSON number in 0..1")
            result.append(float(value))
        return tuple(result)
    except (KeyError, TypeError, ValueError) as exc:
        raise DecisionError("DECISION_RESPONSE_INVALID", str(exc)) from None


async def _join(task: asyncio.Task, *, cancel_on_waiter: bool):
    """Keep this service's admitted request/close task owned until it exits."""
    waiter = asyncio.current_task()
    baseline = 0 if waiter is None else waiter.cancelling()
    cancellation = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            if (
                waiter is not None and waiter.cancelling() > baseline
            ) or not task.cancelled():
                if cancellation is None and cancel_on_waiter:
                    task.cancel()
                cancellation = cancellation or exc
        except BaseException:
            break
    try:
        result = task.result()
    except BaseException as exc:
        if cancellation is not None:
            raise cancellation from exc
        raise
    if cancellation is not None:
        raise cancellation
    return result


class DecisionService:
    """One session's gate and active SDK calls, without memory/tool policy."""

    def __init__(self, *, config: DecisionBackendConfig = DecisionBackendConfig()):
        self.config = config
        self._gate = asyncio.Semaphore(config.max_concurrent)
        self._closed = False
        self._active: set[asyncio.Task] = set()
        self._close_task: asyncio.Task | None = None
        self._physical_close_failure: DecisionError | None = None

    @property
    def physical_close_complete(self) -> bool:
        return (
            self._closed
            and self._close_task is not None
            and self._close_task.done()
            and not self._active
            and self._physical_close_failure is None
        )

    def stop_admission(self) -> None:
        self._closed = True

    def prepare_batches(
        self,
        connection: RetrievalConnection,
        *,
        item_count: int,
        build_request: Callable[[int, int], DecisionRequest],
    ) -> tuple[PreparedDecisionBatch, ...]:
        if self._closed:
            raise DecisionError("DECISION_UNAVAILABLE", "Decision service is closing")
        if (
            connection.shape not in {"system_one", "openai_decisions"}
            or not connection.complete
        ):
            raise DecisionError(
                "DECISION_UNAVAILABLE",
                "Decision connection or authentication is incomplete",
            )
        if type(item_count) is not int or item_count < 0:
            raise ValueError("Decision item_count must be a nonnegative integer")

        def prepare(start, end):
            request = build_request(start, end)
            if len(request.questions) != end - start:
                raise ValueError("Decision question count differs from its input slice")
            return PreparedDecisionBatch(
                connection,
                _encode(connection, request),
                tuple(question.name for question in request.questions),
                start,
            )

        # Preflight every single item before any accepted network work. No
        # logical-array cap or truncation is introduced by the physical bounds.
        for index in range(item_count):
            batch = prepare(index, index + 1)
            if len(batch.payload_bytes) > self.config.maximum_request_bytes:
                raise DecisionError(
                    "DECISION_RESOURCE_BOUNDARY",
                    f"input_index {index} exceeds the {self.config.maximum_request_bytes}-byte request boundary",
                    input_index=index,
                    limit=self.config.maximum_request_bytes,
                )
        batches = []
        start = 0
        while start < item_count:
            selected = prepare(start, start + 1)
            end = start + 1
            while end < min(item_count, start + self.config.batch_size):
                candidate = prepare(start, end + 1)
                if len(candidate.payload_bytes) > self.config.maximum_request_bytes:
                    break
                selected = candidate
                end += 1
            batches.append(selected)
            start = end
        return tuple(batches)

    async def evaluate(
        self,
        batches: Sequence[PreparedDecisionBatch],
        *,
        deadline_monotonic: float | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> tuple[float, ...]:
        # Deliver pending caller cancellation before creating network work;
        # historical, already-handled cancellation counts remain harmless.
        await asyncio.sleep(0)
        if self._closed:
            raise DecisionError("DECISION_UNAVAILABLE", "Decision service is closing")
        if cancel_event is not None and cancel_event.is_set():
            raise asyncio.CancelledError
        task = asyncio.create_task(
            self._evaluate(tuple(batches), deadline_monotonic, cancel_event),
            name="decision-request",
        )
        self._active.add(task)
        task.add_done_callback(self._active.discard)
        watcher = None
        if cancel_event is not None:

            async def watch():
                await cancel_event.wait()
                if not task.done():
                    task.cancel()

            watcher = asyncio.create_task(watch(), name="decision-cell-stop")
        primary = None
        result = None
        try:
            result = await _join(task, cancel_on_waiter=True)
        except BaseException as exc:
            primary = exc
        if watcher is not None:
            watcher.cancel()
            try:
                await _join(
                    asyncio.gather(watcher, return_exceptions=True),
                    cancel_on_waiter=False,
                )
            except BaseException as exc:
                if primary is None:
                    primary = exc
        if primary is not None:
            raise primary
        if self._closed or (cancel_event is not None and cancel_event.is_set()):
            raise asyncio.CancelledError
        if deadline_monotonic is not None and monotonic() >= deadline_monotonic:
            raise TimeoutError("Decision invocation deadline expired")
        return result

    async def _evaluate(self, batches, deadline, cancel_event):
        probabilities = []
        async with asyncio.timeout_at(deadline):
            for batch in batches:
                async with self._gate:
                    if self._closed or (
                        cancel_event is not None and cancel_event.is_set()
                    ):
                        raise asyncio.CancelledError
                    probabilities.extend(await self._request(batch, deadline))
        return tuple(probabilities)

    async def _request(self, batch, deadline):
        await prepare_openai_sdk()
        import openai

        remaining = (
            self.config.timeout_seconds if deadline is None else deadline - monotonic()
        )
        if remaining <= 0:
            raise TimeoutError("Decision invocation deadline expired")
        timeout = min(self.config.timeout_seconds, remaining)
        client, boundary = sdk_client(
            batch.connection,
            timeout_seconds=timeout,
            maximum_response_bytes=self.config.maximum_response_bytes,
        )
        scrub = expand_provider_diagnostic_scrubber(
            boundary.capture_scrub_set(), batch.connection.endpoint
        )
        primary = None
        decoded = None
        try:
            payload = json.loads(batch.payload_bytes)
            options = openai_auth_request_options(
                requires_api_key=batch.connection.authentication != "none"
            )
            async with asyncio.timeout(timeout):
                if batch.connection.shape == "openai_decisions":
                    # The SDK's typed Decision model coerces bool/string into
                    # float. Its supported raw response preserves JSON types;
                    # the existing bounded hook already reads/validates once.
                    response = await provider_diagnostic_await(
                        admit_provider_request(
                            credential_boundary=boundary,
                            payload=payload,
                            operation=lambda: client.decisions.with_raw_response.create(
                                **payload, **options
                            ),
                        ),
                        scrub,
                    )
                    decoded = response.http_response.json()
                else:
                    decoded = await provider_diagnostic_await(
                        admit_provider_request(
                            credential_boundary=boundary,
                            payload=payload,
                            operation=lambda: client.post(
                                batch.connection.endpoint,
                                cast_to=dict,
                                body=payload,
                                options={"headers": options.get("extra_headers", {})},
                            ),
                        ),
                        scrub,
                    )
        except asyncio.CancelledError as exc:
            primary = exc
        except TimeoutError as exc:
            primary = (
                exc
                if deadline is not None and monotonic() >= deadline
                else DecisionError(
                    "DECISION_REQUEST_FAILED", "Decision provider request timed out"
                )
            )
        except ValueError as exc:
            primary = DecisionError(
                "DECISION_RESPONSE_INVALID", scrub.scrub_text(str(exc))
            )
        except openai.OpenAIError as exc:
            primary = DecisionError(
                "DECISION_REQUEST_FAILED", scrub.scrub_text(str(exc))
            )
        except BaseException as exc:
            primary = exc
        finally:
            from types import SimpleNamespace

            closure = SimpleNamespace(closing=False, closed=False)
            try:
                await close_sdk_resource(
                    client, scrub=scrub, primary=primary, closure=closure
                )
            except BaseException as exc:
                if not closure.closed:
                    # An unconfirmed physical close cannot release capacity for
                    # another request. Seal admission before leaving the gate.
                    self.stop_admission()
                    self._physical_close_failure = DecisionError(
                        "DECISION_REQUEST_FAILED",
                        scrub.scrub_text(f"Decision client close failed: {exc}"),
                    )
                if primary is None:
                    primary = (
                        exc
                        if isinstance(exc, asyncio.CancelledError)
                        else self._physical_close_failure
                    )
                elif exc is not primary:
                    primary.add_note(
                        scrub.scrub_text(f"Decision cleanup failure: {exc}")
                    )
        if primary is not None:
            raise primary
        return _probabilities(decoded, batch)

    async def aclose(self, *, deadline_monotonic: float | None = None) -> None:
        self.stop_admission()
        if self._close_task is None:
            self._close_task = asyncio.create_task(
                self._close(), name="decision-service-close"
            )
        await _join(self._close_task, cancel_on_waiter=False)
        if deadline_monotonic is not None and monotonic() >= deadline_monotonic:
            raise TimeoutError("Decision service closed after its close deadline")

    async def _close(self):
        tasks = tuple(self._active)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._physical_close_failure is not None:
            raise self._physical_close_failure
