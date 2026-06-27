"""Shared tool-name taxonomies used across runtime domains."""

from __future__ import annotations


FILE_WRITE_TOOL_NAMES = frozenset({"edit_file", "write_file"})
TERMINAL_TOOL_NAMES = frozenset({"terminal", "terminal_process"})
PLAN_WORKFLOW_TOOL_NAMES = frozenset({"enter_plan", "ask_plan_question", "exit_plan"})
READ_ONLY_RECOVERY_TOOL_NAMES = frozenset({"read_file", "search_files", "artifact_read"})
