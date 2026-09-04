from __future__ import annotations

import asyncio
import os
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
    UserDeclaredModelTarget,
)
from pulsara_agent.settings import (
    LocalDashScopeCredentials,
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
    )


def _settings_with(
    *connections: ModelConnectionConfig,
    postgres: LocalPostgresConfig | None = None,
    dashscope: LocalDashScopeCredentials | None = None,
) -> LocalSettings:
    return LocalSettings(
        postgres=postgres,
        model_connections=connections,
        model_api_keys=tuple(
            LocalModelApiKey(connection.id, f"secret-{index}")
            for index, connection in enumerate(connections)
            if connection.requires_api_key
        ),
        dashscope_credentials=dashscope or LocalDashScopeCredentials(),
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
        dashscope=LocalDashScopeCredentials("embedding-secret", "rerank-secret"),
    )

    encoded = local_settings_to_dict(value)

    assert local_settings_from_dict(encoded) == value
    assert encoded["model_connections"][0]["api_key"] == "secret-0"
    assert encoded["dashscope_credentials"] == {
        "embedding_api_key": "embedding-secret",
        "rerank_api_key": "rerank-secret",
    }
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
        {"dashscope_credentials": {}},
    ],
)
def test_local_settings_rejects_open_or_invalid_shape(mutation: dict[str, object]) -> None:
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
    assert LocalPostgresConfig(
        "postgresql://pulsara@localhost:5432/pulsara"
    ).admin_dsn is None
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
            dashscope=LocalDashScopeCredentials("embedding-secret", None),
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
        postgres = LocalPostgresConfig(
            "postgresql://pulsara@localhost:5432/pulsara"
        )
        await asyncio.gather(
            store.add_model_connection(connection=connection, api_key="secret"),
            store.save_postgres(postgres),
        )
        observed = store.read()
        assert observed.postgres == postgres
        assert observed.model_connections == (connection,)
        assert observed.model_api_key(connection.id) == "secret"

    asyncio.run(scenario())


def test_store_publishes_no_auth_connection_without_a_key(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = LocalSettingsStore(tmp_path / "local-settings.yaml")
        connection = ModelConnectionConfig(
            ModelConnectionId("model-connection:" + "c" * 32),
            ModelTargetKey("user_declared", WireApi.OPENAI_RESPONSES, "local-model"),
            "http://127.0.0.1:11434/v1",
            UserDeclaredModelTarget(
                "Local No Auth",
                256_000,
                8_192,
                True,
                ReasoningProviderDefault(),
                ModelConnectionAuthentication.NONE,
            ),
        )

        observed = await store.add_model_connection(
            connection=connection,
            api_key=None,
        )

        assert observed.model_connections == (connection,)
        assert observed.model_api_keys == ()

    asyncio.run(scenario())


def test_explicit_postgres_save_repairs_an_invalid_settings_document(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = tmp_path / "local-settings.yaml"
        path.write_text("schema: [\n", encoding="utf-8")
        store = LocalSettingsStore(path)
        postgres = LocalPostgresConfig(
            "postgresql://pulsara@localhost:5432/pulsara"
        )

        assert await store.save_postgres(postgres) == LocalSettings(postgres=postgres)
        assert store.read() == LocalSettings(postgres=postgres)

    asyncio.run(scenario())


def test_explicit_model_add_repairs_an_invalid_settings_document(tmp_path: Path) -> None:
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


def test_delete_model_connection_removes_only_its_record_and_secret(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        first, second = _connection("a"), _connection("b")
        postgres = LocalPostgresConfig(
            "postgresql://pulsara@localhost:5432/pulsara"
        )
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
                dashscope_credentials=LocalDashScopeCredentials(
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
        assert observed.dashscope_api_key("embedding") == "embedding-secret"
        assert "first-secret" not in path.read_text(encoding="utf-8")

        same, deleted_again = await store.delete_model_connection(first.id)
        assert deleted_again is False
        assert same == observed

    asyncio.run(scenario())


def test_dashscope_keys_replace_and_clear_independently(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = LocalSettingsStore(tmp_path / "local-settings.yaml")
        await asyncio.gather(
            store.save_dashscope_api_key("embedding", "embedding-secret"),
            store.save_dashscope_api_key("rerank", "rerank-secret"),
        )
        assert store.read().dashscope_api_key("embedding") == "embedding-secret"
        assert store.read().dashscope_api_key("rerank") == "rerank-secret"

        await store.delete_dashscope_api_key("embedding")
        assert store.read().dashscope_api_key("embedding") is None
        assert store.read().dashscope_api_key("rerank") == "rerank-secret"

    asyncio.run(scenario())


def test_missing_secret_has_a_narrow_typed_failure() -> None:
    with pytest.raises(LocalSettingsSecretMissing, match="DashScope"):
        LocalSettings().require_dashscope_api_key("embedding")
