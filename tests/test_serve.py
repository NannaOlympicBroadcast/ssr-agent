from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock

from ssr.config import Settings
from ssr.serve import app
import ssr.serve

@pytest.fixture()
def mock_agent(tmp_path: Path):
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    home.mkdir()
    proj.mkdir()
    settings = Settings(home=home, project_dir=proj, default_model="test-model")
    settings.ensure_dirs()
    
    agent = MagicMock()
    # Mock models config
    model_entry = MagicMock()
    model_entry.id = "test-model"
    model_entry.provider = "gemini"
    model_entry.model = "gemini-3-flash-lite"
    model_entry.api_key_env = "GEMINI_API_KEY"
    model_entry.base_url = None
    
    agent.models_config.list_models.return_value = [model_entry]
    agent.models_config.get_primary.return_value = model_entry
    agent.run.return_value = "Hello! I am SSR Agent. I can help you write code."
    
    ssr.serve.agent_instance = agent
    return agent

def test_list_models(mock_agent):
    client = TestClient(app)
    response = client.get("/v1/models")
    assert response.status_code == 200
    data = response.json()
    assert data["object"] == "list"
    assert len(data["data"]) == 1
    assert data["data"][0]["id"] == "test-model"
    assert data["data"][0]["owned_by"] == "gemini"

def test_chat_completions_non_streaming(mock_agent):
    client = TestClient(app)
    payload = {
        "model": "test-model",
        "messages": [
            {"role": "user", "content": "Write a python function."}
        ],
        "stream": False
    }
    response = client.post("/v1/chat/completions", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert "id" in data
    assert data["object"] == "chat.completion"
    assert data["choices"][0]["message"]["content"] == "Hello! I am SSR Agent. I can help you write code."
    assert data["choices"][0]["message"]["role"] == "assistant"
    assert data["choices"][0]["finish_reason"] == "stop"
    mock_agent.run.assert_called_once_with("Write a python function.")

def test_chat_completions_streaming(mock_agent):
    client = TestClient(app)
    payload = {
        "model": "test-model",
        "messages": [
            {"role": "user", "content": "Write a python function."}
        ],
        "stream": True
    }
    response = client.post("/v1/chat/completions", json=payload)
    assert response.status_code == 200
    # Read the event stream
    lines = [line for line in response.iter_lines() if line]
    # Check that it has streamed chunks and ends with [DONE]
    assert any("data: " in line for line in lines)
    assert lines[-1] == "data: [DONE]"
    
    # Let's parse one of the chunks
    first_chunk_line = [l for l in lines if "chat.completion.chunk" in l][0]
    import json
    chunk_data = json.loads(first_chunk_line.replace("data: ", ""))
    assert chunk_data["choices"][0]["delta"]["content"] is not None
