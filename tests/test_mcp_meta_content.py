"""Formal MCP content retains order and valid images around bad blocks."""

import asyncio
import base64
import json
from io import BytesIO
from time import monotonic

from PIL import Image
from jsonschema import Draft202012Validator

from pulsara_agent.capability.source_query import call_mcp_tool_output_schema, read_mcp_resource_output_schema
from pulsara_agent.conversation_kernel.image_validation import HostPromptImageValidator
from pulsara_agent.conversation_kernel.mcp.content import adapt_call_result, adapt_resource_result
from pulsara_agent.conversation_kernel.mcp.content import adapt_known_result
from pulsara_agent.conversation_kernel.mcp.results import McpKnownToolResult
from pulsara_agent.identity import random_id
from pulsara_agent.capability.mcp_search import search_mcp
from pulsara_agent.ports.tool_execution import encode_tool_public_json
from pulsara_agent.conversation_kernel.tool_outputs import assemble_tool_business_value
from types import SimpleNamespace as N


def test_formal_images_validate_individually_with_audience_and_opaque_structured_content():
    async def scenario():
        image = BytesIO()
        Image.new('RGB', (2, 2), 'red').save(image, format='PNG')
        encoded = base64.b64encode(image.getvalue()).decode()
        valid = {'type': 'image', 'data': encoded, 'mimeType': 'image/png'}
        structured = {'content': [{'type': 'image', 'data': 'business-data'}], 'nested': {'count': 4}}
        validator = HostPromptImageValidator()
        try:
            value, images, status = await adapt_call_result({'content': [
                {'type': 'text', 'text': 'before'}, valid,
                {'type': 'image', 'data': 'broken', 'mimeType': 'image/png'},
                {'type': 'text', 'text': 'hidden', 'annotations': {'audience': ['user']}},
                valid, {'type': 'text', 'text': 'after'},
            ], 'structuredContent': structured}, validator=validator, deadline=monotonic()+20)
        finally:
            await validator.aclose()
        assert status == 'ERROR'
        assert [part.type for part in value.content] == ['text', 'image', 'text', 'image', 'text']
        assert value.content[0].value == 'before' and value.content[-1].value == 'after'
        assert '未交付实际图片' in value.content[2].value
        assert len(images) == 2 and all(item.image_ref for item in images)
        assert json.loads(encode_tool_public_json(value.public_fields))['structuredContent'] == structured
        Draft202012Validator(call_mcp_tool_output_schema()).validate(assemble_tool_business_value(
            public_fields=value.public_fields, content_parts=value.content, result_state=status))
    asyncio.run(scenario())


def test_resource_text_and_binary_keep_full_contents_and_order():
    async def scenario():
        value, images, status = await adapt_resource_result({'contents': [
            {'uri': 'fixture://text', 'text': 'alpha\nbeta\n'},
            {'uri': 'fixture://bytes', 'mimeType': 'application/octet-stream', 'blob': 'AAEC'},
        ]}, validator=None, deadline=monotonic()+10)
        assert status == 'SUCCESS' and not images
        assert [part.value for part in value.content] == ['资源：fixture://text\n', 'alpha\nbeta\n', '资源：fixture://bytes\nMIME：application/octet-stream\n', 'Base64：AAEC']
        Draft202012Validator(read_mcp_resource_output_schema()).validate(assemble_tool_business_value(
            public_fields=value.public_fields, content_parts=value.content, result_state=status))
    asyncio.run(scenario())


def test_known_text_payload_is_not_reinterpreted_as_a_formal_protocol_result():
    async def scenario():
        text = '{"content":[{"type":"image","data":"this is diagnostic text"}]}'
        known = McpKnownToolResult('TEXT', 'SYSTEM_ERROR', text.encode(), random_id('mcp-tool'), 'sha256:'+'a'*64)
        value, images, state = await adapt_known_result(known, validator=None, deadline=monotonic()+10)
        assert state == 'SYSTEM_ERROR' and not images
        assert [part.value for part in value.content] == [text]
    asyncio.run(scenario())


def test_recognizable_damaged_resource_image_without_mime_keeps_placeholder_and_other_content():
    async def scenario():
        image = BytesIO()
        Image.new('RGB',(2,2),'red').save(image,format='PNG')
        validator = HostPromptImageValidator()
        try:
            value, images, state = await adapt_resource_result({'contents':[
                {'uri':'fixture://broken','blob':base64.b64encode(image.getvalue()[:40]).decode()},
                {'uri':'fixture://text','text':'preserved'},
            ]},validator=validator,deadline=monotonic()+20)
        finally:
            await validator.aclose()
        assert state == 'ERROR' and not images
        assert '未交付实际图片' in value.content[1].value
        assert value.content[-1].value == 'preserved'
    asyncio.run(scenario())


def test_bm25_search_returns_tool_matches_first_and_contiguous_first_description_match():
    first = N(server_id='docs', remote_tool_name='读取', description='x'*600+'读取文档'+'y'*600)
    second = N(server_id='docs', remote_tool_name='search', description='unrelated')
    server = N(server_id='读取', display_name='读取', sanitized_instructions='读取')
    results = search_mcp('读取', tools=[first,second], servers=[server])
    assert [item[0] for item in results] == ['tool','service']
    assert results[0][1] is first
    assert '读取文档' in results[0][2]
    assert results[0][2].startswith('…') and results[0][2].endswith('…')
    assert search_mcp('notfound', tools=[first,second], servers=[server]) == []


def test_bm25_service_match_does_not_match_unrelated_tools_from_that_service():
    unrelated = N(server_id='weather', remote_tool_name='calculate', description='Add two integers')
    matching = N(server_id='math', remote_tool_name='weather_report', description='Current weather')
    server = N(server_id='weather', display_name='weather', sanitized_instructions='Weather service')
    # A service-only hit does not issue schemas/references for its unrelated tools.
    assert [(kind, item) for kind, item, _ in search_mcp('weather', tools=[unrelated], servers=[server])] == [('service', server)]
    # Actual tool-name/description hits remain eligible even alongside a service hit.
    assert [(kind, item) for kind, item, _ in search_mcp('weather', tools=[unrelated, matching], servers=[server])] == [('tool', matching), ('service', server)]
