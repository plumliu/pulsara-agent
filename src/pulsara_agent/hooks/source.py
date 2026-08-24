"""Complete no-follow discovery of USER and exact WORKSPACE Hook sources."""

from __future__ import annotations

import errno
import os
from pathlib import Path
import stat
from time import monotonic

from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.hooks.config_parser import (
    HookConfigParseError,
    MAXIMUM_HOOK_CONFIG_BYTES,
    parse_hook_config,
)
from pulsara_agent.hooks.contracts import (
    FrozenHookDefinitionView,
    FrozenHookSourceProvenance,
    FrozenHookSourceSnapshot,
    HookDiagnostic,
    HookSourceIdentity,
    HookSourceKind,
    HookSourceSnapshotDisposition,
    HookSourceTrustAssessment,
    HookTrustDisposition,
    HookTrustSubject,
    HookVisibilityScope,
)
from pulsara_agent.hooks.trust import HookTrustStore, normalized_definition_digest


MAXIMUM_HOOK_PATH_BYTES = 4 * 1024


class _MissingSource(FileNotFoundError):
    pass


class _SourceDiscoveryRaced(OSError):
    pass


class LocalHookSourceProvider:
    def __init__(
        self,
        *,
        workspace_root: Path,
        workspace_kind: str,
        workspace_state_key: str,
        pulsara_home: Path | None = None,
        trust_store: HookTrustStore | None = None,
    ) -> None:
        if workspace_kind not in {"project", "transient"}:
            raise ValueError("Hook workspace kind is invalid")
        self._workspace_root = workspace_root.expanduser().resolve()
        self._workspace_kind = workspace_kind
        self._workspace_state_key = workspace_state_key
        self._pulsara_home = (
            require_pulsara_home()
            if pulsara_home is None
            else require_pulsara_home(str(pulsara_home))
        )
        self._trust = trust_store or HookTrustStore(self._pulsara_home)

    @property
    def trust_store(self) -> HookTrustStore:
        return self._trust

    def discover(
        self, *, deadline_monotonic: float | None = None
    ) -> FrozenHookDefinitionView:
        snapshots = [
            self._read_source(
                kind=HookSourceKind.USER_FILE,
                expected=self._pulsara_home / "hooks.json",
                allowed_root=self._pulsara_home,
                workspace_key=None,
                deadline_monotonic=deadline_monotonic,
            )
        ]
        if self._workspace_kind == "project":
            snapshots.append(
                self._read_source(
                    kind=HookSourceKind.WORKSPACE_FILE,
                    expected=self._workspace_root / ".pulsara" / "hooks.json",
                    allowed_root=self._workspace_root,
                    workspace_key=self._workspace_state_key,
                    deadline_monotonic=deadline_monotonic,
                )
            )
        return FrozenHookDefinitionView(tuple(snapshots))

    def source_subject(self, kind: HookSourceKind) -> HookTrustSubject:
        if kind is HookSourceKind.USER_FILE:
            return HookTrustSubject(kind, "user")
        if self._workspace_kind != "project":
            raise ValueError("transient workspaces have no WORKSPACE Hook source")
        return HookTrustSubject(kind, self._workspace_state_key)

    def _read_source(
        self,
        *,
        kind: HookSourceKind,
        expected: Path,
        allowed_root: Path,
        workspace_key: str | None,
        deadline_monotonic: float | None,
    ) -> FrozenHookSourceSnapshot:
        expected = Path(os.path.abspath(expected))
        identity = HookSourceIdentity(
            kind,
            expected,
            HookVisibilityScope.USER
            if kind is HookSourceKind.USER_FILE
            else HookVisibilityScope.WORKSPACE,
            workspace_key,
        )
        subject = self.source_subject(kind)
        provenance = FrozenHookSourceProvenance(
            identity,
            subject,
            None,
            "USER" if kind is HookSourceKind.USER_FILE else "WORKSPACE",
        )
        if len(os.fsencode(expected)) > MAXIMUM_HOOK_PATH_BYTES:
            return self._unavailable(provenance, "HOOK_SOURCE_PATH_BOUND_EXCEEDED")
        try:
            _require_deadline(deadline_monotonic)
            raw = _read_regular_file_no_follow(
                expected,
                allowed_root=allowed_root,
                deadline_monotonic=deadline_monotonic,
            )
            _require_deadline(deadline_monotonic)
        except _MissingSource:
            raw = b'{"hooks":{}}'
        except _SourceDiscoveryRaced as exc:
            return self._unavailable(
                provenance,
                "HOOK_SOURCE_DISCOVERY_RACED",
                detail=type(exc).__name__,
            )
        except (OSError, ValueError, TimeoutError) as exc:
            return self._unavailable(
                provenance,
                "HOOK_SOURCE_UNAVAILABLE",
                detail=type(exc).__name__,
            )
        try:
            _require_deadline(deadline_monotonic)
            parsed = parse_hook_config(raw, provenance=provenance)
            _require_deadline(deadline_monotonic)
        except HookConfigParseError as exc:
            return self._unavailable(
                provenance,
                "HOOK_SOURCE_PARSE_FAILED",
                detail=str(exc),
            )
        except TimeoutError as exc:
            return self._unavailable(
                provenance,
                "HOOK_SOURCE_UNAVAILABLE",
                detail=type(exc).__name__,
            )
        digest = normalized_definition_digest(parsed.provenance, parsed.definitions)
        diagnostics = list(parsed.diagnostics)
        try:
            _require_deadline(deadline_monotonic)
            trust = self._trust.assess(subject, digest)
            _require_deadline(deadline_monotonic)
        except TimeoutError as exc:
            return self._unavailable(
                provenance,
                "HOOK_SOURCE_UNAVAILABLE",
                detail=type(exc).__name__,
            )
        except ValueError:
            trust = HookSourceTrustAssessment(
                HookTrustDisposition.UNTRUSTED,
                digest,
                None,
                True,
                None,
            )
            diagnostics.append(
                HookDiagnostic(
                    "HOOK_TRUST_STATE_INVALID",
                    "Hook trust state is invalid; command execution is disabled",
                    source_label=parsed.provenance.display_label,
                )
            )
        return FrozenHookSourceSnapshot(
            parsed.provenance,
            HookSourceSnapshotDisposition.COMPLETE,
            parsed.definitions,
            tuple(diagnostics),
            trust,
        )

    @staticmethod
    def _unavailable(
        provenance: FrozenHookSourceProvenance,
        code: str,
        *,
        detail: str | None = None,
    ) -> FrozenHookSourceSnapshot:
        message = code if detail is None else f"{code}: {detail}"
        return FrozenHookSourceSnapshot(
            provenance,
            HookSourceSnapshotDisposition.UNAVAILABLE,
            (),
            (HookDiagnostic(code, message, source_label=provenance.display_label),),
            HookSourceTrustAssessment(
                HookTrustDisposition.UNAVAILABLE, None, None, True, None
            ),
        )


def _read_regular_file_no_follow(
    expected: Path,
    *,
    allowed_root: Path,
    deadline_monotonic: float | None,
) -> bytes:
    root = allowed_root.expanduser().resolve()
    try:
        relative = expected.relative_to(root)
    except ValueError as exc:
        raise ValueError("Hook source escapes its allowed root") from exc
    parts = relative.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("Hook source path is invalid")
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    nonblocking = getattr(os, "O_NONBLOCK", 0)
    try:
        directory_fd = os.open(root, directory_flags | nofollow)
    except FileNotFoundError as exc:
        raise _MissingSource from exc
    opened_directories: list[int] = [directory_fd]
    file_fd: int | None = None
    try:
        for component in parts[:-1]:
            _require_deadline(deadline_monotonic)
            try:
                directory_fd = os.open(
                    component,
                    directory_flags | nofollow,
                    dir_fd=directory_fd,
                )
            except FileNotFoundError as exc:
                raise _MissingSource from exc
            opened_directories.append(directory_fd)
        _require_deadline(deadline_monotonic)
        try:
            file_fd = os.open(
                parts[-1],
                os.O_RDONLY | nofollow | nonblocking,
                dir_fd=directory_fd,
            )
        except FileNotFoundError as exc:
            raise _MissingSource from exc
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.EMLINK}:
                raise ValueError("Hook source cannot be a symlink") from exc
            raise
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("Hook source must be a regular file")
        if before.st_size > MAXIMUM_HOOK_CONFIG_BYTES:
            raise ValueError("Hook source exceeds 1 MiB")
        chunks: list[bytes] = []
        total = 0
        while True:
            _require_deadline(deadline_monotonic)
            chunk = os.read(
                file_fd, min(64 * 1024, MAXIMUM_HOOK_CONFIG_BYTES + 1 - total)
            )
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAXIMUM_HOOK_CONFIG_BYTES:
                raise ValueError("Hook source exceeds 1 MiB")
        after = os.fstat(file_fd)
        observed = os.stat(parts[-1], dir_fd=directory_fd, follow_symlinks=False)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        identity_path = (
            observed.st_dev,
            observed.st_ino,
            observed.st_size,
            observed.st_mtime_ns,
            observed.st_ctime_ns,
        )
        if identity_before != identity_after or identity_after != identity_path:
            raise _SourceDiscoveryRaced(
                "Hook source discovery raced with replacement or in-place rewrite"
            )
        return b"".join(chunks)
    finally:
        if file_fd is not None:
            os.close(file_fd)
        for fd in reversed(opened_directories):
            os.close(fd)


def _require_deadline(deadline_monotonic: float | None) -> None:
    if deadline_monotonic is not None and monotonic() >= deadline_monotonic:
        raise TimeoutError("Hook source discovery deadline expired")


__all__ = ["LocalHookSourceProvider"]
