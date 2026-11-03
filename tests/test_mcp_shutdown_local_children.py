"""Local-child exit facts are separate from HTTP/import cleanup completion."""

import asyncio
import sys

from pulsara_agent.conversation_kernel.mcp import supervisor as module
from pulsara_agent.conversation_kernel.mcp.sdk_facade import BoundedMcpSdkClient
from pulsara_agent.mcp_config import _parse_server
from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary
from tests.test_round6_mcp_production import _FakeMcpClient


def test_http_connection_cleanup_does_not_claim_a_local_child(tmp_path):
    async def scenario():
        opening, closing, release_close = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class Client(_FakeMcpClient):
            # Reuse the SDK's actual HTTP-vs-stdio child fact, even while its
            # connection owner is still joining network cleanup.
            local_children_stopped = BoundedMcpSdkClient.local_children_stopped

            async def open(self):
                opening.set()
                await asyncio.Event().wait()

            async def aclose(self):
                closing.set()
                await release_close.wait()
                await super().aclose()

        config = _parse_server('http', {'transport': {
            'type': 'streamable_http', 'endpoint': 'http://127.0.0.1:1/mcp',
            'allow_http_localhost': True,
        }})
        owner = module.McpHostSupervisor(
            workspace_root=tmp_path, configs=(config,), client_factory=Client,
            credential_boundary=ProcessCredentialBoundary(),
        )
        close = None
        try:
            await owner.start()
            await asyncio.wait_for(opening.wait(), 1)
            close = asyncio.create_task(owner.aclose())
            await asyncio.wait_for(closing.wait(), 1)
            assert any(not task.done() for task in owner._connection_tasks)
            assert owner._all_slots
            assert owner.local_children_stopped
            assert not owner.physical_close_complete
            assert not close.done()
        finally:
            release_close.set()
            if close is not None:
                await asyncio.wait_for(close, 1)
            else:
                await owner.aclose()
        assert owner.local_children_stopped and owner.physical_close_complete

    asyncio.run(scenario())


def test_close_during_sdk_import_cannot_construct_or_launch_late_stdio(tmp_path, monkeypatch):
    async def scenario():
        importing, import_cancelled, release_import = asyncio.Event(), asyncio.Event(), asyncio.Event()
        constructed, launched = [], []

        class Client(_FakeMcpClient):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                constructed.append(self)

            async def open(self):
                launched.append(self)

        async def import_sdk():
            importing.set()
            try:
                await release_import.wait()
            except asyncio.CancelledError:
                # Physical import workers may finish after the caller's close
                # fence. Even a completed import that returns its factory must
                # not admit a new client/stdio launch after that fence.
                import_cancelled.set()
                await release_import.wait()
            return Client

        monkeypatch.setattr(module, '_prepare_sdk_client_factory', import_sdk)
        owner = module.McpHostSupervisor(
            workspace_root=tmp_path,
            configs=(_parse_server('stdio', {'transport': {
                'type': 'stdio', 'command': sys.executable,
            }}),), credential_boundary=ProcessCredentialBoundary(),
        )
        close = None
        try:
            await owner.start()
            await asyncio.wait_for(importing.wait(), 1)
            close = asyncio.create_task(owner.aclose())
            await asyncio.wait_for(import_cancelled.wait(), 1)
            assert owner._closed
            assert owner.local_children_stopped
            assert not owner.physical_close_complete
            assert constructed == launched == []
            release_import.set()
            await asyncio.wait_for(close, 1)
            assert constructed == launched == []
            assert not owner._all_slots
            assert not owner._connection_tasks
        finally:
            release_import.set()
            if close is not None:
                await asyncio.wait_for(close, 1)
            else:
                await owner.aclose()
        assert owner.local_children_stopped and owner.physical_close_complete

    asyncio.run(scenario())
