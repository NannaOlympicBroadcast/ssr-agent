"""Multi-model configuration management."""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from ssr.config import Settings

@dataclass
class ModelEntry:
    id: str
    provider: str  # "gemini" | "anthropic" | "openai"
    model: str
    api_key_env: str
    base_url: str | None = None
    api_key: str | None = None

class ModelsConfig:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.path = settings.models_config
        self.primary: str = ""
        self.models: list[ModelEntry] = []
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            # Auto-create backward-compatible config
            self.primary = "default-gemini"
            model_name = self.settings.default_model
            self.models = [
                ModelEntry(
                    id="default-gemini",
                    provider="gemini",
                    model=model_name,
                    api_key_env="GEMINI_API_KEY"
                )
            ]
            self.save()
            return

        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.primary = data.get("primary", "")
            self.models = [ModelEntry(**m) for m in data.get("models", [])]
        except Exception:
            # Fallback if corrupt
            self.primary = "default-gemini"
            self.models = [
                ModelEntry(
                    id="default-gemini",
                    provider="gemini",
                    model=self.settings.default_model,
                    api_key_env="GEMINI_API_KEY"
                )
            ]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "primary": self.primary,
            "models": [asdict(m) for m in self.models]
        }
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def get_primary(self) -> ModelEntry:
        for m in self.models:
            if m.id == self.primary:
                return m
        if self.models:
            return self.models[0]
        return ModelEntry(
            id="default-gemini",
            provider="gemini",
            model=self.settings.default_model,
            api_key_env="GEMINI_API_KEY"
        )

    def get_fallbacks(self) -> list[ModelEntry]:
        primary_entry = self.get_primary()
        return [m for m in self.models if m.id != primary_entry.id]

    def switch_primary(self, model_id: str) -> bool:
        if any(m.id == model_id for m in self.models):
            self.primary = model_id
            self.save()
            return True
        return False

    def add_model(self, entry: ModelEntry) -> None:
        self.models = [m for m in self.models if m.id != entry.id]
        self.models.append(entry)
        if not self.primary:
            self.primary = entry.id
        self.save()

    def remove_model(self, model_id: str) -> bool:
        self.models = [m for m in self.models if m.id != model_id]
        if self.primary == model_id:
            self.primary = self.models[0].id if self.models else ""
        self.save()
        return True

    def list_models(self) -> list[ModelEntry]:
        return self.models
