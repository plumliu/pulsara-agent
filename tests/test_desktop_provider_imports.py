"""The local desktop shell does not open or initialize a provider SDK."""
import subprocess
import sys
import asyncio
from threading import Event

import pytest


def test_desktop_shell_does_not_import_the_openai_sdk():
    code = """
import sys
from pulsara_agent.desktop_app.application import DesktopApplication
from pulsara_agent.llm.adapters.openai.client import OpenAITransportTimeoutPolicy
from pulsara_agent.retrieval.embedding.openai_compatible import HttpEmbeddingProvider
from pulsara_agent.retrieval.rerank.http import HttpRerankProvider
assert not any(name == 'openai' or name.startswith('openai.') for name in sys.modules)
assert 'PIL.Image' not in sys.modules
"""
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True)


def test_sdk_first_use_keeps_the_event_loop_available(monkeypatch):
    from pulsara_agent.llm.adapters.openai import client

    started, release = Event(), Event()

    def load(name):
        assert name == "openai"
        started.set()
        assert release.wait(5)

    monkeypatch.setattr(client.importlib, "import_module", load)

    async def exercise():
        task = asyncio.create_task(client.prepare_openai_sdk())
        try:
            assert await asyncio.to_thread(started.wait, 5)
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
            await task

    asyncio.run(exercise())


def test_repeated_sdk_cancellation_joins_the_actual_import_thread(monkeypatch):
    from pulsara_agent.llm.adapters.openai import client

    started, release, finished = Event(), Event(), Event()

    def load(_name):
        started.set()
        assert release.wait(5)
        finished.set()

    monkeypatch.setattr(client.importlib, "import_module", load)

    async def exercise():
        task = asyncio.create_task(client.prepare_openai_sdk())
        try:
            assert await asyncio.to_thread(started.wait, 5)
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()

    asyncio.run(exercise())


def test_sdk_import_failure_remains_the_original_failure(monkeypatch):
    from pulsara_agent.llm.adapters.openai import client

    failure = ImportError("fixture SDK unavailable")

    def load(_name):
        raise failure

    monkeypatch.setattr(client.importlib, "import_module", load)
    with pytest.raises(ImportError) as caught:
        asyncio.run(client.prepare_openai_sdk())
    assert caught.value is failure
