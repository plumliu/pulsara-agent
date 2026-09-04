from __future__ import annotations

import pytest

from keyring.backends.macOS import Keyring as MacOSKeyring

from pulsara_agent.llm.model_connections import ModelConnectionId
from pulsara_agent.local_credentials import (
    CredentialDeleteOutcome,
    CredentialState,
    CredentialStoreError,
    DashScopeEmbeddingCredential,
    DashScopeRerankCredential,
    InMemoryCredentialStore,
    MacOSKeychainCredentialStore,
    ModelProviderCredential,
    PULSARA_KEYCHAIN_SERVICE,
    credential_account,
)


def test_closed_keychain_account_mapping_is_readable_and_non_hashed() -> None:
    connection = ModelConnectionId("model-connection:" + "a" * 32)
    assert PULSARA_KEYCHAIN_SERVICE == "com.pulsara.agent.credentials.v1"
    assert credential_account(ModelProviderCredential(connection)) == (
        f"model/{connection.value}"
    )
    assert credential_account(DashScopeEmbeddingCredential()) == (
        "retrieval/dashscope/embedding"
    )
    assert credential_account(DashScopeRerankCredential()) == (
        "retrieval/dashscope/rerank"
    )


def test_in_memory_store_exposes_state_not_secret_and_borrow_is_linear() -> None:
    store = InMemoryCredentialStore()
    key = DashScopeEmbeddingCredential()
    assert store.state(key) is CredentialState.MISSING
    assert store.put(key, "sentinel-secret") is CredentialState.PRESENT
    assert store.state(key) is CredentialState.PRESENT
    borrow = store.borrow(key)
    assert borrow.value == "sentinel-secret"
    borrow.close()
    with pytest.raises(RuntimeError, match="closed"):
        _ = borrow.value
    assert store.delete(key) is CredentialDeleteOutcome.DELETED
    assert store.delete(key) is CredentialDeleteOutcome.MISSING


@pytest.mark.parametrize(
    ("attribute", "state"),
    (("denied", CredentialState.DENIED), ("unavailable", CredentialState.UNAVAILABLE)),
)
def test_denied_and_unavailable_are_not_collapsed_into_missing(
    attribute: str, state: CredentialState
) -> None:
    store = InMemoryCredentialStore()
    setattr(store, attribute, True)
    key = DashScopeRerankCredential()
    assert store.state(key) is state
    with pytest.raises(CredentialStoreError) as captured:
        store.borrow(key)
    assert captured.value.state is state


class _FakeMacOSBackend(MacOSKeyring):
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str):
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str):
        self.values[(service, username)] = password

    def delete_password(self, service: str, username: str):
        del self.values[(service, username)]


class _NotMacOSBackend:
    pass


def test_production_store_accepts_only_the_macos_keychain_backend() -> None:
    rejected = MacOSKeychainCredentialStore(_NotMacOSBackend())
    assert rejected.state(DashScopeEmbeddingCredential()) is (
        CredentialState.UNAVAILABLE
    )
    with pytest.raises(CredentialStoreError) as captured:
        rejected.put(DashScopeEmbeddingCredential(), "sentinel")
    assert captured.value.state is CredentialState.UNAVAILABLE

    accepted = MacOSKeychainCredentialStore(_FakeMacOSBackend())
    assert accepted.put(DashScopeEmbeddingCredential(), "sentinel") is (
        CredentialState.PRESENT
    )
    assert accepted.state(DashScopeEmbeddingCredential()) is CredentialState.PRESENT
    with accepted.borrow(DashScopeEmbeddingCredential()) as borrow:
        assert borrow.value == "sentinel"
    assert accepted.delete(DashScopeEmbeddingCredential()) is (
        CredentialDeleteOutcome.DELETED
    )
