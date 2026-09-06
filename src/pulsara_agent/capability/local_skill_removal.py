"""Descriptor-relative unbind and cleanup of one inspected loose Skill directory."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import os
from pathlib import Path
import stat
from uuid import uuid4

from pulsara_agent.exclusive_publish import PlatformExclusiveDirectoryPublisher
from pulsara_agent.local_source_binding import open_absolute_directory_nofollow
from .user_skill_config import remove_user_skill_override


@dataclass(frozen=True, slots=True)
class LocalSkillRemovalIdentity:
    root_device: int
    root_inode: int
    directory_device: int
    directory_inode: int

    def __post_init__(self):
        if any(
            type(value) is not int or value < 0
            for value in (
                self.root_device,
                self.root_inode,
                self.directory_device,
                self.directory_inode,
            )
        ):
            raise ValueError("invalid Skill filesystem identity")


class LocalSkillRemovalDisposition(StrEnum):
    REMOVED = "REMOVED"
    NOT_FOUND = "NOT_FOUND"
    STALE = "STALE"
    CLEANUP_ATTENTION = "CLEANUP_ATTENTION"


@dataclass(frozen=True, slots=True)
class LocalSkillRemovalOutcome:
    disposition: LocalSkillRemovalDisposition
    path: Path
    cleanup_path: Path | None = None


_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def observe_loose_skill_removal(root: Path, name: str) -> LocalSkillRemovalIdentity:
    _validate_name(name)
    root_fd = open_absolute_directory_nofollow(root)
    try:
        child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=root_fd)
        try:
            return _identity(root_fd, child_fd)
        finally:
            os.close(child_fd)
    finally:
        os.close(root_fd)


def remove_inspected_loose_skill(
    root: Path,
    name: str,
    *,
    expected: LocalSkillRemovalIdentity,
    config_path: Path,
) -> LocalSkillRemovalOutcome:
    """Caller resolves the exact allowed root; no root/path discovery happens here."""
    _validate_name(name)
    path = root / name / "SKILL.md"
    try:
        root_fd = open_absolute_directory_nofollow(root)
    except FileNotFoundError:
        return LocalSkillRemovalOutcome(LocalSkillRemovalDisposition.NOT_FOUND, path)
    try:
        try:
            child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=root_fd)
        except FileNotFoundError:
            return LocalSkillRemovalOutcome(
                LocalSkillRemovalDisposition.NOT_FOUND, path
            )
        except OSError:
            return LocalSkillRemovalOutcome(LocalSkillRemovalDisposition.STALE, path)
        try:
            if _identity(root_fd, child_fd) != expected:
                return LocalSkillRemovalOutcome(
                    LocalSkillRemovalDisposition.STALE, path
                )
            # Rebind the path once at the visible namespace cut. No tree hashes.
            rebound = open_absolute_directory_nofollow(root)
            try:
                if _identity(rebound, child_fd) != expected:
                    return LocalSkillRemovalOutcome(
                        LocalSkillRemovalDisposition.STALE, path
                    )
            finally:
                os.close(rebound)
            named = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            if (named.st_dev, named.st_ino) != (
                expected.directory_device,
                expected.directory_inode,
            ):
                return LocalSkillRemovalOutcome(
                    LocalSkillRemovalDisposition.STALE, path
                )
            hidden = f".pulsara-skill-delete-{uuid4().hex}"
            PlatformExclusiveDirectoryPublisher().publish(root_fd, name, hidden)
            try:
                renamed = os.stat(hidden, dir_fd=root_fd, follow_symlinks=False)
            except OSError:
                return LocalSkillRemovalOutcome(
                    LocalSkillRemovalDisposition.CLEANUP_ATTENTION, path, root / hidden
                )
            if (renamed.st_dev, renamed.st_ino) != (
                expected.directory_device,
                expected.directory_inode,
            ):
                # Namespace interference after the last observation: never delete
                # the replacement or claim an unchanged/fully removed outcome.
                return LocalSkillRemovalOutcome(
                    LocalSkillRemovalDisposition.CLEANUP_ATTENTION, path, root / hidden
                )
            attention = False
            try:
                try:
                    os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                except FileNotFoundError:
                    remove_user_skill_override(skill_path=path, config_path=config_path)
                else:
                    # A new visible incarnation is not owned by this deletion.
                    # Leave its pathname rule alone, and report partial cleanup.
                    attention = True
            except (OSError, ValueError):
                attention = True
            cleanup_path = root / hidden
            try:
                _remove_contents(child_fd)
                current = os.stat(hidden, dir_fd=root_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (
                    expected.directory_device,
                    expected.directory_inode,
                ):
                    raise OSError("Skill delete stage changed")
                os.rmdir(hidden, dir_fd=root_fd)
                cleanup_path = None
            except (OSError, RecursionError):
                attention = True
            return LocalSkillRemovalOutcome(
                LocalSkillRemovalDisposition.CLEANUP_ATTENTION
                if attention
                else LocalSkillRemovalDisposition.REMOVED,
                path,
                cleanup_path,
            )
        finally:
            os.close(child_fd)
    finally:
        os.close(root_fd)


def _validate_name(name: str):
    if (
        not name
        or name.startswith(".")
        or Path(name).name != name
        or "/" in name
        or "\x00" in name
    ):
        raise ValueError("Skill deletion needs one visible immediate child")


def _identity(root_fd: int, child_fd: int) -> LocalSkillRemovalIdentity:
    root, child = os.fstat(root_fd), os.fstat(child_fd)
    return LocalSkillRemovalIdentity(
        root.st_dev, root.st_ino, child.st_dev, child.st_ino
    )


def _remove_contents(directory_fd: int):
    for name in os.listdir(directory_fd):
        observed = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if not stat.S_ISDIR(observed.st_mode):
            os.unlink(name, dir_fd=directory_fd)
            continue
        child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
        try:
            held = os.fstat(child_fd)
            if (held.st_dev, held.st_ino) != (observed.st_dev, observed.st_ino):
                raise OSError("Skill descendant changed")
            _remove_contents(child_fd)
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino):
                raise OSError("Skill descendant binding changed")
            os.rmdir(name, dir_fd=directory_fd)
        finally:
            os.close(child_fd)
