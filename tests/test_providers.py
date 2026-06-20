from pathlib import Path
import tempfile
import pytest
from ssr.config import Settings
from ssr.models import ModelEntry
from ssr.providers import get_provider
from ssr.providers.gemini import GeminiProvider

def test_get_provider_gemini():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        settings = Settings(
            home=tmp_path / "home",
            project_dir=tmp_path / "project",
            gemini_api_key="dummy-gemini-key",
        )
        
        entry = ModelEntry(
            id="test-gemini",
            provider="gemini",
            model="gemini-3.1-flash-lite",
            api_key_env="GEMINI_API_KEY",
        )
        
        provider = get_provider(settings, entry)
        assert isinstance(provider, GeminiProvider)
        assert provider.name == "gemini"
        assert provider.model_name == "gemini-3.1-flash-lite"


def test_get_provider_direct_key(monkeypatch):
    import google.genai
    from unittest.mock import MagicMock

    mock_client = MagicMock()
    monkeypatch.setattr(google.genai, "Client", mock_client)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        settings = Settings(
            home=tmp_path / "home",
            project_dir=tmp_path / "project",
        )

        entry = ModelEntry(
            id="test-gemini",
            provider="gemini",
            model="gemini-3.1-flash-lite",
            api_key_env="GEMINI_API_KEY",
            api_key="direct-custom-api-key",
        )

        provider = get_provider(settings, entry)
        assert isinstance(provider, GeminiProvider)
        mock_client.assert_called_once_with(api_key="direct-custom-api-key")

