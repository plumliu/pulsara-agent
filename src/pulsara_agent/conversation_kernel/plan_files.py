"""The fixed Markdown artifact side effect of an accepted Plan submission."""
from __future__ import annotations

import os
from pathlib import Path
import re
from time import monotonic
import unicodedata


def publish_plan_markdown(workspace_root: Path, *, plan: bytes, summary: str | None,
                          deadline_monotonic: float) -> tuple[str | None, str | None]:
    # The filesystem owner supplies no-clobber atomic byte publication. Plan
    # supplies only the fixed directory/name policy, without broad write grants.
    from pulsara_agent.tools.builtins.filesystem import _atomic_create_bytes

    try:
        base = unicodedata.normalize("NFC", summary or "").strip()
        base = re.sub(r'[\x00-\x1f\x7f/\\<>:"|?*]+', " ", base).strip(" .") or "方案"
        if base.lower().endswith(".md"):
            base = base[:-3].strip(" .") or "方案"
        root = Path(workspace_root)
        # PC_NAME_MAX is a real filesystem boundary, not a public text limit.
        name_max = os.pathconf(root, "PC_NAME_MAX")
        index = 1
        while True:
            if monotonic() >= deadline_monotonic:
                raise TimeoutError("方案文件保存超过文件操作期限。")
            suffix = ("" if index == 1 else f" ({index})") + ".md"
            available = name_max - len(suffix.encode("utf-8"))
            if available <= 0:
                raise OSError("文件系统无法容纳方案文件名。")
            stem = base.encode("utf-8")[:available].decode("utf-8", errors="ignore").rstrip(" .") or "方案"
            relative = Path(".pulsara") / "plans" / (stem + suffix)
            try:
                _atomic_create_bytes(root / relative, plan)
                return relative.as_posix(), None
            except FileExistsError:
                index += 1
    except (OSError, ValueError, RuntimeError) as error:
        return None, str(error)
