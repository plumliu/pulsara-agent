"""Exact normalized-definition trust state for local command Hooks."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
from typing import Callable, Iterator

from pulsara_agent.hooks.contracts import (
    FrozenHookDefinition,
    FrozenHookSourceProvenance,
    HookSourceTrustAssessment,
    HookTrustDisposition,
    HookTrustSubject,
)

try:  # pragma: no cover - platform branch
    import fcntl
except ImportError:  # pragma: no cover - Windows branch
    fcntl = None  # type: ignore[assignment]


TRUST_DIGEST_CONTRACT = "pulsara.hook-definition-trust.v1"


@dataclass(frozen=True, slots=True)
class HookSourceTrustState:
    subject: HookTrustSubject
    enabled: bool = True
    trusted_definition_digest: str | None = None
    trusted_at: str | None = None


def normalized_definition_digest(
    provenance: FrozenHookSourceProvenance,
    definitions: tuple[FrozenHookDefinition, ...],
) -> str:
    body = {
        "contract": TRUST_DIGEST_CONTRACT,
        "source": {
            "kind": provenance.identity.kind.value,
            "path": provenance.identity.canonical_path.as_posix(),
            "visibility": provenance.identity.visibility_scope.value,
            "workspace_state_key": provenance.identity.workspace_state_key,
        },
        "environment": list(provenance.declaration_environment),
        "definitions": [
            {
                "ordinal": item.source_local_definition_ordinal,
                "event_ordinal": item.event_ordinal,
                "group_ordinal": item.group_ordinal,
                "handler_ordinal": item.handler_ordinal,
                "event": item.event_type.external_name,
                "matcher": item.matcher.pattern,
                "matcher_matches_all": item.matcher.matches_all,
                "matcher_ignored": item.matcher.ignored_by_profile,
                "command": item.command,
                "commandWindows": item.command_windows,
                "timeout": item.timeout_seconds,
                "async": item.asynchronous,
                "statusMessage": item.status_message,
                "additionalContextLimit": item.additional_context_limit,
            }
            for item in definitions
        ],
    }
    encoded = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(
        TRUST_DIGEST_CONTRACT.encode("ascii")
        + b"\x00"
        + len(encoded).to_bytes(8, "big")
        + encoded
    ).hexdigest()


class HookTrustStore:
    def __init__(self, pulsara_home: Path) -> None:
        self._root = pulsara_home.expanduser().resolve() / "hooks"

    def read(self, subject: HookTrustSubject) -> HookSourceTrustState:
        path = self._state_path(subject)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return HookSourceTrustState(subject)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Hook trust state is invalid") from exc
        if not isinstance(value, dict) or set(value) != {
            "enabled",
            "trusted_definition_digest",
            "trusted_at",
        }:
            raise ValueError("Hook trust state has an invalid shape")
        enabled = value["enabled"]
        digest = value["trusted_definition_digest"]
        trusted_at = value["trusted_at"]
        if not isinstance(enabled, bool):
            raise ValueError("Hook trust enabled state is invalid")
        if digest is not None and (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            raise ValueError("Hook trust digest is invalid")
        if trusted_at is not None and not isinstance(trusted_at, str):
            raise ValueError("Hook trusted_at is invalid")
        return HookSourceTrustState(subject, enabled, digest, trusted_at)

    def assess(
        self, subject: HookTrustSubject, current_digest: str
    ) -> HookSourceTrustAssessment:
        state = self.read(subject)
        if not state.enabled:
            disposition = HookTrustDisposition.DISABLED
        elif state.trusted_definition_digest is None:
            disposition = HookTrustDisposition.UNTRUSTED
        elif state.trusted_definition_digest != current_digest:
            disposition = HookTrustDisposition.MODIFIED
        else:
            disposition = HookTrustDisposition.TRUSTED
        return HookSourceTrustAssessment(
            disposition,
            current_digest,
            state.trusted_definition_digest,
            state.enabled,
            state.trusted_at,
        )

    def trust(self, subject: HookTrustSubject, *, expected_digest: str) -> None:
        with self._locked(subject):
            state = self._read_for_mutation(subject)
            self._write(
                HookSourceTrustState(
                    subject,
                    enabled=state.enabled,
                    trusted_definition_digest=expected_digest,
                    trusted_at=datetime.now(timezone.utc).isoformat(),
                )
            )

    def revoke(self, subject: HookTrustSubject) -> None:
        with self._locked(subject):
            state = self._read_for_mutation(subject)
            self._write(
                HookSourceTrustState(
                    subject,
                    enabled=state.enabled,
                    trusted_definition_digest=None,
                    trusted_at=None,
                )
            )

    def set_enabled(self, subject: HookTrustSubject, *, enabled: bool) -> None:
        with self._locked(subject):
            state = self._read_for_mutation(subject)
            self._write(
                HookSourceTrustState(
                    subject,
                    enabled=enabled,
                    trusted_definition_digest=state.trusted_definition_digest,
                    trusted_at=state.trusted_at,
                )
            )

    def trust_current(
        self,
        subject: HookTrustSubject,
        *,
        current_digest: str,
        expected_digest: str,
    ) -> None:
        if current_digest != expected_digest:
            raise ValueError("current Hook definition digest does not match --expected")
        self.trust(subject, expected_digest=expected_digest)

    def trust_after_revalidation(
        self,
        subject: HookTrustSubject,
        *,
        expected_digest: str,
        current_digest_reader: Callable[[], str],
    ) -> None:
        with self._locked(subject):
            current_digest = current_digest_reader()
            if current_digest != expected_digest:
                raise ValueError(
                    "current Hook definition digest does not match --expected"
                )
            state = self._read_for_mutation(subject)
            self._write(
                HookSourceTrustState(
                    subject,
                    enabled=state.enabled,
                    trusted_definition_digest=expected_digest,
                    trusted_at=datetime.now(timezone.utc).isoformat(),
                )
            )

    def _read_for_mutation(self, subject: HookTrustSubject) -> HookSourceTrustState:
        try:
            return self.read(subject)
        except ValueError:
            return HookSourceTrustState(subject)

    def _write(self, state: HookSourceTrustState) -> None:
        path = self._state_path(state.subject)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = json.dumps(
            {
                "enabled": state.enabled,
                "trusted_definition_digest": state.trusted_definition_digest,
                "trusted_at": state.trusted_at,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        fd, temporary_name = tempfile.mkstemp(prefix=".trust-", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb", closefd=True) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    @contextmanager
    def _locked(self, subject: HookTrustSubject) -> Iterator[None]:
        path = self._lock_path(subject)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with path.open("a+b") as stream:
            if fcntl is not None:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _state_path(self, subject: HookTrustSubject) -> Path:
        if subject.source_kind.value == "USER_FILE":
            return self._root / "trust" / "user.json"
        return self._root / "trust" / "workspace" / f"{subject.stable_locator}.json"

    def _lock_path(self, subject: HookTrustSubject) -> Path:
        if subject.source_kind.value == "USER_FILE":
            return self._root / "locks" / "user.lock"
        return self._root / "locks" / "workspace" / f"{subject.stable_locator}.lock"


__all__ = [
    "HookSourceTrustState",
    "HookTrustStore",
    "normalized_definition_digest",
]
