from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

from pulsara_agent.process_credential_boundary import ProcessCredentialBoundary, ProcessCredentialBoundAsyncClient
from pulsara_agent.settings import LocalSettings
from pulsara_agent.web.config import WebConnection
from pulsara_agent.web.providers import (
    CandidateUnsupported, FetchRequest, SearchRequest, normalize, request_for,
    validate_fetch, validate_search,
)
from pulsara_agent.web.service import WebService


class Settings:
    def __init__(self, provider=None):
        self.value = LocalSettings(web=None if provider is None else WebConnection(provider, api_key="test-only-secret"))
        self.reads = 0

    def read(self):
        self.reads += 1
        return self.value


def run(coro):
    return asyncio.run(coro)


def success(provider, *, body="body", title="Page"):
    if provider == "firecrawl":
        return {"success": True, "data": {"markdown": body, "metadata": {"title": title, "statusCode": 200}}}
    if provider == "tavily":
        return {"results": [{"url": "https://example.com/", "raw_content": body}], "failed_results": []}
    if provider == "exa":
        return {"results": [{"id": "provider-document", "url": "https://redirect.example/", "text": body, "title": title}],
                "statuses": [{"id": "https://example.com/", "status": "success"}]}
    return {"content": body, "title": title}


@pytest.mark.parametrize("arguments", [
    {}, {"query": " "}, {"query": "x", "count": True}, {"query": "x", "count": 21},
    {"query": "x", "count": 1.0}, {"query": "x", "after": None},
    {"query": "x", "after": "2025-02-29"}, {"query": "x", "before": "2026-1-01"},
    {"query": "x", "after": "2026-01-01", "before": "2026-01-01"},
    {"query": "x", "include_domains": None}, {"query": "x", "exclude_domains": ["*.example.com"]},
    {"query": "x", "include_domains": ["https://example.com"]},
    {"query": "x", "include_domains": ["example.com:443"]},
    {"query": "x", "include_domains": ["example.com/path"]},
    {"query": "x", "include_domains": ["a..com"]},
    {"query": "x", "provider": "exa"},
])
def test_search_common_validation_precedes_all_dispatch(arguments):
    async def check():
        calls = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: calls.append(req))) as client:
            result = await WebService(Settings(), client).execute("web_search", arguments)
        assert result["meta"] == {"status": "ERROR"}
        assert calls == []
    run(check())


@pytest.mark.parametrize("target", [
    {"kind": "URL", "url": "ftp://example.com"},
    {"kind": "URL", "url": "https://name:password@example.com/"},
    {"kind": "URL", "url": "https://example.com:bad/"},
    {"kind": "URL", "url": "https://exa mple.com/"},
    {"kind": "URL", "url": "https://example.com/\\evil"},
    {"kind": "URL", "url": "https://example.com/\n"},
    {"kind": "URL", "url": "https://example.com", "headers": {}},
])
def test_fetch_target_validation_rejects_invalid_credentials_and_extra_fields(target):
    with pytest.raises(ValueError):
        validate_fetch({"target": target})


def test_formal_provider_search_maps_preserve_all_explicit_filters():
    request = validate_search({"query": "  original query  ", "count": 20,
                              "include_domains": ["example.com", "other.com"],
                              "exclude_domains": ["bad.example.com"],
                              "after": "2024-02-29", "before": "2026-01-01"})
    _, ds = request_for("deepseek", request)
    assert ds == {"query": "  original query   (site:example.com OR site:other.com) -site:bad.example.com after:2024-02-29 before:2026-01-01", "count": 20, "need_content": False}
    _, fc = request_for("firecrawl", request)
    assert fc == {"query": request.query, "limit": 20, "sources": [{"type": "web"}], "highlights": True,
                  "includeDomains": ["example.com", "other.com"], "tbs": "cdr:1,cd_min:02/29/2024,cd_max:01/01/2026"}
    _, tv = request_for("tavily", request)
    assert tv["query"] == request.query and tv["max_results"] == 20
    assert tv["start_date"] == "2024-02-29" and tv["end_date"] == "2026-01-01"
    assert tv["include_domains_mode"] == "restrict" and tv["filter_by_published_date"] is False
    assert tv["include_published_date"] is True and tv["include_answer"] is False and tv["include_raw_content"] is False
    _, exa = request_for("exa", request)
    assert exa == {"query": request.query, "numResults": 20, "type": "auto", "contents": {"highlights": True},
                   "includeDomains": ["example.com", "other.com"], "excludeDomains": ["bad.example.com"],
                   "startPublishedDate": "2024-02-29T00:00:00Z", "endPublishedDate": "2026-01-01T00:00:00Z"}


def test_provider_request_limits_are_specific_and_never_truncate():
    assert request_for("deepseek", SearchRequest("😀" * 128))[1]["query"] == "😀" * 128
    for provider, request in [
        ("deepseek", SearchRequest("😀" * 129)),
        ("deepseek", SearchRequest("x" * 128, include_domains=("example.com",))),
        ("firecrawl", SearchRequest("x" * 501)),
        ("firecrawl", SearchRequest("x", after="2026-01-01")),
        ("firecrawl", SearchRequest("x", before="2026-01-01")),
        ("tavily", SearchRequest("x", include_domains=("example.com",) * 301)),
        ("tavily", SearchRequest("x", exclude_domains=("example.com",) * 151)),
        ("exa", SearchRequest("x", include_domains=("example.com",) * 1201)),
        ("exa", FetchRequest("https://example.com/" + "a" * 2048)),
        ("deepseek", FetchRequest("https://example.com/" + "a" * 8192)),
    ]:
        with pytest.raises(CandidateUnsupported):
            request_for(provider, request)


@pytest.mark.parametrize("provider", ["deepseek", "firecrawl", "tavily", "exa"])
def test_search_normalization_retains_order_optional_facts_and_domain_scope(provider):
    request = SearchRequest("x", include_domains=("EXAMPLE.com",), exclude_domains=("bad.example.com",))
    rows = [
        {"url": "https://evil-example.com"}, {"url": "https://example.com.evil.com"},
        {"url": "https://bad.example.com"}, {"url": "https://sub.bad.example.com"},
        {"url": "https://EXAMPLE.com/a", "title": "", "date": "2020-01-01", "published_date": "2020-01-01", "publishedDate": "2020-01-01"},
        {"url": "https://sub.example.com/b", "title": " Real ", "summary": "long", "snippet": "short", "content": "excerpt", "description": "description", "highlights": ["a", "b"]},
    ]
    payload = {"success": True, "data": {"web": rows}} if provider == "firecrawl" else {"results": rows}
    result = normalize(provider, request, payload)
    assert result.error is None
    assert [x["value"]["target"]["url"] for x in result.content] == ["https://EXAMPLE.com/a", "https://sub.example.com/b"]
    assert "title" not in result.content[0]["value"]
    assert result.content[0]["value"]["date"] == "2020-01-01"
    assert result.content[1]["value"]["snippet"] == {"deepseek": "long", "tavily": "excerpt", "firecrawl": "a\n\n[…]\n\nb", "exa": "a\n\n[…]\n\nb"}[provider]


@pytest.mark.parametrize("provider", ["deepseek", "firecrawl", "tavily", "exa"])
def test_fetch_success_and_explicit_empty_keep_provider_body_and_optional_title(provider):
    request = FetchRequest("https://example.com/")
    body = " # Page\n\n[link](https://example.com/long)\n![](https://example.com/img.png)\n" * 3000
    result = normalize(provider, request, success(provider, body=body, title=""))
    assert result.error is None and result.title is None
    assert result.content == ({"type": "text", "value": body},)
    empty = normalize(provider, request, success(provider, body=""))
    assert empty.error is None and empty.content == ()
    assert request_for(provider, request)[1] == {
        "deepseek": {"url": request.url},
        "firecrawl": {"url": request.url, "formats": ["markdown"], "onlyMainContent": True},
        "tavily": {"urls": [request.url], "format": "markdown"},
        "exa": {"urls": [request.url], "text": {"linkStyle": "full"}},
    }[provider]


def test_fetch_checks_target_failure_and_exa_input_status_separately_from_result_id():
    req = FetchRequest("https://example.com/")
    fc = success("firecrawl")
    fc["data"]["metadata"].update(statusCode=404, error="blocked page")
    assert "target page returned HTTP 404" in normalize("firecrawl", req, fc).error
    tv = {"results": [], "failed_results": [{"url": req.url, "error": "denied"}]}
    assert "denied" in normalize("tavily", req, tv).error
    exa = success("exa")
    assert normalize("exa", req, exa).error is None  # doc ID and redirect URL differ legally
    exa["statuses"][0]["id"] = "wrong-input"
    assert "status for requested URL" in normalize("exa", req, exa).error
    exa["statuses"][0] = {"id": req.url, "status": "error", "error": {"tag": "CRAWL_FAILED"}}
    partial = normalize("exa", req, exa)
    assert partial.content and "CRAWL_FAILED" in partial.error
    for provider in ("deepseek", "firecrawl", "tavily", "exa"):
        payload = success(provider)
        row = payload["data"] if provider == "firecrawl" else payload["results"][0] if provider in {"tavily", "exa"} else payload
        row.pop({"deepseek": "content", "firecrawl": "markdown", "tavily": "raw_content", "exa": "text"}[provider])
        assert "missing requested extracted body" in normalize(provider, req, payload).error


def test_anonymous_requests_isolate_saved_client_and_environment_auth_and_stop_on_successful_empty(monkeypatch):
    for name in ("FIRECRAWL_API_KEY", "TAVILY_API_KEY", "EXA_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.setenv(name, "must-not-send")
    async def check():
        requests = []
        def handler(req):
            requests.append(req)
            assert "Authorization" not in req.headers and "cookie" not in req.headers and "x-api-key" not in req.headers
            assert req.headers.get("x-tavily-access-mode") == ("keyless" if req.url.host == "api.tavily.com" else None)
            return httpx.Response(429, json={"error": "busy"}) if len(requests) == 1 else httpx.Response(200, json={"results": []})
        settings = Settings()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), auth=("default", "password"),
                                     headers={"Authorization": "Bearer ambient", "x-api-key": "ambient"}, cookies={"session": "paid"}) as client:
            result = await WebService(settings, client).execute("web_search", {"query": "x"})
        assert result == {"meta": {"status": "SUCCESS"}, "content": []}
        assert [r.url.host for r in requests] == ["api.firecrawl.dev", "api.tavily.com"]
        assert settings.reads == 1
    run(check())


def test_keyed_account_failure_then_same_provider_keyless_uses_frozen_settings_and_first_success():
    async def check():
        settings = Settings("firecrawl")
        calls = []
        def handler(req):
            calls.append(req)
            if len(calls) == 1:
                assert req.headers["Authorization"] == "Bearer test-only-secret"
                settings.value = replace(settings.value, web=WebConnection("exa", api_key="later-only-secret"))
                return httpx.Response(402, json={"error": "account depleted"})
            assert req.url.host == "api.firecrawl.dev" and "Authorization" not in req.headers
            return httpx.Response(200, json=success("firecrawl", body="preserved"))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            value = await WebService(settings, client).execute("web_fetch", {"target": {"kind": "URL", "url": "https://example.com/"}})
        assert len(calls) == 2 and settings.reads == 1
        assert value == {"target": {"kind": "URL", "url": "https://example.com/"},
                         "meta": {"status": "SUCCESS", "title": "Page"}, "content": [{"type": "text", "value": "preserved"}]}
    run(check())


def test_unsupported_filter_candidate_skips_without_weakening_next_request():
    async def check():
        calls = []
        def handler(req):
            calls.append(req)
            assert req.url.host == "api.tavily.com"
            assert json.loads(req.content)["start_date"] == "2026-01-01"
            return httpx.Response(200, json={"results": []})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await WebService(Settings("deepseek"), client).execute("web_search", {"query": "x" * 129, "after": "2026-01-01"})
        assert result["meta"]["status"] == "SUCCESS" and len(calls) == 1
    run(check())


def test_final_failure_keeps_only_first_useful_partial_and_exactly_scrubs_credentials():
    async def check():
        calls = []
        def handler(req):
            calls.append(req)
            if req.url.host == "api.deepseek.com":
                return httpx.Response(200, json={"results": [{"url": "https://example.com/", "summary": "useful"}, {"url": "invalid"}]})
            if req.url.host == "api.firecrawl.dev":
                return httpx.Response(200, json={"success": False, "error": "stopped test-only-secret", "data": {"web": [{"url": "https://second.com/", "title": "otherpartial"}]}})
            return httpx.Response(503, text="diagnostic payload test-only-secret")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await WebService(Settings("deepseek"), client).execute("web_search", {"query": "x"})
        assert result["meta"]["status"] == "ERROR" and len(calls) == 3
        assert result["content"][0]["value"]["snippet"] == "useful"
        text = json.dumps(result)
        assert "test-only-secret" not in text and "diagnostic payload" in text and "HTTP 503" in text
        assert "otherpartial" not in text
    run(check())


def test_retry_after_is_real_operation_and_access_scoped_with_no_invented_ban():
    async def check():
        calls = []
        def handler(req):
            calls.append((req.url.host, req.url.path, "Authorization" in req.headers))
            if req.url.host == "api.firecrawl.dev":
                return httpx.Response(429, json={"error": "rate limited"}, headers={"Retry-After": "60"})
            return httpx.Response(200, json={"results": []})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            settings = Settings("firecrawl")
            service = WebService(settings, client)
            await service.execute("web_search", {"query": "x"})
            await service.execute("web_search", {"query": "y"})
            assert calls == [("api.firecrawl.dev", "/v2/search", True), ("api.firecrawl.dev", "/v2/search", False),
                             ("api.tavily.com", "/search", False), ("api.tavily.com", "/search", False)]
            await service.execute("web_fetch", {"target": {"kind": "URL", "url": "https://example.com/"}})
            assert ("api.firecrawl.dev", "/v2/scrape", True) in calls
            settings.value = replace(settings.value, web=WebConnection("firecrawl", api_key="rotated-key"))
            await service.execute("web_search", {"query": "z"})
            assert calls[-2:] == [("api.firecrawl.dev", "/v2/search", True), ("api.tavily.com", "/search", False)]
    run(check())


def test_plain_429_and_target_page_failure_do_not_block_later_calls():
    async def check():
        calls = []
        def handler(req):
            calls.append(req)
            if req.url.host == "api.firecrawl.dev":
                value = success("firecrawl")
                value["data"]["metadata"]["statusCode"] = 429
                return httpx.Response(200, json=value, headers={"Retry-After": "60"})
            return httpx.Response(429, json={"error": "busy"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = WebService(Settings(), client)
            for _ in range(2):
                await service.execute("web_fetch", {"target": {"kind": "URL", "url": "https://example.com/"}})
        assert len(calls) == 4
    run(check())


def test_retry_after_http_date_expiry_and_invalid_header():
    from pulsara_agent.web.service import _retry_after
    future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=90), usegmt=True)
    assert 88 < _retry_after(future) <= 90
    assert _retry_after("0") == 0
    assert _retry_after("invented") is None
    assert _retry_after("1.5") is None
    assert _retry_after("-5") is None


def test_real_cancellation_stops_forward_traversal_and_deadline_never_restarts():
    async def check():
        entered = asyncio.Event()
        calls = []
        async def handler(req):
            calls.append(req)
            entered.set()
            await asyncio.Future()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = WebService(Settings(), client)
            task = asyncio.create_task(service.execute("web_search", {"query": "x"}))
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert len(calls) == 1
            result = await service.execute("web_search", {"query": "x"}, deadline_monotonic=time.monotonic() - 1)
            assert result["meta"]["status"] == "ERROR" and len(calls) == 1
    run(check())


def test_process_credential_gate_rejects_protected_query_before_transport_and_scrubs_response():
    async def check():
        boundary = ProcessCredentialBoundary("process-credential-value")
        calls = []
        def handler(req):
            calls.append(req)
            return httpx.Response(200, json={"success": True, "data": {"web": [{"url": "https://example.com", "description": "process-credential-value useful diagnostics"}]}})
        async with ProcessCredentialBoundAsyncClient(credential_boundary=boundary, transport=httpx.MockTransport(handler)) as client:
            service = WebService(Settings(), client, credential_boundary=boundary)
            rejected = await service.execute("web_search", {"query": "process-credential-value"})
            assert rejected["meta"]["status"] == "ERROR" and not calls
            accepted = await service.execute("web_search", {"query": "safe"})
            assert accepted["meta"]["status"] == "SUCCESS" and len(calls) == 1
            assert "process-credential-value" not in str(accepted) and "useful diagnostics" in str(accepted)
    run(check())


def test_cancelled_later_candidate_preserves_first_partial_body_and_failure_facts():
    async def check():
        later_entered = asyncio.Event()
        calls = []
        async def handler(req):
            calls.append(req)
            if req.url.host == "api.deepseek.com":
                return httpx.Response(200, json={"content": "accepted partial text", "title": "Known title", "error": "upstream interrupted"})
            later_entered.set()
            await asyncio.Future()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = WebService(Settings("deepseek"), client)
            task = asyncio.create_task(service.execute("web_fetch", {"target": {"kind": "URL", "url": "https://example.com/"}}))
            await later_entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError) as caught:
                await task
            result = caught.value.result
        assert len(calls) == 2
        assert result["meta"] == {"status": "CANCELLED"}
        assert result["target"] == {"kind": "URL", "url": "https://example.com/"}
        assert result["content"][0] == {"type": "text", "value": "accepted partial text"}
        assert "Known title" in result["content"][1]["value"]
        assert "upstream interrupted" in result["content"][1]["value"]
    run(check())


def test_deadline_expiry_after_partial_preserves_body_without_dispatching_later_candidates(monkeypatch):
    async def check():
        calls = []
        clock = [100.0]
        monkeypatch.setattr("pulsara_agent.web.service.time.monotonic", lambda: clock[0])
        def handler(req):
            calls.append(req)
            clock[0] = 102.0
            return httpx.Response(200, json={"content": "known partial", "error": "upstream interrupted"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await WebService(Settings("deepseek"), client).execute("web_fetch", {
                "target": {"kind": "URL", "url": "https://example.com/"}}, deadline_monotonic=101.0)
        assert len(calls) == 1 and result["meta"]["status"] == "ERROR"
        assert result["content"][0]["value"] == "known partial"
        assert "deadline expired" in result["content"][1]["value"]
    run(check())


def test_concurrent_admission_awaits_process_gate_without_blocking_event_loop():
    async def check():
        boundary = ProcessCredentialBoundary()
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = []
        async def handler(req):
            calls.append(req)
            if len(calls) == 1:
                entered.set()
                await release.wait()
            return httpx.Response(200, json={"success": True, "data": {"web": []}})
        async with ProcessCredentialBoundAsyncClient(credential_boundary=boundary, transport=httpx.MockTransport(handler)) as client:
            service = WebService(Settings(), client, credential_boundary=boundary)
            first = asyncio.create_task(service.execute("web_search", {"query": "a"}))
            await entered.wait()  # mock transport holds the boundary until return
            second = asyncio.create_task(service.execute("web_search", {"query": "b"}))
            await asyncio.sleep(0)
            release.set()  # this task must stay runnable while second waits for gate
            values = await asyncio.wait_for(asyncio.gather(first, second), timeout=2)
        assert len(calls) == 2 and all(x["meta"]["status"] == "SUCCESS" for x in values)
    run(check())


def test_domain_filtered_empty_is_success_and_does_not_dispatch_the_next_backend():
    async def check():
        calls = []
        def handler(req):
            calls.append(req)
            return httpx.Response(200, json={"success": True, "data": {"web": [{"url": "https://example.com/"}]}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await WebService(Settings(), client).execute("web_search", {"query": "x", "exclude_domains": ["example.com"]})
        assert result == {"meta": {"status": "SUCCESS"}, "content": []}
        assert len(calls) == 1
    run(check())


def test_ordinary_cache_or_warning_facts_do_not_invent_incomplete_extraction():
    payload = success("firecrawl")
    payload["data"]["warning"] = "optional color analysis unavailable"
    result = normalize("firecrawl", FetchRequest("https://example.com/"), payload)
    assert result.error is None and result.content == ({"type": "text", "value": "body"},)
    exa = success("exa")
    exa["statuses"][0]["source"] = "cached"
    assert normalize("exa", FetchRequest("https://example.com/"), exa).error is None


def test_settings_read_failure_retains_legal_target_without_leaking_process_credential():
    from pulsara_agent.settings import LocalSettingsUnavailable
    class UnavailableSettings:
        def read(self):
            raise LocalSettingsUnavailable("private parser detail")
    async def check():
        boundary = ProcessCredentialBoundary("process-credential-value")
        async with ProcessCredentialBoundAsyncClient(credential_boundary=boundary,
                                                     transport=httpx.MockTransport(lambda req: pytest.fail("no network expected"))) as client:
            result = await WebService(UnavailableSettings(), client, credential_boundary=boundary).execute(
                "web_fetch", {"target": {"kind": "URL", "url": "https://example.com/process-credential-value"}})
        assert result["meta"]["status"] == "ERROR"
        assert "process-credential-value" not in json.dumps(result)
        assert "web settings unavailable" in result["content"][0]["value"]
    run(check())


def test_firecrawl_early_calendar_year_is_not_machine_strftime_dependent():
    _, body = request_for("firecrawl", SearchRequest("x", after="0001-01-01", before="0100-02-03"))
    assert body["tbs"] == "cdr:1,cd_min:01/01/0001,cd_max:02/03/0100"


def test_http_failure_diagnostics_do_not_copy_another_candidate_raw_body():
    async def check():
        def handler(req):
            if req.url.host == "api.deepseek.com":
                return httpx.Response(500, json={"content": "first useful body", "error": "first interrupted"})
            if req.url.host == "api.firecrawl.dev":
                return httpx.Response(500, json={"success": False, "error": "second interrupted", "data": {
                    "markdown": "unselected different page body", "metadata": {"title": "Known other title"}}})
            return httpx.Response(422, json={"detail": {"message": "specific provider rejection", "code": "REJECTED"}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await WebService(Settings("deepseek"), client).execute("web_fetch", {
                "target": {"kind": "URL", "url": "https://example.com/"}})
        assert result["content"][0]["value"] == "first useful body"
        diagnostic = result["content"][1]["value"]
        assert "specific provider rejection" in diagnostic and "REJECTED" in diagnostic
        assert "second interrupted" in diagnostic and "Known other title" in diagnostic
        assert "unselected different page body" not in diagnostic
        assert "first useful body" not in diagnostic
    run(check())


def test_tavily_real_daily_cap_shape_reports_quota_cause_without_upgrade_actions():
    async def check():
        calls = []
        def handler(req):
            calls.append(req)
            if req.url.host == "api.firecrawl.dev":
                return httpx.Response(429, json={"success": False, "error": "Firecrawl unavailable"})
            return httpx.Response(429, json={"error": {
                "code": "daily_cap_reached",
                "message": "You reached the daily keyless Tavily limit. To continue immediately, pay via x402 agentic payment or sign up for a Tavily API key.",
                "next_actions": [
                    {"type": "agentic_payment", "resource": "https://docs.tavily.com/documentation/machine-payments/x402",
                     "instruction": "Pay for this request using the x402 agentic payment protocol to continue immediately."},
                    {"type": "signup", "resource": "https://tavily.com",
                     "instruction": "Ask the user to sign up and retry with an API key"},
                ],
                "window": "day", "retry_after_seconds": 219,
            }}, headers={"Retry-After": "219"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await WebService(Settings(), client).execute("web_search", {"query": "x"})
        assert len(calls) == 2 and result["meta"] == {"status": "ERROR"}
        diagnostic = result["content"][0]["value"]
        assert "Tavily keyless daily quota reached (daily_cap_reached)" in diagnostic
        assert "API HTTP 429" in diagnostic and "Firecrawl unavailable" in diagnostic
        for upgrade_action in ("next_actions", "agentic_payment", "signup", "sign up", "x402", "Pay for", "API key"):
            assert upgrade_action not in diagnostic
    run(check())


def test_unknown_structured_provider_error_preserves_specific_nested_reason():
    request = SearchRequest("x")
    result = normalize("tavily", request, {"error": {
        "code": "unfamiliar_provider_error",
        "message": "Search backend rejected this query",
        "detail": {"reason": "unsupported query operator", "next_actions": [{"instruction": "sign up"}]},
        "next_actions": [{"type": "signup", "instruction": "create an account"}],
    }})
    assert "unfamiliar_provider_error" in result.error
    assert "Search backend rejected this query" in result.error
    assert "unsupported query operator" in result.error
    assert "next_actions" not in result.error and "sign up" not in result.error
