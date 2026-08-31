"""Local same-origin Web application for the Pulsara conversation Kernel."""

from pulsara_agent.web_app.application import (
    LocalWebApplication,
    LocalWebApplicationState,
    run_local_web_application,
)

__all__ = [
    "LocalWebApplication",
    "LocalWebApplicationState",
    "run_local_web_application",
]
