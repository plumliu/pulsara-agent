"""Web API request/response adapters; httpx owns the transport lifecycle.

Provider contracts: native_tools/web.md. These adapters preserve the one query or
one URL request; provider limits skip a candidate rather than weaken its filters.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from urllib.parse import urlsplit


@dataclass(frozen=True)
class SearchRequest:
    query: str
    count: int = 10
    include_domains: tuple[str, ...] = ()
    exclude_domains: tuple[str, ...] = ()
    after: str | None = None
    before: str | None = None


@dataclass(frozen=True)
class FetchRequest:
    url: str

    @property
    def target(self) -> dict:
        return {"kind": "URL", "url": self.url}


@dataclass(frozen=True)
class ProviderResult:
    content: tuple[dict, ...] = ()
    title: str | None = None
    error: str | None = None


class CandidateUnsupported(ValueError):
    pass


def validate_url(value: object) -> str:
    # Same HTTP(S) URL boundary as the existing presentation URL Target owner.
    if (not isinstance(value, str) or not value.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError("URL must be a complete HTTP(S) address without credentials")
    value = value.strip()
    try:
        parsed = urlsplit(value)
        if (not re.match(r"^https?://", value, re.I) or not parsed.hostname
                or any(c.isspace() for c in parsed.netloc) or "\\" in value
                or any(ord(c) < 32 or ord(c) == 127 for c in value)
                or parsed.username is not None or parsed.password is not None):
            raise ValueError
        _ = parsed.port
    except ValueError:
        # Rejected inputs can contain passwords; do not echo them.
        raise ValueError("URL must be a complete HTTP(S) address without credentials") from None
    return value


def _domains(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} must be an array of hostnames")
    result = []
    for host in value:
        if not isinstance(host, str) or not host or host != host.strip():
            raise ValueError(f"{name} must contain nonempty hostnames")
        try:
            ascii_host = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise ValueError(f"{name} must contain hostnames without protocol, port, path or wildcard") from None
        if (len(ascii_host.rstrip(".")) > 253
                or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                       for label in ascii_host.rstrip(".").split("."))):
            raise ValueError(f"{name} must contain hostnames without protocol, port, path or wildcard")
        result.append(host)
    return tuple(result)


def validate_search(arguments: Mapping[str, object]) -> SearchRequest:
    if not isinstance(arguments, Mapping) or set(arguments) - {
        "query", "count", "include_domains", "exclude_domains", "after", "before"
    }:
        raise ValueError("web_search accepts only query, count, include_domains, exclude_domains, after and before")
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be nonempty text")
    count = arguments.get("count", 10)
    if type(count) is not int or not 1 <= count <= 20:
        raise ValueError("count must be an integer from 1 to 20 (Tavily's request limit)")
    dates = {}
    for field in ("after", "before"):
        if field in arguments:
            value = arguments[field]
            if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
                raise ValueError(f"{field} must be a calendar date YYYY-MM-DD")
            try:
                date.fromisoformat(value)
            except ValueError:
                raise ValueError(f"{field} must be a calendar date YYYY-MM-DD") from None
            dates[field] = value
    if len(dates) == 2 and dates["after"] >= dates["before"]:
        raise ValueError("after must precede before")
    return SearchRequest(query, count,
                         _domains(arguments.get("include_domains", ()), "include_domains"),
                         _domains(arguments.get("exclude_domains", ()), "exclude_domains"), **dates)


def validate_fetch(arguments: Mapping[str, object]) -> FetchRequest:
    if not isinstance(arguments, Mapping) or set(arguments) != {"target"}:
        raise ValueError("web_fetch accepts only target")
    target = arguments["target"]
    if not isinstance(target, Mapping) or set(target) != {"kind", "url"} or target["kind"] != "URL":
        raise ValueError('target must contain only kind:"URL" and url')
    return FetchRequest(validate_url(target["url"]))


def request_for(provider: str, request: SearchRequest | FetchRequest) -> tuple[str, dict]:
    if isinstance(request, FetchRequest):
        if provider == "deepseek":
            if len(request.url) > 8192:
                raise CandidateUnsupported("DeepSeek fetch URL exceeds its 8192 character limit")
            return "https://api.deepseek.com/v1/tools/fetch", {"url": request.url}
        if provider == "firecrawl":
            return "https://api.firecrawl.dev/v2/scrape", {
                "url": request.url, "formats": ["markdown"], "onlyMainContent": True}
        if provider == "tavily":
            return "https://api.tavily.com/extract", {"urls": [request.url], "format": "markdown"}
        if provider == "exa":
            if len(request.url) > 2048:
                raise CandidateUnsupported("Exa contents URL exceeds its 2048 character limit")
            return "https://api.exa.ai/contents", {"urls": [request.url], "text": {"linkStyle": "full"}}
    elif provider == "deepseek":
        query = request.query
        if request.include_domains:
            query += " (" + " OR ".join(f"site:{host}" for host in request.include_domains) + ")"
        for host in request.exclude_domains:
            query += f" -site:{host}"
        for field in ("after", "before"):
            if value := getattr(request, field):
                query += f" {field}:{value}"
        if len(query) > 128:
            raise CandidateUnsupported("DeepSeek compiled query exceeds its 128 Unicode code point limit")
        return "https://api.deepseek.com/v1/tools/search", {"query": query, "count": request.count, "need_content": False}
    elif provider == "firecrawl":
        if len(request.query) > 500:
            raise CandidateUnsupported("Firecrawl query exceeds its 500 character limit")
        if bool(request.after) != bool(request.before):
            # Official tbs docs verify only a two-sided custom date range.
            raise CandidateUnsupported("Firecrawl cannot express the requested single-sided date filter with a verified formal mapping")
        body = {"query": request.query, "limit": request.count, "sources": [{"type": "web"}], "highlights": True}
        if request.include_domains:
            body["includeDomains"] = list(request.include_domains)
        elif request.exclude_domains:
            body["excludeDomains"] = list(request.exclude_domains)
        if request.after:
            body["tbs"] = ("cdr:1,cd_min:" + _firecrawl_date(request.after)
                           + ",cd_max:" + _firecrawl_date(request.before))
        return "https://api.firecrawl.dev/v2/search", body
    elif provider == "tavily":
        if len(request.include_domains) > 300 or len(request.exclude_domains) > 150:
            raise CandidateUnsupported("Tavily accepts at most 300 included and 150 excluded domains per request")
        body = {"query": request.query, "max_results": request.count, "topic": "general", "search_depth": "basic",
                "include_published_date": True, "filter_by_published_date": False,
                "include_answer": False, "include_raw_content": False, "include_images": False}
        for field in ("include_domains", "exclude_domains"):
            if getattr(request, field):
                body[field] = list(getattr(request, field))
        if request.include_domains:
            body["include_domains_mode"] = "restrict"
        for source, dest in (("after", "start_date"), ("before", "end_date")):
            if value := getattr(request, source):
                body[dest] = value
        return "https://api.tavily.com/search", body
    elif provider == "exa":
        if len(request.include_domains) > 1200 or len(request.exclude_domains) > 1200:
            raise CandidateUnsupported("Exa accepts at most 1200 domains per domain filter")
        body = {"query": request.query, "numResults": request.count, "type": "auto", "contents": {"highlights": True}}
        for source, dest in (("include_domains", "includeDomains"), ("exclude_domains", "excludeDomains")):
            if getattr(request, source):
                body[dest] = list(getattr(request, source))
        for source, dest in (("after", "startPublishedDate"), ("before", "endPublishedDate")):
            if value := getattr(request, source):
                body[dest] = value + "T00:00:00Z"
        return "https://api.exa.ai/search", body
    raise ValueError("unsupported web provider")


def _firecrawl_date(value: str) -> str:
    year, month, day = value.split("-")
    return f"{month}/{day}/{year}"


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _fragments(value: object) -> str | None:
    if isinstance(value, list):
        return "\n\n[…]\n\n".join(part for item in value if (part := _text(item))) or None
    return _text(value)


def _host(value: str) -> str:
    return value.rstrip(".").encode("idna").decode("ascii").lower()


def _allowed(url: str, request: SearchRequest) -> bool:
    host = _host(urlsplit(url).hostname)
    def matches(domain):
        domain = _host(domain)
        return host == domain or host.endswith("." + domain)
    return (not request.include_domains or any(map(matches, request.include_domains))) and not any(map(matches, request.exclude_domains))


def error_reason(value: object, *, provider: str | None = None) -> str:
    """Project provider error causes without forwarding suggested account actions."""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        if provider == "tavily" and value.get("code") == "daily_cap_reached":
            return "Tavily keyless daily quota reached (daily_cap_reached)"
        reasons = []
        for field in ("code", "tag", "type", "message", "detail", "reason", "description", "error"):
            if value.get(field) is not None:
                reasons.append(field + ": " + error_reason(value[field], provider=provider))
        return "; ".join(reasons) or "provider returned an error without a specific reason"
    if isinstance(value, list):
        return "; ".join(error_reason(item, provider=provider) for item in value)
    return str(value)


def normalize(provider: str, request: SearchRequest | FetchRequest, payload: object) -> ProviderResult:
    if not isinstance(payload, dict):
        return ProviderResult(error="response must be a JSON object")
    if isinstance(request, SearchRequest):
        return _search_result(provider, request, payload)
    return _fetch_result(provider, request, payload)


def _search_result(provider: str, request: SearchRequest, payload: dict) -> ProviderResult:
    problems = []
    if provider == "firecrawl":
        if payload.get("success") is not True:
            problems.append("API reported failure: " + error_reason(payload.get("error", "missing success"), provider=provider))
        data = payload.get("data")
        rows = data.get("web") if isinstance(data, dict) else None
    else:
        rows = payload.get("results")
        if payload.get("error"):
            problems.append("API reported failure: " + error_reason(payload["error"], provider=provider))
    if not isinstance(rows, list):
        return ProviderResult(error="; ".join(problems + ["missing search results array"]))
    content = []
    for index, row in enumerate(rows):
        try:
            if not isinstance(row, dict):
                raise ValueError("record is not an object")
            url = validate_url(row.get("url"))
            if not _allowed(url, request):
                continue
            record = {"target": {"kind": "URL", "url": url}}
            optional = {"title": _text(row.get("title"))}
            if provider == "deepseek":
                optional.update(snippet=_text(row.get("summary")) or _text(row.get("snippet")), date=_text(row.get("date")))
            elif provider == "firecrawl":
                optional.update(snippet=_fragments(row.get("highlights")) or _text(row.get("description")), date=_text(row.get("date")))
            elif provider == "tavily":
                optional.update(snippet=_text(row.get("content")), date=_text(row.get("published_date")))
            else:
                optional.update(snippet=_fragments(row.get("highlights")), date=_text(row.get("publishedDate")))
            record.update({key: value for key, value in optional.items() if value is not None})
            content.append({"type": "object", "value": record})
        except (ValueError, UnicodeError) as error:
            problems.append(f"search record {index}: {error}")
    return ProviderResult(tuple(content), error="; ".join(problems) or None)


def _fetch_result(provider: str, request: FetchRequest, payload: dict) -> ProviderResult:
    problems = []
    row = payload
    body_field = "content"
    if provider == "firecrawl":
        if payload.get("success") is not True:
            problems.append("API reported failure: " + error_reason(payload.get("error", "missing success"), provider=provider))
        row = payload.get("data")
        body_field = "markdown"
        if isinstance(row, dict):
            metadata = row.get("metadata", {})
            if not isinstance(metadata, dict):
                metadata = {}
            status = metadata.get("statusCode")
            if isinstance(status, int) and status >= 400:
                problems.append(f"target page returned HTTP {status}")
            if metadata.get("error"):
                problems.append("target extraction: " + str(metadata["error"]))
    elif provider in {"tavily", "exa"}:
        rows = payload.get("results")
        row = rows[0] if isinstance(rows, list) and len(rows) == 1 else None
        body_field = "raw_content" if provider == "tavily" else "text"
        if provider == "tavily":
            failures = payload.get("failed_results")
            if failures:
                problems.append("target extraction failed: " + str(failures))
        else:
            statuses = payload.get("statuses")
            matching = [item for item in statuses if isinstance(item, dict) and item.get("id") == request.url] if isinstance(statuses, list) else []
            if len(matching) != 1:
                problems.append("missing unambiguous status for requested URL")
            elif matching[0].get("status") != "success":
                problems.append("target extraction failed: " + str(matching[0]))
    if payload.get("error"):
        problems.append("API reported failure: " + error_reason(payload["error"], provider=provider))
    if not isinstance(row, dict):
        return ProviderResult(error="; ".join(problems + ["missing corresponding extracted result"]))
    body = row.get(body_field)
    content = ({"type": "text", "value": body},) if isinstance(body, str) and body else ()
    if not isinstance(body, str):
        problems.append("missing requested extracted body field " + body_field)
    metadata = row.get("metadata") if provider == "firecrawl" else row
    title = _text(metadata.get("title")) if isinstance(metadata, dict) else None
    return ProviderResult(content, title, "; ".join(problems) or None)
