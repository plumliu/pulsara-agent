import os

from pulsara_agent.settings import PulsaraSettings, load_env_file


def test_settings_can_load_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "PULSARA_API_KEY='dummy-key'",
                "PULSARA_BASE_URL=https://example.test/v1 # comment",
                "export PULSARA_PRO_MODEL=gpt-5",
                'PULSARA_FLASH_MODEL="gpt-5-mini"',
            ]
        ),
        encoding="utf-8",
    )
    for key in (
        "PULSARA_API_KEY",
        "PULSARA_BASE_URL",
        "PULSARA_PRO_MODEL",
        "PULSARA_FLASH_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)

    settings = PulsaraSettings.from_env_file(env_file)

    assert settings.llm.api_key == "dummy-key"
    assert settings.llm.base_url == "https://example.test/v1"
    assert settings.llm.pro_model == "gpt-5"
    assert settings.llm.flash_model == "gpt-5-mini"


def test_env_file_does_not_override_existing_environment_by_default(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("PULSARA_PRO_MODEL=from-file\n", encoding="utf-8")
    monkeypatch.setenv("PULSARA_PRO_MODEL", "from-env")

    loaded = load_env_file(env_file)

    assert loaded["PULSARA_PRO_MODEL"] == "from-file"
    assert os.environ["PULSARA_PRO_MODEL"] == "from-env"
