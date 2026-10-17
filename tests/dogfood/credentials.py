"""Redact saved credential values in local dogfood output."""

from pulsara_agent.process_credential_boundary import ProcessCredentialScrubSet
from pulsara_agent.settings import LocalSettings


def secret_scrubber(settings: LocalSettings) -> ProcessCredentialScrubSet:
    return settings.credential_scrub_set()
