"""User-level, process-local web candidate selection and business settlement."""
from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from pulsara_agent.process_credential_boundary import (
    ProcessCredentialBoundary,
    ProcessCredentialBoundAsyncClient,
    ProcessCredentialScrubSet,
)
from pulsara_agent.settings import LocalSettingsStore, LocalSettingsUnavailable
from pulsara_agent.web.config import resolve_web_connection
from pulsara_agent.web.providers import (
    CandidateUnsupported,
    ProviderResult,
    error_reason,
    normalize,
    request_for,
    validate_fetch,
    validate_search,
)


class WebService:
    """One owner shared by native/JS/root/subagent operations, independent of UI.

    httpx owns HTTP, cancellation and connection pooling. Pulsara supplies fixed
    official endpoints, explicit per-request authentication, normalization and
    one forward traversal of the admitted candidates. No SDK environment-key
    discovery or client default authentication participates in free requests.
    """

    def __init__(
        self,
        settings: LocalSettingsStore,
        client: httpx.AsyncClient | None = None,
        *,
        credential_boundary: ProcessCredentialBoundary | None = None,
    ) -> None:
        self._settings = settings
        self._boundary = credential_boundary
        self._owns_client = client is None
        if client is None:
            if credential_boundary is None:
                client = httpx.AsyncClient(timeout=None)
            else:
                client = ProcessCredentialBoundAsyncClient(
                    credential_boundary=credential_boundary, timeout=None)
        elif credential_boundary is not None and (
            not isinstance(client, ProcessCredentialBoundAsyncClient)
            or client._credential_boundary is not credential_boundary
        ):
            raise ValueError("an injected web client must share the process credential boundary")
        self._client = client
        # Exact credential value is a private owner key, never a hash or result.
        # Operations remain separate: a fetch target failure cannot block search.
        self._available_after: dict[tuple[str, str | None, str], float] = {}

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def execute(
        self,
        tool_name: str,
        arguments: Mapping[str, object],
        *,
        deadline_monotonic: float | None = None,
    ) -> dict:
        target = None
        try:
            if tool_name == "web_search":
                request = validate_search(arguments)
            elif tool_name == "web_fetch":
                request = validate_fetch(arguments)
                target = request.target
            else:
                raise ValueError("unsupported web tool")
        except ValueError as error:
            return _business("ERROR", (), target=target, error=str(error))

        scrub = ProcessCredentialScrubSet()
        settings_error = None
        try:
            # One immutable settings/credential cut, before the first network await.
            snapshot = self._settings.read()
            selected = resolve_web_connection(snapshot)
            for secret in snapshot.credential_scrub_set().values:
                scrub.observe(secret)
        except (LocalSettingsUnavailable, OSError, ValueError) as error:
            settings_error = f"web settings unavailable ({type(error).__name__})"
        if self._boundary is not None:
            async with self._boundary.async_guard(deadline_monotonic=deadline_monotonic) as guard:
                scrub.observe(guard.value)
        if settings_error is not None:
            return scrub.scrub_json(_business("ERROR", (), target=target, error=settings_error))
        if selected is not None:
            scrub.observe(selected[1])
        candidates = ([] if selected is None else [selected]) + [("firecrawl", None), ("tavily", None)]
        failures = []
        partial = None
        now = time.monotonic()
        self._available_after = {key: until for key, until in self._available_after.items() if until > now}
        for provider, api_key in candidates:
            label = provider + (" account" if api_key is not None else " keyless")
            scope = (provider, api_key, tool_name)
            if self._available_after.get(scope, 0) > time.monotonic():
                failures.append(label + ": Retry-After is still active")
                continue
            try:
                url, body = request_for(provider, request)
            except CandidateUnsupported as error:
                failures.append(label + ": " + str(error))
                continue
            remaining = None if deadline_monotonic is None else deadline_monotonic - time.monotonic()
            if remaining is not None and remaining <= 0:
                failures.append("web operation deadline expired")
                break
            headers = {"Accept": "application/json"}
            if api_key is not None:
                headers["x-api-key" if provider == "exa" else "Authorization"] = (
                    api_key if provider == "exa" else "Bearer " + api_key)
            elif provider == "tavily":
                headers["X-Tavily-Access-Mode"] = "keyless"
            # send(Request, auth=None) avoids all default client headers, cookies,
            # netrc/default auth and SDK environment credentials. No redirects:
            # API authentication must never travel to another origin.
            try:
                http_request = httpx.Request("POST", url, json=body, headers=headers,
                                             extensions={"timeout": httpx.Timeout(remaining).as_dict()})
                response = await self._client.send(http_request, auth=None, follow_redirects=False)
            except asyncio.CancelledError as error:
                known = list(failures) + [label + ": request cancelled before a complete result was accepted"]
                error.result = scrub.scrub_json(_business(
                    "CANCELLED", () if partial is None else partial.content,
                    target=target, error="\n".join(known)))
                raise
            except (httpx.HTTPError, ValueError, UnicodeError) as error:
                failures.append(label + ": " + type(error).__name__ + ": " + str(error))
                continue
            try:
                payload = response.json()
            except ValueError:
                payload = None
                attempt = ProviderResult(error="invalid JSON response: " + response.text)
            else:
                attempt = normalize(provider, request, payload)
            if not response.is_success:
                detail = f"API HTTP {response.status_code}"
                if attempt.error:
                    detail += ": " + attempt.error
                # Keep actual error fields, not another copy of raw provider
                # JSON containing bodies/records from unselected candidates.
                if isinstance(payload, dict):
                    for field in ("message", "detail", "code", "tag"):
                        if payload.get(field) is not None:
                            detail += f"; {field}: " + error_reason(payload[field], provider=provider)
                attempt = ProviderResult(attempt.content, attempt.title, detail)
                if response.status_code in {429, 503}:
                    wait = _retry_after(response.headers.get("Retry-After"))
                    if wait is not None:
                        self._available_after[scope] = time.monotonic() + wait
            if attempt.error is None:
                value = _business("SUCCESS", attempt.content, target=target, title=attempt.title)
                return scrub.scrub_json(value)
            if partial is None and attempt.content:
                partial = attempt
            reason = attempt.error
            if attempt.title:
                reason += " (page title: " + attempt.title + ")"
            failures.append(label + ": " + reason)
        value = _business("ERROR", () if partial is None else partial.content,
                          target=target, error="\n".join(failures))
        return scrub.scrub_json(value)


def _business(status: str, content: tuple[dict, ...], *, target=None, title=None, error=None) -> dict:
    result = {"meta": {"status": status}, "content": list(content)}
    if target is not None:
        result["target"] = target
    if status == "SUCCESS" and title:
        result["meta"]["title"] = title
    if error:
        result["content"].append({"type": "text", "value": error})
    return result


def _retry_after(raw: str | None) -> float | None:
    if raw is None:
        return None
    raw = raw.strip()
    if raw.isascii() and raw.isdecimal():
        wait = float(raw)
        return wait if math.isfinite(wait) else None
    try:
        until = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if until.tzinfo is None:
        return None
    return max(0.0, (until - datetime.now(timezone.utc)).total_seconds())
