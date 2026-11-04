"""Physical decode memory admission, independent of media length/output pixels.

The active video specification §8 owns this formula. psutil owns platform memory
observation; Linux additionally enforces an address-space allocation allowance.
"""

from __future__ import annotations

from contextvars import ContextVar
from pathlib import Path
import sys

import psutil

from pulsara_agent.conversation_kernel.limits import STAGE2_LIMITS
from pulsara_agent.media.source import MediaError

WORKER_MEMORY_BUDGET: ContextVar[int | None] = ContextVar(
    "media_worker_memory_budget", default=None
)
MEMORY_OBSERVATION_SECONDS = 0.02


def linux_cgroup_headroom(
    root: Path = Path("/sys/fs/cgroup"), membership: Path = Path("/proc/self/cgroup")
) -> int | None:
    roots = [
        (root, "memory.max", "memory.current"),
        (root / "memory", "memory.limit_in_bytes", "memory.usage_in_bytes"),
    ]
    try:
        for line in membership.read_text().splitlines():
            _, controllers, path = line.split(":", 2)
            if not controllers:
                roots.append((root / path.lstrip("/"), "memory.max", "memory.current"))
            elif "memory" in controllers.split(","):
                roots.append(
                    (
                        root / "memory" / path.lstrip("/"),
                        "memory.limit_in_bytes",
                        "memory.usage_in_bytes",
                    )
                )
    except (OSError, ValueError):
        pass
    headrooms = []
    for directory, limit_name, current_name in roots:
        while directory.is_relative_to(root):
            try:
                limit = (directory / limit_name).read_text().strip()
                if limit != "max":
                    headrooms.append(
                        max(0, int(limit) - int((directory / current_name).read_text()))
                    )
            except (OSError, ValueError):
                pass
            if directory == root:
                break
            directory = directory.parent
    return min(headrooms, default=None)


def available_worker_memory() -> int:
    available = psutil.virtual_memory().available
    if sys.platform == "linux":
        cgroup = linux_cgroup_headroom()
        if cgroup is not None:
            available = min(available, cgroup)
    budget = available // STAGE2_LIMITS.foreground_io_hard_concurrency
    if budget <= 0:
        raise MediaError(
            "RESOURCE_BOUNDARY", "No physical memory is available for media decoding."
        )
    return budget


def install_worker_memory_limit(budget: int) -> None:
    WORKER_MEMORY_BUDGET.set(budget)
    process = psutil.Process()
    current = process.memory_info()
    if current.rss >= budget:
        raise MediaError(
            "RESOURCE_BOUNDARY",
            "Media worker startup exceeds the admitted physical memory allocation.",
        )
    if sys.platform == "linux":
        import resource

        _, hard = resource.getrlimit(resource.RLIMIT_AS)
        limit = current.vms + budget - current.rss
        if hard != resource.RLIM_INFINITY:
            limit = min(limit, hard)
        resource.setrlimit(resource.RLIMIT_AS, (limit, hard))


def admit_video_geometry(
    width: int, height: int, sar: float = 1.0, *, reference_frames: int = 1
) -> None:
    budget = WORKER_MEMORY_BUDGET.get()
    if budget is None:
        return
    # FFmpeg reference storage + two conversion buffers, at 8 bytes/pixel
    # (four 16-bit components); display geometry adds RGB render+resize buffers.
    decoded = width * height * 8 * (reference_frames + 2)
    display = max(width, round(width * sar)) * height * 3 * 2
    remaining = budget - psutil.Process().memory_info().rss
    if decoded + display > remaining:
        raise MediaError(
            "RESOURCE_BOUNDARY",
            "Source frame decode and display geometry exceed the admitted physical memory allocation.",
        )
