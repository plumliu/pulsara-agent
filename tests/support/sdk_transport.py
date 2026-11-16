"""Inject the existing SDK HTTP transport without importing domain test modules."""

import httpx2

from pulsara_agent.llm.adapters.openai import client as openai_client
from pulsara_agent.process_credential_boundary import ProcessCredentialBoundHttpx2Client


def install_transport(monkeypatch, response):
    observed = []

    def respond(request):
        observed.append(request)
        return (
            response(request)
            if callable(response)
            else httpx2.Response(200, json=response)
        )

    def injected(**kwargs):
        return ProcessCredentialBoundHttpx2Client(
            **kwargs, transport=httpx2.MockTransport(respond), trust_env=False
        )

    monkeypatch.setattr(openai_client, "ProcessCredentialBoundHttpx2Client", injected)
    return observed
