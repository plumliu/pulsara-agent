"""Current-target login replacement joins the real SDK callback owner."""
from __future__ import annotations

import asyncio
import socket
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import yaml

from tests.test_mcp_oauth_reauthorization import login, revocable_server
from tests.test_mcp_oauth_target_status import _controller


def _change(state, field, revision=1):
    path = state.service.user_config_path
    raw = yaml.safe_load(path.read_text())
    config = raw['servers'][state.target.server_id]
    if field == 'scope':
        config['auth']['scope'] = f'updated-{revision}'
    elif field == 'endpoint':
        config['transport']['endpoint'] = state.base + f'/mcp?revision={revision}'
    elif field == 'headers':
        config['public_headers'] = {'X-Revision': str(revision)}
    else:
        raise AssertionError(field)
    path.write_text(yaml.safe_dump(raw))
    return state.service.inspect(state.target)


async def _waiting(flow):
    async with asyncio.timeout(5):
        while flow.state != 'awaiting_user':
            if flow.task.done():
                await flow.task
            await asyncio.sleep(.01)


@pytest.mark.parametrize('field', ['scope', 'endpoint', 'headers'])
def test_current_target_replaces_waiting_flow_through_public_login(tmp_path, field):
    async def scenario():
        async with revocable_server(tmp_path) as state:
            state.auto_callback = False
            prior = await state.service.authorize(state.target)
            await _waiting(prior)
            assert await state.service.authorize(state.target) is prior
            assert state.browser_visits == 1
            current = _change(state, field)
            assert (await _controller(state.service).mcp_authorization('revocable', action='status'))['state'] == 'idle'
            state.auto_callback = True
            replacement = await state.service.authorize(state.target)
            assert replacement is not prior and replacement.config == current
            await asyncio.wait_for(replacement.task, 5)
            assert prior.task.done() and prior.listener is None
            assert state.browser_visits == 2 and state.issued == 1
            assert (await _controller(state.service).mcp_authorization('revocable', action='status'))['state'] == 'authorized'
            assert state.tool_calls == 0
    asyncio.run(scenario())


@pytest.mark.parametrize('finish', ['cancel', 'close'])
def test_consecutive_replacements_and_immediate_cancellation_join_every_listener(tmp_path, finish):
    async def scenario():
        async with revocable_server(tmp_path) as state:
            state.auto_callback = False
            original = await state.service.authorize(state.target)
            await _waiting(original)
            port = urlsplit(original.config.auth.redirect_uri).port
            _change(state, 'scope', 1)
            second = await state.service.authorize(state.target)
            # No event-loop yield: second has not entered its coroutine yet.
            _change(state, 'scope', 2)
            third = await state.service.authorize(state.target)
            assert second is not original and third is not second
            if finish == 'cancel':
                await asyncio.wait_for(state.service.oauth.cancel(state.target.owner), 5)
            else:
                await asyncio.wait_for(state.service.aclose(), 5)
            assert all(flow.task.done() and flow.listener is None for flow in (original, second, third))
            assert all(flow.state == 'cancelled' for flow in (original, second, third))
            assert state.target.owner not in state.service.oauth._logins
            assert state.browser_visits == 1 and state.issued == 0
            with socket.socket() as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(('127.0.0.1', port))
            if finish == 'cancel':
                state.auto_callback = True
                await login(state)
                assert state.browser_visits == 2
    asyncio.run(scenario())


@pytest.mark.parametrize('finish', ['success', 'changed_again'])
def test_replacement_waits_for_already_received_callback_cleanup(tmp_path, monkeypatch, finish):
    async def scenario():
        async with revocable_server(tmp_path) as state:
            state.auto_callback = False
            original = await state.service.authorize(state.target)
            await _waiting(original)
            entered, release = asyncio.Event(), asyncio.Event()
            runner = original.listener
            cleanup = type(runner).cleanup

            async def hold_cleanup(self):
                if self is runner:
                    entered.set()
                    await release.wait()
                await cleanup(self)

            monkeypatch.setattr(type(runner), 'cleanup', hold_cleanup)
            query = parse_qs(urlsplit(original.authorization_url).query)
            async with httpx.AsyncClient(trust_env=False) as browser:
                callback = asyncio.create_task(browser.get(query['redirect_uri'][0], params={
                    'code': 'synthetic-code', 'state': query['state'][0], 'iss': state.base,
                }))
                try:
                    await asyncio.wait_for(entered.wait(), 5)
                    assert original.state == 'authorized' and original.settled.is_set()
                    _change(state, 'scope', 1)
                    state.auto_callback = True
                    replacement = await state.service.authorize(state.target)
                    await asyncio.sleep(0)
                    assert not replacement.task.done() and replacement.state == 'connecting'
                    assert not original.task.cancelling()
                    assert state.browser_visits == 1
                    if finish == 'changed_again':
                        _change(state, 'scope', 2)
                    release.set()
                    if finish == 'success':
                        await asyncio.wait_for(replacement.task, 5)
                        assert state.browser_visits == 2 and state.issued == 2
                    else:
                        with pytest.raises(ValueError):
                            await asyncio.wait_for(replacement.task, 5)
                        assert state.browser_visits == 1 and state.issued == 1
                    assert original.listener is None and original.task.done()
                    assert (await callback).status_code == 200
                finally:
                    release.set()
                    await asyncio.gather(callback, return_exceptions=True)
            assert state.tool_calls == 0
    asyncio.run(scenario())


@pytest.mark.parametrize('finish', ['failed', 'cancelled'])
def test_replaced_login_failure_or_cancel_preserves_existing_grant(tmp_path, finish):
    async def scenario():
        async with revocable_server(tmp_path) as state:
            saved = await login(state)
            state.accepted = None
            async def reject():
                from tests.test_mcp_oauth_reauthorization import _test_connection
                assert (await _test_connection(state, tmp_path)).status != 'ready'
            await reject()
            state.auto_callback = False
            original = await state.service.authorize(state.target)
            await _waiting(original)
            _change(state, 'scope')
            state.token_error = finish == 'failed'
            state.auto_callback = finish != 'cancelled'
            replacement = await state.service.authorize(state.target)
            if finish == 'failed':
                with pytest.raises(ValueError):
                    await asyncio.wait_for(replacement.task, 5)
            else:
                await _waiting(replacement)
                await state.service.oauth.cancel(state.target.owner)
            assert original.listener is None and original.task.done()
            assert state.settings.read().mcp_authorization(state.target.owner) == saved
            assert state.tool_calls == 0
    asyncio.run(scenario())
