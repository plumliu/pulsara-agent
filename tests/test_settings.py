from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

import pulsara_agent.settings as settings_module

from pulsara_agent.llm.model_catalog import ModelTargetKey, WireApi
from pulsara_agent.llm.model_connections import (
    ModelConnectionConfig,
    ModelConnectionId,
)
from pulsara_agent.local_credentials import (
    CredentialState,
    InMemoryCredentialStore,
    ModelProviderCredential,
)
from pulsara_agent.settings import (
    LocalPostgresConfig,
    LocalSettings,
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


def test_local_settings_closed_codec_round_trip() -> None:
    value = LocalSettings(
        postgres=LocalPostgresConfig(
            "postgresql://pulsara@localhost:5432/pulsara",
            "postgresql://admin@localhost:5432/postgres",
        ),
        model_connections=(_connection("a"), _connection("b", model="glm-5")),
    )
    assert local_settings_from_dict(local_settings_to_dict(value)) == value


@pytest.mark.parametrize(
    "mutation",
    [
        {"extra": True},
        {"schema": "old"},
        {"postgres": {}},
        {"model_connections": {}},
    ],
)
def test_local_settings_rejects_open_or_invalid_shape(mutation: dict[str, object]) -> None:
    payload = local_settings_to_dict(LocalSettings())
    payload.update(mutation)
    with pytest.raises(ValueError):
        local_settings_from_dict(payload)


def test_local_settings_file_permissions_and_absent_default(tmp_path: Path) -> None:
    path = tmp_path / "private" / "local-settings.yaml"
    assert read_local_settings(path) == LocalSettings()
    write_local_settings(path, LocalSettings(model_connections=(_connection("a"),)))
    assert read_local_settings(path).model_connections == (_connection("a"),)
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert os.stat(path.parent).st_mode & 0o777 == 0o700


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


def test_settings_document_never_contains_reasoning_or_fingerprint(
    tmp_path: Path,
) -> None:
    path = tmp_path / "local-settings.yaml"
    write_local_settings(path, LocalSettings(model_connections=(_connection("a"),)))
    raw = path.read_text(encoding="utf-8")
    assert "reasoning" not in raw
    assert "fingerprint" not in raw
    assert "revision" not in raw


def test_store_serializes_add_add_without_lost_update(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "local-settings.yaml"
        store = LocalSettingsStore(path)
        credentials = InMemoryCredentialStore()
        first, second = _connection("a"), _connection("b")
        await asyncio.gather(
            store.add_model_connection(
                connection=first, api_key="first-secret", credentials=credentials
            ),
            store.add_model_connection(
                connection=second, api_key="second-secret", credentials=credentials
            ),
        )
        observed = store.read()
        assert observed.model_connections == (first, second)
        assert credentials.state(ModelProviderCredential(first.id)) is CredentialState.PRESENT
        assert credentials.state(ModelProviderCredential(second.id)) is CredentialState.PRESENT

    asyncio.run(scenario())


def test_store_serializes_model_add_and_postgres_save(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = LocalSettingsStore(tmp_path / "local-settings.yaml")
        credentials = InMemoryCredentialStore()
        connection = _connection("a")
        postgres = LocalPostgresConfig(
            "postgresql://pulsara@localhost:5432/pulsara"
        )
        await asyncio.gather(
            store.add_model_connection(
                connection=connection,
                api_key="secret",
                credentials=credentials,
            ),
            store.save_postgres(postgres),
        )
        assert store.read() == LocalSettings(postgres, (connection,))

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

        assert await store.save_postgres(postgres) == LocalSettings(postgres)
        assert store.read() == LocalSettings(postgres)

    asyncio.run(scenario())


def test_explicit_model_add_repairs_an_invalid_settings_document(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = tmp_path / "local-settings.yaml"
        path.write_text("schema: [\n", encoding="utf-8")
        store = LocalSettingsStore(path)
        credentials = InMemoryCredentialStore()
        connection = _connection("a")

        observed = await store.add_model_connection(
            connection=connection,
            api_key="secret",
            credentials=credentials,
        )
        assert observed == LocalSettings(model_connections=(connection,))
        assert store.read() == observed
        assert credentials.state(ModelProviderCredential(connection.id)) is (
            CredentialState.PRESENT
        )

    asyncio.run(scenario())


def test_failed_metadata_publish_removes_new_secret(tmp_path: Path) -> None:
    def fail_before_replace(_path: Path, _settings: LocalSettings) -> None:
        raise OSError("before replace")

    async def scenario() -> None:
        connection = _connection("a")
        credentials = InMemoryCredentialStore()
        store = LocalSettingsStore(
            tmp_path / "local-settings.yaml", writer=fail_before_replace
        )
        with pytest.raises(OSError, match="before replace"):
            await store.add_model_connection(
                connection=connection,
                api_key="secret",
                credentials=credentials,
            )
        assert credentials.state(ModelProviderCredential(connection.id)) is CredentialState.MISSING

    asyncio.run(scenario())


def test_after_replace_parent_fsync_failure_rereads_published_metadata(
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
        credentials = InMemoryCredentialStore()
        store = LocalSettingsStore(tmp_path / "local-settings.yaml")

        observed = await store.add_model_connection(
            connection=connection,
            api_key="secret",
            credentials=credentials,
        )

        assert observed.connection(connection.id) == connection
        assert store.read().connection(connection.id) == connection
        assert credentials.state(ModelProviderCredential(connection.id)) is (
            CredentialState.PRESENT
        )

    asyncio.run(scenario())


def test_commit_unknown_and_unreadable_publication_keeps_possible_orphan_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "local-settings.yaml"

    def publish_then_unknown(target: Path, settings: LocalSettings) -> None:
        write_local_settings(target, settings)
        raise settings_module._LocalSettingsCommitUnknown("unknown")

    async def scenario() -> None:
        connection = _connection("a")
        credentials = InMemoryCredentialStore()
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
        with pytest.raises(
            settings_module.LocalSettingsPublishIndeterminate,
            match="indeterminate",
        ):
            await store.add_model_connection(
                connection=connection,
                api_key="secret",
                credentials=credentials,
            )
        assert credentials.state(ModelProviderCredential(connection.id)) is (
            CredentialState.PRESENT
        )

    asyncio.run(scenario())


def test_commit_unknown_confirmed_absent_removes_new_secret(tmp_path: Path) -> None:
    def fail_unknown(_path: Path, _settings: LocalSettings) -> None:
        raise settings_module._LocalSettingsCommitUnknown("unknown")

    async def scenario() -> None:
        connection = _connection("a")
        credentials = InMemoryCredentialStore()
        store = LocalSettingsStore(
            tmp_path / "local-settings.yaml", writer=fail_unknown
        )
        with pytest.raises(LocalSettingsUnavailable, match="was not published"):
            await store.add_model_connection(
                connection=connection,
                api_key="secret",
                credentials=credentials,
            )
        assert credentials.state(ModelProviderCredential(connection.id)) is (
            CredentialState.MISSING
        )

    asyncio.run(scenario())


def test_cancellation_joins_metadata_settlement(tmp_path: Path) -> None:
    loop: asyncio.AbstractEventLoop
    entered = asyncio.Event()
    release = asyncio.Event()

    async def scenario() -> None:
        nonlocal loop
        loop = asyncio.get_running_loop()
        path = tmp_path / "local-settings.yaml"

        def delayed_writer(target: Path, settings: LocalSettings) -> None:
            loop.call_soon_threadsafe(entered.set)
            asyncio.run_coroutine_threadsafe(release.wait(), loop).result()
            write_local_settings(target, settings)

        connection = _connection("a")
        credentials = InMemoryCredentialStore()
        store = LocalSettingsStore(path, writer=delayed_writer)
        task = asyncio.create_task(
            store.add_model_connection(
                connection=connection,
                api_key="secret",
                credentials=credentials,
            )
        )
        await entered.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert store.read().connection(connection.id) == connection
        assert credentials.state(ModelProviderCredential(connection.id)) is CredentialState.PRESENT

    asyncio.run(scenario())
