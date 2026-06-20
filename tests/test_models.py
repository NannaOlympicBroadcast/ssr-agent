from pathlib import Path
import tempfile
import json
from ssr.config import Settings
from ssr.models import ModelsConfig, ModelEntry

def test_models_config_lifecycle():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        settings = Settings(
            home=tmp_path / "home",
            project_dir=tmp_path / "project",
            default_model="test-default-model",
        )
        
        # 1. Loading when file doesn't exist (should auto-create)
        cfg = ModelsConfig(settings)
        assert cfg.primary == "default-gemini"
        assert len(cfg.models) == 1
        assert cfg.models[0].model == "test-default-model"
        
        # 2. Add a model
        new_entry = ModelEntry(
            id="my-claude",
            provider="anthropic",
            model="claude-3-5-sonnet",
            api_key_env="ANTHROPIC_API_KEY",
        )
        cfg.add_model(new_entry)
        assert len(cfg.models) == 2
        assert any(m.id == "my-claude" for m in cfg.list_models())
        
        # 3. Switch primary
        cfg.switch_primary("my-claude")
        assert cfg.get_primary().id == "my-claude"
        
        # 4. Fallbacks
        fallbacks = cfg.get_fallbacks()
        assert len(fallbacks) == 1
        assert fallbacks[0].id == "default-gemini"
        
        # 5. Reloading
        cfg2 = ModelsConfig(settings)
        assert cfg2.primary == "my-claude"
        assert len(cfg2.models) == 2
        
        # 6. Remove model
        cfg2.remove_model("default-gemini")
        assert len(cfg2.models) == 1
        assert cfg2.models[0].id == "my-claude"


def test_is_actual_api_key():
    from ssr.cli import is_actual_api_key

    # Valid env var names (return False)
    assert not is_actual_api_key("GEMINI_API_KEY")
    assert not is_actual_api_key("OPENAI_KEY")
    assert not is_actual_api_key("MY_KEY_123")
    assert not is_actual_api_key("key")

    # Direct keys (return True)
    assert is_actual_api_key("sk-ant-sid01-xxxxxxxx")
    assert is_actual_api_key("AIzaSyB3hD68zL3R9qJ3bN5mP8sT2uW4vX6yZ8")
    assert is_actual_api_key("AIzaSyD-xxx")
    assert is_actual_api_key("sk-proj-xxxxxxxxx")
    assert is_actual_api_key("sk-1234567890abcdef1234567890abcdef")
    assert is_actual_api_key("some.key.with.dots")
    assert is_actual_api_key("some-key-with-dashes")

