"""User-level selections, optimistic concurrency errors and bounded pages."""

from __future__ import annotations
import base64
from dataclasses import dataclass
import json
from pulsara_agent.identity import require_id


class MemoryManagementError(RuntimeError):
    def __init__(self, code: str, status: int, message: str):
        super().__init__(message)
        self.code, self.status = code, status


@dataclass(frozen=True, slots=True)
class MemoryManagementSelection:
    view: str = "global"
    workspace_id: str | None = None

    def __post_init__(self):
        if self.view not in {"global", "project"} or (self.view == "global") != (
            self.workspace_id is None
        ):
            raise ValueError("请选择全局记忆或一个明确项目。")
        if self.workspace_id is not None:
            require_id(self.workspace_id, "workspace")


def normalized_search(value):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TypeError("搜索内容须为字符串。")
    return value.strip()


def page_size(value):
    # The existing user-page request boundary limits one response, never the library.
    if type(value) is not int or not 1 <= value <= 100:
        raise ValueError("记忆页面条数须为1到100。")
    return value


def encode_cursor(filters, key):
    return base64.urlsafe_b64encode(
        json.dumps({"filters": filters, "key": key}, separators=(",", ":")).encode()
    ).decode()


def decode_cursor(value, filters, arity):
    if value is None:
        return None
    try:
        decoded = json.loads(base64.b64decode(value, altchars=b"-_", validate=True))
        key = decoded["key"]
        if (
            decoded["filters"] != filters
            or not isinstance(key, list)
            or len(key) != arity
        ):
            raise ValueError()
        return tuple(key)
    except (TypeError, KeyError, ValueError) as exc:
        raise ValueError("记忆页面游标无效，请重新查询。") from exc
