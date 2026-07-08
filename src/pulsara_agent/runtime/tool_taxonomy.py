"""Shared tool-name taxonomies used across runtime domains."""

from __future__ import annotations


FILE_WRITE_TOOL_NAMES = frozenset({"edit_file", "write_file"})
TERMINAL_TOOL_NAMES = frozenset({"terminal", "terminal_process"})
PLAN_WORKFLOW_TOOL_NAMES = frozenset({"enter_plan", "ask_plan_question", "exit_plan"})
READ_ONLY_RECOVERY_TOOL_NAMES = frozenset({"read_file", "search_files", "artifact_read"})
SUBAGENT_SYSTEM_TOOL_NAMES = frozenset(
    {
        "spawn_agent",
        "wait_agent",
        "stop_agent",
        "list_agents",
        "create_agent_tasks",
        "wait_agent_tasks",
        "stop_agent_task",
    }
)
SUBAGENT_CHILD_REPORT_TOOL_NAMES = frozenset(
    {
        "report_agent_phase",
        "report_agent_result",
    }
)
