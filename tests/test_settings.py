import os

from pulsara_agent.settings import PulsaraSettings, load_env_file


def test_settings_can_load_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "PULSARA_API_KEY='dummy-key'",
                "PULSARA_API=openai_chat_completions",
                "PULSARA_BASE_URL=https://example.test/v1 # comment",
                "export PULSARA_PRO_MODEL=gpt-5",
                'PULSARA_FLASH_MODEL="gpt-5-mini"',
                "PULSARA_OXIGRAPH_URL=http://localhost:7878",
                "PULSARA_POSTGRES_DSN=postgresql://pulsara:pulsara@localhost:5432/pulsara",
            ]
        ),
        encoding="utf-8",
    )
    for key in (
        "PULSARA_API_KEY",
        "PULSARA_API",
        "PULSARA_BASE_URL",
        "PULSARA_PRO_MODEL",
        "PULSARA_FLASH_MODEL",
        "PULSARA_OXIGRAPH_URL",
        "PULSARA_POSTGRES_DSN",
    ):
        monkeypatch.delenv(key, raising=False)

    settings = PulsaraSettings.from_env_file(env_file)

    assert settings.llm.api_key == "dummy-key"
    assert settings.llm.api == "openai_chat_completions"
    assert settings.llm.base_url == "https://example.test/v1"
    assert settings.llm.pro_model == "gpt-5"
    assert settings.llm.flash_model == "gpt-5-mini"
    assert settings.storage.oxigraph_url == "http://localhost:7878"
    assert settings.storage.postgres_dsn == "postgresql://pulsara:pulsara@localhost:5432/pulsara"
    assert settings.redacted_dict()["storage"] == {
        "oxigraph_url": "http://localhost:7878",
        "postgres_dsn_set": True,
    }
    assert settings.redacted_dict()["llm"]["api"] == "openai_chat_completions"


def test_settings_default_llm_api_is_openai_responses(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "PULSARA_API_KEY=dummy-key",
                "PULSARA_PRO_MODEL=gpt-5",
                "PULSARA_FLASH_MODEL=gpt-5-mini",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("PULSARA_API", raising=False)
    monkeypatch.delenv("PULSARA_API_KEY", raising=False)
    monkeypatch.delenv("PULSARA_PRO_MODEL", raising=False)
    monkeypatch.delenv("PULSARA_FLASH_MODEL", raising=False)

    settings = PulsaraSettings.from_env_file(env_file)

    assert settings.llm.api == "openai_responses"


def test_env_file_does_not_override_existing_environment_by_default(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("PULSARA_PRO_MODEL=from-file\n", encoding="utf-8")
    monkeypatch.setenv("PULSARA_PRO_MODEL", "from-env")

    loaded = load_env_file(env_file)

    assert loaded["PULSARA_PRO_MODEL"] == "from-file"
    assert os.environ["PULSARA_PRO_MODEL"] == "from-env"
