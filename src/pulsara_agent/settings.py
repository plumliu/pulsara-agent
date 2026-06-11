"""Application-level configuration for Pulsara."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from pulsara_agent.llm.config import LLMConfig


DEFAULT_OXIGRAPH_URL = "http://localhost:7878"
DEFAULT_POSTGRES_DSN = "postgresql://pulsara:pulsara@localhost:5432/pulsara"


@dataclass(frozen=True, slots=True)
class StorageConfig:
    oxigraph_url: str = DEFAULT_OXIGRAPH_URL
    postgres_dsn: str = DEFAULT_POSTGRES_DSN

    @classmethod
    def from_env(cls, prefix: str = "PULSARA") -> "StorageConfig":
        return cls(
            oxigraph_url=os.getenv(f"{prefix}_OXIGRAPH_URL", DEFAULT_OXIGRAPH_URL).strip(),
            postgres_dsn=os.getenv(f"{prefix}_POSTGRES_DSN", DEFAULT_POSTGRES_DSN).strip(),
        )

    def redacted_dict(self) -> dict:
        return {
            "oxigraph_url": self.oxigraph_url,
            "postgres_dsn_set": bool(self.postgres_dsn),
        }


@dataclass(frozen=True, slots=True)
class PulsaraSettings:
    """Runtime settings loaded by application bootstrap code."""

    llm: LLMConfig
    storage: StorageConfig

    @classmethod
    def from_env(cls, prefix: str = "PULSARA") -> "PulsaraSettings":
        return cls(
            llm=LLMConfig.from_env(prefix=prefix),
            storage=StorageConfig.from_env(prefix=prefix),
        )

    @classmethod
    def from_env_file(
        cls,
        path: str | Path = ".env",
        *,
        prefix: str = "PULSARA",
        override: bool = False,
    ) -> "PulsaraSettings":
        load_env_file(path, override=override)
        return cls.from_env(prefix=prefix)

    def redacted_dict(self) -> dict:
        return {
            "llm": {
                "api": self.llm.api,
                "provider": self.llm.provider,
                "base_url": self.llm.base_url,
                "pro_model": self.llm.pro_model,
                "flash_model": self.llm.flash_model,
                "api_key_set": bool(self.llm.api_key),
            },
            "storage": self.storage.redacted_dict(),
        }


def load_env_file(path: str | Path = ".env", *, override: bool = False) -> dict[str, str]:
    env_path = Path(path)
    if not env_path.exists():
        raise ValueError(f"Environment file not found: {env_path}")
    if not env_path.is_file():
        raise ValueError(f"Environment path is not a file: {env_path}")

    loaded: dict[str, str] = {}
    for line_number, raw_line in enumerate(env_path.read_text(encoding="utf-8").splitlines(), start=1):
        parsed = _parse_env_line(raw_line)
        if parsed is None:
            continue
        key, value = parsed
        if not key:
            raise ValueError(f"Invalid empty environment key in {env_path}:{line_number}")
        if override or key not in os.environ:
            os.environ[key] = value
        loaded[key] = value
    return loaded


def _parse_env_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped[len("export ") :].strip()
    if "=" not in stripped:
        raise ValueError(f"Invalid .env line without '=': {line}")
    key, value = stripped.split("=", 1)
    key = key.strip()
    value = _strip_inline_comment(value.strip())
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return key, value


def _strip_inline_comment(value: str) -> str:
    in_single = False
    in_double = False
    for index, char in enumerate(value):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            if index == 0 or value[index - 1].isspace():
                return value[:index].rstrip()
    return value
