"""Exact normalized-definition trust state for local command Hooks."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
from typing import Callable, Iterator
from uuid import uuid4

from pulsara_agent.hooks.contracts import (
    FrozenHookDefinition,
    FrozenHookSourceProvenance,
    HookSourceTrustAssessment,
    HookTrustDisposition,
    HookTrustSubject,
    LocalFileHookSourceIdentity,
    LocalFileHookTrustSubject,
    PluginHookSourceIdentity,
    PluginHookTrustSubject,
)
from pulsara_agent.local_source_binding import (
    open_absolute_directory_nofollow,
    open_or_create_absolute_directory_nofollow,
    prepare_local_source_path,
)

try:  # pragma: no cover - platform branch
    import fcntl
except ImportError:  # pragma: no cover - Windows branch
    fcntl = None  # type: ignore[assignment]


TRUST_DIGEST_CONTRACT = "pulsara.hook-definition-trust.v1"
MAXIMUM_HOOK_TRUST_STATE_BYTES = 64 * 1024
_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_WRITE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


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
    identity = provenance.identity
    if isinstance(identity, LocalFileHookSourceIdentity):
        source = {
            "kind": identity.kind.value,
            "path": identity.canonical_path.as_posix(),
            "visibility": identity.visibility_scope.value,
            "workspace_state_key": identity.workspace_state_key,
        }
    elif isinstance(identity, PluginHookSourceIdentity):
        source = {
            "kind": identity.kind.value,
            "path": identity.canonical_path.as_posix(),
            "visibility": identity.visibility_scope.value,
            "workspace_state_key": identity.workspace_state_key,
            "plugin_id": identity.plugin_id,
            "package_install_id": identity.package_install_id,
            "config_relative_path": identity.config_relative_path,
        }
    else:  # pragma: no cover - closed union
        raise TypeError("Hook source identity union is open")
    body = {
        "contract": TRUST_DIGEST_CONTRACT,
        "source": source,
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


def _read_regular_file_nofollow(path: Path) -> bytes:
    parent = open_absolute_directory_nofollow(path.parent)
    descriptor: int | None = None
    try:
        descriptor = os.open(path.name, _READ_FLAGS, dir_fd=parent)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise OSError("Hook trust state is not a regular file")
        if before.st_size > MAXIMUM_HOOK_TRUST_STATE_BYTES:
            raise ValueError("Hook trust state exceeds its scalar contract")
        chunks: list[bytes] = []
        retained = 0
        while True:
            chunk = os.read(
                descriptor,
                min(64 * 1024, MAXIMUM_HOOK_TRUST_STATE_BYTES + 1 - retained),
            )
            if not chunk:
                break
            retained += len(chunk)
            if retained > MAXIMUM_HOOK_TRUST_STATE_BYTES:
                raise ValueError("Hook trust state exceeds its scalar contract")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        def evidence(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
            return (
                value.st_dev,
                value.st_ino,
                value.st_mode,
                value.st_size,
                value.st_mtime_ns,
                value.st_ctime_ns,
            )
        if evidence(before) != evidence(after) or evidence(after) != evidence(current):
            raise OSError("Hook trust state changed during observation")
        return b"".join(chunks)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)


class HookTrustStore:
    def __init__(self, pulsara_home: Path) -> None:
        # The configured home itself is the already-adopted root binding (and
        # may be an operator-selected alias); every control-directory and file
        # component below that one root is created/opened no-follow.
        self._root = prepare_local_source_path(pulsara_home.resolve()) / "hooks"

    def read(self, subject: HookTrustSubject) -> HookSourceTrustState:
        path = self._state_path(subject)
        try:
            raw = _read_regular_file_nofollow(path)
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
        if len(payload) > MAXIMUM_HOOK_TRUST_STATE_BYTES:
            raise ValueError("Hook trust state exceeds its scalar contract")
        parent = open_or_create_absolute_directory_nofollow(path.parent)
        temporary = f".trust-{uuid4().hex}"
        fd: int | None = None
        try:
            fd = os.open(temporary, _WRITE_FLAGS, 0o600, dir_fd=parent)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb", closefd=True) as stream:
                fd = None
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(
                temporary,
                path.name,
                src_dir_fd=parent,
                dst_dir_fd=parent,
            )
            os.fsync(parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
            if fd is not None:
                os.close(fd)
            os.close(parent)

    @contextmanager
    def _locked(self, subject: HookTrustSubject) -> Iterator[None]:
        path = self._lock_path(subject)
        parent = open_or_create_absolute_directory_nofollow(path.parent)
        descriptor: int | None = None
        try:
            descriptor = os.open(
                path.name,
                os.O_RDWR
                | os.O_CREAT
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=parent,
            )
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise OSError("Hook trust lock is not a regular file")
            if fcntl is not None:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(parent)

    def _state_path(self, subject: HookTrustSubject) -> Path:
        if isinstance(subject, LocalFileHookTrustSubject):
            if subject.source_kind.value == "USER_FILE":
                return self._root / "trust" / "user.json"
            assert subject.workspace_state_key is not None
            return (
                self._root
                / "trust"
                / "workspace"
                / f"{subject.workspace_state_key}.json"
            )
        if not isinstance(subject, PluginHookTrustSubject):
            raise TypeError("Hook trust subject union is open")
        if subject.visibility_scope.value == "USER":
            return (
                self._root
                / "trust"
                / "plugin"
                / "user"
                / f"{subject.plugin_id}.json"
            )
        assert subject.workspace_state_key is not None
        return (
            self._root
            / "trust"
            / "plugin"
            / "workspace"
            / subject.workspace_state_key
            / f"{subject.plugin_id}.json"
        )

    def _lock_path(self, subject: HookTrustSubject) -> Path:
        if isinstance(subject, LocalFileHookTrustSubject):
            if subject.source_kind.value == "USER_FILE":
                return self._root / "locks" / "user.lock"
            assert subject.workspace_state_key is not None
            return (
                self._root
                / "locks"
                / "workspace"
                / f"{subject.workspace_state_key}.lock"
            )
        if not isinstance(subject, PluginHookTrustSubject):
            raise TypeError("Hook trust subject union is open")
        if subject.visibility_scope.value == "USER":
            return (
                self._root
                / "locks"
                / "plugin"
                / "user"
                / f"{subject.plugin_id}.lock"
            )
        assert subject.workspace_state_key is not None
        return (
            self._root
            / "locks"
            / "plugin"
            / "workspace"
            / subject.workspace_state_key
            / f"{subject.plugin_id}.lock"
        )


__all__ = [
    "HookSourceTrustState",
    "HookTrustStore",
    "normalized_definition_digest",
]
