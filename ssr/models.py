"""Model registry for SSR Agent (~/.ssr/models.json)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ModelConfig:
    name: str
    provider: str = "gemini"
    api_key: str | None = None
    base_url: str | None = None


def _default_registry(default_model: str) -> dict:
    return {"primary": default_model, "models": [{"name": default_model, "provider": "gemini"}]}


def ensure_models_file(path: Path, default_model: str) -> None:
    if not path.exists():
        path.write_text(json.dumps(_default_registry(default_model), indent=2), "utf-8")


def load_registry(path: Path, default_model: str) -> dict:
    if not path.exists():
        return _default_registry(default_model)
    try:
        data = json.loads(path.read_text("utf-8"))
    except json.JSONDecodeError:
        return _default_registry(default_model)
    data.setdefault("primary", default_model)
    data.setdefault("models", [])
    return data


def list_models(path: Path, default_model: str) -> list[ModelConfig]:
    data = load_registry(path, default_model)
    out = []
    for item in data.get("models", []):
        if isinstance(item, str):
            out.append(ModelConfig(name=item))
        elif isinstance(item, dict) and item.get("name"):
            out.append(ModelConfig(**{k: item.get(k) for k in ("name", "provider", "api_key", "base_url") if item.get(k) is not None}))
    if not out:
        out.append(ModelConfig(name=data.get("primary") or default_model))
    return out


def set_primary(path: Path, default_model: str, model_name: str) -> dict:
    data = load_registry(path, default_model)
    names = [m if isinstance(m, str) else m.get("name") for m in data.get("models", [])]
    if model_name not in names:
        data.setdefault("models", []).append({"name": model_name, "provider": _infer_provider(model_name)})
    data["primary"] = model_name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    return data


def _infer_provider(name: str) -> str:
    low = name.lower()
    if low.startswith("claude"):
        return "anthropic"
    if low.startswith("gpt") or low.startswith("o"):
        return "openai"
    return "gemini"
