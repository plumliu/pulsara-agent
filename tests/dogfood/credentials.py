"""Redact saved credential values in local dogfood output."""

import json

from pulsara_agent.process_credential_boundary import ProcessCredentialScrubSet
from pulsara_agent.settings import LocalSettings


def secret_scrubber(settings: LocalSettings) -> ProcessCredentialScrubSet:
    scrub = ProcessCredentialScrubSet()
    for item in settings.model_api_keys + settings.mcp_credentials:
        scrub.observe(item.value)
    scrub.observe(settings.dashscope_credentials.embedding_api_key)
    scrub.observe(settings.dashscope_credentials.rerank_api_key)

    def oauth_values(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {
                    "access_token",
                    "refresh_token",
                    "client_secret",
                    "id_token",
                } and isinstance(child, str):
                    scrub.observe(child)
                else:
                    oauth_values(child)
        elif isinstance(value, list):
            for child in value:
                oauth_values(child)

    for item in settings.mcp_oauth:
        for raw in (item.token_json, item.client_json, item.auth_json):
            oauth_values(json.loads(raw))
    return scrub
