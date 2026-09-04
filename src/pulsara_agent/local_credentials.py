"""Closed typed access to Pulsara credentials in macOS Keychain."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from threading import Lock
from typing import Protocol

from pulsara_agent.llm.model_connections import ModelConnectionId


PULSARA_KEYCHAIN_SERVICE = "com.pulsara.agent.credentials.v1"


class CredentialState(StrEnum):
    PRESENT = "PRESENT"
    MISSING = "MISSING"
    DENIED = "DENIED"
    UNAVAILABLE = "UNAVAILABLE"


class CredentialDeleteOutcome(StrEnum):
    DELETED = "DELETED"
    MISSING = "MISSING"
    DENIED = "DENIED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class ModelProviderCredential:
    connection_id: ModelConnectionId


@dataclass(frozen=True, slots=True)
class DashScopeEmbeddingCredential:
    pass


@dataclass(frozen=True, slots=True)
class DashScopeRerankCredential:
    pass


CredentialKey = (
    ModelProviderCredential
    | DashScopeEmbeddingCredential
    | DashScopeRerankCredential
)


def credential_account(key: CredentialKey) -> str:
    if isinstance(key, ModelProviderCredential):
        return f"model/{key.connection_id.value}"
    if isinstance(key, DashScopeEmbeddingCredential):
        return "retrieval/dashscope/embedding"
    if isinstance(key, DashScopeRerankCredential):
        return "retrieval/dashscope/rerank"
    raise TypeError(type(key).__name__)


class CredentialStoreError(RuntimeError):
    def __init__(self, state: CredentialState) -> None:
        self.state = state
        super().__init__(f"credential store outcome: {state.value}")


@dataclass(slots=True)
class CredentialBorrow:
    _value: str = field(repr=False)
    _closed: bool = False

    @property
    def value(self) -> str:
        if self._closed:
            raise RuntimeError("credential borrow is closed")
        return self._value

    def close(self) -> None:
        self._value = ""
        self._closed = True

    def __enter__(self) -> "CredentialBorrow":
        if self._closed:
            raise RuntimeError("credential borrow is closed")
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class LocalCredentialStore(Protocol):
    def state(self, key: CredentialKey) -> CredentialState: ...

    def put(self, key: CredentialKey, value: str) -> CredentialState: ...

    def delete(self, key: CredentialKey) -> CredentialDeleteOutcome: ...

    def borrow(self, key: CredentialKey) -> CredentialBorrow: ...


class MacOSKeychainCredentialStore:
    """Production adapter that refuses every non-macOS keyring backend."""

    def __init__(self, backend: object | None = None) -> None:
        self._backend = backend
        self._backend_lock = Lock()

    def _require_backend(self):
        with self._backend_lock:
            if self._backend is None:
                try:
                    import keyring

                    self._backend = keyring.get_keyring()
                except BaseException as exc:
                    raise CredentialStoreError(CredentialState.UNAVAILABLE) from exc
            try:
                from keyring.backends.macOS import Keyring as MacOSKeyring
            except BaseException as exc:
                raise CredentialStoreError(CredentialState.UNAVAILABLE) from exc
            if not isinstance(self._backend, MacOSKeyring):
                raise CredentialStoreError(CredentialState.UNAVAILABLE)
            return self._backend

    def state(self, key: CredentialKey) -> CredentialState:
        try:
            value = self._require_backend().get_password(
                PULSARA_KEYCHAIN_SERVICE, credential_account(key)
            )
        except CredentialStoreError as exc:
            return exc.state
        except BaseException as exc:
            return _failure_state(exc)
        return CredentialState.PRESENT if value else CredentialState.MISSING

    def put(self, key: CredentialKey, value: str) -> CredentialState:
        if not value:
            raise ValueError("credential value must be non-empty")
        try:
            backend = self._require_backend()
            backend.set_password(
                PULSARA_KEYCHAIN_SERVICE, credential_account(key), value
            )
            confirmed = backend.get_password(
                PULSARA_KEYCHAIN_SERVICE, credential_account(key)
            )
        except CredentialStoreError:
            raise
        except BaseException as exc:
            raise CredentialStoreError(_failure_state(exc)) from exc
        if confirmed != value:
            raise CredentialStoreError(CredentialState.UNAVAILABLE)
        return CredentialState.PRESENT

    def delete(self, key: CredentialKey) -> CredentialDeleteOutcome:
        try:
            backend = self._require_backend()
            if backend.get_password(
                PULSARA_KEYCHAIN_SERVICE, credential_account(key)
            ) is None:
                return CredentialDeleteOutcome.MISSING
            backend.delete_password(
                PULSARA_KEYCHAIN_SERVICE, credential_account(key)
            )
        except CredentialStoreError as exc:
            return CredentialDeleteOutcome(exc.state.value)
        except BaseException as exc:
            state = _failure_state(exc)
            if _is_missing_error(exc):
                return CredentialDeleteOutcome.MISSING
            return CredentialDeleteOutcome(state.value)
        return CredentialDeleteOutcome.DELETED

    def borrow(self, key: CredentialKey) -> CredentialBorrow:
        try:
            value = self._require_backend().get_password(
                PULSARA_KEYCHAIN_SERVICE, credential_account(key)
            )
        except CredentialStoreError:
            raise
        except BaseException as exc:
            raise CredentialStoreError(_failure_state(exc)) from exc
        if not value:
            raise CredentialStoreError(CredentialState.MISSING)
        return CredentialBorrow(value)


@dataclass(slots=True)
class InMemoryCredentialStore:
    """Explicit test adapter; never selected by production bootstrap."""

    values: dict[str, str] = field(default_factory=dict, repr=False)
    unavailable: bool = False
    denied: bool = False
    borrow_count: int = 0

    def state(self, key: CredentialKey) -> CredentialState:
        blocked = self._blocked_state()
        if blocked is not None:
            return blocked
        return (
            CredentialState.PRESENT
            if credential_account(key) in self.values
            else CredentialState.MISSING
        )

    def put(self, key: CredentialKey, value: str) -> CredentialState:
        blocked = self._blocked_state()
        if blocked is not None:
            raise CredentialStoreError(blocked)
        if not value:
            raise ValueError("credential value must be non-empty")
        self.values[credential_account(key)] = value
        return CredentialState.PRESENT

    def delete(self, key: CredentialKey) -> CredentialDeleteOutcome:
        blocked = self._blocked_state()
        if blocked is not None:
            return CredentialDeleteOutcome(blocked.value)
        if self.values.pop(credential_account(key), None) is None:
            return CredentialDeleteOutcome.MISSING
        return CredentialDeleteOutcome.DELETED

    def borrow(self, key: CredentialKey) -> CredentialBorrow:
        blocked = self._blocked_state()
        if blocked is not None:
            raise CredentialStoreError(blocked)
        try:
            value = self.values[credential_account(key)]
        except KeyError as exc:
            raise CredentialStoreError(CredentialState.MISSING) from exc
        self.borrow_count += 1
        return CredentialBorrow(value)

    def _blocked_state(self) -> CredentialState | None:
        if self.denied:
            return CredentialState.DENIED
        if self.unavailable:
            return CredentialState.UNAVAILABLE
        return None


def _failure_state(exc: BaseException) -> CredentialState:
    if isinstance(exc, PermissionError):
        return CredentialState.DENIED
    message = str(exc).casefold()
    if any(word in message for word in ("denied", "cancel", "interaction not allowed")):
        return CredentialState.DENIED
    return CredentialState.UNAVAILABLE


def _is_missing_error(exc: BaseException) -> bool:
    message = str(exc).casefold()
    return "not found" in message or "does not exist" in message


__all__ = [
    "CredentialBorrow",
    "CredentialDeleteOutcome",
    "CredentialKey",
    "CredentialState",
    "CredentialStoreError",
    "DashScopeEmbeddingCredential",
    "DashScopeRerankCredential",
    "InMemoryCredentialStore",
    "LocalCredentialStore",
    "MacOSKeychainCredentialStore",
    "ModelProviderCredential",
    "PULSARA_KEYCHAIN_SERVICE",
    "credential_account",
]
