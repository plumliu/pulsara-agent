"""The independent user-level transcription connection slot."""

from dataclasses import dataclass, field
from urllib.parse import urlsplit

from pulsara_agent.llm.model_target import canonicalize_endpoint


@dataclass(frozen=True, slots=True)
class AsrConnection:
    endpoint: str
    model_id: str
    api_key: str = field(repr=False)

    def __post_init__(self):
        if not isinstance(self.endpoint, str) or self.endpoint != self.endpoint.strip():
            raise ValueError("请填写完整转写请求地址。")
        canonicalize_endpoint(self.endpoint)
        if not urlsplit(self.endpoint).path.strip("/"):
            raise ValueError("请填写完整转写请求地址，包含 API 路径。")
        if (
            not isinstance(self.model_id, str)
            or not self.model_id
            or self.model_id != self.model_id.strip()
        ):
            raise ValueError("请填写转写模型 ID。")
        if (
            not isinstance(self.api_key, str)
            or not self.api_key
            or self.api_key != self.api_key.strip()
        ):
            raise ValueError("请填写 API key。")

        if self.api_key in self.endpoint or self.api_key in self.model_id:
            raise ValueError("转写公开配置不能包含 API key。")

    def public(self):
        return {
            "endpoint": self.endpoint,
            "model_id": self.model_id,
            "credential_configured": True,
        }

    def private(self):
        return {
            "endpoint": self.endpoint,
            "model_id": self.model_id,
            "api_key": self.api_key,
        }


def asr_connection_from_dict(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"endpoint", "model_id", "api_key"}:
        raise ValueError("转写配置字段无效。")
    return AsrConnection(**value)
