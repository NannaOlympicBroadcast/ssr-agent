"""Data model for the context pool."""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ContextItem:
    """A single retrievable piece of context."""

    category: str
    title: str
    text: str
    source: str = ""           # file path or logical origin
    metadata: dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        h = hashlib.sha1(f"{self.category}:{self.source}:{self.title}".encode()).hexdigest()
        return h[:16]

    def snippet(self, limit: int = 280) -> str:
        text = " ".join(self.text.split())
        return text[:limit] + ("…" if len(text) > limit else "")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "category": self.category,
            "title": self.title,
            "source": self.source,
            "text": self.text,
            "metadata": self.metadata,
        }


class ContextPoolBase(ABC):
    """Abstract base class for context pools."""

    @abstractmethod
    def add(self, item: ContextItem) -> None:
        pass

    @abstractmethod
    def extend(self, items: list[ContextItem]) -> None:
        pass

    @abstractmethod
    def by_category(self, category: str) -> list[ContextItem]:
        pass

    @abstractmethod
    def categories_summary(self) -> dict[str, int]:
        pass

    @abstractmethod
    def __len__(self) -> int:
        pass


@dataclass
class ContextPool(ContextPoolBase):
    """In-memory collection of context items, grouped by category."""

    items: list[ContextItem] = field(default_factory=list)

    def add(self, item: ContextItem) -> None:
        self.items.append(item)

    def extend(self, items: list[ContextItem]) -> None:
        self.items.extend(items)

    def by_category(self, category: str) -> list[ContextItem]:
        if hasattr(category, "value"):
            category = category.value
        return [i for i in self.items if getattr(i.category, "value", i.category) == category]

    def categories_summary(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for item in self.items:
            cat_name = getattr(item.category, "value", item.category)
            out[cat_name] = out.get(cat_name, 0) + 1
        return out

    def __len__(self) -> int:
        return len(self.items)

# Keep enum around for backwards compatibility where used across project
import enum
class ContextCategory(str, enum.Enum):
    TOOLS = "tools"
    CONFIGURATIONS = "configurations"
    SKILLS = "skills"
    MEMORY = "memory"

    @classmethod
    def coerce(cls, value: "str | ContextCategory") -> str:
        if isinstance(value, ContextCategory):
            return value.value
        if hasattr(value, "value"):
            return value.value
        return str(value).strip().lower()
