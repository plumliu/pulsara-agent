"""User-level web credentials, including explicit saved-model key references."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

from pulsara_agent.llm.model_connections import ModelConnectionId
from pulsara_agent.llm.model_target import canonicalize_endpoint

if TYPE_CHECKING:
    from pulsara_agent.llm.model_connections import ModelConnectionConfig
    from pulsara_agent.settings import LocalSettings


WebProvider = Literal["deepseek", "firecrawl", "tavily", "exa"]
WEB_PROVIDERS = frozenset({"deepseek", "firecrawl", "tavily", "exa"})


@dataclass(frozen=True, slots=True)
class WebConnection:
    provider: WebProvider
    api_key: str | None = field(default=None, repr=False)
    model_connection_id: ModelConnectionId | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.provider, str) or self.provider not in WEB_PROVIDERS:
            raise ValueError("请选择有效的网页服务供应商。")
        if (self.api_key is None) == (self.model_connection_id is None):
            raise ValueError("网页配置必须选择 API key 或已保存模型凭据引用。")
        if self.api_key is not None and (
            not isinstance(self.api_key, str)
            or not self.api_key
            or self.api_key != self.api_key.strip()
        ):
            raise ValueError("请填写 API key。")
        if self.model_connection_id is not None and (
            not isinstance(self.model_connection_id, ModelConnectionId)
            or self.provider != "deepseek"
        ):
            raise ValueError("仅 DeepSeek 支持已保存模型凭据引用。")

    def private(self) -> dict[str, object]:
        if self.model_connection_id is not None:
            return {
                "provider": self.provider,
                "model_connection_id": self.model_connection_id.value,
            }
        return {"provider": self.provider, "api_key": self.api_key}


def web_connection_from_dict(value: object) -> WebConnection | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("网页配置字段无效。")
    if set(value) == {"provider", "api_key"}:
        return WebConnection(provider=value["provider"], api_key=value["api_key"])
    if set(value) == {"provider", "model_connection_id"}:
        if not isinstance(value["model_connection_id"], str):
            raise ValueError("已保存模型凭据引用无效。")
        return WebConnection(
            provider=value["provider"],
            model_connection_id=ModelConnectionId(value["model_connection_id"]),
        )
    raise ValueError("网页配置字段无效。")


def _is_official_deepseek(connection: ModelConnectionConfig) -> bool:
    if connection.target.route_id != "deepseek" and connection.user_declared is None:
        return False
    if any(character.isspace() for character in connection.base_url):
        return False
    try:
        endpoint = canonicalize_endpoint(connection.base_url)
    except (ValueError, UnicodeError):
        return False
    # Key ownership follows the official origin; the adapter owns web paths.
    parsed = urlsplit(endpoint)
    return parsed.scheme == "https" and parsed.netloc == "api.deepseek.com"


def _saved_deepseek_key(
    settings: LocalSettings, connection_id: ModelConnectionId
) -> str | None:
    connection = settings.connection(connection_id)
    if (
        connection is None
        or not connection.requires_api_key
        or not _is_official_deepseek(connection)
    ):
        return None
    key = settings.model_api_key(connection_id)
    return key if isinstance(key, str) and key and key == key.strip() else None


def deepseek_candidate(settings: LocalSettings) -> ModelConnectionConfig | None:
    """First usable official saved connection in the user's saved order."""
    return next(
        (
            connection
            for connection in settings.model_connections
            if _saved_deepseek_key(settings, connection.id) is not None
        ),
        None,
    )


def resolve_web_connection(settings: LocalSettings) -> tuple[WebProvider, str] | None:
    connection = settings.web
    if connection is None:
        return None
    key = connection.api_key
    if connection.model_connection_id is not None:
        key = _saved_deepseek_key(settings, connection.model_connection_id)
    return (connection.provider, key) if key is not None else None


def _connection_label(
    settings: LocalSettings, connection: ModelConnectionConfig
) -> str:
    label = (
        connection.user_declared.configuration_name
        if connection.user_declared is not None
        else f"DeepSeek / {connection.target.model_id}"
    )
    return settings.credential_scrub_set().scrub_text(label)


def web_settings_public(settings: LocalSettings) -> dict[str, object]:
    """Truthful account availability without projecting any credential value."""
    selected = settings.web
    candidate = deepseek_candidate(settings)
    resolved = resolve_web_connection(settings)
    connection = None
    if selected is not None:
        referenced = (
            settings.connection(selected.model_connection_id)
            if selected.model_connection_id is not None
            else None
        )
        connection = {
            "provider": selected.provider,
            "has_api_key": resolved is not None,
            "credential_source": "model_connection"
            if selected.model_connection_id is not None
            else "api_key",
            "model_connection_id": selected.model_connection_id.value
            if selected.model_connection_id is not None
            else None,
            "model_connection_label": _connection_label(settings, referenced)
            if referenced is not None
            else None,
        }
    return {
        "connection": connection,
        "deepseek_candidate": {
            "model_connection_id": candidate.id.value,
            "label": _connection_label(settings, candidate),
        }
        if candidate is not None
        else None,
        "mode": "account" if resolved is not None else "free",
    }
