"""SDK-owned HTTP with Pulsara's credential and response resource boundaries."""

from pulsara_agent.llm.adapters.openai.client import (
    OpenAITransportTimeoutPolicy,
    build_async_openai_client,
    bounded_json_response_hook,
)
from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary


def sdk_client(connection, *, timeout_seconds, maximum_response_bytes):
    boundary = ProcessCredentialBoundary(connection.api_key or "")

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
        response_hooks=[bounded_json_response_hook(maximum_response_bytes)],
    )
    return client, boundary
