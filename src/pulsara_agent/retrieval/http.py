"""SDK-owned HTTP with Pulsara's credential and response resource boundaries."""

import json
import httpx2

from pulsara_agent.llm.adapters.openai.client import (
    OpenAITransportTimeoutPolicy,
    build_async_openai_client,
)
from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary


def reject_duplicate_keys(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("provider response contains duplicate JSON keys")
        result[name] = value
    return result


class BoundedJSONStream(httpx2.AsyncByteStream):
    def __init__(self, response, maximum_bytes):
        self.response = response
        self.maximum_bytes = maximum_bytes

    async def __aiter__(self):
        size = 0
        async for chunk in self.response.aiter_bytes():
            size += len(chunk)
            if size > self.maximum_bytes:
                raise ValueError("provider response exceeds its byte bound")
            yield chunk

    async def aclose(self):
        await self.response.aclose()


def sdk_client(connection, *, timeout_seconds, maximum_response_bytes):
    boundary = ProcessCredentialBoundary(connection.api_key or "")

    async def inspect_response(response):
        # HTTPX2 owns decompression; count its decoded bytes before SDK buffering.
        decoded = httpx2.Response(
            response.status_code, headers=response.headers, stream=response.stream
        )
        response.headers.pop("content-encoding", None)
        response.stream = BoundedJSONStream(decoded, maximum_response_bytes)
        try:
            body = await response.aread()
            if len(body) > maximum_response_bytes:
                raise ValueError("provider response exceeds its byte bound")
            if response.is_success:
                json.loads(body, object_pairs_hook=reject_duplicate_keys)
        except BaseException:
            await response.aclose()
            raise

    client = build_async_openai_client(
        api_key=connection.api_key,
        base_url=(
            connection.endpoint[: -len("/decisions")]
            if connection.shape == "openai_decisions"
            else connection.endpoint
        ),
        timeout_policy=OpenAITransportTimeoutPolicy(*([timeout_seconds] * 5)),
        credential_boundary=boundary,
        max_retries=0,
        response_hooks=[inspect_response],
    )
    return client, boundary
