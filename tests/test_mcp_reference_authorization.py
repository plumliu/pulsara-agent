"""Real SDK grant acceptance and refresh keep exact call-reference targets."""

import asyncio
from dataclasses import replace
from time import time

import pytest

from tests.test_mcp_oauth import oauth_server
from tests.test_mcp_oauth_reauthorization import login, revocable_server, _test_connection


def test_refresh_preserves_grant_identity_and_logout_invalidates_it(tmp_path):
    async def scenario():
        async with oauth_server(tmp_path) as (manager, target, config, _, _, _, _, _):
            flow = await manager.begin(target.owner, lambda: config)
            record = await flow.task
            bound = manager.bind(target.owner, config, lambda: config)
            before = bound.reference_identity()
            expired = replace(record, expires_at=time()-1)
            await manager.settings.replace_mcp_authorization(target.owner, expired, expected=record)
            await manager.authorization(target.owner, config, lambda: config)
            assert bound.reference_identity() == before
            await manager.logout(target.owner)
            assert bound.reference_identity() != before
    asyncio.run(scenario())


def test_new_accepted_grant_changes_identity_but_failed_login_does_not(tmp_path):
    async def scenario():
        async with revocable_server(tmp_path) as state:
            await login(state)
            config = state.service.inspect(state.target)
            bound = config.authorization_provider
            before = bound.reference_identity()
            state.accepted = None
            await _test_connection(state, tmp_path)
            state.token_error = True
            failed = await state.service.authorize(state.target)
            with pytest.raises(ValueError):
                await failed.task
            assert bound.reference_identity() == before
            state.token_error = False
            await login(state)
            assert bound.reference_identity() != before
    asyncio.run(scenario())
