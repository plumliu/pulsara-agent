"""Explicit non-production settings for compatibility-only test wiring."""

from pulsara_agent.settings import StorageConfig


def compatibility_storage_config() -> StorageConfig:
    """Return valid settings whose endpoints fail fast if accidentally used.

    Compatibility tests may select deprecated in-memory wiring explicitly, but
    ``StorageConfig`` itself represents production-capable configuration and
    therefore rejects empty endpoints.
    """

    return StorageConfig(
        postgres_dsn="postgresql://test:test@127.0.0.1:1/pulsara_test",
        oxigraph_url="http://127.0.0.1:1",
    )
