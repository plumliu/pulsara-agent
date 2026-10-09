from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from pathlib import Path

import pytest

import pulsara_agent.settings as settings_module
from pulsara_agent.llm.model_catalog import (
    ModelTargetKey,
    ReasoningProviderDefault,
    WireApi,
)
from pulsara_agent.llm.model_connections import (
    ModelConnectionAuthentication,
    ModelConnectionConfig,
    ModelConnectionId,
    ReasoningWireProfile,
    UserDeclaredModelTarget,
)
from tests.retrieval_fixtures import retrieval_settings, save_model
from pulsara_agent.retrieval.config import MemoryRetrievalSettings
from pulsara_agent.settings import (
    LocalModelApiKey,
    LocalPostgresConfig,
    LocalSettings,
    LocalSettingsPublishIndeterminate,
    LocalSettingsSecretMissing,
    LocalSettingsStore,
    LocalSettingsUnavailable,
    local_settings_from_dict,
    local_settings_to_dict,
    read_local_settings,
    write_local_settings,
)


def _connection(suffix: str, *, model: str = "glm-5.3") -> ModelConnectionConfig:
    return ModelConnectionConfig(
        ModelConnectionId(f"model-connection:{suffix * 32}"),
        ModelTargetKey("zhipuai", WireApi.OPENAI_CHAT_COMPLETIONS, model),
        "https://open.bigmodel.cn/api/paas/v4",
        ReasoningWireProfile.CATALOG_STANDARD,
    )


def _settings_with(
    *connections: ModelConnectionConfig,
    postgres: LocalPostgresConfig | None = None,
    retrieval: MemoryRetrievalSettings | None = None,
) -> LocalSettings:
    return LocalSettings(
        postgres=postgres,
        model_connections=connections,
        model_api_keys=tuple(
            LocalModelApiKey(connection.id, f"secret-{index}")
            for index, connection in enumerate(connections)
            if connection.requires_api_key
        ),
        memory_retrieval=retrieval or MemoryRetrievalSettings(),
    )


def test_local_settings_closed_codec_round_trip_includes_local_secrets() -> None:
    first, second = _connection("a"), _connection("b", model="glm-5")
    value = _settings_with(
        first,
        second,
        postgres=LocalPostgresConfig(
            "postgresql://pulsara@localhost:5432/pulsara",
            "postgresql://admin@localhost:5432/postgres",
        ),
        retrieval=retrieval_settings("embedding-secret", "rerank-secret"),
    )

    encoded = local_settings_to_dict(value)

    assert local_settings_from_dict(encoded) == value
    assert encoded["model_connections"][0]["api_key"] == "secret-0"
    assert encoded["memory_retrieval"]["embedding"]["api_key"] == "embedding-secret"
    assert encoded["memory_retrieval"]["rerank"]["api_key"] == "rerank-secret"
    assert "secret-0" not in repr(value)
    assert "embedding-secret" not in repr(value)
    assert "rerank-secret" not in repr(value)


@pytest.mark.parametrize(
    "mutation",
    [
        {"extra": True},
        {"schema": "old"},
        {"postgres": {}},
        {"model_connections": {}},
        {"memory_retrieval": {}},
    ],
)
def test_local_settings_rejects_open_or_invalid_shape(
    mutation: dict[str, object],
) -> None:
    payload = local_settings_to_dict(LocalSettings())
    payload.update(mutation)
    with pytest.raises(ValueError):
        local_settings_from_dict(payload)


def test_local_settings_rejects_bearer_connection_without_exact_key() -> None:
    with pytest.raises(ValueError, match="do not match"):
        LocalSettings(model_connections=(_connection("a"),))


def test_local_settings_file_permissions_and_absent_default(tmp_path: Path) -> None:
    path = tmp_path / "private" / "local-settings.yaml"
    assert read_local_settings(path) == LocalSettings()
    expected = _settings_with(_connection("a"))
    write_local_settings(path, expected)
    assert read_local_settings(path) == expected
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert os.stat(path.parent).st_mode & 0o777 == 0o700


def test_local_settings_rejects_permissions_that_expose_secrets(tmp_path: Path) -> None:
    path = tmp_path / "private" / "local-settings.yaml"
    write_local_settings(path, _settings_with(_connection("a")))
    path.chmod(0o644)
    with pytest.raises(LocalSettingsUnavailable, match="permissions"):
        read_local_settings(path)

    path.chmod(0o600)
    path.parent.chmod(0o755)
    with pytest.raises(LocalSettingsUnavailable, match="directory permissions"):
        read_local_settings(path)


def test_local_settings_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.yaml"
    target.write_text("schema: anything\n", encoding="utf-8")
    path = tmp_path / "local-settings.yaml"
    path.symlink_to(target)
    with pytest.raises(LocalSettingsUnavailable):
        read_local_settings(path)


def test_local_settings_invalid_document_is_typed_unavailable(tmp_path: Path) -> None:
    path = tmp_path / "local-settings.yaml"
    path.write_text("schema: [\n", encoding="utf-8")
    with pytest.raises(LocalSettingsUnavailable):
        read_local_settings(path)


def test_postgres_dsn_is_closed_and_direct() -> None:
    assert (
        LocalPostgresConfig("postgresql://pulsara@localhost:5432/pulsara").admin_dsn
        is None
    )
    for value in ("", "sqlite:///tmp/x", "postgresql://localhost"):
        with pytest.raises(ValueError):
            LocalPostgresConfig(value)


def test_settings_document_contains_secrets_but_no_derived_control_state(
    tmp_path: Path,
) -> None:
    path = tmp_path / "private" / "local-settings.yaml"
    write_local_settings(
        path,
        _settings_with(
            _connection("a"),
            retrieval=retrieval_settings("embedding-secret", None),
        ),
    )
    raw = path.read_text(encoding="utf-8")
    assert "secret-0" in raw
    assert "embedding-secret" in raw
    assert "reasoning" not in raw
    assert "fingerprint" not in raw
    assert "revision" not in raw


def test_store_serializes_add_add_without_lost_update(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = LocalSettingsStore(tmp_path / "local-settings.yaml")
        first, second = _connection("a"), _connection("b")
        await asyncio.gather(
            store.add_model_connection(connection=first, api_key="first-secret"),
            store.add_model_connection(connection=second, api_key="second-secret"),
        )
        observed = store.read()
        assert observed.model_connections == (first, second)
        assert observed.model_api_key(first.id) == "first-secret"
        assert observed.model_api_key(second.id) == "second-secret"

    asyncio.run(scenario())


def test_store_serializes_model_add_and_postgres_save(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = LocalSettingsStore(tmp_path / "local-settings.yaml")
        connection = _connection("a")
        postgres = LocalPostgresConfig("postgresql://pulsara@localhost:5432/pulsara")
        await asyncio.gather(
            store.add_model_connection(connection=connection, api_key="secret"),
            store.save_postgres(postgres),
        )
        observed = store.read()
        assert observed.postgres == postgres
        assert observed.model_connections == (connection,)
        assert observed.model_api_key(connection.id) == "secret"

    asyncio.run(scenario())


@pytest.mark.parametrize("input_modalities", (None, ("text",), ("text", "image")))
def test_store_publishes_no_auth_connection_without_a_key(
    tmp_path: Path,
    input_modalities: tuple[str, ...] | None,
) -> None:
    async def scenario() -> None:
        store = LocalSettingsStore(tmp_path / "local-settings.yaml")
        connection = ModelConnectionConfig(
            ModelConnectionId("model-connection:" + "c" * 32),
            ModelTargetKey("user_declared", WireApi.OPENAI_RESPONSES, "local-model"),
            "http://127.0.0.1:11434/v1",
            ReasoningWireProfile.PROVIDER_DEFAULT,
            UserDeclaredModelTarget(
                "Local No Auth",
                256_000,
                8_192,
                True,
                ReasoningProviderDefault(),
                ModelConnectionAuthentication.NONE,
                input_modalities=input_modalities,
            ),
        )

        observed = await store.add_model_connection(
            connection=connection,
            api_key=None,
        )

        assert observed.model_connections == (connection,)
        assert observed.model_api_keys == ()
        assert store.read().model_connections == (connection,)

    asyncio.run(scenario())


def test_explicit_postgres_save_repairs_an_invalid_settings_document(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = tmp_path / "local-settings.yaml"
        path.write_text("schema: [\n", encoding="utf-8")
        store = LocalSettingsStore(path)
        postgres = LocalPostgresConfig("postgresql://pulsara@localhost:5432/pulsara")

        assert await store.save_postgres(postgres) == LocalSettings(postgres=postgres)
        assert store.read() == LocalSettings(postgres=postgres)

    asyncio.run(scenario())


def test_explicit_model_add_repairs_an_invalid_settings_document(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = tmp_path / "local-settings.yaml"
        path.write_text("schema: [\n", encoding="utf-8")
        store = LocalSettingsStore(path)
        connection = _connection("a")

        observed = await store.add_model_connection(
            connection=connection,
            api_key="secret",
        )
        assert observed.connection(connection.id) == connection
        assert observed.model_api_key(connection.id) == "secret"
        assert store.read() == observed

    asyncio.run(scenario())


def test_failed_single_document_publish_exposes_no_new_secret(tmp_path: Path) -> None:
    def fail_before_replace(_path: Path, _settings: LocalSettings) -> None:
        raise OSError("before replace")

    async def scenario() -> None:
        path = tmp_path / "local-settings.yaml"
        store = LocalSettingsStore(path, writer=fail_before_replace)
        with pytest.raises(OSError, match="before replace"):
            await store.add_model_connection(
                connection=_connection("a"),
                api_key="secret",
            )
        assert not path.exists()

    asyncio.run(scenario())


def test_after_replace_parent_fsync_failure_rereads_complete_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_fsync = settings_module.os.fsync
    calls = 0

    def fail_parent_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("parent fsync failed")
        real_fsync(descriptor)

    monkeypatch.setattr(settings_module.os, "fsync", fail_parent_fsync)

    async def scenario() -> None:
        connection = _connection("a")
        store = LocalSettingsStore(tmp_path / "local-settings.yaml")
        observed = await store.add_model_connection(
            connection=connection,
            api_key="secret",
        )
        assert observed.connection(connection.id) == connection
        assert observed.model_api_key(connection.id) == "secret"

    asyncio.run(scenario())


def test_commit_unknown_and_unreadable_document_is_indeterminate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "local-settings.yaml"

    def publish_then_unknown(target: Path, settings: LocalSettings) -> None:
        write_local_settings(target, settings)
        raise settings_module._LocalSettingsCommitUnknown("unknown")

    async def scenario() -> None:
        store = LocalSettingsStore(path, writer=publish_then_unknown)
        reads = 0
        real_read = store.read

        def become_unreadable() -> LocalSettings:
            nonlocal reads
            reads += 1
            if reads == 1:
                return real_read()
            raise LocalSettingsUnavailable("read unavailable")

        monkeypatch.setattr(store, "read", become_unreadable)
        with pytest.raises(LocalSettingsPublishIndeterminate, match="indeterminate"):
            await store.add_model_connection(
                connection=_connection("a"),
                api_key="secret",
            )

    asyncio.run(scenario())


def test_cancellation_joins_single_document_settlement(tmp_path: Path) -> None:
    loop: asyncio.AbstractEventLoop
    entered = asyncio.Event()
    release = asyncio.Event()

    async def scenario() -> None:
        nonlocal loop
        loop = asyncio.get_running_loop()

        def delayed_writer(target: Path, settings: LocalSettings) -> None:
            loop.call_soon_threadsafe(entered.set)
            asyncio.run_coroutine_threadsafe(release.wait(), loop).result()
            write_local_settings(target, settings)

        connection = _connection("a")
        store = LocalSettingsStore(
            tmp_path / "local-settings.yaml", writer=delayed_writer
        )
        task = asyncio.create_task(
            store.add_model_connection(connection=connection, api_key="secret")
        )
        await entered.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert store.read().connection(connection.id) == connection
        assert store.read().model_api_key(connection.id) == "secret"

    asyncio.run(scenario())


def test_update_model_connection_preserves_identity_order_and_other_settings(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = LocalSettingsStore(tmp_path / "local-settings.yaml")
        first, second = _connection("a"), _connection("b")
        original = _settings_with(
            first, second, retrieval=retrieval_settings("embedding", None)
        )
        write_local_settings(store.path, original)
        changed = replace(first, reasoning_wire_profile=ReasoningWireProfile.EFFORT)
        await asyncio.gather(
            store.update_model_connection(connection=changed, api_key=None),
            save_model(store, "rerank", "rerank"),
        )
        observed = store.read()
        assert observed.model_connections == (changed, second)
        assert observed.model_api_key(first.id) == "secret-0"
        assert observed.model_api_key(second.id) == "secret-1"
        assert observed.memory_retrieval.embedding.api_key == "embedding"
        assert observed.memory_retrieval.rerank.api_key == "rerank"
        assert observed.memory_retrieval.ranking_mode == "off"
        await store.update_model_connection(connection=changed, api_key="rotated")
        assert store.read().model_api_key(first.id) == "rotated"
        await store.delete_model_connection(first.id)
        with pytest.raises(KeyError):
            await store.update_model_connection(
                connection=changed, api_key="no-resurrection"
            )
        assert store.read().model_connections == (second,)

    asyncio.run(scenario())


def test_update_model_connection_requires_explicit_key_for_new_endpoint(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = LocalSettingsStore(tmp_path / "local-settings.yaml")
        original = _connection("a")
        await store.add_model_connection(connection=original, api_key="original")
        changed = replace(original, base_url="https://another.example/v1")
        with pytest.raises(ValueError, match="重新填写 API key"):
            await store.update_model_connection(connection=changed, api_key=None)
        assert store.read().connection(original.id) == original
        assert store.read().model_api_key(original.id) == "original"
        await store.update_model_connection(
            connection=changed, api_key="new-service-key"
        )
        assert store.read().connection(original.id) == changed
        assert store.read().model_api_key(original.id) == "new-service-key"

    asyncio.run(scenario())


def test_update_model_authentication_removes_or_requires_its_key(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = LocalSettingsStore(tmp_path / "local-settings.yaml")
        declared = UserDeclaredModelTarget(
            "Custom",
            256_000,
            8192,
            True,
            ReasoningProviderDefault(),
            ModelConnectionAuthentication.BEARER_API_KEY,
        )
        original = ModelConnectionConfig(
            ModelConnectionId.new(),
            ModelTargetKey("user_declared", WireApi.OPENAI_RESPONSES, "custom"),
            "https://example.test/v1",
            ReasoningWireProfile.PROVIDER_DEFAULT,
            declared,
        )
        await store.add_model_connection(connection=original, api_key="original")
        no_auth = replace(
            original,
            user_declared=replace(
                declared, authentication=ModelConnectionAuthentication.NONE
            ),
        )
        await store.update_model_connection(connection=no_auth, api_key=None)
        assert store.read().model_api_keys == ()
        with pytest.raises(ValueError, match="重新填写 API key"):
            await store.update_model_connection(connection=original, api_key=None)
        await store.update_model_connection(connection=original, api_key="restored")
        assert store.read().model_api_key(original.id) == "restored"
        with pytest.raises(ValueError, match="来源不可更改"):
            await store.update_model_connection(
                connection=replace(_connection("a"), id=original.id), api_key="another"
            )

    asyncio.run(scenario())


def test_delete_model_connection_removes_only_its_record_and_secret(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        first, second = _connection("a"), _connection("b")
        postgres = LocalPostgresConfig("postgresql://pulsara@localhost:5432/pulsara")
        path = tmp_path / "local-settings.yaml"
        write_local_settings(
            path,
            LocalSettings(
                postgres=postgres,
                model_connections=(first, second),
                model_api_keys=(
                    LocalModelApiKey(first.id, "first-secret"),
                    LocalModelApiKey(second.id, "second-secret"),
                ),
                memory_retrieval=retrieval_settings(
                    "embedding-secret", "rerank-secret"
                ),
            ),
        )
        store = LocalSettingsStore(path)

        observed, deleted = await store.delete_model_connection(first.id)

        assert deleted is True
        assert observed.model_connections == (second,)
        assert observed.model_api_key(first.id) is None
        assert observed.model_api_key(second.id) == "second-secret"
        assert observed.postgres == postgres
        assert observed.memory_retrieval.embedding.api_key == "embedding-secret"
        assert "first-secret" not in path.read_text(encoding="utf-8")

        same, deleted_again = await store.delete_model_connection(first.id)
        assert deleted_again is False
        assert same == observed

    asyncio.run(scenario())


def test_retrieval_connections_replace_and_clear_independently(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = LocalSettingsStore(tmp_path / "local-settings.yaml")
        await asyncio.gather(
            save_model(store, "embedding", "embedding-secret"),
            save_model(store, "rerank", "rerank-secret"),
        )
        assert store.read().memory_retrieval.embedding.api_key == "embedding-secret"
        assert store.read().memory_retrieval.rerank.api_key == "rerank-secret"

        await store.clear_retrieval_connection("embedding")
        assert store.read().memory_retrieval.embedding is None
        assert store.read().memory_retrieval.rerank.api_key == "rerank-secret"

    asyncio.run(scenario())


def test_missing_secret_has_a_narrow_typed_failure() -> None:
    with pytest.raises(LocalSettingsSecretMissing, match="model"):
        LocalSettings().require_model_api_key(
            ModelConnectionId("model-connection:" + "a" * 32)
        )


def test_unsupported_schema_cannot_be_repaired_into_empty_settings(tmp_path):
    from pulsara_agent.settings import LocalSettingsSchemaUnsupported

    path = tmp_path / "local-settings.yaml"
    path.parent.chmod(0o700)
    original = b"schema: pulsara-local-settings:v2\nmodel_connections: preserve-me\n"
    path.write_bytes(original)
    path.chmod(0o600)
    store = LocalSettingsStore(path)
    with pytest.raises(LocalSettingsSchemaUnsupported):
        store.read()
    with pytest.raises(LocalSettingsSchemaUnsupported):
        asyncio.run(store.save_postgres(None))
    assert path.read_bytes() == original


def test_failed_embedding_confirmation_does_not_partially_save_or_enable(tmp_path):
    from tests.retrieval_fixtures import input_for

    async def exercise():
        store = LocalSettingsStore(tmp_path / "local-settings.yaml")
        await save_model(store, "embedding", "old-key")
        before = store.read()
        payload = input_for("embedding", "new-key", model="new-model", activate=True)
        payload["expected_embedding"] = None
        with pytest.raises(ValueError, match="CONFIRMATION_REQUIRED"):
            await store.save_retrieval_connection("embedding", payload)
        assert store.read() == before
        await store.set_retrieval_mode(embedding_enabled=True)
        enabled = store.read()
        assert enabled.memory_retrieval.embedding == before.memory_retrieval.embedding
        assert (
            enabled.memory_retrieval.embedding.embedding_contract
            == before.memory_retrieval.embedding.embedding_contract
        )

    asyncio.run(exercise())
