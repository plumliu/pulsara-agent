"""Launch the Protocol-v3 Go client against the canonical kernel Host."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import os
import secrets
import shutil
import sys
import tempfile
from uuid import uuid4

from pulsara_agent.conversation_kernel.host import KernelHostSession
from pulsara_agent.terminal_client.binary import resolve_terminal_client_binary
from pulsara_agent.terminal_client.launcher import (
    TerminalClientExit,
    TerminalClientLaunchError,
    _clear_terminal_display_and_scrollback,
    _launch_one_terminal_client,
)
from pulsara_agent.terminal_protocol.generated_v3 import terminal_kernel_v3_pb2 as wire
from pulsara_agent.terminal_protocol.v3_gateway import (
    TerminalKernelProtocolServer,
    install_fingerprint,
)


BOOTSTRAP_TTL_SECONDS = 30.0


async def launch_terminal_kernel_client(
    *,
    host_session: KernelHostSession,
    binary_path: Path | str | None = None,
    clear_scrollback: bool = False,
) -> TerminalClientExit:
    binary = await resolve_terminal_client_binary(binary_path)
    runtime_root = Path(tempfile.mkdtemp(prefix="pulsara-tui-v3-", dir="/tmp"))
    os.chmod(runtime_root, 0o700)
    server = TerminalKernelProtocolServer(
        socket_path=runtime_root / "client.sock",
        session_provider=lambda host_id: (
            host_session
            if host_id == host_session.host_session_id
            else _missing_host(host_id)
        ),
    )
    client_instance_id = f"terminal-v3-client:{uuid4().hex}"
    try:
        await server.start()
        if clear_scrollback:
            _clear_terminal_display_and_scrollback(sys.stdout)
        carrier = _bootstrap(
            server=server,
            host_session=host_session,
            client_instance_id=client_instance_id,
        )
        returncode = await _launch_one_terminal_client(
            binary=binary,
            carrier=carrier,  # type: ignore[arg-type]
        )
        if returncode != 0:
            raise TerminalClientLaunchError(
                f"Protocol v3 terminal client exited with status {returncode}"
            )
        return TerminalClientExit(returncode, binary, client_instance_id)
    finally:
        await server.close()
        shutil.rmtree(runtime_root, ignore_errors=True)


def _bootstrap(
    *,
    server: TerminalKernelProtocolServer,
    host_session: KernelHostSession,
    client_instance_id: str,
) -> wire.TerminalKernelBootstrapCarrier:
    expires = datetime.now(UTC) + timedelta(seconds=BOOTSTRAP_TTL_SECONDS)
    carrier = wire.TerminalKernelBootstrapCarrier(
        carrier_version=1,
        launch_id=server.launch_id,
        launch_capability=server.launch_capability,
        client_instance_id=client_instance_id,
        host_session_id=host_session.host_session_id,
        session_id=host_session.session_id,
        unix_socket_path=str(server.socket_path),
        requested_role=wire.ATTACHMENT_ROLE_CONTROLLER,
        parent_pid=os.getpid(),
        expires_at_utc=expires.isoformat().replace("+00:00", "Z"),
        carrier_nonce=secrets.token_bytes(32),
    )
    install_fingerprint(
        "terminal-kernel-bootstrap:v3", carrier, "carrier_fingerprint"
    )
    return carrier


def _missing_host(host_id: str) -> KernelHostSession:
    raise KeyError(host_id)


__all__ = ["launch_terminal_kernel_client"]
