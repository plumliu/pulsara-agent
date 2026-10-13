"""Tool execution outcome vocabulary."""

from enum import StrEnum


class ToolResultState(StrEnum):
    SUCCESS = "success"
    ERROR = "error"
    INTERRUPTED = "interrupted"
    DENIED = "denied"
    RUNNING = "running"
