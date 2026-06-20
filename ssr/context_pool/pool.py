"""Data model for the context pool."""

from __future__ import annotations

import abc
import enum
import hashlib
from dataclasses import dataclass, field


class ContextCategory(str, enum.Enum):
    TOOLS = "tools"
    CONFIGURATIONS = "configurations"
    SKILLS = "skills"
    MEMORY = "memory"
    REFS = "refs"

    @classmethod
    def coerce(cls, value: "str | ContextCategory") -> "ContextCategory":
        if isinstance(value, ContextCategory):
            return value
        return cls(str(value).strip().lower())


@dataclass
class ContextItem:
    """A single retrievable piece of context."""

    category: ContextCategory
    title: str
    text: str
    source: str = ""           # file path or logical origin
    metadata: dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        h = hashlib.sha1(f"{self.category.value}:{self.source}:{self.title}".encode()).hexdigest()
        return h[:16]

    def snippet(self, limit: int = 280) -> str:
        text = " ".join(self.text.split())
        return text[:limit] + ("…" if len(text) > limit else "")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "category": self.category.value,
            "title": self.title,
            "source": self.source,
            "text": self.text,
            "metadata": self.metadata,
        }


class AbstractContextPool(abc.ABC):
    """Abstract base class for a context pool."""

    @abc.abstractmethod
    def add(self, item: ContextItem) -> None:
        """Add a context item."""
        pass

    @abc.abstractmethod
    def extend(self, items: list[ContextItem]) -> None:
        """Add a list of context items."""
        pass

    @abc.abstractmethod
    def by_category(self, category: "str | ContextCategory") -> list[ContextItem]:
        """Get items by category."""
        pass

    @abc.abstractmethod
    def categories_summary(self) -> dict[str, int]:
        """Get a summary of item counts by category."""
        pass

    @abc.abstractmethod
    def __len__(self) -> int:
        """Get the total number of items."""
        pass


@dataclass
class ContextPool(AbstractContextPool):
    """In-memory collection of context items, grouped by category."""

    items: list[ContextItem] = field(default_factory=list)

    def add(self, item: ContextItem) -> None:
        self.items.append(item)

    def extend(self, items: list[ContextItem]) -> None:
        self.items.extend(items)

    def by_category(self, category: "str | ContextCategory") -> list[ContextItem]:
        cat = ContextCategory.coerce(category)
        return [i for i in self.items if i.category == cat]

    def categories_summary(self) -> dict[str, int]:
        out: dict[str, int] = {c.value: 0 for c in ContextCategory}
        for item in self.items:
            out[item.category.value] += 1
        return out

    def __len__(self) -> int:
        return len(self.items)
